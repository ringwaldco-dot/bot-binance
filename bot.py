# -*- coding: utf-8 -*-

import os
import json
import time
import threading
import numpy as np
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from binance.client import Client
import warnings
warnings.filterwarnings("ignore")
import google.generativeai as genai
from onchain_sentiment import get_onchain_signal, format_signal_telegram
from market_monitor import MonitorMercado
from listing_detector import ListingDetector

load_dotenv()

BINANCE_API_KEY = os.getenv('BINANCE_API_KEY')
BINANCE_SECRET_KEY = os.getenv('BINANCE_SECRET_KEY')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

TELEGRAM_TOKEN = "8513198629:AAHmlayu6y_Z2e2SUCkvKkLIEhj6kstxYT4"
TELEGRAM_CHAT_ID = "1576867878"

CAPITAL_TOTAL = 30.0
MAX_POSICIONES = 3
MAX_POSICIONES_PUMP = 2
MONTO_BASE = CAPITAL_TOTAL / MAX_POSICIONES
MONTO_MIN = 5.0
MONTO_MAX = 20.0
MONTO_PUMP = 7.0
TAKE_PROFIT = 0.012
TAKE_PROFIT_PUMP = 0.015
STOP_LOSS = 0.008
STOP_LOSS_PUMP = 0.006
TRAILING_STOP = 0.005
CRASH_THRESHOLD = -8.0
CUT_LOSS_UMBRAL = -0.003
CUT_LOSS_MINUTOS = 5
CICLO_PUMP_SEGUNDOS = 15
CICLO_MAIN_SEGUNDOS = 90

HISTORIAL_FILE = "historial_binance.json"
BLACKLIST_FILE = "blacklist.json"
REPORTE_FILE = "ultimo_reporte.json"
RANKING_FILE = "ranking_pares.json"

MONITOR_CICLO = 0
_lock = threading.Lock()

client_binance = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)

# Gemini 2.0 Flash — reemplaza Groq
genai.configure(api_key=GEMINI_API_KEY)
client_gemini = genai.GenerativeModel(
    model_name='gemini-2.0-flash',
    generation_config=genai.GenerationConfig(temperature=0.2, max_output_tokens=100)
)

monitor_mercado = MonitorMercado()
listing_detector = ListingDetector()

# ============================================================
# MODO — 24/7 GLOBAL, SIN RESTRICCIONES HORARIAS
# ============================================================

def obtener_modo_horario():
    """Opera igual a las 3am que a las 3pm. Mercado crypto es global 24/7."""
    return 'activo', 0.012, 0.008

# ============================================================
# RANKING
# ============================================================

def cargar_ranking():
    if os.path.exists(RANKING_FILE):
        with open(RANKING_FILE, "r") as f:
            return json.load(f)
    return {}

def guardar_ranking(ranking):
    with open(RANKING_FILE, "w") as f:
        json.dump(ranking, f, indent=2)

def actualizar_ranking(par, ganancia_pct):
    ranking = cargar_ranking()
    if par not in ranking:
        ranking[par] = {'ops': 0, 'ganancias': 0, 'perdidas': 0, 'pct_total': 0, 'score': 50}
    ranking[par]['ops'] += 1
    ranking[par]['pct_total'] = round(ranking[par]['pct_total'] + ganancia_pct, 3)
    if ganancia_pct > 0:
        ranking[par]['ganancias'] += 1
        ranking[par]['score'] = min(100, ranking[par]['score'] + 5)
    else:
        ranking[par]['perdidas'] += 1
        ranking[par]['score'] = max(0, ranking[par]['score'] - 8)
    guardar_ranking(ranking)

def obtener_score_par(par):
    return cargar_ranking().get(par, {}).get('score', 50)

# ============================================================
# TELEGRAM
# ============================================================

def enviar_telegram(mensaje):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "HTML"})
    except:
        pass

# ============================================================
# HISTORIAL
# ============================================================

def cargar_historial():
    if os.path.exists(HISTORIAL_FILE):
        with open(HISTORIAL_FILE, "r") as f:
            return json.load(f)
    return []

def guardar_historial(historial):
    with open(HISTORIAL_FILE, "w") as f:
        json.dump(historial, f, indent=2)

# ============================================================
# BLACKLIST
# ============================================================

def cargar_blacklist():
    if os.path.exists(BLACKLIST_FILE):
        with open(BLACKLIST_FILE, "r") as f:
            return json.load(f)
    return {}

def guardar_blacklist(blacklist):
    with open(BLACKLIST_FILE, "w") as f:
        json.dump(blacklist, f, indent=2)

def esta_en_blacklist(par):
    blacklist = cargar_blacklist()
    if par not in blacklist:
        return False
    expira = datetime.fromisoformat(blacklist[par]['expira'])
    if datetime.now() > expira:
        del blacklist[par]
        guardar_blacklist(blacklist)
        return False
    return True

def agregar_a_blacklist(par, razon):
    blacklist = cargar_blacklist()
    expira = (datetime.now() + timedelta(hours=24)).isoformat()
    veces = blacklist.get(par, {}).get('veces', 0) + 1
    blacklist[par] = {'razon': razon, 'expira': expira, 'veces': veces}
    guardar_blacklist(blacklist)
    print(f"  {par} blacklist 24hs ({veces}x)")
    enviar_telegram(f"🚫 <b>BLACKLIST</b> {par}\nRazón: {razon}")

