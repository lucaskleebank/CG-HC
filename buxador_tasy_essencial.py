"""
buxador_tasy_essencial.py
-------------------------
Versão didática e enxuta do "Buxador Tasy" (tasy.exe), reconstruída a partir do
executável original. Mantém a MESMA técnica e os MESMOS seletores/JS:

  1. Abre o Edge com --remote-debugging-port=9222 (Chrome DevTools Protocol)
  2. Descobre as abas do HCMed e do Tasy em http://localhost:9222/json
  3. Conecta via websocket e manda JavaScript para a página (Runtime.evaluate)
  4. HCMed: busca pelo RGHC -> nome do paciente + lista de exames
  5. Tasy:  entra no PEP -> lupa -> digita o RGHC em NR_PRONTUARIO -> escolhe o
            atendimento "Internado" -> lê as grades (SlickGrid) de
            "Sinais vitais e monitorização" e "Ganhos e perdas"

Não inclui: interface gráfica, parsers dos laudos em PDF do HCMed, RX.

Requisitos (Windows, Edge instalado):
    pip install websocket-client
Uso:
    python buxador_tasy_essencial.py 1234567A 7654321B
    -> faça login no HCMed e no Tasy na janela que abrir e tecle ENTER no terminal.
"""

import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.request
from datetime import datetime, timedelta

import websocket  # pip install websocket-client

PORTA = 9222
PERFIL = os.path.join(os.environ.get("LOCALAPPDATA", r"C:\Temp"), "HCMed_Scraper_Profile")
URL_HCMED = "http://hcmed.phcnet.usp.br/hcmed"
URL_TASY = "https://tasy.icesp.org.br"


# =============================================================================
# 1. Abrir o Edge "destrancado"
# =============================================================================
def abrir_edge():
    cmd = (
        f'start "" msedge "{URL_HCMED}" "{URL_TASY}" '
        f'--remote-debugging-port={PORTA} --remote-allow-origins=* '
        f'--disable-popup-blocking --user-data-dir="{PERFIL}"'
    )
    subprocess.Popen(cmd, shell=True)


# =============================================================================
# 2. Cliente CDP mínimo
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

    def esperar(self, js_condicao, timeout=15, intervalo=0.4):
        limite = time.time() + timeout
        while time.time() < limite:
            try:
                if self.eval(js_condicao):
                    return True
            except Exception:
                pass
            time.sleep(intervalo)
        return False


def conectar_abas(tentativas=30):
    ws_hcmed = ws_tasy = None
    for _ in range(tentativas):
        try:
            with urllib.request.urlopen(f"http://localhost:{PORTA}/json", timeout=2) as r:
                abas = json.loads(r.read())
            for aba in abas:
                if aba["type"] != "page" or aba["url"].startswith("chrome-extension"):
                    continue
                url = aba["url"].lower()
                if "hcmed" in url and not ws_hcmed:
                    ws_hcmed = aba["webSocketDebuggerUrl"]
                elif ("tasy" in url or "10.65.239.90" in url) and not ws_tasy:
                    ws_tasy = aba["webSocketDebuggerUrl"]
            if ws_hcmed and ws_tasy:
                break
        except Exception:
            pass
        time.sleep(1)
    if not ws_hcmed:
        raise RuntimeError("Separador do HCMed não encontrado.")
    if not ws_tasy:
        raise RuntimeError("Separador do Tasy não encontrado.")
    return CDP(ws_hcmed), CDP(ws_tasy)


def normalizar(txt):
    """minúsculas, sem acento, espaços simples — para comparar cabeçalhos/textos."""
    txt = unicodedata.normalize("NFD", str(txt)).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", txt).strip().lower()


# =============================================================================
# 3. JavaScript reutilizável do Tasy
# =============================================================================
JS_IS_VISIBLE = """
    function isVisible(el) {
        if (!el) return false;
        try {
            var st = window.getComputedStyle(el);
            var rect = el.getBoundingClientRect();
            return st && st.display !== 'none' && st.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
        } catch (e) { return false; }
    }
"""

