import os
import json
import time
import numpy as np
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from binance.client import Client
from groq import Groq

# ← NUEVO: importar el módulo on-chain
from onchain_sentiment import get_onchain_signal, format_signal_telegram

load_dotenv()

BINANCE_API_KEY = os.getenv('BINANCE_API_KEY')
BINANCE_SECRET_KEY = os.getenv('BINANCE_SECRET_KEY')
GROQ_API_KEY = os.getenv('GROQ_API_KEY')

TELEGRAM_TOKEN = "8513198629:AAHmlayu6y_Z2e2SUCkvKkLIEhj6kstxYT4"
TELEGRAM_CHAT_ID = "1576867878"

CAPITAL_TOTAL = 30.0
MAX_POSICIONES = 3
MONTO_BASE = CAPITAL_TOTAL / MAX_POSICIONES
MONTO_MIN = 5.0
MONTO_MAX = 20.0
TAKE_PROFIT = 0.012
TAKE_PROFIT_PUMP = 0.025
STOP_LOSS = 0.008
TRAILING_STOP = 0.006
CRASH_THRESHOLD = -8.0
HISTORIAL_FILE = "historial_binance.json"
BLACKLIST_FILE = "blacklist.json"
REPORTE_FILE = "ultimo_reporte.json"
RANKING_FILE = "ranking_pares.json"

client_binance = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)
client_groq = Groq(api_key=GROQ_API_KEY)

# ============================================================
# MODO HORARIO
# ============================================================
def obtener_modo_horario():
    hora = datetime.now().hour
    if 8 <= hora < 16:
        return 'activo', 0.012, 0.008
    elif 16 <= hora < 22:
        return 'normal', 0.015, 0.010
    else:
        return 'nocturno', 0.020, 0.006

# ============================================================
# RANKING DE PARES
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
    ranking = cargar_ranking()
    return ranking.get(par, {}).get('score', 50)

def par_tiene_buen_score(par, minimo=30):
    return obtener_score_par(par) >= minimo

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
    print(f"  {par} agregado a blacklist por 24hs (perdidas: {veces})")
    enviar_telegram(f"🚫 <b>BLACKLIST</b> {par}\nRazón: {razon}\nBaneado por 24 horas")

def actualizar_blacklist_post_venta(par, ganancia_pct):
    actualizar_ranking(par, ganancia_pct)
    if ganancia_pct < 0:
        blacklist = cargar_blacklist()
        veces_perdida = blacklist.get(par, {}).get('veces', 0) + 1
        if veces_perdida >= 2:
            agregar_a_blacklist(par, f"Perdio {veces_perdida} veces seguidas")
        else:
            blacklist[par] = {'veces': veces_perdida, 'ultima_perdida': datetime.now().isoformat()}
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
        monto = MONTO_BASE * 1.3
        print(f"  Racha ganadora ({ganancias}/{len(ultimas)}) - aumentando monto")
    elif ratio <= 0.3:
        monto = MONTO_BASE * 0.7
        print(f"  Racha perdedora ({ganancias}/{len(ultimas)}) - reduciendo monto")
    else:
        monto = MONTO_BASE
    return round(max(MONTO_MIN, min(MONTO_MAX, monto)), 2)

def calcular_monto_diversificado(historial, capital_disponible):
    monto = calcular_monto_dinamico(historial)
    monto = min(monto, capital_disponible * 0.9)
    if monto < MONTO_MIN:
        return 0
    return round(monto, 2)