def actualizar_blacklist_post_venta(par, ganancia_pct):
    actualizar_ranking(par, ganancia_pct)
    if ganancia_pct < 0:
        blacklist = cargar_blacklist()
        veces = blacklist.get(par, {}).get('veces', 0) + 1
        if veces >= 2:
            agregar_a_blacklist(par, f"Perdio {veces} veces seguidas")
        else:
            blacklist[par] = {'veces': veces, 'ultima_perdida': datetime.now().isoformat()}
            guardar_blacklist(blacklist)
    else:
        blacklist = cargar_blacklist()
        if par in blacklist and 'expira' not in blacklist[par]:
            del blacklist[par]
            guardar_blacklist(blacklist)

# ============================================================
# MONTO DINAMICO
# ============================================================

def calcular_monto_dinamico(historial):
    cerradas = [p for p in historial if p.get('estado') in ['cerrada_ganancia', 'cerrada_perdida']]
    if len(cerradas) < 3:
        return MONTO_BASE
    ultimas = cerradas[-5:]
    ganancias = sum(1 for p in ultimas if p.get('estado') == 'cerrada_ganancia')
    ratio = ganancias / len(ultimas)
    if ratio >= 0.8:
        return round(min(MONTO_MAX, MONTO_BASE * 1.3), 2)
    elif ratio <= 0.3:
        return round(max(MONTO_MIN, MONTO_BASE * 0.7), 2)
    return MONTO_BASE

def calcular_monto_diversificado(historial, capital_disponible):
    monto = calcular_monto_dinamico(historial)
    monto = min(monto, capital_disponible * 0.9)
    return round(monto, 2) if monto >= MONTO_MIN else 0

# ============================================================
# REPORTE DIARIO
# ============================================================

def enviar_reporte_diario():
    try:
        ultimo = {}
        if os.path.exists(REPORTE_FILE):
            with open(REPORTE_FILE, "r") as f:
                ultimo = json.load(f)
        hoy = datetime.utcnow().strftime("%Y-%m-%d")
        if ultimo.get('fecha') == hoy or datetime.utcnow().hour != 8:
            return
        historial = cargar_historial()
        ayer = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        ops_ayer = [p for p in historial if p.get('fecha_cierre', '').startswith(ayer)]
        ganancias = [p for p in ops_ayer if p.get('estado') == 'cerrada_ganancia']
        perdidas = [p for p in ops_ayer if p.get('estado') == 'cerrada_perdida']
        total_g = sum(p.get('ganancia_pct', 0) for p in ganancias)
        total_p = sum(p.get('ganancia_pct', 0) for p in perdidas)
        todas_g = [p for p in historial if p.get('estado') == 'cerrada_ganancia']
        todas_p = [p for p in historial if p.get('estado') == 'cerrada_perdida']
        neto_total = sum(p.get('ganancia_pct', 0) for p in todas_g + todas_p)
        ranking = cargar_ranking()
        top = sorted(ranking.items(), key=lambda x: x[1]['score'], reverse=True)[:3]
        top_str = " | ".join([f"{p[0]}({p[1]['score']})" for p in top]) or "Sin datos"
        pumps_g = len([p for p in todas_g if p.get('estrategia') == 'pump'])
        pumps_p = len([p for p in todas_p if p.get('estrategia') == 'pump'])
        scalp_g = len([p for p in todas_g if p.get('estrategia') != 'pump'])
        scalp_p = len([p for p in todas_p if p.get('estrategia') != 'pump'])
        cut_losses = len([p for p in todas_p if 'cut_loss' in p.get('razon_cierre', '')])
        rebalanceos = len([p for p in historial if 'rebalanceo' in p.get('razon_cierre', '')])
        enviar_telegram(
            f"📊 <b>REPORTE DIARIO UTC</b> {ayer}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 Ops: {len(ops_ayer)} | ✅ {len(ganancias)} (+{total_g:.2f}%) | 🔴 {len(perdidas)} ({total_p:.2f}%)\n"
            f"💰 Neto día: {total_g+total_p:+.2f}%\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🚀 Pump: {pumps_g}G/{pumps_p}P | 📉 Scalp: {scalp_g}G/{scalp_p}P\n"
            f"✂️ Cut losses: {cut_losses} | 🔄 Rebalanceos: {rebalanceos}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📦 Acumulado: {neto_total:+.2f}% | 🚫 BL: {len(cargar_blacklist())}\n"
            f"🏆 Top: {top_str}"
        )
        try:
            sig_btc = get_onchain_signal("BTCUSDT")
            sig_eth = get_onchain_signal("ETHUSDT")
            fg = sig_btc['components']['fear_greed']
            enviar_telegram(
                f"📡 <b>Sentiment apertura UTC</b>\n"
                f"BTC {sig_btc['emoji']} {sig_btc['score']:+.3f} {sig_btc['action']}\n"
                f"ETH {sig_eth['emoji']} {sig_eth['score']:+.3f} {sig_eth['action']}\n"
                f"Fear &amp; Greed: {fg['value']} ({fg['label']})"
            )
        except Exception as e:
            print(f"  Error onchain reporte: {e}")
        with open(REPORTE_FILE, "w") as f:
            json.dump({'fecha': hoy}, f)
    except Exception as e:
        print(f"Error reporte: {e}")

# ============================================================
# INDICADORES
# ============================================================

def obtener_precio(par):
    try:
        return float(client_binance.get_symbol_ticker(symbol=par)['price'])
    except:
        return None

def obtener_capital_disponible():
    try:
        for b in client_binance.get_account()['balances']:
            if b['asset'] == 'USDT':
                return float(b['free'])
        return 0
    except:
        return 0

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