# Fecha pop-ups/modais: manda ESC para o modal de cima; se não der, clica em "fechar/×"
JS_ESCAPE = "(function() {" + JS_IS_VISIBLE + r"""
    function scoreModal(el) {
        if (!el || !isVisible(el)) return -1;
        var rect = el.getBoundingClientRect();
        var z = parseInt(window.getComputedStyle(el).zIndex, 10); if (isNaN(z)) z = 0;
        return (rect.width * rect.height) + z;
    }
    function sendEscape(target) {
        if (!target) return false;
        try { if (target.focus) target.focus({ preventScroll: true }); } catch (e) {}
        try {
            var opts = { key: 'Escape', code: 'Escape', keyCode: 27, which: 27, bubbles: true, cancelable: true };
            target.dispatchEvent(new KeyboardEvent('keydown', opts));
            target.dispatchEvent(new KeyboardEvent('keyup', opts));
            return true;
        } catch (e) { return false; }
    }
    window.focus();

    var backdropSelectors = ['.modal-backdrop', '.backdrop', '.overlay', '.ui-widget-overlay',
                             '.swal2-backdrop', '.k-overlay', '[class*="backdrop"]', '[class*="overlay"]'];
    var backdrop = null;
    for (var b = 0; b < backdropSelectors.length && !backdrop; b++) {
        var found = Array.from(document.querySelectorAll(backdropSelectors[b])).filter(isVisible);
        if (found.length) { found.sort(function(a, b) { return scoreModal(b) - scoreModal(a); }); backdrop = found[0]; }
    }

    var modalSelectors = ['[role="dialog"]', '[aria-modal="true"]', '.modal', '.modal-dialog', '.popup',
                          '.popup-container', '.ui-dialog', '.swal2-popup', '.k-window', '.window-container', '.dialog'];
    var candidates = [];
    if (document.activeElement) candidates.push(document.activeElement);
    modalSelectors.forEach(function(sel) { candidates = candidates.concat(Array.from(document.querySelectorAll(sel))); });
    candidates.push(document.body); candidates.push(document);
    candidates = candidates
        .filter(function(el, i, arr) { return arr.indexOf(el) === i; })
        .filter(function(el) { return el && (el === document || el === document.body || isVisible(el) || el === document.activeElement); });
    if (backdrop) candidates.sort(function(a, b) { return scoreModal(b) - scoreModal(a); });
    for (var i = 0; i < candidates.length; i++) { if (sendEscape(candidates[i])) return true; }

    function closeCandidates(root) {
        var sel = ['button', '[role="button"]', '[aria-label]', '[title]', '[data-action]',
                   '[class*="close"]', '[class*="fechar"]', '[class*="dismiss"]'].join(',');
        var els = Array.from((root || document).querySelectorAll(sel)).filter(isVisible);
        var f = els.filter(function(el) {
            var txt = ((el.innerText || el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' +
                       (el.getAttribute('title') || '')).toLowerCase();
            return /(^|\b)(x|×|close|fechar|dismiss|sair|cancelar)(\b|$)/i.test(txt) ||
                   txt.indexOf('fechar') >= 0 || txt.indexOf('close') >= 0 || txt.indexOf('×') >= 0 || txt.trim() === 'x';
        });
        f.sort(function(a, b) { return scoreModal(b) - scoreModal(a); });
        return f;
    }
    var roots = [];
    if (backdrop && backdrop.parentElement) { roots.push(backdrop.parentElement); roots.push(backdrop.parentElement.parentElement); }
    roots.push(document.body);
    for (var r = 0; r < roots.length; r++) {
        if (!roots[r]) continue;
        var btns = closeCandidates(roots[r]);
        for (var c = 0; c < btns.length; c++) {
            try { btns[c].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window })); return true; } catch (e) {}
        }
    }
    return false;
})();
"""

