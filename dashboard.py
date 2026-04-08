import os
import json
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from binance.client import Client

load_dotenv()

BINANCE_API_KEY = os.getenv('BINANCE_API_KEY')
BINANCE_SECRET_KEY = os.getenv('BINANCE_SECRET_KEY')
HISTORIAL_FILE = "historial_binance.json"
BLACKLIST_FILE = "blacklist.json"
RANKING_FILE = "ranking_pares.json"

client_binance = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)

def cargar_json(archivo):
    if os.path.exists(archivo):
        with open(archivo, "r") as f:
            return json.load(f)
    return {}

def obtener_precio(par):
    try:
        ticker = client_binance.get_symbol_ticker(symbol=par)
        return float(ticker['price'])
    except:
        return None

def obtener_capital():
    try:
        account = client_binance.get_account()
        for b in account['balances']:
            if b['asset'] == 'USDT':
                return float(b['free'])
        return 0
    except:
        return 0

def generar_html():
    historial = cargar_json(HISTORIAL_FILE) if os.path.exists(HISTORIAL_FILE) else []
    blacklist = cargar_json(BLACKLIST_FILE)
    ranking = cargar_json(RANKING_FILE)

    abiertas = [p for p in historial if p.get('estado') == 'abierta']
    ganancias = [p for p in historial if p.get('estado') == 'cerrada_ganancia']
    perdidas = [p for p in historial if p.get('estado') == 'cerrada_perdida']
    g_pct = sum(p.get('ganancia_pct', 0) for p in ganancias)
    p_pct = sum(p.get('ganancia_pct', 0) for p in perdidas)
    neto = g_pct + p_pct
    capital = obtener_capital()

    posiciones_html = ""
    for pos in abiertas:
        precio_actual = obtener_precio(pos['par']) or 0
        precio_compra = float(pos.get('precio_compra', 0))
        cambio = ((precio_actual - precio_compra) / precio_compra * 100) if precio_compra > 0 else 0
        color = "#00ff88" if cambio >= 0 else "#ff4444"
        posiciones_html += f"""
        <tr>
            <td>{pos['par']}</td>
            <td>{pos.get('estrategia','scalp').upper()}</td>
            <td>${precio_compra:.4f}</td>
            <td>${precio_actual:.4f}</td>
            <td style="color:{color}">{cambio:+.3f}%</td>
            <td>${pos.get('monto',0)}</td>
            <td>{pos.get('fecha','')[:16]}</td>
        </tr>"""

    historial_html = ""
    for op in reversed(historial[-20:]):
        if op.get('estado') in ['cerrada_ganancia', 'cerrada_perdida']:
            pct = op.get('ganancia_pct', 0)
            color = "#00ff88" if pct > 0 else "#ff4444"
            emoji = "✅" if pct > 0 else "🔴"
            historial_html += f"""
            <tr>
                <td>{emoji} {op['par']}</td>
                <td>{op.get('estrategia','scalp').upper()}</td>
                <td style="color:{color}">{pct:+.3f}%</td>
                <td>{op.get('fecha','')[:16]}</td>
                <td>{op.get('razon','')[:40]}</td>
            </tr>"""

    ranking_html = ""
    top = sorted(ranking.items(), key=lambda x: x[1].get('score', 50), reverse=True)[:10]
    for par, data in top:
        score = data.get('score', 50)
        color = "#00ff88" if score > 60 else "#ffaa00" if score > 40 else "#ff4444"
        ranking_html += f"""
        <tr>
            <td>{par}</td>
            <td style="color:{color}">{score}/100</td>
            <td>{data.get('ops',0)}</td>
            <td>{data.get('ganancias',0)}</td>
            <td>{data.get('perdidas',0)}</td>
            <td>{data.get('pct_total',0):+.2f}%</td>
        </tr>"""

    blacklist_html = ""
    for par, data in blacklist.items():
        if 'expira' in data:
            blacklist_html += f"<span style='background:#ff4444;padding:4px 8px;border-radius:4px;margin:4px'>{par}</span>"

    return f"""<!DOCTYPE html>
<html>
<head>
    <title>Bot Binance Dashboard</title>
    <meta charset="utf-8">
    <meta http-equiv="refresh" content="30">
    <style>
        * {{ margin:0; padding:0; box-sizing:border-box; }}
        body {{ background:#0a0a0a; color:#fff; font-family:'Courier New',monospace; padding:20px; }}
        h1 {{ color:#f0b90b; text-align:center; margin-bottom:20px; font-size:24px; }}
        h2 {{ color:#f0b90b; margin:20px 0 10px; font-size:16px; }}
        .stats {{ display:grid; grid-template-columns:repeat(4,1fr); gap:15px; margin-bottom:20px; }}
        .card {{ background:#111; border:1px solid #333; border-radius:8px; padding:15px; text-align:center; }}
        .card .value {{ font-size:24px; font-weight:bold; margin-top:5px; }}
        .card .label {{ color:#888; font-size:12px; }}
        .green {{ color:#00ff88; }}
        .red {{ color:#ff4444; }}
        .yellow {{ color:#f0b90b; }}
        table {{ width:100%; border-collapse:collapse; background:#111; border-radius:8px; overflow:hidden; }}
        th {{ background:#222; padding:10px; text-align:left; font-size:12px; color:#888; }}
        td {{ padding:8px 10px; border-bottom:1px solid #1a1a1a; font-size:12px; }}
        tr:hover {{ background:#1a1a1a; }}
        .update {{ color:#555; text-align:center; margin-top:20px; font-size:11px; }}
        .section {{ margin-bottom:30px; }}
    </style>
</head>
<body>
    <h1>🤖 BOT BINANCE DASHBOARD</h1>

    <div class="stats">
        <div class="card">
            <div class="label">💰 Capital USDT</div>
            <div class="value yellow">${capital:.2f}</div>
        </div>
        <div class="card">
            <div class="label">📈 Neto Total</div>
            <div class="value {'green' if neto >= 0 else 'red'}">{neto:+.2f}%</div>
        </div>
        <div class="card">
            <div class="label">✅ Ganancias</div>
            <div class="value green">{len(ganancias)} ops</div>
        </div>
        <div class="card">
            <div class="label">🔴 Pérdidas</div>
            <div class="value red">{len(perdidas)} ops</div>
        </div>
    </div>

    <div class="section">
        <h2>📊 POSICIONES ABIERTAS ({len(abiertas)})</h2>
        <table>
            <tr><th>Par</th><th>Estrategia</th><th>Compra</th><th>Actual</th><th>%</th><th>Monto</th><th>Fecha</th></tr>
            {posiciones_html if posiciones_html else '<tr><td colspan="7" style="text-align:center;color:#555">Sin posiciones abiertas</td></tr>'}
        </table>
    </div>

    <div class="section">
        <h2>🏆 RANKING DE PARES</h2>
        <table>
            <tr><th>Par</th><th>Score</th><th>Ops</th><th>Ganancias</th><th>Pérdidas</th><th>Total %</th></tr>
            {ranking_html if ranking_html else '<tr><td colspan="6" style="text-align:center;color:#555">Sin datos todavía</td></tr>'}
        </table>
    </div>

    <div class="section">
        <h2>🚫 BLACKLIST ({len(blacklist)} pares)</h2>
        <div style="padding:10px;background:#111;border-radius:8px">
            {blacklist_html if blacklist_html else '<span style="color:#555">Sin pares baneados</span>'}
        </div>
    </div>

    <div class="section">
        <h2>📋 ÚLTIMAS 20 OPERACIONES</h2>
        <table>
            <tr><th>Par</th><th>Estrategia</th><th>Resultado</th><th>Fecha</th><th>Razón</th></tr>
            {historial_html if historial_html else '<tr><td colspan="5" style="text-align:center;color:#555">Sin operaciones todavía</td></tr>'}
        </table>
    </div>

    <div class="update">Actualización automática cada 30 segundos | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
</body>
</html>"""

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(generar_html().encode())

    def log_message(self, format, *args):
        pass

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    server = HTTPServer(('0.0.0.0', port), Handler)
    print(f"Dashboard corriendo en puerto {port}")
    server.serve_forever()