def calcular_macd(precios):
    if len(precios) < 26:
        return 0, 0
    p = np.array(precios)
    macd = calcular_ema(p, 12) - calcular_ema(p, 26)
    return macd, calcular_ema(np.array([macd] * 9), 9)

def calcular_bollinger(precios, periodo=20):
    if len(precios) < periodo:
        return precios[-1], precios[-1], precios[-1]
    u = precios[-periodo:]
    m = np.mean(u)
    s = np.std(u)
    return m + 2*s, m, m - 2*s

def obtener_datos_mercado(par, intervalo='5m', limite=50):
    try:
        klines = client_binance.get_klines(symbol=par, interval=intervalo, limit=limite)
        precios = [float(k[4]) for k in klines]
        vols = [float(k[5]) for k in klines]
        rsi = calcular_rsi(precios)
        macd, signal = calcular_macd(precios)
        bb_sup, bb_med, bb_inf = calcular_bollinger(precios)
        vol_prom = np.mean(vols[-10:])
        return {
            'par': par, 'precio_actual': precios[-1],
            'cambio_1h': round(((precios[-1] - precios[-12]) / precios[-12]) * 100, 3),
            'rsi': round(rsi, 2), 'macd': macd, 'macd_signal': signal,
            'bb_sup': bb_sup, 'bb_media': bb_med, 'bb_inf': bb_inf,
            'volumen_ratio': vols[-1] / vol_prom if vol_prom > 0 else 1,
            'precios': precios
        }
    except:
        return None

def confirmar_dos_timeframes(par):
    try:
        d5 = obtener_datos_mercado(par, '5m', 50)
        d1h = obtener_datos_mercado(par, '1h', 50)
        if not d5 or not d1h:
            return False, "Sin datos"
        s5 = sum([d5['rsi'] < 45, d5['macd'] > d5['macd_signal'], d5['precio_actual'] <= d5['bb_inf'] * 1.005])
        s1h = sum([d1h['rsi'] < 55, d1h['macd'] > d1h['macd_signal'], d1h['precio_actual'] <= d1h['bb_inf'] * 1.01])
        return s5 >= 1 or s1h >= 1, f"5m:{s5}/3 1h:{s1h}/3"
    except:
        return False, "Error"

def es_caida_libre(par, cambio_24h):
    try:
        klines = client_binance.get_klines(symbol=par, interval='1h', limit=24)
        p = [float(k[4]) for k in klines]
        c6h = ((p[-1] - p[-6]) / p[-6]) * 100
        c12h = ((p[-1] - p[-12]) / p[-12]) * 100
        acelerando = all(((p[i] - p[i-1]) / p[i-1]) * 100 < -0.3 for i in range(-4, 0))
        if c6h < CRASH_THRESHOLD or c12h < CRASH_THRESHOLD * 1.5 or acelerando:
            print(f"  CRASH: 6h={c6h:.1f}% 12h={c12h:.1f}%")
            return True
        return False
    except:
        return False

def analizar_sentimiento_groq(par, d5, d1h, cambio_24h, modo, score_par, onchain_score=0.0):
    """Análisis con Gemini 2.0 Flash (reemplaza Groq)."""
    try:
        if onchain_score >= 0.35:   oc = f"ALCISTA ({onchain_score:+.2f})"
        elif onchain_score >= 0.15: oc = f"LEV.ALCISTA ({onchain_score:+.2f})"
        elif onchain_score <= -0.35: oc = f"BAJISTA ({onchain_score:+.2f})"
        elif onchain_score <= -0.15: oc = f"LEV.BAJISTA ({onchain_score:+.2f})"
        else:                        oc = f"NEUTRO ({onchain_score:+.2f})"

        prompt = f"""Trader experto crypto scalping 24/7 global.
Par:{par} Score:{score_par}/100 24h:{cambio_24h}%
5M: RSI {d5['rsi']} MACD {'▲' if d5['macd']>d5['macd_signal'] else '▼'} BB {'PISO' if d5['precio_actual']<=d5['bb_inf']*1.005 else 'MED'}
1H: RSI {d1h['rsi']} MACD {'▲' if d1h['macd']>d1h['macd_signal'] else '▼'} BB {'PISO' if d1h['precio_actual']<=d1h['bb_inf']*1.01 else 'MED'}
OnChain:{oc}
Comprá si 1+ timeframe positivo. OnChain BAJISTA: exigí señales fuertes.
Responde SOLO JSON sin texto adicional: {{"comprar":true,"confianza":8,"razon":"1 linea"}}"""

        response = client_gemini.generate_content(prompt)
        texto = response.text.strip().replace('```json', '').replace('```', '').strip()
        i, f = texto.find('{'), texto.rfind('}')
        if i != -1 and f != -1:
            return json.loads(texto[i:f+1])
    except Exception as e:
        print(f"  Error Gemini: {e}")
    return None

def obtener_mejores_pares():
    try:
        tickers = client_binance.get_ticker()
        pares = [t for t in tickers
                 if t['symbol'].endswith('USDT')
                 and float(t['quoteVolume']) > 2000000
                 and float(t['lastPrice']) > 0.0001
                 and float(t['lastPrice']) < 500]
        pares.sort(key=lambda x: float(x['quoteVolume']), reverse=True)
        return pares[:80]
    except:
        return []