# Lê uma grade SlickGrid: junta as metades esquerda/direita pela posição vertical (style.top)
JS_LER_GRID = r"""
(function() {
    function getHeaders(selector) {
        return Array.from(document.querySelectorAll(selector + ' .slick-header-column')).map(h => {
            var n = h.querySelector('.slick-column-name');
            return (n ? n.textContent : h.textContent).replace(/\s+/g, ' ').trim();
        });
    }
    var headersLeft = getHeaders('.slick-header-columns-left');
    var headersRight = getHeaders('.slick-header-columns-right');
    if (headersLeft.length === 0 && headersRight.length === 0) headersLeft = getHeaders('.slick-header-columns');

    var rowTops = {};
    function processCanvas(selector, type) {
        document.querySelectorAll(selector + ' > .ui-widget-content.slick-row, ' + selector + ' > .slick-row').forEach(r => {
            var top = r.style.top;
            if (!rowTops[top]) rowTops[top] = { left: [], right: [] };
            rowTops[top][type] = Array.from(r.querySelectorAll('.slick-cell')).map(c => c.textContent.replace(/\s+/g, ' ').trim());
        });
    }
    processCanvas('.grid-canvas-left', 'left');
    processCanvas('.grid-canvas-right', 'right');
    if (Object.keys(rowTops).length === 0)
        processCanvas('.grid-canvas:not(.grid-canvas-left):not(.grid-canvas-right)', 'left');

    var rows = [];
    Object.keys(rowTops).sort((a, b) => parseInt(a) - parseInt(b)).forEach(top => rows.push(rowTops[top]));
    return JSON.stringify({ headers: { left: headersLeft, right: headersRight }, rows: rows });
})();
"""

# Paginação "100 linhas por página" (em 3 etapas: abrir a lista, escolher 100, conferir)
JS_PAGINACAO_ABRIR = "(function() {" + JS_IS_VISIBLE + """
    var roots = Array.from(document.querySelectorAll('.row-per-page')).filter(isVisible);
    if (!roots.length) return false;
    var clickable = roots[0].querySelector('.w-listbox__label a, .w-listbox__label, .w-listbox');
    if (!clickable) return false;
    clickable.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
    return true;
})();
"""
JS_PAGINACAO_100 = "(function() {" + JS_IS_VISIBLE + r"""
    var dialogs = Array.from(document.querySelectorAll('.ngdialog-content')).filter(isVisible);
    var scope = dialogs.length ? dialogs[dialogs.length - 1] : document;
    var options = Array.from(scope.querySelectorAll('a, button, [role="option"], .w-listbox__option')).filter(function(el) {
        var txt = ((el.innerText || el.textContent || '') + '').replace(/\s+/g, ' ').trim();
        return txt === '100' || txt.indexOf('100') === 0 || (el.getAttribute('w-code') || '').indexOf('100') >= 0 ||
               (el.className || '').indexOf('opt-item-100') >= 0;
    });
    if (!options.length) return false;
    options[0].dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
    return true;
})();
"""
JS_PAGINACAO_OK = "(function() {" + JS_IS_VISIBLE + r"""
    var label = Array.from(document.querySelectorAll('.row-per-page .w-listbox__label')).find(isVisible);
    var t = label ? ((label.innerText || label.textContent || '') + '').replace(/\s+/g, ' ').trim() : '';
    var loaders = Array.from(document.querySelectorAll('.w-loader, .loading, .spinner, .ajax-loader')).some(isVisible);
    return t.indexOf('100') >= 0 && !loaders;
})();
"""


def js_clicar_menu_arvore(texto):
    """Itens do menu lateral do PEP (span.wtree-span)."""
    return f"""
        (function() {{
            var spans = Array.from(document.querySelectorAll('span.wtree-span'));
            var btn = spans.find(el => el.textContent && el.textContent.includes({json.dumps(texto)}));
            if (btn) {{ btn.click(); return true; }} return false;
        }})();
    """


