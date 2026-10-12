"""
buxador_mv_essencial.py
-----------------------
Versão didática e enxuta do "Buxador" (mv.exe), reconstruída a partir do
executável original. Mantém a MESMA técnica e os MESMOS seletores/JS:

  1. Abre o Edge com --remote-debugging-port (Chrome DevTools Protocol, CDP)
  2. Descobre as abas do HCMed e do MV em http://localhost:<porta>/json
  3. Conecta via websocket e manda JavaScript para a página (Runtime.evaluate)
  4. HCMed: busca pelo RGHC -> pega o nome do paciente e a lista de exames
  5. MV:    busca pelo NOME -> abre Controles/Alergias -> lê a tabela de sinais
            vitais (apresentação analítica) -> captura o PDF do balanço hídrico

Não inclui: interface gráfica, parsers dos laudos em PDF, pedidos de exame,
Telegram, várias instâncias em paralelo.

Requisitos (Windows, Edge instalado):
    pip install websocket-client
Uso:
    python buxador_mv_essencial.py 1234567A 7654321B
    -> faça login no HCMed e no MV na janela que abrir e tecle ENTER no terminal.
"""

import json
import subprocess
import sys
import time
import unicodedata
import urllib.request
import base64

import websocket  # pip install websocket-client

PORTA = 9222
PERFIL = r"C:\Temp\buxador_edge_perfil"
URL_HCMED = "http://hcmed.phcnet.usp.br/hcmed"
URL_MV = "http://hishc.phcnet.usp.br"


# =============================================================================
# 1. Abrir o Edge "destrancado" (porta de depuração + perfil próprio)
# =============================================================================
def abrir_edge():
    cmd = (
        f'start "" msedge "{URL_HCMED}" "{URL_MV}" '
        f'--remote-debugging-port={PORTA} --remote-allow-origins=* '
        f'--disable-popup-blocking --user-data-dir="{PERFIL}" '
        f'--no-first-run --no-default-browser-check '
        f'--disable-backgrounding-occluded-windows --disable-renderer-backgrounding '
        f'--disable-background-timer-throttling'
    )
    subprocess.Popen(cmd, shell=True)


# =============================================================================
# 2. Descobrir as abas e conectar via websocket
# =============================================================================
def conectar_abas(tentativas=30):
    ws_url_hcmed = ws_url_mv = None
    for _ in range(tentativas):
        try:
            with urllib.request.urlopen(f"http://localhost:{PORTA}/json", timeout=2) as r:
                abas = json.loads(r.read())
            for aba in abas:
                if aba["type"] != "page" or aba["url"].startswith("chrome-extension"):
                    continue
                url = aba["url"].lower()
                if "hcmed" in url and not ws_url_hcmed:
                    ws_url_hcmed = aba["webSocketDebuggerUrl"]
                elif any(k in url for k in ("hishc", "mvpep", "phcnet")) and "hcmed" not in url and not ws_url_mv:
                    ws_url_mv = aba["webSocketDebuggerUrl"]
            if ws_url_hcmed and ws_url_mv:
                break
        except Exception:
            pass
        time.sleep(1)
    if not (ws_url_hcmed and ws_url_mv):
        raise RuntimeError("Não achei as abas do HCMed e do MV.")
    return CDP(ws_url_hcmed), CDP(ws_url_mv)


# =============================================================================
# 3. Cliente CDP mínimo: manda um comando, espera a resposta com o mesmo id
# =============================================================================
class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url)
        self.msg_id = 0

    def send(self, method, params=None):
        self.msg_id += 1
        alvo = self.msg_id
        self.ws.send(json.dumps({"id": alvo, "method": method, "params": params or {}}))
        self.ws.settimeout(20.0)
        while True:  # ignora eventos até chegar a resposta do nosso id
            res = json.loads(self.ws.recv())
            if res.get("id") == alvo:
                return res.get("result")

    def eval(self, js, await_promise=False):
        """Executa JS na página (como colar no Console do F12) e devolve o valor."""
        res = self.send("Runtime.evaluate", {
            "expression": js, "returnByValue": True, "awaitPromise": await_promise,
        })
        if res and "result" in res:
            return res["result"].get("value")
        return None

    def esperar(self, js_condicao, timeout=15, intervalo=0.3):
        """Repete o JS até ele devolver algo verdadeiro (ou estourar o tempo)."""
        limite = time.time() + timeout
        while time.time() < limite:
            try:
                if self.eval(js_condicao):
                    return True
            except Exception:
                pass
            time.sleep(intervalo)
        return False