# ============================================================
# REPORTE DIARIO — agrega resumen on-chain a las 8am
# ============================================================
def enviar_reporte_diario():
    try:
        ultimo = {}
        if os.path.exists(REPORTE_FILE):
            with open(REPORTE_FILE, "r") as f:
                ultimo = json.load(f)
        ultima_fecha = ultimo.get('fecha', '')
        hoy = datetime.now().strftime("%Y-%m-%d")
        hora_actual = datetime.now().hour
        if ultima_fecha == hoy or hora_actual != 8:
            return
        historial = cargar_historial()
        ayer = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        ops_ayer = [p for p in historial if p.get('fecha_cierre', '').startswith(ayer)]
        ganancias = [p for p in ops_ayer if p.get('estado') == 'cerrada_ganancia']
        perdidas = [p for p in ops_ayer if p.get('estado') == 'cerrada_perdida']
        total_g = sum(p.get('ganancia_pct', 0) for p in ganancias)
        total_p = sum(p.get('ganancia_pct', 0) for p in perdidas)
        neto = total_g + total_p
        todas_ganancias = [p for p in historial if p.get('estado') == 'cerrada_ganancia']
        todas_perdidas = [p for p in historial if p.get('estado') == 'cerrada_perdida']
        neto_total = sum(p.get('ganancia_pct', 0) for p in todas_ganancias) + sum(p.get('ganancia_pct', 0) for p in todas_perdidas)
        blacklist = cargar_blacklist()
        ranking = cargar_ranking()
        top_pares = sorted(ranking.items(), key=lambda x: x[1]['score'], reverse=True)[:3]
        top_str = " | ".join([f"{p[0]}({p[1]['score']})" for p in top_pares]) if top_pares else "Sin datos"
        reporte = f"""📊 <b>REPORTE DIARIO</b> {ayer}
━━━━━━━━━━━━━━━━━━━━
📈 Operaciones: {len(ops_ayer)}
✅ Ganancias: {len(ganancias)} (+{total_g:.2f}%)
🔴 Pérdidas: {len(perdidas)} ({total_p:.2f}%)
💰 Neto del día: {neto:+.2f}%
━━━━━━━━━━━━━━━━━━━━
📦 Acumulado total: {neto_total:+.2f}%
🚫 Pares en blacklist: {len(blacklist)}
🏆 Top pares: {top_str}
━━━━━━━━━━━━━━━━━━━━
🤖 Bot operando normalmente"""
        enviar_telegram(reporte)

        # ← NUEVO: resumen on-chain de BTC y ETH junto al reporte
        try:
            sig_btc = get_onchain_signal("BTCUSDT")
            sig_eth = get_onchain_signal("ETHUSDT")
            resumen = (
                f"📡 <b>Sentiment On-Chain — apertura del día</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"BTC {sig_btc['emoji']} <code>{sig_btc['score']:+.3f}</code> {sig_btc['action']}\n"
                f"ETH {sig_eth['emoji']} <code>{sig_eth['score']:+.3f}</code> {sig_eth['action']}\n"
                f"Fear &amp; Greed: <code>{sig_btc['components']['fear_greed']['value']}</code> "
                f"({sig_btc['components']['fear_greed']['label']})"
            )
            enviar_telegram(resumen)
        except Exception as e:
            print(f"  Error onchain en reporte: {e}")

        with open(REPORTE_FILE, "w") as f:
            json.dump({'fecha': hoy}, f)
    except Exception as e:
        print(f"Error reporte: {e}")

# ============================================================
# INDICADORES TECNICOS
# ============================================================
def obtener_precio(par):
    try:
        ticker = client_binance.get_symbol_ticker(symbol=par)
        return float(ticker['price'])
    except:
        return None

def obtener_capital_disponible():
    try:
        account = client_binance.get_account()
        for b in account['balances']:
            if b['asset'] == 'USDT':
                return float(b['free'])
        return 0
    except:
        return 0

def calcular_rsi(precios, periodo=14):
    if len(precios) < periodo + 1:
        return 50
    deltas = np.diff(precios)
    ganancias = np.where(deltas > 0, deltas, 0)
    perdidas = np.where(deltas < 0, -deltas, 0)
    avg_ganancia = np.mean(ganancias[-periodo:])
    avg_perdida = np.mean(perdidas[-periodo:])
    if avg_perdida == 0:
        return 100
    rs = avg_ganancia / avg_perdida
    return 100 - (100 / (1 + rs))

