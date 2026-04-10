# -*- coding: utf-8 -*-

import os
import json
import time
import threading
import math
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
MONTO_MIN = 8.0
MONTO_MAX = 20.0
MONTO_PUMP = 8.0
TAKE_PROFIT = 0.012
TAKE_PROFIT_PUMP = 0.015
STOP_LOSS = 0.008
STOP_LOSS_PUMP = 0.006
TRAILING_STOP = 0.004  # base, se ajusta dinámicamente por tramos
CRASH_THRESHOLD = -8.0
CUT_LOSS_UMBRAL = -0.002
CUT_LOSS_MINUTOS = 2      # 2 minutos en pérdida → corta
CICLO_PUMP_SEGUNDOS = 15
CICLO_MAIN_SEGUNDOS = 90

# Protección de capital
CAPITAL_MINIMO = 5.0         # si baja de $5 → pausa total (sin posiciones abiertas)
CAPITAL_ALERTA = 10.0        # si baja de $10 → alerta Telegram

# Gestión de riesgo dinámica
RACHA_PERDIDAS_REDUCIR = 3   # 3 pérdidas seguidas → reduce montos
RACHA_GANANCIAS_SUBIR = 3    # 3 ganancias seguidas → sube montos

# Posiciones estancadas
HORAS_ESTANCADO = 4          # horas sin movimiento antes de liberar
UMBRAL_ESTANCADO_MAX = 0.5   # máximo +0.5% para considerar estancada
UMBRAL_ESTANCADO_MIN = -1.0  # mínimo -1.0% para considerar estancada
ALERTA_CADA_HORAS = 6        # resumen cada 6hs

HISTORIAL_FILE = "historial_binance.json"
BLACKLIST_FILE = "blacklist.json"
REPORTE_FILE = "ultimo_reporte.json"
RANKING_FILE = "ranking_pares.json"

MONITOR_CICLO = 0
ALERTA_CICLO = 0
_RESUMEN_CICLO = 0
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
    expira = (datetime.now() + timedelta(hours=4)).isoformat()  # 4hs, antes 24hs
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
            resultado = json.loads(texto[i:f+1])
            print(f"  Gemini: comprar={resultado.get('comprar')} confianza={resultado.get('confianza')}/10")
            return resultado
        print(f"  Gemini respuesta inválida: {texto[:100]}")
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

def btc_en_caida():
    """Retorna True si BTC cayó más de 2% en la última hora — pausa entradas."""
    try:
        klines = client_binance.get_klines(symbol='BTCUSDT', interval='1h', limit=2)
        p_anterior = float(klines[0][4])
        p_actual = float(klines[1][4])
        cambio = ((p_actual - p_anterior) / p_anterior) * 100
        if cambio <= -2.0:
            print(f"  [BTC FILTER] BTC cayó {cambio:.2f}% en 1h — pausando entradas")
            return True
        return False
    except:
        return False

def filtrar_candidatos(pares_tickers):
    """Sin distinción horaria. Excluye pares con caída mayor a -5% en 24h."""
    candidatos = []
    for t in pares_tickers:
        cambio = float(t['priceChangePercent'])
        vol = float(t['quoteVolume'])
        par = t['symbol']
        if -8 <= cambio <= 5.0 and vol > 500000 and not esta_en_blacklist(par):
            candidatos.append({'par': par, 'cambio_24h': cambio, 'volumen': vol, 'score': obtener_score_par(par)})
    candidatos.sort(key=lambda x: (x['score'], -x['cambio_24h']), reverse=True)
    return candidatos[:20]

def trailing_dinamico(ganancia_pct, minutos_en_posicion=0):
    """Trailing stop dinámico por tramos de ganancia y tiempo."""
    # Primeros 10 minutos — trailing ajustado para salir rápido si no sube
    if minutos_en_posicion < 10:
        if ganancia_pct >= 1.0:
            return 0.008  # ganó 1%+ en menos de 10min → trailing 0.8%
        return 0.004      # menos de 1% → trailing 0.4% ajustado

    # Después de 10 minutos — si está subiendo fuerte, dejar correr
    if ganancia_pct >= 5.0:
        return 0.025   # ganó +5% → trailing 2.5%, dejar correr el pump
    elif ganancia_pct >= 3.0:
        return 0.020   # ganó +3% → trailing 2%
    elif ganancia_pct >= 1.0:
        return 0.012   # ganó +1% → trailing 1.2%
    else:
        return 0.006   # menos de 1% → trailing 0.6%