def sem_acento_maiusculo(txt):
    return unicodedata.normalize("NFD", txt).encode("ascii", "ignore").decode("utf-8").upper().strip()


# =============================================================================
# 4. HCMed: RGHC -> nome do paciente + lista de exames
# =============================================================================
def hcmed_buscar_paciente(hc: CDP, rghc: str):
    hc.eval("window.location.href = 'http://hcmed.phcnet.usp.br/hcmed/painel/painel.php';")
    hc.esperar("!!document.getElementById('p_nclimycli')")

    # Preenche o campo de RGHC "do jeito que o site percebe" e chama a busca do próprio site
    r = json.dumps(rghc)
    hc.eval(f"""
        let input = document.getElementById('p_nclimycli');
        if (input) {{
            let setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
            setter.call(input, {r});
            input.dispatchEvent(new Event('input',  {{ bubbles: true }}));
            input.dispatchEvent(new Event('change', {{ bubbles: true }}));
            input.dispatchEvent(new Event('blur',   {{ bubbles: true }}));
            if (typeof buscarmeupaciente === 'function') {{ buscarmeupaciente({r}); }}
            else {{
                let btn = document.querySelector('.busca_button') ||
                          document.querySelector('input[onclick*="buscarmeupaciente"]');
                if (btn) btn.click();
            }}
        }}
    """)
    time.sleep(2)

    # Se aparecer uma lista de homônimos/opções, clica na que tem o mesmo RGHC
    hc.eval(f"""
        (function() {{
            let originalTarget = {r}.toUpperCase().trim();
            let targetNum = originalTarget.replace(/[^0-9]/g, '');
            let links = document.querySelectorAll('a[onclick*="buscarmeupaciente2"]');
            for (let a of links) {{
                let m = (a.getAttribute('onclick') || '').match(/buscarmeupaciente2\\s*\\(\\s*['"]([^'"]+)['"]/i);
                if (m) {{
                    let op = m[1].toUpperCase().trim();
                    if (op === originalTarget || (targetNum.length > 3 && op.replace(/[^0-9]/g, '') === targetNum)) {{
                        a.click(); return true;
                    }}
                }}
            }}
            return false;
        }})();
    """)
    time.sleep(2)

    # Nome: a célula da tabela logo depois de "Paciente:"
    nome = hc.eval("""
        (function() {
            let tds = document.querySelectorAll('td');
            for (let i = 0; i < tds.length; i++) {
                if (tds[i].innerText && tds[i].innerText.trim() === 'Paciente:') {
                    let nextTd = tds[i].nextElementSibling;
                    if (nextTd) return nextTd.innerText.trim();
                }
            }
            return '';
        })();
    """) or ""

    # Abre "Listar exames" e lê a tabela de resultados
    hc.eval("let a = Array.from(document.querySelectorAll('a')).find(x => x.href.includes('ListarExames.php')); if (a) a.click();")
    hc.esperar("!!document.querySelector('table#ResultadosExames')", timeout=20)
    exames_json = hc.eval("""
        (function() {
            let res = []; let rows = document.querySelectorAll('table#ResultadosExames tr');
            for (let r of rows) {
                if (r.id === 'cabecalho_head') continue;
                let tds = r.querySelectorAll('td');
                if (tds.length >= 7) {
                    let dt = tds[1].innerText.trim();
                    let chk = tds[0].querySelector('input[type="checkbox"]');
                    let exame = chk ? (chk.getAttribute('data-exame') || tds[3].innerText || '').trim()
                                    : tds[3].innerText.trim();
                    let origem = chk ? (chk.getAttribute('data-origem') || '') : '';
                    res.push({ data: dt, exame: exame, origem: origem, val: chk ? chk.value : '' });
                }
            }
            return JSON.stringify(res);
        })();
    """)
    exames = json.loads(exames_json) if exames_json else []
    return nome, exames


# =============================================================================
# 5. MV: nome -> Controles/Alergias -> sinais vitais + PDF do balanço hídrico
# =============================================================================
def mv_abrir_internados(mv: CDP):
    mv.eval("""
        (function() {
            let links = document.querySelectorAll('a');
            for (let a of links) {
                let msg = (a.getAttribute('ng-reflect-message') || '').trim();
                let txt = (a.innerText || '').trim();
                if (txt.includes('INTERNADOS || HOSPITAL DIA') || msg.includes('INTERNADOS || HOSPITAL DIA') ||
                    msg === 'INTERNADOS' || txt === 'INTERNADOS') { a.click(); return true; }
            }
            return false;
        })();
    """)
    return mv.esperar("!!document.querySelector('span.clickable-area.semi-bold');", timeout=20)


