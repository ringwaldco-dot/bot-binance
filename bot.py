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
from groq import Groq

from onchain_sentiment import get_onchain_signal, format_signal_telegram
from market_monitor import MonitorMercado
from listing_detector import ListingDetector

load_dotenv()

BINANCE_API_KEY = os.getenv('BINANCE_API_KEY')
BINANCE_SECRET_KEY = os.getenv('BINANCE_SECRET_KEY')
GROQ_API_KEY = os.getenv('GROQ_API_KEY')

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
CUT_LOSS_UMBRAL = -0.003        # -0.3% — umbral para activar cut loss
CUT_LOSS_MINUTOS = 5            # minutos en pérdida antes de cortar
HISTORIAL_FILE = "historial_binance.json"
BLACKLIST_FILE = "blacklist.json"
REPORTE_FILE = "ultimo_reporte.json"
RANKING_FILE = "ranking_pares.json"
MONITOR_CICLO = 0

_lock = threading.Lock()

client_binance = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)
client_groq = Groq(api_key=GROQ_API_KEY)
monitor_mercado = MonitorMercado()
listing_detector = ListingDetector()

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
    print(f"  {par} blacklist 24hs ({veces} veces)")
    enviar_telegram(f"🚫 <b>BLACKLIST</b> {par}\nRazón: {razon}\nBaneado 24hs")

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
    elif ratio <= 0.3:
        monto = MONTO_BASE * 0.7
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
# REPORTE DIARIO
# ============================================================
def enviar_reporte_diario():
    try:
        ultimo = {}
        if os.path.exists(REPORTE_FILE):
            with open(REPORTE_FILE, "r") as f:
                ultimo = json.load(f)
        ultima_fecha = ultimo.get('fecha', '')
        hoy = datetime.now().strftime("%Y-%m-%d")
        if ultima_fecha == hoy or datetime.now().hour != 8:
            return
        historial = cargar_historial()
        ayer = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        ops_ayer = [p for p in historial if p.get('fecha_cierre', '').startswith(ayer)]
        ganancias = [p for p in ops_ayer if p.get('estado') == 'cerrada_ganancia']
        perdidas = [p for p in ops_ayer if p.get('estado') == 'cerrada_perdida']
        total_g = sum(p.get('ganancia_pct', 0) for p in ganancias)
        total_p = sum(p.get('ganancia_pct', 0) for p in perdidas)
        neto = total_g + total_p
        todas_g = [p for p in historial if p.get('estado') == 'cerrada_ganancia']
        todas_p = [p for p in historial if p.get('estado') == 'cerrada_perdida']
        neto_total = sum(p.get('ganancia_pct', 0) for p in todas_g) + sum(p.get('ganancia_pct', 0) for p in todas_p)
        blacklist = cargar_blacklist()
        ranking = cargar_ranking()
        top_pares = sorted(ranking.items(), key=lambda x: x[1]['score'], reverse=True)[:3]
        top_str = " | ".join([f"{p[0]}({p[1]['score']})" for p in top_pares]) if top_pares else "Sin datos"
        pumps_g = [p for p in todas_g if p.get('estrategia') == 'pump']
        pumps_p = [p for p in todas_p if p.get('estrategia') == 'pump']
        scalp_g = [p for p in todas_g if p.get('estrategia') != 'pump']
        scalp_p = [p for p in todas_p if p.get('estrategia') != 'pump']
        cut_losses = [p for p in todas_p if 'cut_loss' in p.get('razon_cierre', '')]
        rebalanceos = [p for p in historial if 'rebalanceo' in p.get('razon_cierre', '')]
        reporte = f"""📊 <b>REPORTE DIARIO</b> {ayer}
━━━━━━━━━━━━━━━━━━━━
📈 Operaciones: {len(ops_ayer)}
✅ Ganancias: {len(ganancias)} (+{total_g:.2f}%)
🔴 Pérdidas: {len(perdidas)} ({total_p:.2f}%)
💰 Neto del día: {neto:+.2f}%
━━━━━━━━━━━━━━━━━━━━
🚀 Pump: {len(pumps_g)}G / {len(pumps_p)}P
📉 Scalp: {len(scalp_g)}G / {len(scalp_p)}P
✂️ Cut losses: {len(cut_losses)} | 🔄 Rebalanceos: {len(rebalanceos)}
━━━━━━━━━━━━━━━━━━━━
📦 Acumulado total: {neto_total:+.2f}%
🚫 Blacklist: {len(blacklist)} | 🏆 Top: {top_str}
━━━━━━━━━━━━━━━━━━━━
🤖 Bot operando normalmente"""
        enviar_telegram(reporte)
        try:
            sig_btc = get_onchain_signal("BTCUSDT")
            sig_eth = get_onchain_signal("ETHUSDT")
            fg = sig_btc['components']['fear_greed']
            if fg['value'] <= 20:
                desc = "⚠️ Extremo miedo — posibles rebotes"
            elif fg['value'] <= 40:
                desc = "😟 Miedo — buscar rebotes"
            elif fg['value'] <= 60:
                desc = "😐 Neutral"
            elif fg['value'] <= 80:
                desc = "😀 Codicia — cuidado con sobrecompra"
            else:
                desc = "🔥 Extrema codicia — riesgo de corrección"
            enviar_telegram(
                f"📡 <b>Sentiment apertura</b>\n"
                f"BTC {sig_btc['emoji']} {sig_btc['score']:+.3f} {sig_btc['action']}\n"
                f"ETH {sig_eth['emoji']} {sig_eth['score']:+.3f} {sig_eth['action']}\n"
                f"Fear &amp; Greed: {fg['value']} ({fg['label']})\n{desc}"
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
    avg_g = np.mean(ganancias[-periodo:])
    avg_p = np.mean(perdidas[-periodo:])
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
    precios = np.array(precios)
    macd = calcular_ema(precios, 12) - calcular_ema(precios, 26)
    signal = calcular_ema(np.array([macd] * 9), 9)
    return macd, signal

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
        volumenes = [float(k[5]) for k in klines]
        rsi = calcular_rsi(precios)
        macd, signal = calcular_macd(precios)
        bb_sup, bb_med, bb_inf = calcular_bollinger(precios)
        vol_prom = np.mean(volumenes[-10:])
        return {
            'par': par,
            'precio_actual': precios[-1],
            'cambio_1h': round(((precios[-1] - precios[-12]) / precios[-12]) * 100, 3),
            'rsi': round(rsi, 2),
            'macd': macd,
            'macd_signal': signal,
            'bb_sup': bb_sup,
            'bb_media': bb_med,
            'bb_inf': bb_inf,
            'volumen_ratio': volumenes[-1] / vol_prom if vol_prom > 0 else 1,
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
    try:
        if onchain_score >= 0.35:
            oc = f"ALCISTA ({onchain_score:+.2f})"
        elif onchain_score >= 0.15:
            oc = f"LEV.ALCISTA ({onchain_score:+.2f})"
        elif onchain_score <= -0.35:
            oc = f"BAJISTA ({onchain_score:+.2f})"
        elif onchain_score <= -0.15:
            oc = f"LEV.BAJISTA ({onchain_score:+.2f})"
        else:
            oc = f"NEUTRO ({onchain_score:+.2f})"

        prompt = f"""Trader experto crypto scalping.
Par:{par} Score:{score_par}/100 Modo:{modo} 24h:{cambio_24h}%
5M: RSI {d5['rsi']} MACD {'▲' if d5['macd']>d5['macd_signal'] else '▼'} BB {'PISO' if d5['precio_actual']<=d5['bb_inf']*1.005 else 'MED'}
1H: RSI {d1h['rsi']} MACD {'▲' if d1h['macd']>d1h['macd_signal'] else '▼'} BB {'PISO' if d1h['precio_actual']<=d1h['bb_inf']*1.01 else 'MED'}
OnChain:{oc}
Comprá si 1+ timeframe positivo. Nocturno: exigí ambos. OnChain BAJISTA: señales fuertes.
JSON: {{"comprar":true,"confianza":8,"razon":"1 linea"}}"""

        r = client_groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2, max_tokens=100
        )
        texto = r.choices[0].message.content.strip().replace('```json','').replace('```','').strip()
        i, f = texto.find('{'), texto.rfind('}')
        if i != -1 and f != -1:
            return json.loads(texto[i:f+1])
    except Exception as e:
        print(f"   Error Groq: {e}")
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

def filtrar_candidatos(pares_tickers, modo):
    candidatos = []
    for t in pares_tickers:
        cambio = float(t['priceChangePercent'])
        vol = float(t['quoteVolume'])
        par = t['symbol']
        umbral_max = -0.5 if modo == 'nocturno' else 3.0
        if -10 <= cambio <= umbral_max and vol > 2000000 and not esta_en_blacklist(par):
            candidatos.append({'par': par, 'cambio_24h': cambio, 'volumen': vol, 'score': obtener_score_par(par)})
    candidatos.sort(key=lambda x: (x['score'], -x['cambio_24h']), reverse=True)
    return candidatos[:20]

def ejecutar_compra(par, monto, datos):
    try:
        orden = client_binance.order_market_buy(symbol=par, quoteOrderQty=monto)
        qty = float(orden['executedQty'])
        precio = float(orden['fills'][0]['price']) if orden.get('fills') else obtener_precio(par)
        rsi_val = datos['rsi'] if isinstance(datos, dict) else datos
        print(f"   COMPRA OK! {qty} {par} a ${precio}")
        enviar_telegram(f"🟢 <b>COMPRA</b> {par}\n💰 ${precio} | RSI:{rsi_val} | Score:{obtener_score_par(par)}/100 | ${monto}")
        return True, qty, precio
    except Exception as e:
        print(f"   Error comprando: {e}")
        return False, 0, 0

def ejecutar_venta(par, cantidad, precio_actual, pct, tipo):
    try:
        info = client_binance.get_symbol_info(par)
        step = next(f['stepSize'] for f in info['filters'] if f['filterType'] == 'LOT_SIZE')
        dec = len(step.rstrip('0').split('.')[-1]) if '.' in step else 0
        cantidad = round(cantidad, dec)
        orden = client_binance.order_market_sell(symbol=par, quantity=cantidad)
        emojis = {'ganancia': '✅', 'pump': '🚀', 'trailing': '📉', 'perdida': '🔴'}
        nombres = {'ganancia': 'TAKE PROFIT', 'pump': 'PUMP PROFIT', 'trailing': 'TRAILING STOP', 'perdida': 'STOP LOSS'}
        enviar_telegram(f"{emojis.get(tipo,'✅')} <b>{nombres.get(tipo,'VENTA')}</b> {par}\n{'Ganancia' if pct>0 else 'Pérdida'}: {pct:+.3f}%\n💰 ${precio_actual}")
        actualizar_blacklist_post_venta(par, pct)
        return True
    except Exception as e:
        print(f"   Error vendiendo: {e}")
        return False

# ============================================================
# CUT LOSS AGRESIVO
# Si una posición lleva >5 min con pérdida >= -0.3%, vender.
# ============================================================
def revisar_cut_loss():
    """Corre cada ciclo. Vende posiciones que llevan demasiado tiempo en pérdida."""
    with _lock:
        historial = cargar_historial()
        cambios = False

        for i, pos in enumerate(historial):
            if pos.get('estado') != 'abierta':
                continue

            precio_actual = obtener_precio(pos['par'])
            if not precio_actual:
                continue

            precio_compra = float(pos['precio_compra'])
            cambio = (precio_actual - precio_compra) / precio_compra
            pct = round(cambio * 100, 3)

            # En ganancia o neutral — resetear contador
            if cambio >= 0:
                if historial[i].get('en_perdida_desde') is not None:
                    historial[i]['en_perdida_desde'] = None
                    cambios = True
                continue

            if cambio <= CUT_LOSS_UMBRAL:
                ahora = datetime.now()
                if historial[i].get('en_perdida_desde') is None:
                    # Primera vez bajo el umbral — iniciar contador
                    historial[i]['en_perdida_desde'] = ahora.isoformat()
                    cambios = True
                    print(f"  [CUT LOSS] {pos['par']} {pct}% — iniciando contador")
                else:
                    desde = datetime.fromisoformat(historial[i]['en_perdida_desde'])
                    minutos = (ahora - desde).total_seconds() / 60
                    print(f"  [CUT LOSS] {pos['par']} {pct}% | {minutos:.1f}min en pérdida")

                    if minutos >= CUT_LOSS_MINUTOS:
                        print(f"  [CUT LOSS] CORTANDO {pos['par']} — {minutos:.1f}min en {pct}%")
                        exito = ejecutar_venta(pos['par'], pos.get('cantidad', 0), precio_actual, pct, 'perdida')
                        if exito:
                            historial[i]['estado'] = 'cerrada_perdida'
                            historial[i]['precio_venta'] = precio_actual
                            historial[i]['ganancia_pct'] = pct
                            historial[i]['fecha_cierre'] = ahora.strftime("%Y-%m-%d %H:%M:%S")
                            historial[i]['razon_cierre'] = f'cut_loss_{minutos:.0f}min'
                            cambios = True
                            enviar_telegram(
                                f"✂️ <b>CUT LOSS</b> {pos['par']}\n"
                                f"📉 {pct:+.3f}% por {minutos:.0f} minutos\n"
                                f"💡 Capital liberado para mejor oportunidad"
                            )
            else:
                # Pérdida menor al umbral — resetear contador
                if historial[i].get('en_perdida_desde') is not None:
                    historial[i]['en_perdida_desde'] = None
                    cambios = True

        if cambios:
            guardar_historial(historial)

# ============================================================
# REBALANCEO DINÁMICO
# Vende la peor posición para entrar en una mejor oportunidad.
# ============================================================
def elegir_posicion_sacrificable():
    """Devuelve la posición con menor ganancia actual."""
    historial = cargar_historial()
    posiciones = [p for p in historial if p.get('estado') == 'abierta']
    if not posiciones:
        return None

    peor = None
    peor_cambio = float('inf')
    for pos in posiciones:
        precio_actual = obtener_precio(pos['par'])
        if not precio_actual:
            continue
        cambio = (precio_actual - float(pos['precio_compra'])) / float(pos['precio_compra'])
        if cambio < peor_cambio:
            peor_cambio = cambio
            peor = {
                'par': pos['par'],
                'cantidad': pos.get('cantidad', 0),
                'precio_actual': precio_actual,
                'cambio_pct': round(cambio * 100, 3),
                'monto': pos.get('monto', 0)
            }
    return peor

def rebalancear_si_necesario(oportunidad, tipo='pump', confianza=0):
    """
    Solo actúa si no hay capital suficiente.
    tipo='pump': necesita ratio_vol >= 2.0
    tipo='scalp': necesita confianza >= 7
    Retorna monto liberado o 0.
    """
    capital = obtener_capital_disponible()
    if capital >= MONTO_MIN:
        return 0  # Hay capital, no hace falta

    # Verificar calidad de la oportunidad
    if tipo == 'pump':
        if oportunidad.get('ratio_vol', 0) < 2.0:
            return 0
    elif tipo == 'scalp':
        if confianza < 7:
            return 0

    sacrificable = elegir_posicion_sacrificable()
    if not sacrificable:
        return 0

    par = sacrificable['par']
    pct = sacrificable['cambio_pct']
    precio_actual = sacrificable['precio_actual']
    cantidad = sacrificable['cantidad']
    monto_liberado = round(cantidad * precio_actual, 2)
    opp_nombre = oportunidad.get('par', oportunidad.get('par_binance', '?'))

    print(f"  [REBALANCEO] Sacrificando {par} ({pct:+.3f}%) → {opp_nombre}")

    with _lock:
        exito = ejecutar_venta(par, cantidad, precio_actual, pct, 'perdida' if pct < 0 else 'ganancia')
        if exito:
            historial = cargar_historial()
            for i, pos in enumerate(historial):
                if pos.get('par') == par and pos.get('estado') == 'abierta':
                    historial[i]['estado'] = 'cerrada_ganancia' if pct >= 0 else 'cerrada_perdida'
                    historial[i]['precio_venta'] = precio_actual
                    historial[i]['ganancia_pct'] = pct
                    historial[i]['fecha_cierre'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    historial[i]['razon_cierre'] = f'rebalanceo_por_{opp_nombre}'
                    break
            guardar_historial(historial)
            enviar_telegram(
                f"🔄 <b>REBALANCEO</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"❌ Vendido: {par} {pct:+.3f}%\n"
                f"✅ Nueva entrada: <b>{opp_nombre}</b>\n"
                f"💡 {'Pump vol ' + str(oportunidad.get('ratio_vol','?')) + 'x' if tipo=='pump' else 'Scalp confianza ' + str(confianza) + '/10'}"
            )
            return monto_liberado
    return 0

# ============================================================
# THREAD DEDICADO A PUMPS
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
                        'par': par,
                        'cambio_3m': round(c3m, 3),
                        'cambio_5m': round(c5m, 3),
                        'ratio_vol': round(ratio_vol, 2),
                        'rsi': round(rsi, 1),
                        'score': obtener_score_par(par)
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
            historial[i]['estado'] = estado
            historial[i]['precio_venta'] = precio_actual
            historial[i]['ganancia_pct'] = pct
            historial[i]['fecha_cierre'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            break
    guardar_historial(historial)

def vigilar_posicion_pump(par, precio_compra, cantidad, monto):
    precio_maximo = precio_compra
    inicio = time.time()
    print(f"  [WATCH] {par} ${precio_compra:.6f} | TP {TAKE_PROFIT_PUMP*100}% SL {STOP_LOSS_PUMP*100}%")
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
            print(f"  [WATCH] TAKE PROFIT {par} +{pct}%!")
            with _lock:
                ejecutar_venta(par, cantidad, precio_actual, pct, 'pump')
                _cerrar_posicion_historial(par, precio_actual, pct, 'cerrada_ganancia')
            return
        if cambio <= -STOP_LOSS_PUMP:
            print(f"  [WATCH] STOP LOSS {par} {pct}%")
            with _lock:
                ejecutar_venta(par, cantidad, precio_actual, pct, 'perdida')
                _cerrar_posicion_historial(par, precio_actual, pct, 'cerrada_perdida')
            return

    # Timeout
    precio_actual = obtener_precio(par) or precio_compra
    cambio = (precio_actual - precio_compra) / precio_compra
    pct = round(cambio * 100, 3)
    print(f"  [WATCH] TIMEOUT {par} {pct}%")
    with _lock:
        ejecutar_venta(par, cantidad, precio_actual, pct, 'ganancia' if pct > 0 else 'perdida')
        _cerrar_posicion_historial(par, precio_actual, pct, 'cerrada_ganancia' if pct > 0 else 'cerrada_perdida')

def ciclo_pump_agresivo():
    print("  [PUMP THREAD] Iniciado ✓")
    while True:
        try:
            modo, _, _ = obtener_modo_horario()
            if modo == 'nocturno':
                time.sleep(60)
                continue

            with _lock:
                historial = cargar_historial()
                pumps_ab = len([p for p in historial if p.get('estado') == 'abierta' and p.get('estrategia') == 'pump'])
                total_ab = len([p for p in historial if p.get('estado') == 'abierta'])
                capital = obtener_capital_disponible()

            if pumps_ab >= MAX_POSICIONES_PUMP or total_ab >= MAX_POSICIONES:
                time.sleep(20)
                continue

            pumps = detectar_pumps_rapido()
            if not pumps:
                time.sleep(20)
                continue

            print(f"\n  [PUMP THREAD] {len(pumps)} candidatos — {datetime.now().strftime('%H:%M:%S')}")

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

                # REBALANCEO: si no hay capital pero el pump es bueno, sacrificar la peor posición
                if capital < MONTO_MIN:
                    liberado = rebalancear_si_necesario(p, tipo='pump')
                    if liberado:
                        capital = obtener_capital_disponible()
                        time.sleep(1)  # esperar que Binance actualice el balance
                    else:
                        continue

                monto = min(MONTO_PUMP, capital * 0.9)
                if monto < MONTO_MIN:
                    continue

                print(f"  [PUMP THREAD] ENTRANDO {par} | +{p['cambio_5m']}% | Vol {p['ratio_vol']}x | RSI {p['rsi']} | ${monto}")

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
                        t = threading.Thread(target=vigilar_posicion_pump, args=(par, precio, cantidad, monto), daemon=True)
                        t.start()

        except Exception as e:
            print(f"  [PUMP THREAD] Error: {e}")
        time.sleep(20)

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
        trailing = cambio >= tp_actual and caida >= TRAILING_STOP
        print(f"  {pos['par']} [{pos.get('estrategia','scalp')}] | {pct:+.3f}% | Max:{precio_maximo:.6f}")
        if trailing:
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
            print(f"  Manteniendo... ({caida*100:.2f}% desde max)")
    guardar_historial(historial)
    return cerradas

def mostrar_resumen():
    historial = cargar_historial()
    if not historial:
        return
    g = [p for p in historial if p.get('estado') == 'cerrada_ganancia']
    p = [p for p in historial if p.get('estado') == 'cerrada_perdida']
    ab = [p for p in historial if p.get('estado') == 'abierta']
    bl = cargar_blacklist()
    pumps_ab = len([x for x in ab if x.get('estrategia') == 'pump'])
    print(f"  G:{len(g)} (+{sum(x.get('ganancia_pct',0) for x in g):.2f}%) P:{len(p)} ({sum(x.get('ganancia_pct',0) for x in p):.2f}%) | Abiertas:{len(ab)}(pump:{pumps_ab}) | BL:{len(bl)}")

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
        analisis = analizar_sentimiento_groq(par, d5, d1h, señal['cambio_24h'], 'monitor', obtener_score_par(par), sig['score'])
        if not analisis or not analisis.get('comprar') or analisis.get('confianza', 0) < 8:
            continue
        monto = calcular_monto_diversificado(historial, capital_disponible)
        if monto == 0:
            continue
        monto = round(monto * 0.7, 2)
        if monto < MONTO_MIN:
            continue
        exito, cantidad, precio = ejecutar_compra(par, monto, d5)
        if exito:
            historial.append({
                'par': par, 'precio_compra': precio, 'precio_maximo': precio,
                'cantidad': cantidad, 'monto': monto,
                'rsi_entrada': d5['rsi'], 'confianza': analisis.get('confianza'),
                'razon': analisis.get('razon'), 'score_entrada': obtener_score_par(par),
                'onchain_score': sig['score'], 'fuentes_monitor': señal['n_fuentes'],
                'en_perdida_desde': None,
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
    modo, tp_actual, sl_actual = obtener_modo_horario()
    hora = datetime.now().hour

    print("="*60)
    print(f"  BOT v4 - Modo: {modo.upper()} ({hora}hs)")
    print("="*60)
    mostrar_resumen()
    print("="*60)

    enviar_reporte_diario()

    with _lock:
        revisar_posiciones(tp_actual, sl_actual)

    # CUT LOSS — revisar posiciones en pérdida prolongada
    revisar_cut_loss()

    historial = cargar_historial()
    posiciones_abiertas = len([p for p in historial if p.get('estado') == 'abierta'])

    if posiciones_abiertas >= MAX_POSICIONES:
        print(f"\nMaximo posiciones ({posiciones_abiertas}/{MAX_POSICIONES})")
        return

    capital_disponible = obtener_capital_disponible()
    print(f"\nCapital: ${capital_disponible:.2f} | Modo: {modo}")

    mejores_pares = obtener_mejores_pares()
    if not mejores_pares:
        return

    pares_en_uso = {p['par'] for p in historial if p.get('estado') == 'abierta'}

    # MONITOR AMPLIO cada 5 ciclos
    MONITOR_CICLO += 1
    if MONITOR_CICLO >= 5:
        MONITOR_CICLO = 0
        try:
            señales = monitor_mercado.escanear()
            if señales and posiciones_abiertas < MAX_POSICIONES:
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
                sig = {"score": 0.0, "action": "NEUTRAL", "block": False}
                try:
                    sig = get_onchain_signal(par)
                    if sig['block']:
                        continue
                except:
                    pass
                datos = obtener_datos_mercado(par)
                if not datos:
                    continue
                monto = calcular_monto_diversificado(historial, capital_disponible)
                monto = round(monto * 0.5, 2)
                if monto < MONTO_MIN:
                    continue
                exito, cantidad, precio = ejecutar_compra(par, monto, datos)
                if exito:
                    historial.append({
                        'par': par, 'precio_compra': precio, 'precio_maximo': precio,
                        'cantidad': cantidad, 'monto': monto, 'rsi_entrada': datos['rsi'],
                        'confianza': 9, 'razon': 'NUEVO LISTING', 'onchain_score': sig['score'],
                        'en_perdida_desde': None,
                        'estado': 'abierta', 'estrategia': 'listing',
                        'fecha': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })
                    guardar_historial(historial)
                    posiciones_abiertas += 1
                    pares_en_uso.add(par)
                    capital_disponible -= monto
        except Exception as e:
            print(f"  Error listings: {e}")

    # SCALPING
    candidatos = filtrar_candidatos(mejores_pares, modo)
    print(f"\n{len(candidatos)} candidatos scalping\n")
    for c in candidatos:
        if posiciones_abiertas >= MAX_POSICIONES:
            break
        if c['par'] in pares_en_uso:
            continue
        par = c['par']
        score = c['score']
        print(f"Analizando {par} | 24h:{c['cambio_24h']}% | Score:{score}")
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
        analisis = analizar_sentimiento_groq(par, d5, d1h, c['cambio_24h'], modo, score, sig['score'])
        if not analisis:
            continue
        confianza_minima = 7 if modo == 'nocturno' else 6
        if sig['action'] == 'SLIGHT_SHORT':
            confianza_minima = min(8, confianza_minima + 1)

        if analisis.get('comprar') and analisis.get('confianza', 0) >= confianza_minima:
            # REBALANCEO para scalping si no hay capital y confianza >= 7
            if capital_disponible < MONTO_MIN:
                liberado = rebalancear_si_necesario(c, tipo='scalp', confianza=analisis.get('confianza', 0))
                if liberado:
                    capital_disponible = obtener_capital_disponible()
                    time.sleep(1)
                else:
                    print(f"  Sin capital y rebalanceo no aplica — saltando")
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
                    'score_entrada': score, 'modo': modo,
                    'onchain_score': sig['score'], 'onchain_action': sig['action'],
                    'en_perdida_desde': None,
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
    enviar_telegram(
        "🤖 <b>Bot Binance v4</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🚀 Thread pump dedicado — ciclo 20s\n"
        "✂️ Cut loss: -0.3% por 5min\n"
        "🔄 Rebalanceo dinámico activo\n"
        "📊 Scalping + Monitor + Listings"
    )

    pump_thread = threading.Thread(target=ciclo_pump_agresivo, daemon=True)
    pump_thread.start()
    print("  [PUMP THREAD] Lanzado ✓")

    while True:
        try:
            main()
        except Exception as e:
            print(f"Error main: {e}")
            enviar_telegram(f"⚠️ Error: {e}")
        time.sleep(120)