def verificar_notional(par, cantidad, precio_actual):
    """Verifica si la venta cumple el notional mínimo de Binance."""
    try:
        info = client_binance.get_symbol_info(par)
        for f in info['filters']:
            if f['filterType'] == 'NOTIONAL':
                min_notional = float(f.get('minNotional', 0))
                valor = cantidad * precio_actual
                return valor >= min_notional, valor, min_notional
        return True, cantidad * precio_actual, 0
    except:
        return True, cantidad * precio_actual, 0

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
        asset = par.replace('USDT', '')
        info = client_binance.get_symbol_info(par)
        step = next(f['stepSize'] for f in info['filters'] if f['filterType'] == 'LOT_SIZE')
        dec = len(step.rstrip('0').split('.')[-1]) if '.' in step else 0

        # Siempre leer balance real de Binance
        try:
            balances = client_binance.get_account()['balances']
            b = next((x for x in balances if x['asset'] == asset), None)
            balance_real = float(b['free']) if b else 0  # solo free, no locked
        except:
            balance_real = 0

        print(f"  [VENTA DEBUG] {par} tipo:{tipo} historial:{cantidad} balance_real:{balance_real:.4f} dec:{dec}")

        # Si no hay balance real, marcar como cerrada
        if balance_real <= 0:
            print(f"  [VENTA] {par} balance real 0 — marcando cerrada")
            return 'sin_balance'

        # Usar floor para no exceder el balance disponible
        factor = 10 ** dec
        cantidad_venta = math.floor(balance_real * factor) / factor
        if cantidad_venta <= 0:
            print(f"  [VENTA] {par} cantidad redondeada 0")
            return 'sin_balance'

        # Para ganancias verificar notional, para perdidas vender sin importar
        if tipo not in ('perdida', 'trailing'):
            cumple, valor, min_notional = verificar_notional(par, cantidad_venta, precio_actual)
            if not cumple:
                print(f"  [NOTIONAL] {par} ${valor:.2f} < ${min_notional:.2f} — esperando")
                return False

        orden = client_binance.order_market_sell(symbol=par, quantity=cantidad_venta)
        emojis = {'ganancia': '✅', 'pump': '🚀', 'trailing': '📉', 'perdida': '🔴'}
        nombres = {'ganancia': 'TAKE PROFIT', 'pump': 'PUMP PROFIT', 'trailing': 'TRAILING STOP', 'perdida': 'STOP LOSS'}
        enviar_telegram(f"{emojis.get(tipo,'✅')} <b>{nombres.get(tipo,'VENTA')}</b> {par}\n{'Ganancia' if pct>0 else 'Perdida'}: {pct:+.3f}%\n💰 ${precio_actual}")
        actualizar_blacklist_post_venta(par, pct)
        return True
    except Exception as e:
        print(f"  Error vendiendo: {e}")
        return False

# ============================================================
# POSICIONES ESTANCADAS
# ============================================================

def revisar_posiciones_estancadas():
    """Vende posiciones que llevan más de X horas sin moverse cerca del 0%."""
    with _lock:
        historial = cargar_historial()
        cambios = False
        ahora = datetime.now()
        for i, pos in enumerate(historial):
            if pos.get('estado') != 'abierta':
                continue
            try:
                fecha_compra = datetime.strptime(pos['fecha'], "%Y-%m-%d %H:%M:%S")
                horas = (ahora - fecha_compra).total_seconds() / 3600
            except:
                continue
            if horas < HORAS_ESTANCADO:
                continue
            precio_actual = obtener_precio(pos['par'])
            if not precio_actual:
                continue
            pct = ((precio_actual - float(pos['precio_compra'])) / float(pos['precio_compra'])) * 100
            if UMBRAL_ESTANCADO_MIN <= pct <= UMBRAL_ESTANCADO_MAX:
                print(f"  [ESTANCADO] {pos['par']} {pct:+.2f}% — {horas:.1f}hs sin moverse → vendiendo")
                if ejecutar_venta(pos['par'], pos.get('cantidad', 0), precio_actual, round(pct, 3), 'ganancia' if pct >= 0 else 'perdida'):
                    historial[i].update({
                        'estado': 'cerrada_ganancia' if pct >= 0 else 'cerrada_perdida',
                        'precio_venta': precio_actual,
                        'ganancia_pct': round(pct, 3),
                        'razon_cierre': f'estancado_{horas:.0f}hs',
                        'fecha_cierre': ahora.strftime("%Y-%m-%d %H:%M:%S")
                    })
                    cambios = True
                    enviar_telegram(
                        f"⏱️ <b>POSICIÓN LIBERADA</b> {pos['par']}\n"
                        f"📊 {pct:+.2f}% en {horas:.0f}hs sin movimiento\n"
                        f"💡 Capital liberado para nueva oportunidad"
                    )
        if cambios:
            guardar_historial(historial)

# ============================================================
# CUT LOSS AGRESIVO
# ============================================================