def calcular_ema(precios, periodo):
    if len(precios) < periodo:
        return precios[-1]
    k = 2 / (periodo + 1)
    ema = precios[0]
    for precio in precios[1:]:
        ema = precio * k + ema * (1 - k)
    return ema

def calcular_macd(precios):
    if len(precios) < 26:
        return 0, 0
    precios = np.array(precios)
    ema12 = calcular_ema(precios, 12)
    ema26 = calcular_ema(precios, 26)
    macd = ema12 - ema26
    signal = calcular_ema(np.array([macd] * 9), 9)
    return macd, signal

def calcular_bollinger(precios, periodo=20):
    if len(precios) < periodo:
        return precios[-1], precios[-1], precios[-1]
    ultimos = precios[-periodo:]
    media = np.mean(ultimos)
    std = np.std(ultimos)
    return media + 2 * std, media, media - 2 * std

def obtener_datos_mercado(par, intervalo='5m', limite=50):
    try:
        klines = client_binance.get_klines(symbol=par, interval=intervalo, limit=limite)
        precios = [float(k[4]) for k in klines]
        volumenes = [float(k[5]) for k in klines]
        precio_actual = precios[-1]
        cambio_1h = ((precios[-1] - precios[-12]) / precios[-12]) * 100
        rsi = calcular_rsi(precios)
        macd, signal = calcular_macd(precios)
        banda_sup, media_bb, banda_inf = calcular_bollinger(precios)
        volumen_promedio = np.mean(volumenes[-10:])
        volumen_actual = volumenes[-1]
        return {
            'par': par,
            'precio_actual': precio_actual,
            'cambio_1h': round(cambio_1h, 3),
            'rsi': round(rsi, 2),
            'macd': macd,
            'macd_signal': signal,
            'bb_sup': banda_sup,
            'bb_media': media_bb,
            'bb_inf': banda_inf,
            'volumen_ratio': volumen_actual / volumen_promedio if volumen_promedio > 0 else 1,
            'precios': precios
        }
    except:
        return None

def confirmar_dos_timeframes(par):
    try:
        datos_5m = obtener_datos_mercado(par, '5m', 50)
        datos_1h = obtener_datos_mercado(par, '1h', 50)
        if not datos_5m or not datos_1h:
            return False, "No se pudieron obtener datos"
        rsi_5m = datos_5m['rsi']
        macd_5m = datos_5m['macd'] > datos_5m['macd_signal']
        bb_5m = datos_5m['precio_actual'] <= datos_5m['bb_inf'] * 1.005
        rsi_1h = datos_1h['rsi']
        macd_1h = datos_1h['macd'] > datos_1h['macd_signal']
        bb_1h = datos_1h['precio_actual'] <= datos_1h['bb_inf'] * 1.01
        señales_5m = sum([rsi_5m < 40, macd_5m, bb_5m])
        señales_1h = sum([rsi_1h < 50, macd_1h, bb_1h])
        confirmado = señales_5m >= 1 and señales_1h >= 1
        razon = f"5m: {señales_5m}/3 | 1h: {señales_1h}/3"
        return confirmado, razon
    except:
        return False, "Error"

def es_caida_libre(par, cambio_24h):
    try:
        klines_1h = client_binance.get_klines(symbol=par, interval='1h', limit=24)
        precios_1h = [float(k[4]) for k in klines_1h]
        caida_6h = ((precios_1h[-1] - precios_1h[-6]) / precios_1h[-6]) * 100
        caida_12h = ((precios_1h[-1] - precios_1h[-12]) / precios_1h[-12]) * 100
        caidas_por_hora = [((precios_1h[i] - precios_1h[i-1]) / precios_1h[i-1]) * 100 for i in range(-4, 0)]
        acelerando = all(c < -0.3 for c in caidas_por_hora)
        es_crash = caida_6h < CRASH_THRESHOLD or caida_12h < CRASH_THRESHOLD * 1.5 or acelerando
        if es_crash:
            print(f"  CRASH: 6h={caida_6h:.1f}% 12h={caida_12h:.1f}% acelerando={acelerando}")
        return es_crash
    except:
        return False