def mv_selecionar_paciente(mv: CDP, nome: str) -> bool:
    termo = json.dumps(sem_acento_maiusculo(nome))
    js_clicar = f"""
        (function() {{
            let termo = {termo};
            if (!termo) return false;
            let pacs = document.querySelectorAll('span.clickable-area.semi-bold');
            for (let p of pacs) {{
                let txt = (p.innerText || '').normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').toUpperCase();
                if (txt.includes(termo)) {{ p.click(); return true; }}
            }}
            return false;
        }})();
    """
    if mv.eval(js_clicar):
        return True

    # Não estava na lista visível: digita o nome no campo "Buscar" e espera a lista mudar
    lista_antes = mv.eval("""
        Array.from(document.querySelectorAll('span.clickable-area.semi-bold'))
             .map(p => (p.innerText || '').trim()).join('|');
    """) or ""
    mv.eval(f"""
        (function() {{
            let inp = document.querySelector('input[placeholder*="Buscar"]');
            if (!inp) return false;
            let setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
            setter.call(inp, {termo});
            inp.dispatchEvent(new Event('input',  {{bubbles: true}}));
            inp.dispatchEvent(new Event('change', {{bubbles: true}}));
            let lupa = document.querySelector('mat-icon.mv-suffix');
            if (lupa) lupa.click();
            else inp.dispatchEvent(new KeyboardEvent('keydown', {{key: 'Enter', keyCode: 13, bubbles: true}}));
            return true;
        }})();
    """)
    mv.esperar(f"""
        Array.from(document.querySelectorAll('span.clickable-area.semi-bold'))
             .map(p => (p.innerText || '').trim()).join('|') !== {json.dumps(lista_antes)};
    """, timeout=15)
    return bool(mv.eval(js_clicar))


def mv_abrir_controles(mv: CDP):
    mv.eval("""
        (function() {
            let links = document.querySelectorAll('a.mv-nav-item');
            for (let a of links) {
                let msg = (a.getAttribute('ng-reflect-message') || '').replace(/\\s+/g, '');
                if (msg.includes('CONTROLES/ALERGIAS')) { a.click(); return true; }
            }
            return false;
        })();
    """)
    time.sleep(3)


def mv_capturar_pdf_balanco(mv: CDP, salvar_em: str):
    """Clica Imprimir -> Confirmar, intercepta window.open e baixa o PDF pela sessão."""
    ok = mv.eval("""
        (function() {
            window.__pdfUrl = null;
            if (!window.__mvOpenOriginal) window.__mvOpenOriginal = window.open;
            window.open = function(url) { window.__pdfUrl = url; return null; };
            const normaliza = (t) => (t || '').normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').trim().toUpperCase();
            const btn = Array.from(document.querySelectorAll('button')).find(b => normaliza(b.innerText).includes('IMPRIMIR'));
            if (!btn) return false;
            btn.click(); return true;
        })();
    """)
    if not ok:
        return None
    mv.esperar("!!document.querySelector('pep-print-dialog') || !!document.querySelector('mat-dialog-container')")
    mv.eval("""
        (function() {
            const normaliza = (t) => (t || '').normalize('NFD').replace(/[\\u0300-\\u036f]/g, '').trim().toUpperCase();
            const dialog = document.querySelector('pep-print-dialog') || document.querySelector('mat-dialog-container');
            if (!dialog) return false;
            const btn = Array.from(dialog.querySelectorAll('button')).find(b => normaliza(b.innerText).includes('CONFIRMAR'));
            if (!btn) return false;
            btn.click(); return true;
        })();
    """)
    if not mv.esperar("!!window.__pdfUrl", timeout=30):
        return None
    url = mv.eval("window.__pdfUrl;")
    data_url = mv.eval(f"""
        async function getPdf() {{
            let resp = await fetch({json.dumps(url)}, {{ credentials: 'include' }});
            let blob = await resp.blob();
            return new Promise((resolve) => {{
                let r = new FileReader();
                r.onloadend = () => resolve(r.result);
                r.readAsDataURL(blob);
            }});
        }}
        getPdf();
    """, await_promise=True)
    if not data_url or "," not in data_url:
        return None
    with open(salvar_em, "wb") as f:
        f.write(base64.b64decode(data_url.split(",", 1)[1]))
    # tecla ESC para fechar o diálogo de impressão, se ficou aberto
    mv.eval("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', keyCode: 27, bubbles: true}));")
    return salvar_em