def revisar_cut_loss():
    with _lock:
        historial = cargar_historial()
        cambios = False
        ahora = datetime.now()
        for i, pos in enumerate(historial):
            if pos.get('estado') != 'abierta':
                continue
            precio_actual = obtener_precio(pos['par'])
            if not precio_actual:
                continue
            cambio = (precio_actual - float(pos['precio_compra'])) / float(pos['precio_compra'])
            pct = round(cambio * 100, 3)

            # Corte directo si lleva más de 2 horas en pérdida mayor a -3%
            try:
                fecha_compra = datetime.strptime(pos['fecha'], "%Y-%m-%d %H:%M:%S")
                horas = (ahora - fecha_compra).total_seconds() / 3600
                if cambio <= -0.02 and horas >= 1:
                    print(f"  [CUT LOSS] {pos['par']} {pct}% — {horas:.1f}hs en pérdida → cortando")
                    if ejecutar_venta(pos['par'], pos.get('cantidad', 0), precio_actual, pct, 'perdida'):
                        historial[i].update({
                            'estado': 'cerrada_perdida', 'precio_venta': precio_actual,
                            'ganancia_pct': pct, 'razon_cierre': f'cut_loss_{horas:.0f}hs',
                            'fecha_cierre': ahora.strftime("%Y-%m-%d %H:%M:%S")
                        })
                        cambios = True
                        enviar_telegram(
                            f"✂️ <b>CUT LOSS</b> {pos['par']}\n"
                            f"📉 {pct:+.3f}% por {horas:.1f}hs\n"
                            f"💡 Capital liberado"
                        )
                    continue
            except:
                pass

            if cambio >= 0:
                if historial[i].get('en_perdida_desde'):
                    historial[i]['en_perdida_desde'] = None
                    cambios = True
                continue
            if cambio <= CUT_LOSS_UMBRAL:
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
        # Ampliar a todos los pares USDT con volumen mínimo $300k (antes $1M y solo top 100)
        usdt = [t for t in tickers
                if t['symbol'].endswith('USDT')
                and float(t['quoteVolume']) > 1000000   # volumen mínimo $1M
                and float(t['lastPrice']) > 0.001        # precio mínimo $0.001
                and float(t['lastPrice']) < 500          # precio máximo $500
                and float(t['priceChangePercent']) > -15 # no en caída libre
                and not esta_en_blacklist(t['symbol'])]

        # Ordenar por % de cambio en 24h descendente — los que más se mueven primero
        usdt.sort(key=lambda x: float(x['priceChangePercent']), reverse=True)

        pumps = []
        for t in usdt[:200]:  # escanear top 200 (antes 100)
            par = t['symbol']
            try:
                klines = client_binance.get_klines(symbol=par, interval='1m', limit=15)
                precios = [float(k[4]) for k in klines]
                vols = [float(k[5]) for k in klines]

                # Cambios en distintos timeframes
                c2m  = ((precios[-1] - precios[-3])  / precios[-3])  * 100
                c5m  = ((precios[-1] - precios[-6])  / precios[-6])  * 100
                c15m = ((precios[-1] - precios[-15]) / precios[-15]) * 100

                vol_prom = np.mean(vols[:-3]) if len(vols) > 3 else 1
                ratio_vol = np.mean(vols[-3:]) / vol_prom if vol_prom > 0 else 1
                rsi = calcular_rsi(precios)

                # Detectar momentum temprano:
                # - Subida de 0.2% en 2 min (entrada temprana, antes 0.4%)
                # - O subida de 1% en 5 min
                # - O subida de 3% en 15 min (tendencia sostenida)
                # - Volumen aumentando (ratio > 1.5)
                # - RSI no sobrecomprado (< 80)
                es_pump = (
                    (c2m >= 0.2 and ratio_vol >= 2.0) or
                    (c5m >= 1.0 and ratio_vol >= 1.5) or
                    (c15m >= 3.0 and ratio_vol >= 1.3)
                ) and rsi < 80

                if es_pump:
                    pumps.append({
                        'par': par,
                        'cambio_2m': round(c2m, 3),
                        'cambio_5m': round(c5m, 3),
                        'cambio_15m': round(c15m, 3),
                        'ratio_vol': round(ratio_vol, 2),
                        'rsi': round(rsi, 1),
                        'score': obtener_score_par(par)
                    })
            except:
                continue
            time.sleep(0.03)

        # Ordenar por momentum — combina ratio de volumen, cambio y score
        pumps.sort(key=lambda x: x['ratio_vol'] * (1 + x['cambio_5m'] / 10) * (1 + x['score'] / 100), reverse=True)
        return pumps[:5]  # top 5 candidatos (antes 3)
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
    """Mantiene la posición mientras sube, vende solo cuando baja desde el máximo."""
    precio_maximo = precio_compra
    inicio = time.time()
    timeout = 3600  # máximo 1 hora (antes 5 minutos)
    print(f"  [WATCH] {par} ${precio_compra:.6f} — trailing inteligente activo")

    while time.time() - inicio < timeout:
        time.sleep(5)
        precio_actual = obtener_precio(par)
        if not precio_actual:
            continue

        # Actualizar máximo
        if precio_actual > precio_maximo:
            precio_maximo = precio_actual

        cambio = (precio_actual - precio_compra) / precio_compra
        pct = round(cambio * 100, 3)
        caida_desde_max = (precio_maximo - precio_actual) / precio_maximo if precio_maximo > 0 else 0
        minutos_posicion = (time.time() - inicio) / 60
        trail = trailing_dinamico(pct, minutos_posicion)

        # Stop loss — si cae desde la entrada directamente
        if cambio <= -STOP_LOSS_PUMP:
            print(f"  [WATCH] SL {par} {pct}%")
            with _lock:
                ejecutar_venta(par, cantidad, precio_actual, pct, 'perdida')
            _cerrar_posicion_historial(par, precio_actual, pct, 'cerrada_perdida')
            return

        # Trailing inteligente — vende cuando baja X% desde el máximo
        # Solo activa si ya ganó al menos 0.1% para no vender por ruido
        if pct >= 0.1 and caida_desde_max >= trail:
            print(f"  [WATCH] TRAILING {par} max:+{((precio_maximo-precio_compra)/precio_compra)*100:.2f}% actual:+{pct}%")
            with _lock:
                ejecutar_venta(par, cantidad, precio_actual, pct, 'trailing' if pct > 0 else 'perdida')
            _cerrar_posicion_historial(par, precio_actual, pct, 'cerrada_ganancia' if pct > 0 else 'cerrada_perdida')
            return

    # Timeout — vende lo que haya
    precio_actual = obtener_precio(par) or precio_compra
    cambio = (precio_actual - precio_compra) / precio_compra
    pct = round(cambio * 100, 3)
    print(f"  [WATCH] TIMEOUT 1h {par} {pct}%")
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
            # Log detallado del mejor candidato
            if pumps:
                mejor = pumps[0]
                enviar_telegram(
                    f"🔍 <b>Pump detectado</b>\n"
                    f"Par: {mejor['par']}\n"
                    f"2m:{mejor.get('cambio_2m',0):+.2f}% 5m:{mejor['cambio_5m']:+.2f}% Vol:{mejor['ratio_vol']}x RSI:{mejor['rsi']}\n"
                    f"Capital: ${capital:.2f} | Min: ${MONTO_MIN}"
                )
            for idx, p in enumerate(pumps):
                par = p['par']
                # Leer estado sin lock para evitar deadlock
                historial = cargar_historial()
                pares_en_uso = {pos['par'] for pos in historial if pos.get('estado') == 'abierta'}
                pumps_ab = len([pos for pos in historial if pos.get('estado') == 'abierta' and pos.get('estrategia') == 'pump'])
                total_ab = len([pos for pos in historial if pos.get('estado') == 'abierta'])
                capital = obtener_capital_disponible()

                if par in pares_en_uso or esta_en_blacklist(par):
                    motivo = "en uso" if par in pares_en_uso else "blacklist"
                    print(f"  [PUMP] {par} saltado — {motivo}")
                    if idx == 0:  # si es el mejor candidato, avisar
                        enviar_telegram(f"⚠️ Mejor pump {par} saltado ({motivo}) — usando siguiente")
                    continue
                if pumps_ab >= MAX_POSICIONES_PUMP or total_ab >= MAX_POSICIONES:
                    print(f"  [PUMP] max posiciones — pumps:{pumps_ab} total:{total_ab}")
                    break
                if capital < MONTO_MIN:
                    print(f"  [PUMP] sin capital ${capital:.2f}")
                    continue
                monto = min(MONTO_PUMP, capital * 0.9)
                if monto < MONTO_MIN:
                    continue
                print(f"  [PUMP] ENTRANDO {par} +{p['cambio_5m']}% Vol:{p['ratio_vol']}x RSI:{p['rsi']} ${monto}")
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
                        f"💰 ${monto}"
                    )
                    threading.Thread(target=vigilar_posicion_pump, args=(par, precio, cantidad, monto), daemon=True).start()
                    break  # una compra por ciclo pump
        except Exception as e:
            print(f"  [PUMP THREAD] Error: {e}")
        time.sleep(CICLO_PUMP_SEGUNDOS)