def filtrar_candidatos(pares_tickers):
    """Sin distinción horaria — rango -10% a +3% siempre activo."""
    candidatos = []
    for t in pares_tickers:
        cambio = float(t['priceChangePercent'])
        vol = float(t['quoteVolume'])
        par = t['symbol']
        if -10 <= cambio <= 3.0 and vol > 2000000 and not esta_en_blacklist(par):
            candidatos.append({'par': par, 'cambio_24h': cambio, 'volumen': vol, 'score': obtener_score_par(par)})
    candidatos.sort(key=lambda x: (x['score'], -x['cambio_24h']), reverse=True)
    return candidatos[:20]

def ejecutar_compra(par, monto, datos):
    try:
        orden = client_binance.order_market_buy(symbol=par, quoteOrderQty=monto)
        qty = float(orden['executedQty'])
        precio = float(orden['fills'][0]['price']) if orden.get('fills') else obtener_precio(par)
        rsi_val = datos['rsi'] if isinstance(datos, dict) else datos
        print(f"  COMPRA OK! {qty} {par} a ${precio}")
        enviar_telegram(f"🟢 <b>COMPRA</b> {par}\n💰 ${precio} | RSI:{rsi_val} | Score:{obtener_score_par(par)}/100 | ${monto}")
        return True, qty, precio
    except Exception as e:
        print(f"  Error comprando: {e}")
        return False, 0, 0

def ejecutar_venta(par, cantidad, precio_actual, pct, tipo):
    try:
        info = client_binance.get_symbol_info(par)
        step = next(f['stepSize'] for f in info['filters'] if f['filterType'] == 'LOT_SIZE')
        dec = len(step.rstrip('0').split('.')[-1]) if '.' in step else 0
        orden = client_binance.order_market_sell(symbol=par, quantity=round(cantidad, dec))
        emojis = {'ganancia': '✅', 'pump': '🚀', 'trailing': '📉', 'perdida': '🔴'}
        nombres = {'ganancia': 'TAKE PROFIT', 'pump': 'PUMP PROFIT', 'trailing': 'TRAILING STOP', 'perdida': 'STOP LOSS'}
        enviar_telegram(f"{emojis.get(tipo,'✅')} <b>{nombres.get(tipo,'VENTA')}</b> {par}\n{'Ganancia' if pct>0 else 'Pérdida'}: {pct:+.3f}%\n💰 ${precio_actual}")
        actualizar_blacklist_post_venta(par, pct)
        return True
    except Exception as e:
        print(f"  Error vendiendo: {e}")
        return False

# ============================================================
# CUT LOSS AGRESIVO
# ============================================================

def revisar_cut_loss():
    with _lock:
        historial = cargar_historial()
        cambios = False
        for i, pos in enumerate(historial):
            if pos.get('estado') != 'abierta':
                continue
            precio_actual = obtener_precio(pos['par'])
            if not precio_actual:
                continue
            cambio = (precio_actual - float(pos['precio_compra'])) / float(pos['precio_compra'])
            pct = round(cambio * 100, 3)
            if cambio >= 0:
                if historial[i].get('en_perdida_desde'):
                    historial[i]['en_perdida_desde'] = None
                    cambios = True
                continue
            if cambio <= CUT_LOSS_UMBRAL:
                ahora = datetime.now()
                if not historial[i].get('en_perdida_desde'):
                    historial[i]['en_perdida_desde'] = ahora.isoformat()
                    cambios = True
                    print(f"  [CUT LOSS] {pos['par']} {pct}% — contador iniciado")
                else:
                    mins = (ahora - datetime.fromisoformat(historial[i]['en_perdida_desde'])).total_seconds() / 60
                    print(f"  [CUT LOSS] {pos['par']} {pct}% | {mins:.1f}min")
                    if mins >= CUT_LOSS_MINUTOS:
                        print(f"  [CUT LOSS] CORTANDO {pos['par']} — {mins:.0f}min en {pct}%")
                        if ejecutar_venta(pos['par'], pos.get('cantidad', 0), precio_actual, pct, 'perdida'):
                            historial[i].update({
                                'estado': 'cerrada_perdida', 'precio_venta': precio_actual,
                                'ganancia_pct': pct, 'razon_cierre': f'cut_loss_{mins:.0f}min',
                                'fecha_cierre': ahora.strftime("%Y-%m-%d %H:%M:%S")
                            })
                            cambios = True
                            enviar_telegram(
                                f"✂️ <b>CUT LOSS</b> {pos['par']}\n"
                                f"📉 {pct:+.3f}% por {mins:.0f} minutos\n"
                                f"💡 Capital liberado"
                            )
            else:
                if historial[i].get('en_perdida_desde'):
                    historial[i]['en_perdida_desde'] = None
                    cambios = True
        if cambios:
            guardar_historial(historial)

# ============================================================
# REBALANCEO DINAMICO
# ============================================================

def elegir_posicion_sacrificable():
    historial = cargar_historial()
    posiciones = [p for p in historial if p.get('estado') == 'abierta']
    if not posiciones:
        return None
    peor, peor_cambio = None, float('inf')
    for pos in posiciones:
        precio_actual = obtener_precio(pos['par'])
        if not precio_actual:
            continue
        cambio = (precio_actual - float(pos['precio_compra'])) / float(pos['precio_compra'])
        if cambio < peor_cambio:
            peor_cambio = cambio
            peor = {'par': pos['par'], 'cantidad': pos.get('cantidad', 0),
                    'precio_actual': precio_actual, 'cambio_pct': round(cambio * 100, 3)}
    return peor