# =============================================================================
# 4. HCMed: RGHC -> nome do paciente + lista de exames (igual ao Buxador MV)
# =============================================================================
def hcmed_buscar_paciente(hc: CDP, rghc: str):
    hc.eval("window.location.href = 'http://hcmed.phcnet.usp.br/hcmed/painel/painel.php';")
    hc.esperar("!!document.getElementById('p_nclimycli')")
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
    hc.eval(f"""
        (function() {{
            let alvo = {r}.toUpperCase().trim();
            let num = alvo.replace(/[^0-9]/g, '');
            for (let a of document.querySelectorAll('a[onclick*="buscarmeupaciente2"]')) {{
                let m = (a.getAttribute('onclick') || '').match(/buscarmeupaciente2\\s*\\(\\s*['"]([^'"]+)['"]/i);
                if (m) {{
                    let op = m[1].toUpperCase().trim();
                    if (op === alvo || (num.length > 3 && op.replace(/[^0-9]/g, '') === num)) {{ a.click(); return true; }}
                }}
            }}
            return false;
        }})();
    """)
    time.sleep(2)
    nome = hc.eval("""
        (function() {
            let tds = document.querySelectorAll('td');
            for (let i = 0; i < tds.length; i++) {
                if (tds[i].innerText && tds[i].innerText.trim() === 'Paciente:') {
                    let n = tds[i].nextElementSibling; if (n) return n.innerText.trim();
                }
            }
            return '';
        })();
    """) or ""
    hc.eval("let a = Array.from(document.querySelectorAll('a')).find(x => x.href.includes('ListarExames.php')); if (a) a.click();")
    hc.esperar("!!document.querySelector('table#ResultadosExames')", timeout=20)
    bruto = hc.eval("""
        (function() {
            let res = [];
            for (let r of document.querySelectorAll('table#ResultadosExames tr')) {
                if (r.id === 'cabecalho_head') continue;
                let tds = r.querySelectorAll('td');
                if (tds.length >= 7) {
                    let chk = tds[0].querySelector('input[type="checkbox"]');
                    res.push({
                        data: tds[1].innerText.trim(),
                        exame: chk ? (chk.getAttribute('data-exame') || tds[3].innerText || '').trim() : tds[3].innerText.trim(),
                        origem: chk ? (chk.getAttribute('data-origem') || '') : ''
                    });
                }
            }
            return JSON.stringify(res);
        })();
    """)
    return nome, (json.loads(bruto) if bruto else [])


# =============================================================================
# 5. Tasy: entrar no PEP, achar o paciente pelo RGHC, ler as grades
# =============================================================================
def tasy_entrar_no_pep(tasy: CDP):
    """Se ainda não estiver no PEP, clica em 'Prontuário Eletrônico Paciente - PEP'."""
    pronto = "!!document.querySelector('.person-bar-image-container-hover') || !!document.querySelector('.person-icon-finder');"
    if tasy.eval(pronto):
        return True
    tasy.eval("""
        (function() {
            var s = Array.from(document.querySelectorAll('span'))
                         .find(s => s.textContent && s.textContent.includes('Prontuário Eletrônico Paciente - PEP'));
            if (s) { var a = s.closest('a') || s;
                     a.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window})); return true; }
            return false;
        })();
    """)
    return tasy.esperar(pronto, timeout=30)


def tasy_fechar_popups(tasy: CDP, repetir=2):
    for _ in range(repetir):
        tasy.eval(JS_ESCAPE)
        time.sleep(0.5)


