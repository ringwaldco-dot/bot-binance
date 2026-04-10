# -*- coding: utf-8 -*-
# Bot Binance v6 — Reescritura limpia

import os
import json
import math
import time
import threading
import numpy as np
import requests
import warnings
warnings.filterwarnings("ignore")
from datetime import datetime, timedelta
from dotenv import load_dotenv
from binance.client import Client
import google.generativeai as genai
from market_monitor import MonitorMercado
from listing_detector import ListingDetector

load_dotenv()

# ============================================================
# CONFIGURACIÓN
# ============================================================

BINANCE_API_KEY  = os.getenv('BINANCE_API_KEY')
BINANCE_SECRET_KEY = os.getenv('BINANCE_SECRET_KEY')
GEMINI_API_KEY   = os.getenv('GEMINI_API_KEY')
TELEGRAM_TOKEN   = "8513198629:AAHmlayu6y_Z2e2SUCkvKkLIEhj6kstxYT4"
TELEGRAM_CHAT_ID = "1576867878"

MONTO_POR_TRADE  = 8.0      # USDT por operación
MONTO_MIN        = 6.0      # mínimo para operar
MAX_POSICIONES   = 4        # máximo posiciones simultáneas
STOP_LOSS        = 0.025    # -2.5% stop loss
TRAILING_BASE    = 0.004    # trailing mínimo 0.4%
MIN_GANANCIA_TRAIL = 0.001  # activar trailing con +0.1%
MINUTOS_ESTANCADO = 30      # minutos sin moverse para liberar
RANGO_ESTANCADO  = 0.008    # ±0.8% para considerar estancada
CICLO_PUMP       = 15       # segundos entre scans de pump
CICLO_MAIN       = 90       # segundos entre ciclos principales

HISTORIAL_FILE = "historial_binance.json"
BLACKLIST_FILE = "blacklist.json"
RANKING_FILE   = "ranking_pares.json"

# Clientes
client_binance = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)
genai.configure(api_key=GEMINI_API_KEY)
client_gemini = genai.GenerativeModel(
    model_name='gemini-2.0-flash',
    generation_config=genai.GenerationConfig(temperature=0.2, max_output_tokens=100)
)
monitor_mercado = MonitorMercado()
listing_detector = ListingDetector()

# ============================================================
# TELEGRAM
# ============================================================

def tg(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=5
        )
    except:
        pass

# ============================================================
# HISTORIAL
# ============================================================

def cargar_historial():
    if os.path.exists(HISTORIAL_FILE):
        with open(HISTORIAL_FILE) as f:
            return json.load(f)
    return []

def guardar_historial(h):
    with open(HISTORIAL_FILE, 'w') as f:
        json.dump(h, f, indent=2)

def cargar_blacklist():
    if os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE) as f:
            return json.load(f)
    return {}

def guardar_blacklist(bl):
    with open(BLACKLIST_FILE, 'w') as f:
        json.dump(bl, f, indent=2)

def cargar_ranking():
    if os.path.exists(RANKING_FILE):
        with open(RANKING_FILE) as f:
            return json.load(f)
    return {}

def guardar_ranking(r):
    with open(RANKING_FILE, 'w') as f:
        json.dump(r, f, indent=2)

def en_blacklist(par):
    bl = cargar_blacklist()
    if par not in bl:
        return False
    if datetime.now() > datetime.fromisoformat(bl[par]['expira']):
        del bl[par]
        guardar_blacklist(bl)
        return False
    return True

def agregar_blacklist(par, horas=4):
    bl = cargar_blacklist()
    veces = bl.get(par, {}).get('veces', 0) + 1
    bl[par] = {'expira': (datetime.now() + timedelta(hours=horas)).isoformat(), 'veces': veces}
    guardar_blacklist(bl)

def actualizar_ranking(par, pct):
    r = cargar_ranking()
    if par not in r:
        r[par] = {'ops': 0, 'ganancias': 0, 'perdidas': 0, 'pct_total': 0, 'score': 50}
    r[par]['ops'] += 1
    r[par]['pct_total'] = round(r[par]['pct_total'] + pct, 3)
    if pct > 0:
        r[par]['ganancias'] += 1
        r[par]['score'] = min(100, r[par]['score'] + 5)
    else:
        r[par]['perdidas'] += 1
        r[par]['score'] = max(0, r[par]['score'] - 8)
        if r[par]['perdidas'] >= 2:
            agregar_blacklist(par, 4)
    guardar_ranking(r)