# ============================================================
# REVISAR POSICIONES SCALP
# ============================================================

def obtener_balance_asset(asset):
    """Obtiene el balance real de un asset en Binance."""
    try:
        balances = client_binance.get_account()['balances']
        for b in balances:
            if b['asset'] == asset:
                return float(b['free'])  # solo free
        return 0
    except:
        return 0

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

        # Leer balance real de Binance en lugar de confiar en el historial
        asset = pos['par'].replace('USDT', '')
        cantidad_real = obtener_balance_asset(asset)
        if cantidad_real > 0:
            historial[i]['cantidad'] = cantidad_real  # actualizar historial

        cambio = (precio_actual - precio_compra) / precio_compra
        pct = round(cambio * 100, 3)
        precio_maximo = float(pos.get('precio_maximo', precio_compra))
        if precio_actual > precio_maximo:
            precio_maximo = precio_actual
            historial[i]['precio_maximo'] = precio_maximo
        caida = (precio_maximo - precio_actual) / precio_maximo if precio_maximo > 0 else 0
        try:
            fecha_compra = datetime.strptime(pos['fecha'], "%Y-%m-%d %H:%M:%S")
            minutos = (datetime.now() - fecha_compra).total_seconds() / 60
        except:
            minutos = 0
        trail = trailing_dinamico(pct, minutos)
        cantidad_usar = cantidad_real if cantidad_real > 0 else pos.get('cantidad', 0)
        print(f"  {pos['par']} [{pos.get('estrategia','scalp')}] | {pct:+.3f}% | trail:{trail*100:.1f}% | {minutos:.0f}min | qty:{cantidad_usar}")

        # Trailing inteligente — igual que pumps
        if pct >= 0.1 and caida >= trail:
            if ejecutar_venta(pos['par'], cantidad_usar, precio_actual, pct, 'trailing'):
                historial[i].update({'estado': 'cerrada_ganancia', 'precio_venta': precio_actual,
                                     'ganancia_pct': pct, 'fecha_cierre': datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                cerradas += 1
        elif cambio <= -sl_actual:
            if ejecutar_venta(pos['par'], cantidad_usar, precio_actual, pct, 'perdida'):
                historial[i].update({'estado': 'cerrada_perdida', 'precio_venta': precio_actual,
                                     'ganancia_pct': pct, 'fecha_cierre': datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                cerradas += 1
        else:
            print(f"  Manteniendo ({caida*100:.2f}% desde max | max:{((precio_maximo-precio_compra)/precio_compra)*100:.2f}%)")
    guardar_historial(historial)
    return cerradas

def mostrar_resumen():
    historial = cargar_historial()
    capital = obtener_capital_disponible()
    g = [p for p in historial if p.get('estado') == 'cerrada_ganancia']
    p = [p for p in historial if p.get('estado') == 'cerrada_perdida']
    ab = [p for p in historial if p.get('estado') == 'abierta']
    pumps_ab = len([x for x in ab if x.get('estrategia') == 'pump'])
    neto = sum(x.get('ganancia_pct', 0) for x in g+p)
    resumen = f"  G:{len(g)} P:{len(p)} | Abiertas:{len(ab)}(pump:{pumps_ab}) | Neto:{neto:+.2f}% | BL:{len(cargar_blacklist())} | Capital:${capital:.2f}"
    print(resumen)
    # Mandar estado a Telegram cada 10 ciclos (~15 min)
    global _RESUMEN_CICLO
    _RESUMEN_CICLO = getattr(_RESUMEN_CICLO if '_RESUMEN_CICLO' in dir() else type('', (), {'_RESUMEN_CICLO': 0})(), '_RESUMEN_CICLO', 0) + 1
    pass

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
# STOP LOSS DE CAPITAL TOTAL
# ============================================================

def verificar_capital_minimo():
    """Pausa el bot solo si el capital libre es bajo Y no hay posiciones abiertas."""
    capital = obtener_capital_disponible()
    historial = cargar_historial()
    abiertas = [p for p in historial if p.get('estado') == 'abierta']

    # Si hay posiciones abiertas, el capital está invertido — no es una pérdida
    if abiertas:
        if capital <= CAPITAL_ALERTA:
            print(f"  [CAPITAL] ${capital:.2f} libre — capital invertido en {len(abiertas)} posiciones, OK")
        return True

    # Sin posiciones abiertas y capital bajo → sí es problema real
    if capital <= CAPITAL_MINIMO:
        msg = (
            f"🚨 <b>ALERTA CAPITAL CRÍTICO</b>\n"
            f"💰 Capital disponible: ${capital:.2f}\n"
            f"⛔ Bot pausado — mínimo es ${CAPITAL_MINIMO}\n"
            f"💡 Depositá fondos para reanudar"
        )
        print(f"  [CAPITAL] ${capital:.2f} sin posiciones abiertas — PAUSANDO")
        enviar_telegram(msg)
        return False
    if capital <= CAPITAL_ALERTA:
        print(f"  [CAPITAL] ${capital:.2f} — alerta, cerca del mínimo")
        enviar_telegram(
            f"⚠️ <b>Capital bajo</b>\n"
            f"💰 ${capital:.2f} disponible (mínimo: ${CAPITAL_MINIMO})"
        )
    return True

# ============================================================
# GESTIÓN DE RIESGO DINÁMICA
# ============================================================

def analizar_racha(historial):
    """Analiza las últimas operaciones y retorna factor de riesgo."""
    cerradas = [p for p in historial if p.get('estado') in ['cerrada_ganancia', 'cerrada_perdida']]
    if len(cerradas) < 3:
        return 1.0, "neutral"
    ultimas = cerradas[-RACHA_PERDIDAS_REDUCIR:]
    todas_perdidas = all(p.get('estado') == 'cerrada_perdida' for p in ultimas)
    todas_ganancias = all(p.get('estado') == 'cerrada_ganancia' for p in ultimas)
    if todas_perdidas:
        print(f"  [RACHA] {RACHA_PERDIDAS_REDUCIR} pérdidas seguidas → reduciendo montos 30%")
        enviar_telegram(
            f"📉 <b>Racha negativa</b>\n"
            f"❌ {RACHA_PERDIDAS_REDUCIR} pérdidas seguidas\n"
            f"🔽 Montos reducidos al 70%"
        )
        return 0.7, "negativa"
    if todas_ganancias:
        print(f"  [RACHA] {RACHA_GANANCIAS_SUBIR} ganancias seguidas → subiendo montos 20%")
        return 1.2, "positiva"
    return 1.0, "neutral"

# ============================================================
# DETECTOR DE TENDENCIA DE MERCADO
# ============================================================

def analizar_tendencia_mercado():
    """Analiza BTC + ETH para determinar tendencia general: alcista/bajista/lateral."""
    try:
        scores = []
        for par in ['BTCUSDT', 'ETHUSDT']:
            klines = client_binance.get_klines(symbol=par, interval='1h', limit=24)
            precios = [float(k[4]) for k in klines]
            c1h  = ((precios[-1] - precios[-2])  / precios[-2])  * 100
            c4h  = ((precios[-1] - precios[-5])  / precios[-5])  * 100
            c24h = ((precios[-1] - precios[-24]) / precios[-24]) * 100
            score = 0
            if c1h  > 0.5:  score += 1
            if c4h  > 1.0:  score += 1
            if c24h > 2.0:  score += 1
            if c1h  < -0.5: score -= 1
            if c4h  < -1.0: score -= 1
            if c24h < -2.0: score -= 1
            scores.append(score)

        total = sum(scores)
        if total >= 3:
            tendencia = 'alcista'
        elif total <= -3:
            tendencia = 'bajista'
        else:
            tendencia = 'lateral'

        print(f"  [TENDENCIA] Mercado {tendencia} (score:{total})")
        return tendencia, total
    except Exception as e:
        print(f"  [TENDENCIA] Error: {e}")
        return 'lateral', 0

# ============================================================
# ALERTAS PERIÓDICAS
# ============================================================

def enviar_alerta_periodica():
    """Resumen del estado del bot cada 6hs."""
    global ALERTA_CICLO
    ciclos_por_hora = 3600 / CICLO_MAIN_SEGUNDOS  # ciclos en 1 hora
    ciclos_necesarios = int(ALERTA_CADA_HORAS * ciclos_por_hora)
    ALERTA_CICLO += 1
    if ALERTA_CICLO < ciclos_necesarios:
        return
    ALERTA_CICLO = 0
    try:
        historial = cargar_historial()
        capital = obtener_capital_disponible()
        abiertas = [p for p in historial if p.get('estado') == 'abierta']
        cerradas_g = [p for p in historial if p.get('estado') == 'cerrada_ganancia']
        cerradas_p = [p for p in historial if p.get('estado') == 'cerrada_perdida']
        neto = sum(p.get('ganancia_pct', 0) for p in cerradas_g + cerradas_p)
        tendencia, _ = analizar_tendencia_mercado()
        emojis_tend = {'alcista': '📈', 'bajista': '📉', 'lateral': '➡️'}

        pos_str = ""
        for p in abiertas:
            precio_actual = obtener_precio(p['par'])
            if precio_actual:
                pct = ((precio_actual - float(p['precio_compra'])) / float(p['precio_compra'])) * 100
                pos_str += f"\n  • {p['par']} {pct:+.2f}%"

        enviar_telegram(
            f"🕐 <b>Resumen {ALERTA_CADA_HORAS}hs</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Capital: ${capital:.2f} USDT\n"
            f"📊 Ops: ✅{len(cerradas_g)} 🔴{len(cerradas_p)} | Neto: {neto:+.2f}%\n"
            f"{emojis_tend[tendencia]} Mercado: {tendencia}\n"
            f"📂 Abiertas ({len(abiertas)}):{pos_str if pos_str else ' ninguna'}\n"
            f"🚫 Blacklist: {len(cargar_blacklist())}"
        )
    except Exception as e:
        print(f"  Error alerta periódica: {e}")

# ============================================================
# SINCRONIZACIÓN HISTORIAL CON BALANCES REALES
# ============================================================

def sincronizar_historial_con_binance():
    """Al arrancar, compara el historial con los balances reales de Binance.
    Si hay tokens en Binance que no están en el historial, los agrega."""
    try:
        historial = cargar_historial()
        pares_en_historial = {p['par'] for p in historial if p.get('estado') == 'abierta'}

        account = client_binance.get_account()
        balances = account['balances']

        nuevas = []
        for b in balances:
            asset = b['asset']
            libre = float(b['free'])
            if asset == 'USDT' or libre <= 0:
                continue

            par = f"{asset}USDT"
            if par in pares_en_historial:
                continue

            # Verificar que el par existe en Binance
            precio_actual = obtener_precio(par)
            if not precio_actual:
                continue

            valor = libre * precio_actual
            if valor < 1.0:  # ignorar dust (menos de $1)
                continue

            # Agregar al historial con precio actual como precio de compra
            # (no sabemos el precio real de compra)
            print(f"  [SYNC] {par} encontrado en Binance (${valor:.2f}) — agregando al historial")
            nuevas.append({
                'par': par,
                'precio_compra': precio_actual,
                'precio_maximo': precio_actual,
                'cantidad': libre,
                'monto': valor,
                'rsi_entrada': 50,
                'confianza': 5,
                'razon': 'recuperado_al_arrancar',
                'onchain_score': 0.0,
                'en_perdida_desde': None,
                'estado': 'abierta',
                'estrategia': 'scalp',
                'fecha': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })

        if nuevas:
            historial.extend(nuevas)
            guardar_historial(historial)
            pares = [p['par'] for p in nuevas]
            enviar_telegram(
                f"🔄 <b>Sincronización</b>\n"
                f"Encontré {len(nuevas)} posición(es) no registrada(s):\n"
                f"{', '.join(pares)}\n"
                f"Agregadas al historial para vigilancia."
            )
            print(f"  [SYNC] {len(nuevas)} posiciones sincronizadas")
        else:
            print(f"  [SYNC] Historial sincronizado — sin diferencias")

    except Exception as e:
        print(f"  [SYNC] Error: {e}")

# ============================================================
# MAIN
# ============================================================

def main():
    global MONITOR_CICLO
    _, tp_actual, sl_actual = obtener_modo_horario()
    print("="*60)
    print(f"  BOT v5 24/7 — {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC")
    print("="*60)
    historial_tmp = cargar_historial()
    capital_tmp = obtener_capital_disponible()
    ab_tmp = [p for p in historial_tmp if p.get('estado') == 'abierta']
    print(f"  Capital:${capital_tmp:.2f} | Abiertas:{len(ab_tmp)}")
    # Solo loguear en consola, no Telegram cada ciclo
    print(f"  💰 Capital: ${capital_tmp:.2f} | Abiertas: {len(ab_tmp)}")
    print("="*60)
    mostrar_resumen()
    print("="*60)
    enviar_reporte_diario()
    enviar_alerta_periodica()

    # Stop loss de capital total
    if not verificar_capital_minimo():
        return

    with _lock:
        revisar_posiciones(tp_actual, sl_actual)
        revisar_cut_loss()
        revisar_posiciones_estancadas()
    historial = cargar_historial()
    posiciones_abiertas = len([p for p in historial if p.get('estado') == 'abierta'])
    if posiciones_abiertas >= MAX_POSICIONES:
        print(f"\nMaximo posiciones ({posiciones_abiertas}/{MAX_POSICIONES})")
        return
    capital_disponible = obtener_capital_disponible()
    print(f"\nCapital: ${capital_disponible:.2f} USDT")

    # Tendencia de mercado
    tendencia, score_tend = analizar_tendencia_mercado()

    # Gestión de riesgo dinámica
    factor_riesgo, racha = analizar_racha(historial)
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

    # SCALPING — 24/7, confianza mínima 7
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
            monto = round(monto * factor_riesgo, 2)  # ajuste por racha
            if monto < MONTO_MIN:
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

# ============================================================
# DASHBOARD WEB — corre en el mismo proceso como thread
# ============================================================

def iniciar_dashboard():
    try:
        from http.server import HTTPServer, BaseHTTPRequestHandler

        def generar_html():
            historial = cargar_historial()
            blacklist = cargar_blacklist()
            ranking = cargar_ranking()
            abiertas = [p for p in historial if p.get('estado') == 'abierta']
            ganancias = [p for p in historial if p.get('estado') == 'cerrada_ganancia']
            perdidas = [p for p in historial if p.get('estado') == 'cerrada_perdida']
            neto = sum(p.get('ganancia_pct', 0) for p in ganancias + perdidas)
            capital = obtener_capital_disponible()

            posiciones_html = ""
            for pos in abiertas:
                precio_actual = obtener_precio(pos['par']) or 0
                precio_compra = float(pos.get('precio_compra', 0))
                cambio = ((precio_actual - precio_compra) / precio_compra * 100) if precio_compra > 0 else 0
                color = "#00ff88" if cambio >= 0 else "#ff4444"
                posiciones_html += f"<tr><td>{pos['par']}</td><td>{pos.get('estrategia','scalp').upper()}</td><td>${precio_compra:.4f}</td><td>${precio_actual:.4f}</td><td style='color:{color}'>{cambio:+.3f}%</td><td>${pos.get('monto',0)}</td><td>{pos.get('fecha','')[:16]}</td></tr>"

            historial_html = ""
            for op in reversed(historial[-20:]):
                if op.get('estado') in ['cerrada_ganancia', 'cerrada_perdida']:
                    pct = op.get('ganancia_pct', 0)
                    color = "#00ff88" if pct > 0 else "#ff4444"
                    emoji = "✅" if pct > 0 else "🔴"
                    historial_html += f"<tr><td>{emoji} {op['par']}</td><td>{op.get('estrategia','scalp').upper()}</td><td style='color:{color}'>{pct:+.3f}%</td><td>{op.get('fecha','')[:16]}</td><td>{op.get('razon','')[:40]}</td></tr>"

            ranking_html = ""
            top = sorted(ranking.items(), key=lambda x: x[1].get('score', 50), reverse=True)[:10]
            for par, data in top:
                score = data.get('score', 50)
                color = "#00ff88" if score > 60 else "#ffaa00" if score > 40 else "#ff4444"
                ranking_html += f"<tr><td>{par}</td><td style='color:{color}'>{score}/100</td><td>{data.get('ops',0)}</td><td>{data.get('ganancias',0)}</td><td>{data.get('perdidas',0)}</td><td>{data.get('pct_total',0):+.2f}%</td></tr>"

            blacklist_html = "".join([f"<span style='background:#ff4444;padding:4px 8px;border-radius:4px;margin:4px'>{par}</span>" for par, data in blacklist.items() if 'expira' in data]) or "<span style='color:#555'>Sin pares baneados</span>"

            neto_color = 'green' if neto >= 0 else 'red'
            return f"""<!DOCTYPE html><html><head><title>Bot Binance Dashboard</title><meta charset="utf-8"><meta http-equiv="refresh" content="30"><style>*{{margin:0;padding:0;box-sizing:border-box}}body{{background:#0a0a0a;color:#fff;font-family:'Courier New',monospace;padding:20px}}h1{{color:#f0b90b;text-align:center;margin-bottom:20px;font-size:24px}}h2{{color:#f0b90b;margin:20px 0 10px;font-size:16px}}.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:15px;margin-bottom:20px}}.card{{background:#111;border:1px solid #333;border-radius:8px;padding:15px;text-align:center}}.card .value{{font-size:24px;font-weight:bold;margin-top:5px}}.card .label{{color:#888;font-size:12px}}.green{{color:#00ff88}}.red{{color:#ff4444}}.yellow{{color:#f0b90b}}table{{width:100%;border-collapse:collapse;background:#111;border-radius:8px;overflow:hidden}}th{{background:#222;padding:10px;text-align:left;font-size:12px;color:#888}}td{{padding:8px 10px;border-bottom:1px solid #1a1a1a;font-size:12px}}tr:hover{{background:#1a1a1a}}.section{{margin-bottom:30px}}.update{{color:#555;text-align:center;margin-top:20px;font-size:11px}}</style></head><body>
            <h1>🤖 BOT BINANCE DASHBOARD</h1>
            <div class="stats">
                <div class="card"><div class="label">💰 Capital USDT</div><div class="value yellow">${capital:.2f}</div></div>
                <div class="card"><div class="label">📈 Neto Total</div><div class="value {neto_color}">{neto:+.2f}%</div></div>
                <div class="card"><div class="label">✅ Ganancias</div><div class="value green">{len(ganancias)} ops</div></div>
                <div class="card"><div class="label">🔴 Pérdidas</div><div class="value red">{len(perdidas)} ops</div></div>
            </div>
            <div class="section"><h2>📊 POSICIONES ABIERTAS ({len(abiertas)})</h2><table><tr><th>Par</th><th>Estrategia</th><th>Compra</th><th>Actual</th><th>%</th><th>Monto</th><th>Fecha</th></tr>{posiciones_html or "<tr><td colspan='7' style='text-align:center;color:#555'>Sin posiciones</td></tr>"}</table></div>
            <div class="section"><h2>🏆 RANKING DE PARES</h2><table><tr><th>Par</th><th>Score</th><th>Ops</th><th>Ganancias</th><th>Pérdidas</th><th>Total %</th></tr>{ranking_html or "<tr><td colspan='6' style='text-align:center;color:#555'>Sin datos</td></tr>"}</table></div>
            <div class="section"><h2>🚫 BLACKLIST ({len(blacklist)})</h2><div style="padding:10px;background:#111;border-radius:8px">{blacklist_html}</div></div>
            <div class="section"><h2>📋 ÚLTIMAS 20 OPERACIONES</h2><table><tr><th>Par</th><th>Estrategia</th><th>Resultado</th><th>Fecha</th><th>Razón</th></tr>{historial_html or "<tr><td colspan='5' style='text-align:center;color:#555'>Sin operaciones</td></tr>"}</table></div>
            <div class="update">Auto-refresh 30s | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
            </body></html>"""

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                try:
                    self.send_response(200)
                    self.send_header('Content-type', 'text/html; charset=utf-8')
                    self.end_headers()
                    self.wfile.write(generar_html().encode('utf-8'))
                except Exception as e:
                    print(f"Dashboard error: {e}")
            def log_message(self, format, *args):
                pass

        port = int(os.environ.get('PORT', 8080))
        server = HTTPServer(('0.0.0.0', port), Handler)
        print(f"  [DASHBOARD] Corriendo en puerto {port} ✓")
        server.serve_forever()
    except Exception as e:
        print(f"  [DASHBOARD] Error: {e}")

if __name__ == "__main__":
    enviar_telegram(
        "🤖 <b>Bot Binance v5 — 24/7 GLOBAL</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🌍 Sin restricciones horarias\n"
        "🚀 Pump thread: ciclo 15s\n"
        "🔄 Loop principal: ciclo 90s\n"
        "✂️ Cut loss: -0.2% por 2min\n"
        "🔄 Rebalanceo dinámico activo\n"
        "📊 Scalping + Monitor + Listings 24/7\n"
        "🤖 IA: Gemini 2.0 Flash (1M tokens/día)"
    )
    threading.Thread(target=iniciar_dashboard, daemon=True).start()
    threading.Thread(target=ciclo_pump_agresivo, daemon=True).start()
    print("  [PUMP THREAD] Lanzado 24/7 ✓")
    sincronizar_historial_con_binance()  # sincronizar al arrancar
    while True:
        try:
            main()
        except Exception as e:
            print(f"Error main: {e}")
            enviar_telegram(f"⚠️ Error: {e}")
        time.sleep(CICLO_MAIN_SEGUNDOS)