def rebalancear_si_necesario(oportunidad, tipo='pump', confianza=0):
    if obtener_capital_disponible() >= MONTO_MIN:
        return 0
    if tipo == 'pump' and oportunidad.get('ratio_vol', 0) < 2.0:
        return 0
    if tipo == 'scalp' and confianza < 7:
        return 0
    sacrificable = elegir_posicion_sacrificable()
    if not sacrificable:
        return 0
    par = sacrificable['par']
    pct = sacrificable['cambio_pct']
    precio_actual = sacrificable['precio_actual']
    cantidad = sacrificable['cantidad']
    opp = oportunidad.get('par', oportunidad.get('par_binance', '?'))
    print(f"  [REBALANCEO] {par} ({pct:+.3f}%) → {opp}")
    with _lock:
        if ejecutar_venta(par, cantidad, precio_actual, pct, 'perdida' if pct < 0 else 'ganancia'):
            historial = cargar_historial()
            for i, pos in enumerate(historial):
                if pos.get('par') == par and pos.get('estado') == 'abierta':
                    historial[i].update({
                        'estado': 'cerrada_ganancia' if pct >= 0 else 'cerrada_perdida',
                        'precio_venta': precio_actual, 'ganancia_pct': pct,
                        'razon_cierre': f'rebalanceo_por_{opp}',
                        'fecha_cierre': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })
                    break
            guardar_historial(historial)
            enviar_telegram(
                f"🔄 <b>REBALANCEO</b>\n"
                f"❌ Vendido: {par} {pct:+.3f}%\n"
                f"✅ Entrando en: <b>{opp}</b>\n"
                f"💡 {'Pump vol ' + str(oportunidad.get('ratio_vol','?')) + 'x' if tipo=='pump' else 'Scalp ' + str(confianza) + '/10'}"
            )
            return round(cantidad * precio_actual, 2)
    return 0

# ============================================================
# THREAD DEDICADO A PUMPS — 24/7, ciclo 15s
# ============================================================

def detectar_pumps_rapido():
    try:
        tickers = client_binance.get_ticker()
        usdt = [t for t in tickers
                if t['symbol'].endswith('USDT')
                and float(t['quoteVolume']) > 1000000
                and float(t['lastPrice']) > 0.0001
                and float(t['lastPrice']) < 500
                and not esta_en_blacklist(t['symbol'])]
        pumps = []
        for t in usdt[:100]:
            par = t['symbol']
            try:
                klines = client_binance.get_klines(symbol=par, interval='1m', limit=8)
                precios = [float(k[4]) for k in klines]
                vols = [float(k[5]) for k in klines]
                c3m = ((precios[-1] - precios[-3]) / precios[-3]) * 100
                c5m = ((precios[-1] - precios[-5]) / precios[-5]) * 100
                vol_prom = np.mean(vols[:-2])
                ratio_vol = np.mean(vols[-2:]) / vol_prom if vol_prom > 0 else 1
                rsi = calcular_rsi(precios)
                if c3m >= 0.4 and c5m >= 0.5 and ratio_vol >= 1.8 and rsi < 75:
                    pumps.append({
                        'par': par, 'cambio_3m': round(c3m, 3),
                        'cambio_5m': round(c5m, 3), 'ratio_vol': round(ratio_vol, 2),
                        'rsi': round(rsi, 1), 'score': obtener_score_par(par)
                    })
            except:
                continue
            time.sleep(0.05)
        pumps.sort(key=lambda x: x['ratio_vol'] * (1 + x['score'] / 100), reverse=True)
        return pumps[:3]
    except Exception as e:
        print(f"  Error detectar_pumps_rapido: {e}")
        return []