# ============================================================
# BINANCE HELPERS
# ============================================================

def precio(par):
    try:
        return float(client_binance.get_symbol_ticker(symbol=par)['price'])
    except:
        return None

def capital_usdt():
    try:
        for b in client_binance.get_account()['balances']:
            if b['asset'] == 'USDT':
                return float(b['free'])
        return 0
    except:
        return 0

def balance_asset(asset):
    try:
        for b in client_binance.get_account()['balances']:
            if b['asset'] == asset:
                return float(b['free'])
        return 0
    except:
        return 0

def comprar(par, monto):
    try:
        orden = client_binance.order_market_buy(symbol=par, quoteOrderQty=monto)
        qty = float(orden['executedQty'])
        precio_c = float(orden['fills'][0]['price']) if orden.get('fills') else precio(par)
        print(f"  ✅ COMPRA {par} qty:{qty} precio:${precio_c} monto:${monto}")
        tg(f"🟢 <b>COMPRA</b> {par}\n💰 ${precio_c} | ${monto}")
        return True, qty, precio_c
    except Exception as e:
        print(f"  ❌ Error comprando {par}: {e}")
        return False, 0, 0

def vender(par, cantidad, pct, razon):
    try:
        asset = par.replace('USDT', '')
        info = client_binance.get_symbol_info(par)
        step = next(f['stepSize'] for f in info['filters'] if f['filterType'] == 'LOT_SIZE')
        dec = len(step.rstrip('0').split('.')[-1]) if '.' in step else 0

        # Usar balance real
        bal = balance_asset(asset)
        if bal <= 0:
            print(f"  [VENTA] {par} balance 0 — marcando cerrada")
            return 'sin_balance'

        factor = 10 ** dec
        qty = math.floor(bal * factor) / factor
        if qty <= 0:
            return 'sin_balance'

        client_binance.order_market_sell(symbol=par, quantity=qty)
        emoji = '✅' if pct > 0 else '🔴'
        tg(f"{emoji} <b>VENTA</b> {par}\n{pct:+.3f}% | {razon}")
        actualizar_ranking(par, pct)
        return True
    except Exception as e:
        print(f"  ❌ Error vendiendo {par}: {e}")
        return False

# ============================================================
# INDICADORES
# ============================================================

def calcular_rsi(precios, periodo=14):
    if len(precios) < periodo + 1:
        return 50
    deltas = np.diff(precios)
    avg_g = np.mean(np.where(deltas > 0, deltas, 0)[-periodo:])
    avg_p = np.mean(np.where(deltas < 0, -deltas, 0)[-periodo:])
    if avg_p == 0:
        return 100
    return 100 - (100 / (1 + avg_g / avg_p))

def calcular_ema(precios, periodo):
    if len(precios) < periodo:
        return precios[-1]
    k = 2 / (periodo + 1)
    ema = precios[0]
    for p in precios[1:]:
        ema = p * k + ema * (1 - k)
    return ema

# ============================================================
# TRAILING DINÁMICO
# ============================================================

def trailing(pct_ganancia, minutos):
    """Trailing inteligente — más amplio cuando más sube y más tiempo lleva."""
    if minutos < 10:
        # Primeros 10 min — ajustado
        if pct_ganancia >= 1.0: return 0.008
        return 0.004
    else:
        # Después de 10 min — deja correr
        if pct_ganancia >= 5.0: return 0.025
        if pct_ganancia >= 3.0: return 0.020
        if pct_ganancia >= 1.0: return 0.012
        return 0.006

# ============================================================
# SINCRONIZACIÓN CON BINANCE
# ============================================================