def tasy_abrir_paciente(tasy: CDP, rghc: str):
    # lupa de busca de paciente
    tasy.eval("""
        (function() {
            var lupa = document.querySelector('.person-bar-image-container-hover') || document.querySelector('.person-icon-finder');
            if (lupa) { lupa.click(); return true; } return false;
        })();
    """)
    if not tasy.esperar("""!!document.querySelector("input[nmatributo='NR_PRONTUARIO']");""", timeout=15):
        return "Falha: Campo de inserção de processo não carregou."
    time.sleep(1)

    # RGHC no campo de prontuário
    tasy.eval(f"""
        (function() {{
            var inp = document.querySelector("input[nmatributo='NR_PRONTUARIO']");
            if (!inp) return false;
            let setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
            setter.call(inp, {json.dumps(rghc)});
            inp.dispatchEvent(new Event('input',  {{bubbles: true}}));
            inp.dispatchEvent(new Event('change', {{bubbles: true}}));
            return true;
        }})();
    """)
    time.sleep(1)

    # botão Filtrar
    tasy.eval("""
        (function() {
            var btn = document.querySelector('button.wfilter-button');
            if (btn) { btn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window})); return true; }
            return false;
        })();
    """)
    time.sleep(2)

    # atendimento com status "Internado"
    tasy.eval("""
        (function() {
            var rows = document.querySelectorAll('.slick-row');
            for (var i = 0; i < rows.length; i++) {
                if (rows[i].innerText.includes('Internado')) {
                    var cell = rows[i].querySelector('.slick-cell');
                    if (cell) { cell.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window})); return true; }
                }
            }
            return false;
        })();
    """)
    time.sleep(1)

    # botão OK visível (ou Enter, se não achar)
    ok = tasy.eval("""
        (function() {
            var okBtn = Array.from(document.querySelectorAll('button'))
                .filter(b => b.innerText.trim().toUpperCase() === 'OK' && b.offsetParent !== null).pop();
            if (okBtn) { okBtn.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true, view: window})); return true; }
            return false;
        })();
    """)
    if not ok:
        tasy.eval("document.dispatchEvent(new KeyboardEvent('keydown', {'key': 'Enter', 'keyCode': 13, 'bubbles': true}));")
    time.sleep(3)
    tasy_fechar_popups(tasy)  # avisos que o Tasy abre ao entrar no paciente
    return None


def grid_para_linhas(bruto):
    """Junta cabeçalhos e células (esquerda + direita) em uma lista de dicionários."""
    if not bruto:
        return []
    d = json.loads(bruto)
    heads = [normalizar(h) for h in d["headers"]["left"] + d["headers"]["right"]]
    linhas = []
    for r in d["rows"]:
        celulas = r.get("left", []) + r.get("right", [])
        linhas.append({heads[i] if i < len(heads) else f"col_{i}": v for i, v in enumerate(celulas)})
    return linhas


def tasy_ler_grade(tasy: CDP, item_menu: str):
    tasy.eval(js_clicar_menu_arvore(item_menu))
    time.sleep(2)
    tasy_fechar_popups(tasy)
    time.sleep(1)
    # tenta mostrar 100 linhas por página para cobrir 24h
    if tasy.eval(JS_PAGINACAO_ABRIR):
        time.sleep(0.8)
        tasy.eval(JS_PAGINACAO_100)
        tasy.esperar(JS_PAGINACAO_OK, timeout=10)
        time.sleep(1.2)
    return grid_para_linhas(tasy.eval(JS_LER_GRID))


# =============================================================================
# 6. Formatação (versão simplificada da original)
# =============================================================================
def parse_data(txt):
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%y %H:%M"):
        try:
            return datetime.strptime(txt.strip(), fmt)
        except (ValueError, AttributeError):
            pass
    return None


def ultimas_24h(linhas):
    """Mantém só as linhas cuja coluna de data cai nas últimas 24h."""
    agora = datetime.now()
    out = []
    for l in linhas:
        dt = next((parse_data(v) for k, v in l.items() if "data" in k and parse_data(v)), None)
        if dt is None or agora - dt <= timedelta(hours=24):
            out.append(l)
    return out


def num(txt):
    m = re.search(r"([+-]?\d+(?:[.,]\d+)?)", str(txt or ""))
    return float(m.group(1).replace(",", ".")) if m else None


SINAIS = [  # (rótulo, palavras que identificam a coluna)
    ("PAS", ["pas", "pressao arterial sistolica"]),
    ("PAD", ["pad", "pressao arterial diastolica"]),
    ("PAM", ["pam", "pressao arterial media"]),
    ("FC", ["fc", "frequencia cardiaca"]),
    ("FR", ["fr", "frequencia respiratoria"]),
    ("Temp", ["temp", "temperatura"]),
    ("SpO2", ["spo2", "saturacao"]),
]