# ← NUEVO: acepta onchain_score como parámetro extra
def analizar_sentimiento_groq(par, datos_5m, datos_1h, cambio_24h, modo, score_par, onchain_score=0.0):
    try:
        rsi_5m = datos_5m['rsi']
        rsi_1h = datos_1h['rsi']
        macd_alcista_5m = datos_5m['macd'] > datos_5m['macd_signal']
        macd_alcista_1h = datos_1h['macd'] > datos_1h['macd_signal']
        bb_5m = datos_5m['precio_actual'] <= datos_5m['bb_inf'] * 1.005
        bb_1h = datos_1h['precio_actual'] <= datos_1h['bb_inf'] * 1.01

        # ← NUEVO: descripción en lenguaje natural del score on-chain para el prompt
        if onchain_score >= 0.35:
            onchain_desc = f"ALCISTA ({onchain_score:+.2f}) — presión compradora, funding ok"
        elif onchain_score >= 0.15:
            onchain_desc = f"LEVEMENTE ALCISTA ({onchain_score:+.2f}) — señales mixtas positivas"
        elif onchain_score <= -0.35:
            onchain_desc = f"BAJISTA ({onchain_score:+.2f}) — alta presión vendedora, evitar longs"
        elif onchain_score <= -0.15:
            onchain_desc = f"LEVEMENTE BAJISTA ({onchain_score:+.2f}) — precaución"
        else:
            onchain_desc = f"NEUTRO ({onchain_score:+.2f}) — sin sesgo claro"

        prompt = f"""Sos un trader experto en crypto scalping.

Par: {par}
Score histórico del par: {score_par}/100
Modo horario: {modo}
Cambio 24h: {cambio_24h}%

TIMEFRAME 5 MINUTOS:
- RSI: {rsi_5m} {'(SOBREVENTA)' if rsi_5m < 35 else '(neutral)' if rsi_5m < 50 else '(sobrecompra)'}
- MACD: {'ALCISTA' if macd_alcista_5m else 'BAJISTA'}
- Bollinger: {'CERCA DEL PISO' if bb_5m else 'zona media'}

TIMEFRAME 1 HORA:
- RSI: {rsi_1h} {'(SOBREVENTA)' if rsi_1h < 40 else '(neutral)' if rsi_1h < 55 else '(sobrecompra)'}
- MACD: {'ALCISTA' if macd_alcista_1h else 'BAJISTA'}
- Bollinger: {'CERCA DEL PISO' if bb_1h else 'zona media'}

SENTIMENT ON-CHAIN (futuros + mercado):
- {onchain_desc}

Reglas:
- Solo comprá si AMBOS timeframes tienen al menos 1 señal positiva
- Si score_par < 30, sé MUY conservador
- Si score_par > 70, podés ser más agresivo
- En modo nocturno, solo entrá si hay 2+ señales en ambos timeframes
- Si on-chain es BAJISTA, exigí señales técnicas MUY fuertes para comprar
- Si on-chain es ALCISTA, podés ser ligeramente más permisivo

Respondé SOLO con JSON:
{{"comprar": true, "confianza": 8, "razon": "1 linea"}}"""

        respuesta = client_groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=150
        )
        texto = respuesta.choices[0].message.content.strip()
        texto = texto.replace('```json', '').replace('```', '').strip()
        inicio = texto.find('{')
        fin = texto.rfind('}')
        if inicio != -1 and fin != -1:
            texto = texto[inicio:fin+1]
        return json.loads(texto)
    except Exception as e:
        print(f"   Error Groq: {e}")
        return None

def obtener_mejores_pares():
    try:
        tickers = client_binance.get_ticker()
        usdt_pares = [
            t for t in tickers
            if t['symbol'].endswith('USDT')
            and float(t['quoteVolume']) > 2000000
            and float(t['lastPrice']) > 0.0001
            and float(t['lastPrice']) < 500
        ]
        usdt_pares.sort(key=lambda x: float(x['quoteVolume']), reverse=True)
        return usdt_pares[:80]
    except:
        return []