def sincronizar():
    """Detecta tokens en Binance que no están en el historial y los agrega."""
    try:
        historial = cargar_historial()
        pares_activos = {p['par'] for p in historial if p.get('estado') == 'abierta'}
        balances = client_binance.get_account()['balances']
        nuevas = []
        for b in balances:
            asset = b['asset']
            libre = float(b['free'])
            if asset == 'USDT' or libre <= 0:
                continue
            par = f"{asset}USDT"
            if par in pares_activos:
                continue
            p = precio(par)
            if not p:
                continue
            valor = libre * p
            if valor < 1.0:
                continue
            print(f"  [SYNC] {par} ${valor:.2f} — agregando")
            nuevas.append({
                'par': par, 'precio_compra': p, 'precio_maximo': p,
                'cantidad': libre, 'monto': valor,
                'estado': 'abierta', 'estrategia': 'sync',
                'fecha': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
        if nuevas:
            historial.extend(nuevas)
            guardar_historial(historial)
            pares = [n['par'] for n in nuevas]
            tg(f"🔄 <b>Sincronización</b>\nAgregadas: {', '.join(pares)}")
    except Exception as e:
        print(f"  [SYNC] Error: {e}")

# ============================================================
# GESTIÓN DE POSICIONES
# ============================================================

def revisar_posiciones():
    """Revisa todas las posiciones abiertas y vende si corresponde."""
    historial = cargar_historial()
    abiertas = [p for p in historial if p.get('estado') == 'abierta']
    if not abiertas:
        return
    ahora = datetime.now()
    cambios = False

    for i, pos in enumerate(historial):
        if pos.get('estado') != 'abierta':
            continue

        p_actual = precio(pos['par'])
        if not p_actual:
            continue

        p_compra = float(pos['precio_compra'])
        p_max = float(pos.get('precio_maximo', p_compra))
        if p_actual > p_max:
            p_max = p_actual
            historial[i]['precio_maximo'] = p_max
            cambios = True

        pct = round(((p_actual - p_compra) / p_compra) * 100, 3)
        caida = (p_max - p_actual) / p_max if p_max > 0 else 0

        try:
            fecha = datetime.strptime(pos['fecha'], "%Y-%m-%d %H:%M:%S")
            minutos = (ahora - fecha).total_seconds() / 60
        except:
            minutos = 0

        trail = trailing(pct, minutos)
        print(f"  {pos['par']} | {pct:+.3f}% | trail:{trail*100:.1f}% | {minutos:.0f}min")

        # 1. Stop loss directo
        if pct <= -STOP_LOSS * 100:
            res = vender(pos['par'], pos.get('cantidad', 0), pct, f"stop_loss")
            if res:
                historial[i].update({
                    'estado': 'cerrada_perdida', 'precio_venta': p_actual,
                    'ganancia_pct': pct, 'razon_cierre': 'stop_loss',
                    'fecha_cierre': ahora.strftime("%Y-%m-%d %H:%M:%S")
                })
                cambios = True
                continue

        # 2. Trailing stop
        if pct >= MIN_GANANCIA_TRAIL * 100 and caida >= trail:
            res = vender(pos['par'], pos.get('cantidad', 0), pct, f"trailing_max:{((p_max-p_compra)/p_compra)*100:.2f}%")
            if res:
                historial[i].update({
                    'estado': 'cerrada_ganancia', 'precio_venta': p_actual,
                    'ganancia_pct': pct, 'razon_cierre': 'trailing',
                    'fecha_cierre': ahora.strftime("%Y-%m-%d %H:%M:%S")
                })
                cambios = True
                continue

        # 3. Posición estancada
        if minutos >= MINUTOS_ESTANCADO and abs(pct) <= RANGO_ESTANCADO * 100:
            res = vender(pos['par'], pos.get('cantidad', 0), pct, f"estancada_{minutos:.0f}min")
            if res:
                estado = 'cerrada_ganancia' if pct >= 0 else 'cerrada_perdida'
                historial[i].update({
                    'estado': estado, 'precio_venta': p_actual,
                    'ganancia_pct': pct, 'razon_cierre': f'estancada_{minutos:.0f}min',
                    'fecha_cierre': ahora.strftime("%Y-%m-%d %H:%M:%S")
                })
                cambios = True
                tg(f"⏱️ <b>LIBERADA</b> {pos['par']}\n{pct:+.2f}% en {minutos:.0f}min sin movimiento")
                continue

        # 4. Cut loss por tiempo — más de 1h en pérdida > 2%
        if minutos >= 60 and pct <= -2.0:
            res = vender(pos['par'], pos.get('cantidad', 0), pct, f"cut_loss_{minutos:.0f}min")
            if res:
                historial[i].update({
                    'estado': 'cerrada_perdida', 'precio_venta': p_actual,
                    'ganancia_pct': pct, 'razon_cierre': f'cut_loss_{minutos:.0f}min',
                    'fecha_cierre': ahora.strftime("%Y-%m-%d %H:%M:%S")
                })
                cambios = True

    if cambios:
        guardar_historial(historial)

# ============================================================
# REBALANCEO INTELIGENTE
# ============================================================

def elegir_sacrificable():
    """Elige la posición con menos futuro para liberar capital."""
    historial = cargar_historial()
    posiciones = [p for p in historial if p.get('estado') == 'abierta']
    if not posiciones:
        return None

    ahora = datetime.now()
    candidatos = []

    for pos in posiciones:
        p_actual = precio(pos['par'])
        if not p_actual:
            continue
        pct = ((p_actual - float(pos['precio_compra'])) / float(pos['precio_compra'])) * 100
        try:
            minutos = (ahora - datetime.strptime(pos['fecha'], "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
        except:
            minutos = 0

        # Score — mayor = peor posición
        score = 0
        if pct < 0: score += abs(pct) * 10
        if minutos > 15 and abs(pct) < 0.5: score += minutos / 5
        if pct > 2.0: score -= 50  # no sacrificar posiciones con buena ganancia

        candidatos.append({'par': pos['par'], 'cantidad': pos.get('cantidad', 0),
                           'precio_actual': p_actual, 'pct': round(pct, 3), 'score': score})

    if not candidatos:
        return None

    candidatos.sort(key=lambda x: x['score'], reverse=True)
    return candidatos[0]

def rebalancear(oportunidad):
    """Vende la peor posición para entrar en una mejor oportunidad."""
    cap = capital_usdt()
    if cap >= MONTO_MIN:
        return cap

    sacrificable = elegir_sacrificable()
    if not sacrificable:
        return cap

    par = sacrificable['par']
    pct = sacrificable['pct']
    p_actual = sacrificable['precio_actual']
    opp = oportunidad.get('par', '?')

    print(f"  [REBALANCEO] Vendiendo {par} ({pct:+.2f}%) → {opp}")
    res = vender(par, sacrificable['cantidad'], pct, f"rebalanceo→{opp}")
    if res:
        historial = cargar_historial()
        for i, pos in enumerate(historial):
            if pos.get('par') == par and pos.get('estado') == 'abierta':
                historial[i].update({
                    'estado': 'cerrada_ganancia' if pct >= 0 else 'cerrada_perdida',
                    'precio_venta': p_actual, 'ganancia_pct': pct,
                    'razon_cierre': f'rebalanceo→{opp}',
                    'fecha_cierre': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
                break
        guardar_historial(historial)
        tg(f"🔄 <b>REBALANCEO</b>\n❌ {par} {pct:+.2f}%\n✅ Entrando en {opp}")
        time.sleep(1)
        return capital_usdt()
    return cap

# ============================================================
# DETECTOR DE PUMPS
# ============================================================

def detectar_pumps():
    """Detecta pares con momentum fuerte."""
    try:
        tickers = client_binance.get_ticker()
        # Ordenar por cambio 24h descendente — los que más se mueven primero
        usdt = [t for t in tickers
                if t['symbol'].endswith('USDT')
                and float(t['quoteVolume']) > 300000
                and float(t['lastPrice']) > 0.000001
                and not en_blacklist(t['symbol'])]
        usdt.sort(key=lambda x: float(x['priceChangePercent']), reverse=True)

        pumps = []
        for t in usdt[:200]:
            par = t['symbol']
            try:
                klines = client_binance.get_klines(symbol=par, interval='1m', limit=15)
                precios = [float(k[4]) for k in klines]
                vols = [float(k[5]) for k in klines]
                c2m  = ((precios[-1] - precios[-3])  / precios[-3])  * 100
                c5m  = ((precios[-1] - precios[-6])  / precios[-6])  * 100
                c15m = ((precios[-1] - precios[-15]) / precios[-15]) * 100
                vol_prom = np.mean(vols[:-3]) if len(vols) > 3 else 1
                ratio_vol = np.mean(vols[-3:]) / vol_prom if vol_prom > 0 else 1
                rsi = calcular_rsi(precios)

                es_pump = (
                    (c2m >= 0.2 and ratio_vol >= 2.0) or
                    (c5m >= 1.0 and ratio_vol >= 1.5) or
                    (c15m >= 3.0 and ratio_vol >= 1.3)
                ) and rsi < 82

                if es_pump:
                    pumps.append({
                        'par': par, 'c2m': round(c2m, 3), 'c5m': round(c5m, 3),
                        'c15m': round(c15m, 3), 'ratio_vol': round(ratio_vol, 2),
                        'rsi': round(rsi, 1)
                    })
            except:
                continue
            time.sleep(0.03)

        pumps.sort(key=lambda x: x['ratio_vol'] * (1 + x['c5m'] / 10), reverse=True)
        return pumps[:5]
    except Exception as e:
        print(f"  Error detectar_pumps: {e}")
        return []

# ============================================================
# ANALISIS CON GEMINI
# ============================================================

def analizar_gemini(par, c5m, rsi, cambio_24h):
    """Análisis rápido con Gemini para confirmar entrada."""
    try:
        prompt = f"""Crypto scalping. Par:{par} 5m:{c5m:+.2f}% RSI:{rsi} 24h:{cambio_24h:.1f}%
¿Comprar ahora? Responde SOLO JSON: {{"comprar":true,"confianza":8}}"""
        r = client_gemini.generate_content(prompt)
        texto = r.text.strip().replace('```json','').replace('```','').strip()
        i, f = texto.find('{'), texto.rfind('}')
        if i != -1 and f != -1:
            return json.loads(texto[i:f+1])
    except Exception as e:
        print(f"  Gemini error: {e}")
    return None

# ============================================================
# THREAD DE PUMPS — ciclo 15s
# ============================================================

def thread_pumps():
    print("  [PUMP] Thread iniciado ✓")
    while True:
        try:
            historial = cargar_historial()
            abiertas = len([p for p in historial if p.get('estado') == 'abierta'])

            if abiertas >= MAX_POSICIONES:
                time.sleep(CICLO_PUMP)
                continue

            pumps = detectar_pumps()
            if not pumps:
                time.sleep(CICLO_PUMP)
                continue

            mejor = pumps[0]
            par = mejor['par']
            print(f"  [PUMP] {par} +{mejor['c5m']}% Vol:{mejor['ratio_vol']}x RSI:{mejor['rsi']}")

            # Verificar que no esté ya en uso
            pares_activos = {p['par'] for p in historial if p.get('estado') == 'abierta'}
            if par in pares_activos or en_blacklist(par):
                # Intentar con el siguiente
                for p in pumps[1:]:
                    if p['par'] not in pares_activos and not en_blacklist(p['par']):
                        mejor = p
                        par = p['par']
                        break
                else:
                    time.sleep(CICLO_PUMP)
                    continue

            tg(f"🔍 <b>Pump</b> {par}\n+{mejor['c5m']}% | Vol {mejor['ratio_vol']}x | RSI {mejor['rsi']}")

            # Obtener capital — rebalancear si es necesario
            cap = capital_usdt()
            if cap < MONTO_MIN:
                cap = rebalancear(mejor)
                if cap < MONTO_MIN:
                    time.sleep(CICLO_PUMP)
                    continue

            monto = min(MONTO_POR_TRADE, cap * 0.95)
            if monto < MONTO_MIN:
                time.sleep(CICLO_PUMP)
                continue

            exito, cantidad, precio_c = comprar(par, monto)
            if exito:
                historial = cargar_historial()
                historial.append({
                    'par': par, 'precio_compra': precio_c, 'precio_maximo': precio_c,
                    'cantidad': cantidad, 'monto': monto,
                    'estado': 'abierta', 'estrategia': 'pump',
                    'fecha': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
                guardar_historial(historial)
                tg(f"🚀 <b>PUMP COMPRADO</b> {par}\n+{mejor['c5m']}% | Vol {mejor['ratio_vol']}x\n💰 ${monto:.2f}")

        except Exception as e:
            print(f"  [PUMP] Error: {e}")
        time.sleep(CICLO_PUMP)

# ============================================================
# DASHBOARD
# ============================================================

def iniciar_dashboard():
    try:
        from http.server import HTTPServer, BaseHTTPRequestHandler

        def html():
            historial = cargar_historial()
            bl = cargar_blacklist()
            ranking = cargar_ranking()
            abiertas = [p for p in historial if p.get('estado') == 'abierta']
            ganancias = [p for p in historial if p.get('estado') == 'cerrada_ganancia']
            perdidas = [p for p in historial if p.get('estado') == 'cerrada_perdida']
            neto = sum(p.get('ganancia_pct', 0) for p in ganancias + perdidas)
            cap = capital_usdt()

            pos_rows = ""
            for pos in abiertas:
                p_actual = precio(pos['par']) or 0
                p_compra = float(pos.get('precio_compra', 0))
                pct = ((p_actual - p_compra) / p_compra * 100) if p_compra > 0 else 0
                color = "#00ff88" if pct >= 0 else "#ff4444"
                pos_rows += f"<tr><td>{pos['par']}</td><td>{pos.get('estrategia','').upper()}</td><td>${p_compra:.5f}</td><td>${p_actual:.5f}</td><td style='color:{color}'>{pct:+.3f}%</td><td>${pos.get('monto',0):.2f}</td><td>{pos.get('fecha','')[:16]}</td></tr>"

            hist_rows = ""
            for op in reversed(historial[-20:]):
                if op.get('estado') in ['cerrada_ganancia','cerrada_perdida']:
                    pct = op.get('ganancia_pct', 0)
                    color = "#00ff88" if pct > 0 else "#ff4444"
                    hist_rows += f"<tr><td>{'✅' if pct>0 else '🔴'} {op['par']}</td><td>{op.get('estrategia','').upper()}</td><td style='color:{color}'>{pct:+.3f}%</td><td>{op.get('fecha_cierre','')[:16]}</td><td>{op.get('razon_cierre','')[:40]}</td></tr>"

            rank_rows = ""
            for par, d in sorted(ranking.items(), key=lambda x: x[1].get('score',50), reverse=True)[:10]:
                score = d.get('score',50)
                color = "#00ff88" if score>60 else "#ffaa00" if score>40 else "#ff4444"
                rank_rows += f"<tr><td>{par}</td><td style='color:{color}'>{score}</td><td>{d.get('ops',0)}</td><td>{d.get('ganancias',0)}</td><td>{d.get('perdidas',0)}</td><td>{d.get('pct_total',0):+.2f}%</td></tr>"

            bl_html = "".join([f"<span style='background:#ff4444;padding:3px 8px;border-radius:4px;margin:3px;font-size:12px'>{p}</span>" for p,d in bl.items() if 'expira' in d]) or "<span style='color:#555'>Vacía</span>"
            nc = 'green' if neto >= 0 else 'red'

            return f"""<!DOCTYPE html><html><head><title>Bot Binance v6</title><meta charset="utf-8"><meta http-equiv="refresh" content="15"><style>*{{margin:0;padding:0;box-sizing:border-box}}body{{background:#0a0a0a;color:#fff;font-family:'Courier New',monospace;padding:20px}}h1{{color:#f0b90b;text-align:center;margin-bottom:20px}}h2{{color:#f0b90b;margin:20px 0 8px;font-size:14px}}.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}}.card{{background:#111;border:1px solid #222;border-radius:8px;padding:12px;text-align:center}}.val{{font-size:22px;font-weight:bold;margin-top:4px}}.lbl{{color:#666;font-size:11px}}.green{{color:#00ff88}}.red{{color:#ff4444}}.yellow{{color:#f0b90b}}table{{width:100%;border-collapse:collapse;background:#111;border-radius:8px}}th{{background:#1a1a1a;padding:8px;text-align:left;font-size:11px;color:#666}}td{{padding:7px 8px;border-bottom:1px solid #151515;font-size:11px}}.sec{{margin-bottom:25px}}.ts{{color:#333;text-align:center;margin-top:15px;font-size:10px}}</style></head><body>
<h1>🤖 BOT BINANCE v6</h1>
<div class="grid">
<div class="card"><div class="lbl">💰 Capital</div><div class="val yellow">${cap:.2f}</div></div>
<div class="card"><div class="lbl">📈 Neto</div><div class="val {nc}">{neto:+.2f}%</div></div>
<div class="card"><div class="lbl">✅ Ganancias</div><div class="val green">{len(ganancias)}</div></div>
<div class="card"><div class="lbl">🔴 Pérdidas</div><div class="val red">{len(perdidas)}</div></div>
</div>
<div class="sec"><h2>📊 POSICIONES ({len(abiertas)})</h2><table><tr><th>Par</th><th>Estrategia</th><th>Compra</th><th>Actual</th><th>%</th><th>Monto</th><th>Fecha</th></tr>{pos_rows or "<tr><td colspan='7' style='text-align:center;color:#333;padding:20px'>Sin posiciones</td></tr>"}</table></div>
<div class="sec"><h2>🏆 RANKING</h2><table><tr><th>Par</th><th>Score</th><th>Ops</th><th>G</th><th>P</th><th>Total%</th></tr>{rank_rows or "<tr><td colspan='6' style='text-align:center;color:#333;padding:20px'>Sin datos</td></tr>"}</table></div>
<div class="sec"><h2>🚫 BLACKLIST ({len([p for p in bl if 'expira' in bl[p]])})</h2><div style="padding:10px;background:#111;border-radius:8px">{bl_html}</div></div>
<div class="sec"><h2>📋 ÚLTIMAS 20 OPS</h2><table><tr><th>Par</th><th>Estrategia</th><th>%</th><th>Fecha</th><th>Razón</th></tr>{hist_rows or "<tr><td colspan='5' style='text-align:center;color:#333;padding:20px'>Sin ops</td></tr>"}</table></div>
<div class="ts">Refresh 15s | {datetime.now().strftime('%H:%M:%S')} | Bot v6</div>
</body></html>"""

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    self.send_response(200)
                    self.send_header('Content-type','text/html; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(html().encode('utf-8'))
                except Exception as e:
                    print(f"Dashboard error: {e}")
            def log_message(self, *a): pass

        port = int(os.environ.get('PORT', 8080))
        HTTPServer(('0.0.0.0', port), H).serve_forever()
    except Exception as e:
        print(f"Dashboard error: {e}")

# ============================================================
# SCALPING
# ============================================================

def calcular_macd(precios):
    if len(precios) < 26:
        return 0, 0
    ema12 = calcular_ema(precios, 12)
    ema26 = calcular_ema(precios, 26)
    macd = ema12 - ema26
    signal = calcular_ema([macd] * 9, 9)
    return macd, signal

def scalp_candidatos(historial, cap):
    """Busca candidatos de scalping con RSI + MACD."""
    pares_activos = {p['par'] for p in historial if p.get('estado') == 'abierta'}
    try:
        tickers = client_binance.get_ticker()
        candidatos = [t for t in tickers
                      if t['symbol'].endswith('USDT')
                      and float(t['quoteVolume']) > 500000
                      and -8 <= float(t['priceChangePercent']) <= 5
                      and t['symbol'] not in pares_activos
                      and not en_blacklist(t['symbol'])]
        candidatos.sort(key=lambda x: float(x['quoteVolume']), reverse=True)
        candidatos = candidatos[:30]
    except:
        return

    print(f"\n  Scalping: {len(candidatos)} candidatos")
    for t in candidatos:
        if len([p for p in cargar_historial() if p.get('estado') == 'abierta']) >= MAX_POSICIONES:
            break
        par = t['symbol']
        try:
            klines5 = client_binance.get_klines(symbol=par, interval='5m', limit=50)
            precios5 = [float(k[4]) for k in klines5]
            rsi = calcular_rsi(precios5)
            macd, signal = calcular_macd(precios5)
            cambio_24h = float(t['priceChangePercent'])

            # Condiciones de entrada: RSI bajo + MACD alcista
            if rsi > 45 or macd <= signal:
                continue

            print(f"  {par} RSI:{rsi:.1f} MACD:{'▲' if macd>signal else '▼'} 24h:{cambio_24h:.1f}%")

            # Confirmar con Gemini
            analisis = analizar_gemini(par, cambio_24h, rsi, cambio_24h)
            if not analisis or not analisis.get('comprar') or analisis.get('confianza', 0) < 6:
                continue

            monto = min(MONTO_POR_TRADE, cap * 0.95)
            if monto < MONTO_MIN:
                break

            exito, cantidad, precio_c = comprar(par, monto)
            if exito:
                h = cargar_historial()
                h.append({
                    'par': par, 'precio_compra': precio_c, 'precio_maximo': precio_c,
                    'cantidad': cantidad, 'monto': monto,
                    'estado': 'abierta', 'estrategia': 'scalp',
                    'fecha': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
                guardar_historial(h)
                cap -= monto
                tg(f"📈 <b>SCALP</b> {par}\nRSI:{rsi:.1f} | Gemini:{analisis.get('confianza')}/10\n💰 ${monto:.2f}")
            time.sleep(0.3)
        except:
            continue

# ============================================================
# LOOP PRINCIPAL
# ============================================================

SINC_CICLO = 0
MONITOR_CICLO = 0

def main():
    global SINC_CICLO
    print(f"\n{'='*50}")
    print(f"  BOT v6 — {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")
    cap = capital_usdt()
    historial = cargar_historial()
    abiertas = [p for p in historial if p.get('estado') == 'abierta']
    print(f"  Capital: ${cap:.2f} | Abiertas: {len(abiertas)}")
    print(f"{'='*50}")

    # Sincronización cada 20 ciclos (~30 min)
    SINC_CICLO += 1
    if SINC_CICLO >= 20:
        SINC_CICLO = 0
        sincronizar()

    # Revisar posiciones
    revisar_posiciones()

    # Recargar historial actualizado
    historial = cargar_historial()
    abiertas = [p for p in historial if p.get('estado') == 'abierta']
    pares_activos = {p['par'] for p in abiertas}
    cap = capital_usdt()

    if len(abiertas) >= MAX_POSICIONES or cap < MONTO_MIN:
        return

    # MONITOR — cada 5 ciclos (~7.5 min)
    global MONITOR_CICLO
    MONITOR_CICLO += 1
    if MONITOR_CICLO >= 5:
        MONITOR_CICLO = 0
        try:
            señales = monitor_mercado.escanear()
            for señal in señales:
                if len([p for p in cargar_historial() if p.get('estado') == 'abierta']) >= MAX_POSICIONES:
                    break
                par = señal['par_binance']
                if par in pares_activos or en_blacklist(par):
                    continue
                cap = capital_usdt()
                if cap < MONTO_MIN:
                    break
                monto = min(MONTO_POR_TRADE, cap * 0.95)
                exito, cantidad, precio_c = comprar(par, monto)
                if exito:
                    h = cargar_historial()
                    h.append({
                        'par': par, 'precio_compra': precio_c, 'precio_maximo': precio_c,
                        'cantidad': cantidad, 'monto': monto,
                        'estado': 'abierta', 'estrategia': 'monitor',
                        'fecha': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })
                    guardar_historial(h)
                    pares_activos.add(par)
                    tg(f"📡 <b>MONITOR</b> {par}\n{señal['n_fuentes']} fuentes | {señal['confianza_groq']}/10\n💰 ${monto:.2f}")
        except Exception as e:
            print(f"  Error monitor: {e}")

    # LISTINGS — nuevos pares en Binance
    try:
        for listing in listing_detector.detectar_nuevos():
            if len([p for p in cargar_historial() if p.get('estado') == 'abierta']) >= MAX_POSICIONES:
                break
            par = listing['par']
            if par in pares_activos or en_blacklist(par):
                continue
            cap = capital_usdt()
            if cap < MONTO_MIN:
                break
            monto = min(MONTO_POR_TRADE * 0.5, cap * 0.95)  # montos más chicos para listings
            if monto < MONTO_MIN:
                continue
            exito, cantidad, precio_c = comprar(par, monto)
            if exito:
                h = cargar_historial()
                h.append({
                    'par': par, 'precio_compra': precio_c, 'precio_maximo': precio_c,
                    'cantidad': cantidad, 'monto': monto,
                    'estado': 'abierta', 'estrategia': 'listing',
                    'fecha': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
                guardar_historial(h)
                pares_activos.add(par)
                tg(f"🆕 <b>LISTING</b> {par}\nNuevo par detectado\n💰 ${monto:.2f}")
    except Exception as e:
        print(f"  Error listings: {e}")

    # SCALPING — buscar oportunidades cada ciclo
    try:
        scalp_candidatos(historial, cap)
    except Exception as e:
        print(f"  Error scalping: {e}")

if __name__ == "__main__":
    tg(
        "🤖 <b>Bot Binance v6</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🌍 Sin restricciones horarias — 24/7\n"
        "🚀 Pump detector: ciclo 15s\n"
        "📈 Scalping + Monitor + Listings 24/7\n"
        "🔄 Loop principal: ciclo 90s\n"
        "📊 Trailing inteligente activo\n"
        "🔄 Rebalanceo automático\n"
        "⏱️ Libera posiciones estancadas 30min\n"
        "🤖 IA: Gemini 2.0 Flash"
    )
    sincronizar()
    threading.Thread(target=iniciar_dashboard, daemon=True).start()
    threading.Thread(target=thread_pumps, daemon=True).start()
    print("  Threads iniciados ✓")
    while True:
        try:
            main()
        except Exception as e:
            print(f"Error main: {e}")
            tg(f"⚠️ Error: {e}")
        time.sleep(CICLO_MAIN)