def formatar_sinais(linhas):
    if not linhas:
        return "N/D"
    partes = []
    for rotulo, chaves in SINAIS:
        valores = []
        for l in linhas:
            for k, v in l.items():
                if k in chaves or any(k.startswith(c) for c in chaves if len(c) > 4):
                    n = num(v)
                    if n is not None:
                        valores.append(n)
        if valores:
            lo, hi = min(valores), max(valores)
            f = lambda x: f"{x:g}".replace(".", ",")
            partes.append(f"{rotulo} {f(lo)}" if lo == hi else f"{rotulo} {f(lo)}-{f(hi)}")
    return " | ".join(partes) or "N/D"


ITENS_RELEVANTES = [
    r"\bhemodialise\b", r"\bdialise\b", r"\bsonda\b", r"\bnaso\s*enteral\b", r"\bnaso\s*gastrica\b",
    r"\bcolostomia\b", r"\bileostomia\b", r"\bjejunostomia\b", r"\bgastrostomia\b", r"\bbricker\b",
    r"\bestoma\b", r"\bostomia\b", r"\bfistula\b", r"\bhemovac\b", r"\bjackson\s*pratt\b",
    r"\bportovac\b", r"\bporthvac\b", r"\bpigtail\b", r"\bpenrose\b", r"\bvac\b", r"\birrigacao\b",
    r"\bdreno\b", r"\bdrenagem\b", r"ureterostom\w*", r"nefrostom\w*",
]


def formatar_ganhos_perdas(linhas):
    """Soma o volume por item relevante (drenos, sondas, estomas...)."""
    totais = {}
    for l in linhas:
        descricao = normalizar(" ".join(v for k, v in l.items() if "volume" not in k and "data" not in k))
        if not any(re.search(p, descricao) for p in ITENS_RELEVANTES):
            continue
        item = l.get("tipo") or l.get("item") or next(iter(l.values()), "?")
        lado = l.get("lado", "")
        chave = f"{item} {lado}".strip()
        vol = num(next((v for k, v in l.items() if "volume" in k), None))
        if vol is not None:
            totais[chave] = totais.get(chave, 0) + vol
    if not totais:
        return "Sem ganhos/perdas relevantes."
    return " | ".join(f"{k} {v:g} ml" for k, v in totais.items())


# =============================================================================
# Programa principal
# =============================================================================
def main(rghcs):
    abrir_edge()
    input("Faça login no HCMed e no Tasy na janela do Edge e tecle ENTER aqui... ")
    hc, tasy = conectar_abas()
    if not tasy_entrar_no_pep(tasy):
        print("Não consegui abrir o PEP do Tasy.")
        return

    for rghc in rghcs:
        rghc = re.sub(r"[^A-Za-z0-9]", "", rghc).upper()
        print(f"\n===== RGHC {rghc} =====")

        nome, exames = hcmed_buscar_paciente(hc, rghc)
        print(f"{nome or 'Paciente Não Identificado'}")
        print("=== EXAMES LABORATORIAIS (HCMED) ===")
        for e in exames[:15]:
            print(f"  {e['data']}  {e['exame']}  [{e['origem']}]")

        print("=== CONTROLES E DRENOS ===")
        erro = tasy_abrir_paciente(tasy, rghc)
        if erro:
            print("  " + erro)
            continue
        sinais = ultimas_24h(tasy_ler_grade(tasy, "Sinais vitais e monitorização"))
        print(f"  Controles (24h) : {formatar_sinais(sinais)}")
        gp = ultimas_24h(tasy_ler_grade(tasy, "Ganhos e perdas"))
        print(f"  Ganhos e perdas: {formatar_ganhos_perdas(gp)}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python buxador_tasy_essencial.py RGHC1 [RGHC2 ...]")
        sys.exit(1)
    main(sys.argv[1:])