def filtrar_candidatos(pares_tickers, modo):
    candidatos = []
    for t in pares_tickers:
        cambio = float(t['priceChangePercent'])
        volumen = float(t['quoteVolume'])
        par = t['symbol']
        umbral = -1.0 if modo != 'nocturno' else -2.0
        if umbral >= cambio >= -10 and volumen > 2000000 and not esta_en_blacklist(par):
            score = obtener_score_par(par)
            candidatos.append({
                'par': par,
                'cambio_24h': cambio,
                'volumen': volumen,
                'score': score
            })
    candidatos.sort(key=lambda x: (x['score'], -x['cambio_24h']), reverse=True)
    return candidatos[:15]

def detectar_pumps(pares_tickers):
    pumps = []
    for t in pares_tickers:
        par = t['symbol']
        if esta_en_blacklist(par):
            continue
        volumen = float(t['quoteVolume'])
        try:
            klines = client_binance.get_klines(symbol=par, interval='1m', limit=10)
            precios = [float(k[4]) for k in klines]
            volumenes = [float(k[5]) for k in klines]
            cambio_5m = ((precios[-1] - precios[-5]) / precios[-5]) * 100
            volumen_promedio = np.mean(volumenes[:-3])
            volumen_actual = np.mean(volumenes[-3:])
            ratio_volumen = volumen_actual / volumen_promedio if volumen_promedio > 0 else 1
            if (1.0 <= cambio_5m <= 8.0 and ratio_volumen >= 3.0 and volumen > 1000000):
                pumps.append({
                    'par': par,
                    'cambio_5m': round(cambio_5m, 3),
                    'cambio_24h': float(t['priceChangePercent']),
                    'ratio_volumen': round(ratio_volumen, 2),
                    'volumen': volumen,
                    'score': obtener_score_par(par)
                })
        except:
            continue
        time.sleep(0.1)
    pumps.sort(key=lambda x: (x['ratio_volumen'] + x['score']/20), reverse=True)
    return pumps[:5]

def ejecutar_compra(par, monto, datos):
    try:
        orden = client_binance.order_market_buy(symbol=par, quoteOrderQty=monto)
        qty = float(orden['executedQty'])
        precio = float(orden['fills'][0]['price']) if orden.get('fills') else obtener_precio(par)
        print(f"   COMPRA OK! {qty} {par} a ${precio}")
        enviar_telegram(f"🟢 <b>COMPRA</b> {par}\n💰 Precio: ${precio}\n📊 RSI: {datos['rsi']} | Score: {obtener_score_par(par)}/100\n💵 Monto: ${monto}")
        return True, qty, precio
    except Exception as e:
        print(f"   Error comprando: {e}")
        return False, 0, 0

def ejecutar_venta(par, cantidad, precio_actual, pct, tipo):
    try:
        info = client_binance.get_symbol_info(par)
        step = next(f['stepSize'] for f in info['filters'] if f['filterType'] == 'LOT_SIZE')
        decimales = len(step.rstrip('0').split('.')[-1]) if '.' in step else 0
        cantidad = round(cantidad, decimales)
        orden = client_binance.order_market_sell(symbol=par, quantity=cantidad)
        print(f"   VENTA OK! ID: {orden['orderId']}")
        emojis = {'ganancia': '✅', 'pump': '🚀', 'trailing': '📉', 'perdida': '🔴'}
        nombres = {'ganancia': 'TAKE PROFIT', 'pump': 'PUMP PROFIT', 'trailing': 'TRAILING STOP', 'perdida': 'STOP LOSS'}
        emoji = emojis.get(tipo, '✅')
        nombre = nombres.get(tipo, 'VENTA')
        enviar_telegram(f"{emoji} <b>{nombre}</b> {par}\n📈 {'Ganancia' if pct > 0 else 'Pérdida'}: {pct:+.3f}%\n💰 Precio: ${precio_actual}")
        actualizar_blacklist_post_venta(par, pct)
        return True
    except Exception as e:
        print(f"   Error vendiendo: {e}")
        return False