def mv_ler_sinais_vitais(mv: CDP):
    mv.eval("""
        (function() {
            let btn = document.querySelector('mat-button-toggle[value="show-analytical-view"] button');
            if (btn) { btn.click(); return true; }
            for (let span of document.querySelectorAll('.mat-button-toggle-label-content span')) {
                if (span.innerText && span.innerText.includes('Apresentação analítica')) {
                    let b = span.closest('button'); if (b) { b.click(); return true; }
                }
            }
            return false;
        })();
    """)
    time.sleep(2)
    bruto = mv.eval("""
        (function() {
            let tab = document.querySelector('mat-table, table.mat-table, table');
            if (!tab) return null;
            let res = { colunas: [], dados: {} };
            document.querySelectorAll('.mat-header-row .mat-header-cell, thead tr th')
                .forEach(th => res.colunas.push(th.innerText.trim().replace(/\\n/g, ' ')));
            document.querySelectorAll('tbody tr, .mat-row').forEach(linha => {
                let celulas = linha.querySelectorAll('td, .mat-cell');
                if (celulas.length > 1) {
                    let param = celulas[0].innerText.trim();
                    if (!res.dados[param]) res.dados[param] = [];
                    for (let i = 1; i < celulas.length; i++) {
                        let spans = celulas[i].querySelectorAll('.grid-value');
                        let val = "";
                        if (spans.length > 0) {
                            val = Array.from(spans).map(s => s.innerText.trim()).join(' ');
                        } else {
                            let clone = celulas[i].cloneNode(true);
                            clone.querySelectorAll('mat-icon').forEach(ic => clone.removeChild(ic));
                            val = clone.innerText.trim().replace(/\\s+/g, ' ');
                        }
                        if (val && val !== "-" && !/^0,0/.test(val.trim())) {
                            res.dados[param].push({ data_hora: res.colunas[i] || "", valor: val });
                        }
                    }
                }
            });
            return JSON.stringify(res);
        })();
    """)
    return json.loads(bruto) if bruto else None


def mv_voltar_lista(mv: CDP):
    mv.eval("""
        (function() {
            let btn = document.querySelector('a[mattooltip="Voltar a lista de pacientes"]') ||
                      document.querySelector('a[href*="MVPEP_LISTA_INTERNACAO"]');
            if (!btn) {
                let icon = Array.from(document.querySelectorAll('mat-icon')).find(ic => ic.innerText.trim() === 'seta_esquerda');
                if (icon) btn = icon.closest('a');
            }
            if (btn) { btn.click(); return true; }
            return false;
        })();
    """)
    time.sleep(2)


def resumir_vitais(vitais):
    """Mín–máx de cada parâmetro numérico (versão simplificada do original)."""
    if not vitais:
        return "N/D"
    partes = []
    for param, medidas in vitais["dados"].items():
        nums = []
        for m in medidas:
            try:
                nums.append(float(m["valor"].split()[0].replace(",", ".")))
            except ValueError:
                pass
        if nums:
            lo, hi = min(nums), max(nums)
            fmt = lambda x: f"{x:g}".replace(".", ",")
            partes.append(f"{param} {fmt(lo)}" if lo == hi else f"{param} {fmt(lo)}-{fmt(hi)}")
    return " | ".join(partes) or "N/D"


# =============================================================================
# Programa principal
# =============================================================================
def main(rghcs):
    abrir_edge()
    input("Faça login no HCMed e no MV na janela do Edge e tecle ENTER aqui... ")
    hc, mv = conectar_abas()

    for rghc in rghcs:
        rghc = rghc.strip().upper()
        print(f"\n===== RGHC {rghc} =====")

        nome, exames = hcmed_buscar_paciente(hc, rghc)
        print(f"Nome (HCMed): {nome or '?'}")
        for e in exames[:15]:
            print(f"  {e['data']}  {e['exame']}  [{e['origem']}]")

        if not nome:
            print("  MV: sem nome do HCMed, não dá pra buscar no MV.")
            continue

        mv_abrir_internados(mv)
        if not mv_selecionar_paciente(mv, nome):
            print("  MV: paciente não encontrado na lista de internados.")
            continue

        mv_abrir_controles(mv)
        pdf = mv_capturar_pdf_balanco(mv, f"balanco_{rghc}.pdf")
        print(f"  Balanço hídrico: {pdf or 'não capturado'}")
        vitais = mv_ler_sinais_vitais(mv)
        print(f"  # Controles: {resumir_vitais(vitais)}")
        mv_voltar_lista(mv)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python buxador_mv_essencial.py RGHC1 [RGHC2 ...]")
        sys.exit(1)
    main(sys.argv[1:])