def _cerrar_posicion_historial(par, precio_actual, pct, estado):
    historial = cargar_historial()
    for i, pos in enumerate(historial):
        if pos.get('par') == par and pos.get('estado') == 'abierta' and pos.get('estrategia') == 'pump':
            historial[i].update({
                'estado': estado, 'precio_venta': precio_actual,
                'ganancia_pct': pct, 'fecha_cierre': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            break
    guardar_historial(historial)

def vigilar_posicion_pump(par, precio_compra, cantidad, monto):
    precio_maximo = precio_compra
    inicio = time.time()
    print(f"  [WATCH] {par} ${precio_compra:.6f} TP:{TAKE_PROFIT_PUMP*100}% SL:{STOP_LOSS_PUMP*100}%")
    while time.time() - inicio < 300:
        time.sleep(5)
        precio_actual = obtener_precio(par)
        if not precio_actual:
            continue
        if precio_actual > precio_maximo:
            precio_maximo = precio_actual
        cambio = (precio_actual - precio_compra) / precio_compra
        pct = round(cambio * 100, 3)
        caida = (precio_maximo - precio_actual) / precio_maximo if precio_maximo > 0 else 0
        if cambio >= TAKE_PROFIT and caida >= TRAILING_STOP:
            print(f"  [WATCH] TRAILING {par} +{pct}%")
            with _lock:
                ejecutar_venta(par, cantidad, precio_actual, pct, 'trailing')
            _cerrar_posicion_historial(par, precio_actual, pct, 'cerrada_ganancia')
            return
        if cambio >= TAKE_PROFIT_PUMP:
            print(f"  [WATCH] TP {par} +{pct}%!")
            with _lock:
                ejecutar_venta(par, cantidad, precio_actual, pct, 'pump')
            _cerrar_posicion_historial(par, precio_actual, pct, 'cerrada_ganancia')
            return
        if cambio <= -STOP_LOSS_PUMP:
            print(f"  [WATCH] SL {par} {pct}%")
            with _lock:
                ejecutar_venta(par, cantidad, precio_actual, pct, 'perdida')
            _cerrar_posicion_historial(par, precio_actual, pct, 'cerrada_perdida')
            return
    precio_actual = obtener_precio(par) or precio_compra
    cambio = (precio_actual - precio_compra) / precio_compra
    pct = round(cambio * 100, 3)
    print(f"  [WATCH] TIMEOUT {par} {pct}%")
    with _lock:
        ejecutar_venta(par, cantidad, precio_actual, pct, 'ganancia' if pct > 0 else 'perdida')
    _cerrar_posicion_historial(par, precio_actual, pct, 'cerrada_ganancia' if pct > 0 else 'cerrada_perdida')

def ciclo_pump_agresivo():
    """Thread pump — 24/7, sin pausa nocturna, ciclo 15s."""
    print("  [PUMP THREAD] Iniciado 24/7 ✓")
    while True:
        try:
            with _lock:
                historial = cargar_historial()
                pumps_ab = len([p for p in historial if p.get('estado') == 'abierta' and p.get('estrategia') == 'pump'])
                total_ab = len([p for p in historial if p.get('estado') == 'abierta'])
                capital = obtener_capital_disponible()
            if pumps_ab >= MAX_POSICIONES_PUMP or total_ab >= MAX_POSICIONES:
                time.sleep(CICLO_PUMP_SEGUNDOS)
                continue
            pumps = detectar_pumps_rapido()
            if not pumps:
                time.sleep(CICLO_PUMP_SEGUNDOS)
                continue
            print(f"\n  [PUMP] {len(pumps)} candidatos — {datetime.utcnow().strftime('%H:%M:%S')} UTC")
            for p in pumps:
                par = p['par']
                with _lock:
                    historial = cargar_historial()
                    pares_en_uso = {pos['par'] for pos in historial if pos.get('estado') == 'abierta'}
                    pumps_ab = len([pos for pos in historial if pos.get('estado') == 'abierta' and pos.get('estrategia') == 'pump'])
                    total_ab = len([pos for pos in historial if pos.get('estado') == 'abierta'])
                    capital = obtener_capital_disponible()
                if par in pares_en_uso or esta_en_blacklist(par):
                    continue
                if pumps_ab >= MAX_POSICIONES_PUMP or total_ab >= MAX_POSICIONES:
                    break
                if capital < MONTO_MIN:
                    liberado = rebalancear_si_necesario(p, tipo='pump')
                    if liberado:
                        capital = obtener_capital_disponible()
                        time.sleep(1)
                    else:
                        continue
                monto = min(MONTO_PUMP, capital * 0.9)
                if monto < MONTO_MIN:
                    continue
                print(f"  [PUMP] ENTRANDO {par} +{p['cambio_5m']}% Vol:{p['ratio_vol']}x RSI:{p['rsi']} ${monto}")
                with _lock:
                    exito, cantidad, precio = ejecutar_compra(par, monto, {'rsi': p['rsi']})
                    if exito:
                        historial = cargar_historial()
                        historial.append({
                            'par': par, 'precio_compra': precio, 'precio_maximo': precio,
                            'cantidad': cantidad, 'monto': monto, 'rsi_entrada': p['rsi'],
                            'confianza': 9, 'razon': f"PUMP {p['cambio_5m']}% vol {p['ratio_vol']}x",
                            'onchain_score': 0.0, 'en_perdida_desde': None,
                            'estado': 'abierta', 'estrategia': 'pump',
                            'fecha': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        })
                        guardar_historial(historial)
                        enviar_telegram(
                            f"🚀 <b>PUMP</b> {par}\n"
                            f"+{p['cambio_5m']}% | Vol {p['ratio_vol']}x | RSI {p['rsi']}\n"
                            f"💰 ${monto} | TP:{TAKE_PROFIT_PUMP*100}% SL:{STOP_LOSS_PUMP*100}%"
                        )
                        threading.Thread(target=vigilar_posicion_pump, args=(par, precio, cantidad, monto), daemon=True).start()
        except Exception as e:
            print(f"  [PUMP THREAD] Error: {e}")
        time.sleep(CICLO_PUMP_SEGUNDOS)

# ============================================================
# REVISAR POSICIONES SCALP
# ============================================================

def revisar_posiciones(tp_actual, sl_actual):
    historial = cargar_historial()
    posiciones = [p for p in historial if p.get('estado') == 'abierta' and p.get('estrategia') != 'pump']
    if not posiciones:
        return 0
    print(f"\nRevisando {len(posiciones)} posiciones scalp/monitor...")
    cerradas = 0
    for i, pos in enumerate(historial):
        if pos.get('estado') != 'abierta' or pos.get('estrategia') == 'pump':
            continue
        precio_actual = obtener_precio(pos['par'])
        if not precio_actual:
            continue
        precio_compra = float(pos['precio_compra'])
        cambio = (precio_actual - precio_compra) / precio_compra
        pct = round(cambio * 100, 3)
        precio_maximo = float(pos.get('precio_maximo', precio_compra))
        if precio_actual > precio_maximo:
            precio_maximo = precio_actual
            historial[i]['precio_maximo'] = precio_maximo
        caida = (precio_maximo - precio_actual) / precio_maximo if precio_maximo > 0 else 0
        print(f"  {pos['par']} [{pos.get('estrategia','scalp')}] | {pct:+.3f}%")
        if cambio >= tp_actual and caida >= TRAILING_STOP:
            if ejecutar_venta(pos['par'], pos.get('cantidad', 0), precio_actual, pct, 'trailing'):
                historial[i].update({'estado': 'cerrada_ganancia', 'precio_venta': precio_actual,
                                     'ganancia_pct': pct, 'fecha_cierre': datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                cerradas += 1
        elif cambio >= tp_actual:
            if ejecutar_venta(pos['par'], pos.get('cantidad', 0), precio_actual, pct, 'ganancia'):
                historial[i].update({'estado': 'cerrada_ganancia', 'precio_venta': precio_actual,
                                     'ganancia_pct': pct, 'fecha_cierre': datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                cerradas += 1
        elif cambio <= -sl_actual:
            if ejecutar_venta(pos['par'], pos.get('cantidad', 0), precio_actual, pct, 'perdida'):
                historial[i].update({'estado': 'cerrada_perdida', 'precio_venta': precio_actual,
                                     'ganancia_pct': pct, 'fecha_cierre': datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                cerradas += 1
        else:
            print(f"  Manteniendo ({caida*100:.2f}% desde max)")
    guardar_historial(historial)
    return cerradas

def mostrar_resumen():
    historial = cargar_historial()
    if not historial:
        return
    g = [p for p in historial if p.get('estado') == 'cerrada_ganancia']
    p = [p for p in historial if p.get('estado') == 'cerrada_perdida']
    ab = [p for p in historial if p.get('estado') == 'abierta']
    pumps_ab = len([x for x in ab if x.get('estrategia') == 'pump'])
    neto = sum(x.get('ganancia_pct', 0) for x in g+p)
    print(f"  G:{len(g)} P:{len(p)} | Abiertas:{len(ab)}(pump:{pumps_ab}) | Neto:{neto:+.2f}% | BL:{len(cargar_blacklist())}")

def procesar_señales_monitor(señales, historial, capital_disponible, pares_en_uso, posiciones_abiertas, tp_actual, sl_actual):
    for señal in señales:
        if posiciones_abiertas >= MAX_POSICIONES:
            break
        par = señal['par_binance']
        if par in pares_en_uso or esta_en_blacklist(par):
            continue
        sig = {"score": 0.0, "action": "NEUTRAL", "block": False, "emoji": "⚪"}
        try:
            sig = get_onchain_signal(par)
            if sig['block']:
                continue
        except:
            pass
        if es_caida_libre(par, señal['cambio_24h']):
            continue
        d5 = obtener_datos_mercado(par, '5m', 50)
        d1h = obtener_datos_mercado(par, '1h', 50)
        if not d5 or not d1h:
            continue
        analisis = analizar_sentimiento_groq(par, d5, d1h, señal['cambio_24h'], 'activo', obtener_score_par(par), sig['score'])
        if not analisis or not analisis.get('comprar') or analisis.get('confianza', 0) < 8:
            continue
        monto = round(calcular_monto_diversificado(historial, capital_disponible) * 0.7, 2)
        if monto < MONTO_MIN:
            continue
        exito, cantidad, precio = ejecutar_compra(par, monto, d5)
        if exito:
            historial.append({
                'par': par, 'precio_compra': precio, 'precio_maximo': precio,
                'cantidad': cantidad, 'monto': monto, 'rsi_entrada': d5['rsi'],
                'confianza': analisis.get('confianza'), 'razon': analisis.get('razon'),
                'score_entrada': obtener_score_par(par), 'onchain_score': sig['score'],
                'fuentes_monitor': señal['n_fuentes'], 'en_perdida_desde': None,
                'estado': 'abierta', 'estrategia': 'monitor',
                'fecha': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            guardar_historial(historial)
            posiciones_abiertas += 1
            pares_en_uso.add(par)
            capital_disponible -= monto
    return posiciones_abiertas, capital_disponible

# ============================================================
# MAIN
# ============================================================

def main():
    global MONITOR_CICLO
    _, tp_actual, sl_actual = obtener_modo_horario()
    print("="*60)
    print(f"  BOT v5 24/7 — {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")
    print("="*60)
    mostrar_resumen()
    print("="*60)
    enviar_reporte_diario()
    with _lock:
        revisar_posiciones(tp_actual, sl_actual)
        revisar_cut_loss()
    historial = cargar_historial()
    posiciones_abiertas = len([p for p in historial if p.get('estado') == 'abierta'])
    if posiciones_abiertas >= MAX_POSICIONES:
        print(f"\nMaximo posiciones ({posiciones_abiertas}/{MAX_POSICIONES})")
        return
    capital_disponible = obtener_capital_disponible()
    print(f"\nCapital: ${capital_disponible:.2f} USDT")
    mejores_pares = obtener_mejores_pares()
    if not mejores_pares:
        return
    pares_en_uso = {p['par'] for p in historial if p.get('estado') == 'abierta'}

    # MONITOR AMPLIO cada 5 ciclos (~7.5 minutos)
    MONITOR_CICLO += 1
    if MONITOR_CICLO >= 5:
        MONITOR_CICLO = 0
        try:
            señales = monitor_mercado.escanear()
            if señales:
                posiciones_abiertas, capital_disponible = procesar_señales_monitor(
                    señales, historial, capital_disponible,
                    pares_en_uso, posiciones_abiertas, tp_actual, sl_actual
                )
        except Exception as e:
            print(f"  Error monitor: {e}")

    # LISTINGS
    if posiciones_abiertas < MAX_POSICIONES:
        try:
            for listing in listing_detector.detectar_nuevos():
                if posiciones_abiertas >= MAX_POSICIONES:
                    break
                par = listing['par']
                if par in pares_en_uso or esta_en_blacklist(par):
                    continue
                sig = {"score": 0.0, "block": False}
                try:
                    sig = get_onchain_signal(par)
                    if sig['block']:
                        continue
                except:
                    pass
                datos = obtener_datos_mercado(par)
                if not datos:
                    continue
                monto = round(calcular_monto_diversificado(historial, capital_disponible) * 0.5, 2)
                if monto < MONTO_MIN:
                    continue
                exito, cantidad, precio = ejecutar_compra(par, monto, datos)
                if exito:
                    historial.append({
                        'par': par, 'precio_compra': precio, 'precio_maximo': precio,
                        'cantidad': cantidad, 'monto': monto, 'rsi_entrada': datos['rsi'],
                        'confianza': 9, 'razon': 'NUEVO LISTING', 'onchain_score': sig['score'],
                        'en_perdida_desde': None, 'estado': 'abierta', 'estrategia': 'listing',
                        'fecha': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })
                    guardar_historial(historial)
                    posiciones_abiertas += 1
                    pares_en_uso.add(par)
                    capital_disponible -= monto
        except Exception as e:
            print(f"  Error listings: {e}")

    # SCALPING — 24/7, confianza mínima 6 siempre
    candidatos = filtrar_candidatos(mejores_pares)
    print(f"\n{len(candidatos)} candidatos scalping\n")
    for c in candidatos:
        if posiciones_abiertas >= MAX_POSICIONES:
            break
        if c['par'] in pares_en_uso:
            continue
        par = c['par']
        print(f"Analizando {par} | 24h:{c['cambio_24h']}% | Score:{c['score']}")
        if es_caida_libre(par, c['cambio_24h']):
            continue
        sig = {"score": 0.0, "action": "NEUTRAL", "block": False, "emoji": "⚪"}
        try:
            sig = get_onchain_signal(par)
            print(f"  OnChain: {sig['action']} ({sig['score']:+.3f})")
            if sig['block']:
                continue
        except Exception as e:
            print(f"  OnChain error: {e}")
        d5 = obtener_datos_mercado(par, '5m', 50)
        d1h = obtener_datos_mercado(par, '1h', 50)
        if not d5 or not d1h:
            continue
        print(f"  5m RSI:{d5['rsi']} MACD:{'▲' if d5['macd']>d5['macd_signal'] else '▼'} | 1h RSI:{d1h['rsi']}")
        confirmado, razon_tf = confirmar_dos_timeframes(par)
        if not confirmado:
            print(f"  Sin confirmacion: {razon_tf}")
            continue
        analisis = analizar_sentimiento_groq(par, d5, d1h, c['cambio_24h'], 'activo', c['score'], sig['score'])
        if not analisis:
            continue
        confianza_minima = 6
        if sig['action'] == 'SLIGHT_SHORT':
            confianza_minima = 7
        if analisis.get('comprar') and analisis.get('confianza', 0) >= confianza_minima:
            if capital_disponible < MONTO_MIN:
                liberado = rebalancear_si_necesario(c, tipo='scalp', confianza=analisis.get('confianza', 0))
                if liberado:
                    capital_disponible = obtener_capital_disponible()
                    time.sleep(1)
                else:
                    print(f"  Sin capital disponible — saltando")
                    continue
            monto = calcular_monto_diversificado(historial, capital_disponible)
            if monto == 0:
                continue
            if sig['action'] == 'SLIGHT_SHORT':
                monto = round(monto * 0.7, 2)
            print(f"  ENTRADA! {analisis['confianza']}/10 | {analisis.get('razon','')} | ${monto}")
            exito, cantidad, precio = ejecutar_compra(par, monto, d5)
            if exito:
                historial.append({
                    'par': par, 'precio_compra': precio, 'precio_maximo': precio,
                    'cantidad': cantidad, 'monto': monto,
                    'rsi_entrada': d5['rsi'], 'rsi_1h_entrada': d1h['rsi'],
                    'confianza': analisis.get('confianza'), 'razon': analisis.get('razon'),
                    'score_entrada': c['score'], 'onchain_score': sig['score'],
                    'onchain_action': sig['action'], 'en_perdida_desde': None,
                    'estado': 'abierta', 'estrategia': 'scalp',
                    'fecha': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
                guardar_historial(historial)
                posiciones_abiertas += 1
                pares_en_uso.add(par)
                capital_disponible -= monto
        else:
            print(f"  Descartado ({analisis.get('confianza','?')}/10)")
        time.sleep(0.5)

    print(f"\n{'='*60}")
    print(f"  Ciclo completado — {datetime.utcnow().strftime('%H:%M:%S')} UTC")

if __name__ == "__main__":
    enviar_telegram(
        "🤖 <b>Bot Binance v5 — 24/7 GLOBAL</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🌍 Sin restricciones horarias\n"
        "🚀 Pump thread: ciclo 15s\n"
        "🔄 Loop principal: ciclo 90s\n"
        "✂️ Cut loss: -0.3% por 5min\n"
        "🔄 Rebalanceo dinámico activo\n"
        "📊 Scalping + Monitor + Listings 24/7\n"
        "🤖 IA: Gemini 2.0 Flash (1M tokens/día)"
    )
    threading.Thread(target=ciclo_pump_agresivo, daemon=True).start()
    print("  [PUMP THREAD] Lanzado 24/7 ✓")
    while True:
        try:
            main()
        except Exception as e:
            print(f"Error main: {e}")
            enviar_telegram(f"⚠️ Error: {e}")
        time.sleep(CICLO_MAIN_SEGUNDOS)