def revisar_posiciones(tp_actual, sl_actual):
    historial = cargar_historial()
    posiciones = [p for p in historial if p.get('estado') == 'abierta']
    if not posiciones:
        return 0
    print(f"\nRevisando {len(posiciones)} posiciones...")
    cerradas = 0
    for i, pos in enumerate(historial):
        if pos.get('estado') != 'abierta':
            continue
        precio_actual = obtener_precio(pos['par'])
        if not precio_actual:
            continue
        precio_compra = float(pos['precio_compra'])
        cambio = (precio_actual - precio_compra) / precio_compra
        pct = round(cambio * 100, 3)
        estrategia = pos.get('estrategia', 'scalp')
        tp = TAKE_PROFIT_PUMP if estrategia == 'pump' else tp_actual

        precio_maximo = float(pos.get('precio_maximo', precio_compra))
        if precio_actual > precio_maximo:
            precio_maximo = precio_actual
            historial[i]['precio_maximo'] = precio_maximo

        caida_desde_maximo = (precio_maximo - precio_actual) / precio_maximo
        ganancia_actual = (precio_actual - precio_compra) / precio_compra
        trailing_activado = ganancia_actual >= tp_actual and caida_desde_maximo >= TRAILING_STOP

        print(f"  {pos['par']} [{estrategia}] | {pct:+.3f}% | Max: {precio_maximo:.4f} | Score: {obtener_score_par(pos['par'])}")

        if trailing_activado:
            print(f"  TRAILING STOP! +{pct}%")
            if ejecutar_venta(pos['par'], pos.get('cantidad', 0), precio_actual, pct, 'trailing'):
                historial[i]['estado'] = 'cerrada_ganancia'
                historial[i]['precio_venta'] = precio_actual
                historial[i]['ganancia_pct'] = pct
                historial[i]['fecha_cierre'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cerradas += 1
        elif cambio >= tp:
            print(f"  TAKE PROFIT +{pct}%!")
            tipo_venta = 'pump' if estrategia == 'pump' else 'ganancia'
            if ejecutar_venta(pos['par'], pos.get('cantidad', 0), precio_actual, pct, tipo_venta):
                historial[i]['estado'] = 'cerrada_ganancia'
                historial[i]['precio_venta'] = precio_actual
                historial[i]['ganancia_pct'] = pct
                historial[i]['fecha_cierre'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cerradas += 1
        elif cambio <= -sl_actual:
            print(f"  STOP LOSS {pct}%!")
            if ejecutar_venta(pos['par'], pos.get('cantidad', 0), precio_actual, pct, 'perdida'):
                historial[i]['estado'] = 'cerrada_perdida'
                historial[i]['precio_venta'] = precio_actual
                historial[i]['ganancia_pct'] = pct
                historial[i]['fecha_cierre'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cerradas += 1
        else:
            print(f"  Manteniendo... ({caida_desde_maximo*100:.2f}% desde max)")

    guardar_historial(historial)
    return cerradas

def mostrar_resumen():
    historial = cargar_historial()
    if not historial:
        return
    ganancias = [p for p in historial if p.get('estado') == 'cerrada_ganancia']
    perdidas = [p for p in historial if p.get('estado') == 'cerrada_perdida']
    abiertas = [p for p in historial if p.get('estado') == 'abierta']
    g_pct = sum(p.get('ganancia_pct', 0) for p in ganancias)
    p_pct = sum(p.get('ganancia_pct', 0) for p in perdidas)
    blacklist = cargar_blacklist()
    print(f"  G: {len(ganancias)} (+{g_pct:.2f}%) | P: {len(perdidas)} ({p_pct:.2f}%) | Abiertas: {len(abiertas)} | Neto: {g_pct+p_pct:+.2f}% | BL: {len(blacklist)}")

def main():
    modo, tp_actual, sl_actual = obtener_modo_horario()
    hora = datetime.now().hour

    print("="*60)
    print(f"  BOT DEFINITIVO - Modo: {modo.upper()} ({hora}hs)")
    print("="*60)
    print(f"  TP: {tp_actual*100}% | SL: {sl_actual*100}% | Trail: {TRAILING_STOP*100}%")
    mostrar_resumen()
    print("="*60)

    enviar_reporte_diario()
    revisar_posiciones(tp_actual, sl_actual)

    historial = cargar_historial()
    posiciones_abiertas = len([p for p in historial if p.get('estado') == 'abierta'])

    if posiciones_abiertas >= MAX_POSICIONES:
        print(f"\nMaximo de posiciones. Esperando cierres.")
        return

    capital_disponible = obtener_capital_disponible()
    print(f"\nCapital: ${capital_disponible:.2f} | Modo: {modo}")

    if capital_disponible < MONTO_MIN:
        print("Capital insuficiente.")
        return

    mejores_pares = obtener_mejores_pares()
    if not mejores_pares:
        return

    pares_en_uso = {p['par'] for p in historial if p.get('estado') == 'abierta'}

    # ── PUMPS ────────────────────────────────────────────────────────────────
    if modo != 'nocturno':
        print(f"\nDetectando pumps...")
        pumps = detectar_pumps(mejores_pares)
        print(f"{len(pumps)} pumps\n")

        for p in pumps:
            if posiciones_abiertas >= MAX_POSICIONES:
                break
            if p['par'] in pares_en_uso:
                continue
            par = p['par']
            print(f"PUMP! {par} | +{p['cambio_5m']}% | Vol {p['ratio_volumen']}x | Score {p['score']}")
            datos = obtener_datos_mercado(par)
            if not datos or datos['rsi'] > 72:
                continue
            if es_caida_libre(par, p['cambio_24h']):
                continue

            # ← NUEVO: chequeo on-chain antes de entrar al pump
            sig = {"score": 0.0, "action": "NEUTRAL", "block": False, "emoji": "⚪"}
            try:
                sig = get_onchain_signal(par)
                print(f"  OnChain: {sig['action']} ({sig['score']:+.3f})")
                if sig['block']:
                    print(f"  BLOQUEADO por sentiment on-chain")
                    continue
            except Exception as e:
                print(f"  OnChain error (ignorando): {e}")

            monto = calcular_monto_diversificado(historial, capital_disponible)
            if monto == 0:
                continue

            # ← NUEVO: reducir monto 30% si on-chain es levemente negativo
            if sig['action'] == 'SLIGHT_SHORT':
                monto = round(monto * 0.7, 2)
                print(f"  Monto reducido a ${monto} por on-chain negativo")

            exito, cantidad, precio = ejecutar_compra(par, monto, datos)
            if exito:
                historial.append({
                    'par': par, 'precio_compra': precio, 'precio_maximo': precio,
                    'cantidad': cantidad, 'monto': monto, 'rsi_entrada': datos['rsi'],
                    'confianza': 9, 'razon': f"PUMP +{p['cambio_5m']}% vol {p['ratio_volumen']}x",
                    'onchain_score': sig['score'],   # ← NUEVO
                    'estado': 'abierta', 'estrategia': 'pump',
                    'fecha': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
                guardar_historial(historial)
                posiciones_abiertas += 1
                pares_en_uso.add(par)
                capital_disponible -= monto
                enviar_telegram(
                    f"🚀 <b>PUMP</b> {par}\n"
                    f"+{p['cambio_5m']}% | Vol {p['ratio_volumen']}x | Score {p['score']}\n"
                    f"💰 ${monto} | OnChain: {sig['emoji']} {sig['score']:+.3f}"  # ← NUEVO
                )

    # ── SCALPING ─────────────────────────────────────────────────────────────
    candidatos = filtrar_candidatos(mejores_pares, modo)
    print(f"\n{len(candidatos)} candidatos scalping\n")

    for c in candidatos:
        if posiciones_abiertas >= MAX_POSICIONES:
            break
        if c['par'] in pares_en_uso:
            continue
        par = c['par']
        score = c['score']
        print(f"Analizando {par} | 24h: {c['cambio_24h']}% | Score: {score}")

        if es_caida_libre(par, c['cambio_24h']):
            print(f"  Caida libre, saltando")
            continue

        # ← NUEVO: chequeo on-chain antes de pedir datos técnicos
        sig = {"score": 0.0, "action": "NEUTRAL", "block": False, "emoji": "⚪"}
        try:
            sig = get_onchain_signal(par)
            print(f"  OnChain: {sig['action']} ({sig['score']:+.3f})")
            if sig['block']:
                print(f"  BLOQUEADO por on-chain, saltando")
                continue
        except Exception as e:
            print(f"  OnChain error (ignorando): {e}")

        datos_5m = obtener_datos_mercado(par, '5m', 50)
        datos_1h = obtener_datos_mercado(par, '1h', 50)
        if not datos_5m or not datos_1h:
            continue

        print(f"  5m RSI:{datos_5m['rsi']} MACD:{'▲' if datos_5m['macd'] > datos_5m['macd_signal'] else '▼'} | 1h RSI:{datos_1h['rsi']} MACD:{'▲' if datos_1h['macd'] > datos_1h['macd_signal'] else '▼'}")

        confirmado, razon_tf = confirmar_dos_timeframes(par)
        if not confirmado:
            print(f"  Sin confirmacion: {razon_tf}")
            continue

        # ← NUEVO: pasar onchain_score al prompt de Groq
        analisis = analizar_sentimiento_groq(
            par, datos_5m, datos_1h, c['cambio_24h'], modo, score,
            onchain_score=sig['score']
        )
        if not analisis:
            continue

        # ← NUEVO: subir umbral de confianza si on-chain es negativo
        confianza_minima = 8 if modo == 'nocturno' else 7
        if sig['action'] == 'SLIGHT_SHORT':
            confianza_minima = min(9, confianza_minima + 1)
            print(f"  Umbral subido a {confianza_minima}/10 por on-chain negativo")

        if analisis.get('comprar') and analisis.get('confianza', 0) >= confianza_minima:
            monto = calcular_monto_diversificado(historial, capital_disponible)
            if monto == 0:
                continue

            # ← NUEVO: reducir monto si on-chain no acompaña
            if sig['action'] == 'SLIGHT_SHORT':
                monto = round(monto * 0.7, 2)

            print(f"  ENTRADA! {analisis['confianza']}/10 | {analisis.get('razon','')} | ${monto}")
            exito, cantidad, precio = ejecutar_compra(par, monto, datos_5m)
            if exito:
                historial.append({
                    'par': par, 'precio_compra': precio, 'precio_maximo': precio,
                    'cantidad': cantidad, 'monto': monto,
                    'rsi_entrada': datos_5m['rsi'], 'rsi_1h_entrada': datos_1h['rsi'],
                    'confianza': analisis.get('confianza'), 'razon': analisis.get('razon'),
                    'score_entrada': score, 'modo': modo,
                    'onchain_score': sig['score'],    # ← NUEVO
                    'onchain_action': sig['action'],  # ← NUEVO
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
    print(f"  Ciclo: {datetime.now().strftime('%H:%M:%S')} | Modo: {modo}")

if __name__ == "__main__":
    enviar_telegram("🤖 <b>Bot Binance DEFINITIVO</b>\n⏰ Modo horario adaptativo\n🏆 Ranking de pares\n🔄 Auto-reinversión\n📡 Sentiment On-Chain activado\n📊 Análisis completo activado")
    while True:
        try:
            main()
        except Exception as e:
            print(f"Error: {e}")
            enviar_telegram(f"⚠️ Error: {e}")
        time.sleep(120)
