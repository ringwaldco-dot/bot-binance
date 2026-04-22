# -*- coding: utf-8 -*-
# Bot Futuros Binance — Trading Futuros
# Módulo separado para operar futuros USDT-M en simulación

import os
import json
import time
import threading
import numpy as np
import requests
import warnings
warnings.filterwarnings("ignore")
from datetime import datetime, timedelta
from dotenv import load_dotenv
from binance.client import Client

load_dotenv()

# ============================================================
# CONFIGURACIÓN
# ============================================================

BINANCE_API_KEY   = os.getenv('BINANCE_API_KEY')
BINANCE_SECRET_KEY = os.getenv('BINANCE_SECRET_KEY')
TELEGRAM_TOKEN    = "8513198629:AAHmlayu6y_Z2e2SUCkvKkLIEhj6kstxYT4"
TELEGRAM_CHAT_ID  = "1576867878"

# Paper trading
PAPER_BALANCE     = 1000.0    # USDT virtuales
APALANCAMIENTO    = 5         # 5x — conservador para empezar
MONTO_POR_TRADE   = 200.0     # USDT por posición (con apalancamiento = $1000 de exposición)
MAX_POSICIONES    = 2         # máximo 2 posiciones abiertas simultáneas
STOP_LOSS         = 0.025     # -2.5% sobre precio (con 5x = -12.5% del margen)
TAKE_PROFIT       = 0.030     # +3% sobre precio (con 5x = +15% del margen)
TRAILING_TRIGGER  = 0.020     # activar trailing cuando gana +2%
TRAILING_DIST     = 0.010     # distancia del trailing +1%
CICLO_SCAN        = 20        # segundos entre scans
CICLO_POSICIONES  = 10        # segundos entre revisión de posiciones

PAPER_FILE        = "paper_futuros.json"

# Pares que operamos — los más líquidos de futuros
PARES_FUTUROS = [
    'BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'SOLUSDT', 'XRPUSDT',
    'DOGEUSDT', 'ADAUSDT', 'AVAXUSDT', 'LINKUSDT', 'DOTUSDT',
    'MATICUSDT', 'LTCUSDT', 'ATOMUSDT', 'NEARUSDT', 'APTUSDT',
    'ARBUSDT', 'OPUSDT', 'INJUSDT', 'SUIUSDT', 'SEIUSDT'
]

client = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)

# ============================================================
# TELEGRAM
# ============================================================

LAST_UPDATE_ID = 0

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
# PAPER FUTUROS — PERSISTENCIA
# ============================================================

def cargar_paper():
    if os.path.exists(PAPER_FILE):
        with open(PAPER_FILE) as f:
            return json.load(f)
    return {
        "balance": PAPER_BALANCE,
        "balance_inicial": PAPER_BALANCE,
        "posiciones": [],
        "historial": [],
        "margen_usado": 0.0
    }

def guardar_paper(data):
    with open(PAPER_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def paper_stats():
    data = cargar_paper()
    historial = data.get("historial", [])
    posiciones = data.get("posiciones", [])
    balance = data.get("balance", PAPER_BALANCE)
    balance_inicial = data.get("balance_inicial", PAPER_BALANCE)

    # PnL no realizado de posiciones abiertas
    pnl_abierto = 0
    for pos in posiciones:
        p_actual = precio_futuros(pos['par'])
        if p_actual:
            if pos['direccion'] == 'LONG':
                pnl = (p_actual - pos['precio_entrada']) / pos['precio_entrada'] * pos['monto'] * APALANCAMIENTO
            else:
                pnl = (pos['precio_entrada'] - p_actual) / pos['precio_entrada'] * pos['monto'] * APALANCAMIENTO
            pnl_abierto += pnl

    balance_total = balance + pnl_abierto
    rendimiento = ((balance_total - balance_inicial) / balance_inicial) * 100

    ganancias = [op for op in historial if op.get('pnl_usdt', 0) > 0]
    perdidas  = [op for op in historial if op.get('pnl_usdt', 0) <= 0]
    win_rate  = (len(ganancias) / len(historial) * 100) if historial else 0
    pnl_total = sum(op.get('pnl_usdt', 0) for op in historial)

    return {
        "balance_libre": round(balance, 2),
        "pnl_abierto": round(pnl_abierto, 4),
        "balance_total": round(balance_total, 2),
        "balance_inicial": balance_inicial,
        "rendimiento": round(rendimiento, 2),
        "ops_totales": len(historial),
        "ganancias": len(ganancias),
        "perdidas": len(perdidas),
        "win_rate": round(win_rate, 1),
        "pnl_total_usdt": round(pnl_total, 4),
        "posiciones_abiertas": len(posiciones),
        "margen_usado": round(data.get("margen_usado", 0), 2),
    }

# ============================================================
# PRECIO FUTUROS
# ============================================================

def precio_futuros(par):
    try:
        r = requests.get(
            f"https://fapi.binance.com/fapi/v1/ticker/price?symbol={par}",
            timeout=5
        )
        return float(r.json()['price'])
    except:
        return None

def klines_futuros(par, interval='5m', limit=50):
    try:
        r = requests.get(
            f"https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": par, "interval": interval, "limit": limit},
            timeout=8
        )
        return r.json()
    except:
        return []

def funding_rate(par):
    try:
        r = requests.get(
            f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={par}",
            timeout=5
        )
        return float(r.json().get('lastFundingRate', 0))
    except:
        return 0

def open_interest(par):
    """Open interest — cuánto capital hay en posiciones abiertas."""
    try:
        r = requests.get(
            f"https://fapi.binance.com/fapi/v1/openInterest?symbol={par}",
            timeout=5
        )
        return float(r.json().get('openInterest', 0))
    except:
        return 0

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

def calcular_macd(precios):
    if len(precios) < 26:
        return 0, 0
    ema12 = calcular_ema(precios, 12)
    ema26 = calcular_ema(precios, 26)
    macd = ema12 - ema26
    signal = calcular_ema([macd] * 9, 9)
    return macd, signal

def calcular_bollinger(precios, periodo=20):
    if len(precios) < periodo:
        return precios[-1], precios[-1], precios[-1]
    ultimos = precios[-periodo:]
    media = np.mean(ultimos)
    std = np.std(ultimos)
    return media + 2 * std, media, media - 2 * std

# ============================================================
# ANÁLISIS DE SEÑAL
# ============================================================

def analizar_par(par):
    """
    Analiza un par de futuros y determina si hay señal de LONG, SHORT o nada.
    Retorna: {'direccion': 'LONG'/'SHORT'/None, 'confianza': 0-10, 'razon': str}
    """
    try:
        klines = klines_futuros(par, '5m', 60)
        if len(klines) < 30:
            return None

        precios   = [float(k[4]) for k in klines]  # close
        maximos   = [float(k[2]) for k in klines]  # high
        minimos   = [float(k[3]) for k in klines]  # low
        volumenes = [float(k[5]) for k in klines]

        rsi = calcular_rsi(precios)
        macd, signal = calcular_macd(precios)
        bb_upper, bb_mid, bb_lower = calcular_bollinger(precios)
        p_actual = precios[-1]

        # Cambios de precio
        c5m  = ((precios[-1] - precios[-2])  / precios[-2])  * 100
        c15m = ((precios[-1] - precios[-4])  / precios[-4])  * 100
        c1h  = ((precios[-1] - precios[-13]) / precios[-13]) * 100

        # Volumen relativo
        vol_prom = np.mean(volumenes[:-3])
        vol_ratio = np.mean(volumenes[-3:]) / vol_prom if vol_prom > 0 else 1

        # Funding rate — negativo = muchos shorts = posible squeeze al alza
        fr = funding_rate(par)

        score_long  = 0
        score_short = 0
        razones_long  = []
        razones_short = []

        # --- SEÑALES LONG ---
        if rsi < 35:
            score_long += 2
            razones_long.append(f"RSI sobrevendido {rsi:.0f}")
        elif rsi < 45:
            score_long += 1
            razones_long.append(f"RSI bajo {rsi:.0f}")

        if macd > signal and macd > 0:
            score_long += 2
            razones_long.append("MACD alcista")
        elif macd > signal:
            score_long += 1
            razones_long.append("MACD cruzando al alza")

        if p_actual <= bb_lower * 1.005:
            score_long += 2
            razones_long.append("Precio en banda inferior BB")

        if c15m > 0.5 and vol_ratio > 2:
            score_long += 1
            razones_long.append(f"Momentum +{c15m:.1f}% vol {vol_ratio:.1f}x")

        if fr < -0.001:
            score_long += 1
            razones_long.append(f"Funding negativo {fr*100:.3f}% (shorts dominan)")

        if c1h > 1.0 and c15m > 0:
            score_long += 1
            razones_long.append("Tendencia 1h alcista")

        # --- SEÑALES SHORT ---
        if rsi > 70:
            score_short += 2
            razones_short.append(f"RSI sobrecomprado {rsi:.0f}")
        elif rsi > 60:
            score_short += 1
            razones_short.append(f"RSI alto {rsi:.0f}")

        if macd < signal and macd < 0:
            score_short += 2
            razones_short.append("MACD bajista")
        elif macd < signal:
            score_short += 1
            razones_short.append("MACD cruzando a la baja")

        if p_actual >= bb_upper * 0.995:
            score_short += 2
            razones_short.append("Precio en banda superior BB")

        if c15m < -0.5 and vol_ratio > 2:
            score_short += 1
            razones_short.append(f"Caída -{abs(c15m):.1f}% vol {vol_ratio:.1f}x")

        if fr > 0.001:
            score_short += 1
            razones_short.append(f"Funding positivo {fr*100:.3f}% (longs dominan)")

        if c1h < -1.0 and c15m < 0:
            score_short += 1
            razones_short.append("Tendencia 1h bajista")

        # Necesitamos al menos score 4 para entrar
        if score_long >= 4 and score_long > score_short:
            return {
                'direccion': 'LONG',
                'confianza': min(score_long, 10),
                'precio': p_actual,
                'rsi': round(rsi, 1),
                'vol_ratio': round(vol_ratio, 2),
                'razon': ' | '.join(razones_long[:3])
            }
        elif score_short >= 4 and score_short > score_long:
            return {
                'direccion': 'SHORT',
                'confianza': min(score_short, 10),
                'precio': p_actual,
                'rsi': round(rsi, 1),
                'vol_ratio': round(vol_ratio, 2),
                'razon': ' | '.join(razones_short[:3])
            }

        return None

    except Exception as e:
        print(f"  [FUT] Error analizando {par}: {e}")
        return None

# ============================================================
# OPERACIONES PAPER
# ============================================================

def abrir_posicion(par, direccion, señal):
    """Abre una posición simulada de futuros."""
    data = cargar_paper()
    margen = MONTO_POR_TRADE
    exposicion = margen * APALANCAMIENTO

    if data['balance'] < margen:
        print(f"  [FUT] Sin balance suficiente (${data['balance']:.2f} < ${margen})")
        return False

    precio_entrada = señal['precio']
    # Calcular niveles
    if direccion == 'LONG':
        sl = precio_entrada * (1 - STOP_LOSS)
        tp = precio_entrada * (1 + TAKE_PROFIT)
    else:
        sl = precio_entrada * (1 + STOP_LOSS)
        tp = precio_entrada * (1 - TAKE_PROFIT)

    data['balance'] = round(data['balance'] - margen, 4)
    data['margen_usado'] = round(data.get('margen_usado', 0) + margen, 4)
    data['posiciones'].append({
        'par': par,
        'direccion': direccion,
        'precio_entrada': precio_entrada,
        'precio_maximo': precio_entrada if direccion == 'LONG' else None,
        'precio_minimo': precio_entrada if direccion == 'SHORT' else None,
        'sl': sl,
        'tp': tp,
        'margen': margen,
        'exposicion': exposicion,
        'apalancamiento': APALANCAMIENTO,
        'trailing_activo': False,
        'trailing_precio': None,
        'fecha': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })
    guardar_paper(data)

    emoji = '🟢' if direccion == 'LONG' else '🔴'
    tg(
        f"📄 {emoji} <b>[TRADING FUTUROS] {direccion}</b> {par}\n"
        f"💰 Entrada: ${precio_entrada:.4f}\n"
        f"📊 Margen: ${margen} | Exposición: ${exposicion} ({APALANCAMIENTO}x)\n"
        f"🛑 SL: ${sl:.4f} | 🎯 TP: ${tp:.4f}\n"
        f"📈 {señal['razon']}\n"
        f"💵 Balance libre: ${data['balance']:.2f}"
    )
    return True

def cerrar_posicion(pos, razon, precio_cierre=None):
    """Cierra una posición simulada y calcula PnL."""
    data = cargar_paper()
    pos_idx = next((i for i, p in enumerate(data['posiciones']) if p['par'] == pos['par'] and p['fecha'] == pos['fecha']), None)
    if pos_idx is None:
        return False

    if not precio_cierre:
        precio_cierre = precio_futuros(pos['par'])
    if not precio_cierre:
        return False

    precio_entrada = pos['precio_entrada']
    margen = pos['margen']
    apal = pos['apalancamiento']

    if pos['direccion'] == 'LONG':
        pct = ((precio_cierre - precio_entrada) / precio_entrada) * 100
        pnl_usdt = (precio_cierre - precio_entrada) / precio_entrada * margen * apal
    else:
        pct = ((precio_entrada - precio_cierre) / precio_entrada) * 100
        pnl_usdt = (precio_entrada - precio_cierre) / precio_entrada * margen * apal

    # Devolver margen + PnL al balance
    data['balance'] = round(data['balance'] + margen + pnl_usdt, 4)
    data['margen_usado'] = round(max(0, data.get('margen_usado', 0) - margen), 4)
    data['posiciones'].pop(pos_idx)
    data['historial'].append({
        'par': pos['par'],
        'direccion': pos['direccion'],
        'precio_entrada': precio_entrada,
        'precio_cierre': precio_cierre,
        'margen': margen,
        'apalancamiento': apal,
        'pct': round(pct, 3),
        'pnl_usdt': round(pnl_usdt, 4),
        'razon_cierre': razon,
        'fecha_apertura': pos['fecha'],
        'fecha_cierre': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })
    guardar_paper(data)

    emoji = '✅' if pnl_usdt > 0 else '🔴'
    liquidada = ' ⚠️ LIQUIDADA' if razon == 'liquidacion' else ''
    tg(
        f"📄 {emoji} <b>[TRADING FUTUROS] CIERRE{liquidada}</b> {pos['par']} {pos['direccion']}\n"
        f"📊 {pct:+.2f}% | {pnl_usdt:+.4f} USDT\n"
        f"💰 Entrada: ${precio_entrada:.4f} → Cierre: ${precio_cierre:.4f}\n"
        f"📝 Razón: {razon}\n"
        f"💵 Balance: ${data['balance']:.2f}"
    )
    return True

# ============================================================
# GESTIÓN DE POSICIONES ABIERTAS
# ============================================================

def revisar_posiciones():
    """Revisa SL, TP, trailing y liquidaciones de posiciones abiertas."""
    data = cargar_paper()
    posiciones = data.get('posiciones', [])
    if not posiciones:
        return

    ahora = datetime.now()
    for pos in list(posiciones):
        p_actual = precio_futuros(pos['par'])
        if not p_actual:
            continue

        precio_entrada = pos['precio_entrada']
        apal = pos['apalancamiento']

        if pos['direccion'] == 'LONG':
            pct = ((p_actual - precio_entrada) / precio_entrada) * 100
            pnl_pct_margen = pct * apal

            # Actualizar precio máximo para trailing
            if p_actual > pos.get('precio_maximo', precio_entrada):
                data = cargar_paper()
                for pp in data['posiciones']:
                    if pp['par'] == pos['par'] and pp['fecha'] == pos['fecha']:
                        pp['precio_maximo'] = p_actual
                guardar_paper(data)
                pos['precio_maximo'] = p_actual

            # Liquidación — perdió más del 90% del margen
            if pnl_pct_margen <= -90:
                cerrar_posicion(pos, 'liquidacion', p_actual)
                continue

            # Stop loss
            if p_actual <= pos['sl']:
                cerrar_posicion(pos, f"stop_loss {pct:+.2f}%", p_actual)
                continue

            # Take profit
            if p_actual >= pos['tp']:
                cerrar_posicion(pos, f"take_profit {pct:+.2f}%", p_actual)
                continue

            # Trailing stop — activar cuando llega al trigger
            if pct >= TRAILING_TRIGGER * 100:
                precio_maximo = pos.get('precio_maximo', precio_entrada)
                trailing_sl = precio_maximo * (1 - TRAILING_DIST)
                if p_actual <= trailing_sl:
                    cerrar_posicion(pos, f"trailing {pct:+.2f}%", p_actual)
                    continue

        else:  # SHORT
            pct = ((precio_entrada - p_actual) / precio_entrada) * 100
            pnl_pct_margen = pct * apal

            # Actualizar precio mínimo para trailing
            if p_actual < pos.get('precio_minimo', precio_entrada):
                data = cargar_paper()
                for pp in data['posiciones']:
                    if pp['par'] == pos['par'] and pp['fecha'] == pos['fecha']:
                        pp['precio_minimo'] = p_actual
                guardar_paper(data)
                pos['precio_minimo'] = p_actual

            # Liquidación
            if pnl_pct_margen <= -90:
                cerrar_posicion(pos, 'liquidacion', p_actual)
                continue

            # Stop loss
            if p_actual >= pos['sl']:
                cerrar_posicion(pos, f"stop_loss {pct:+.2f}%", p_actual)
                continue

            # Take profit
            if p_actual <= pos['tp']:
                cerrar_posicion(pos, f"take_profit {pct:+.2f}%", p_actual)
                continue

            # Trailing stop
            if pct >= TRAILING_TRIGGER * 100:
                precio_minimo = pos.get('precio_minimo', precio_entrada)
                trailing_sl = precio_minimo * (1 + TRAILING_DIST)
                if p_actual >= trailing_sl:
                    cerrar_posicion(pos, f"trailing {pct:+.2f}%", p_actual)
                    continue

        print(f"  [FUT] {pos['par']} {pos['direccion']} {pct:+.2f}% (margen {pnl_pct_margen:+.1f}%)")

# ============================================================
# SCANNER DE SEÑALES
# ============================================================

def escanear_mercado():
    """Escanea los pares y busca señales de entrada."""
    data = cargar_paper()
    posiciones_abiertas = len(data.get('posiciones', []))
    pares_en_uso = {p['par'] for p in data.get('posiciones', [])}

    if posiciones_abiertas >= MAX_POSICIONES:
        return

    if data['balance'] < MONTO_POR_TRADE:
        print(f"  [FUT] Sin balance (${data['balance']:.2f})")
        return

    print(f"\n  [FUT] Escaneando {len(PARES_FUTUROS)} pares...")
    señales = []

    for par in PARES_FUTUROS:
        if par in pares_en_uso:
            continue
        señal = analizar_par(par)
        if señal:
            señal['par'] = par
            señales.append(señal)
            print(f"  [FUT] ✨ {par} {señal['direccion']} confianza:{señal['confianza']} — {señal['razon']}")
        time.sleep(0.1)

    if not señales:
        print(f"  [FUT] Sin señales")
        return

    # Ordenar por confianza y tomar la mejor
    señales.sort(key=lambda x: x['confianza'], reverse=True)
    mejor = señales[0]

    tg(f"🔍 <b>[TRADING FUTUROS]</b> Señal detectada: {mejor['par']} {mejor['direccion']}\n{mejor['razon']}\nConfianza: {mejor['confianza']}/10")
    abrir_posicion(mejor['par'], mejor['direccion'], mejor)

# ============================================================
# COMANDOS TELEGRAM
# ============================================================

def procesar_comandos():
    global LAST_UPDATE_ID
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": LAST_UPDATE_ID + 1, "timeout": 5},
            timeout=10
        )
        updates = r.json().get('result', [])
        for update in updates:
            LAST_UPDATE_ID = update['update_id']
            msg = update.get('message', {})
            chat_id = str(msg.get('chat', {}).get('id', ''))
            texto = msg.get('text', '').strip().lower()

            if chat_id != TELEGRAM_CHAT_ID:
                continue

            if texto == '/fut_stats':
                stats = paper_stats()
                data = cargar_paper()
                historial = data.get('historial', [])
                ultimas = historial[-5:] if historial else []
                ops_str = ""
                for op in reversed(ultimas):
                    emoji = "✅" if op.get('pnl_usdt', 0) > 0 else "🔴"
                    dir_emoji = "📈" if op.get('direccion') == 'LONG' else "📉"
                    ops_str += f"\n{emoji} {dir_emoji} {op['par']} {op.get('pct', 0):+.2f}% ({op.get('pnl_usdt', 0):+.2f} USDT)"
                tg(
                    f"📄 <b>Trading Futuros — Resultados</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"💵 Balance inicial: ${stats['balance_inicial']:.2f}\n"
                    f"💰 Balance actual: ${stats['balance_total']:.2f}\n"
                    f"📈 Rendimiento: {stats['rendimiento']:+.2f}%\n"
                    f"🔒 Margen en uso: ${stats['margen_usado']:.2f}\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"🔢 Operaciones: {stats['ops_totales']}\n"
                    f"✅ Ganancias: {stats['ganancias']}\n"
                    f"🔴 Pérdidas: {stats['perdidas']}\n"
                    f"🎯 Win rate: {stats['win_rate']:.1f}%\n"
                    f"💵 P&L total: {stats['pnl_total_usdt']:+.2f} USDT\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📂 Posiciones abiertas: {stats['posiciones_abiertas']}\n"
                    + (f"\n<b>Últimas ops:</b>{ops_str}" if ops_str else "")
                )

            elif texto == '/fut_estado':
                data = cargar_paper()
                posiciones = data.get('posiciones', [])
                stats = paper_stats()
                pos_str = ""
                for p in posiciones:
                    p_actual = precio_futuros(p['par'])
                    if p_actual:
                        if p['direccion'] == 'LONG':
                            pct = ((p_actual - p['precio_entrada']) / p['precio_entrada']) * 100
                        else:
                            pct = ((p['precio_entrada'] - p_actual) / p['precio_entrada']) * 100
                        pnl = pct * p['apalancamiento']
                        dir_emoji = "📈" if p['direccion'] == 'LONG' else "📉"
                        pos_str += f"\n{dir_emoji} {p['par']} {p['direccion']} {pct:+.2f}% (margen {pnl:+.1f}%)"
                tg(
                    f"📄 <b>[TRADING FUTUROS] Estado</b>\n"
                    f"💵 Balance libre: ${stats['balance_libre']:.2f}\n"
                    f"📊 PnL abierto: ${stats['pnl_abierto']:+.4f}\n"
                    f"💰 Total: ${stats['balance_total']:.2f} ({stats['rendimiento']:+.2f}%)\n"
                    f"📂 Posiciones: {stats['posiciones_abiertas']}"
                    + (pos_str if pos_str else "\nSin posiciones abiertas")
                )

            elif texto == '/fut_cerrar':
                data = cargar_paper()
                posiciones = list(data.get('posiciones', []))
                if not posiciones:
                    tg("📄 [TRADING FUTUROS] Sin posiciones abiertas")
                    continue
                cerradas = 0
                for pos in posiciones:
                    if cerrar_posicion(pos, 'comando_manual'):
                        cerradas += 1
                tg(f"📄 ✅ [TRADING FUTUROS] Cerradas {cerradas} posiciones")

            elif texto == '/fut_reset':
                guardar_paper({
                    "balance": PAPER_BALANCE,
                    "balance_inicial": PAPER_BALANCE,
                    "posiciones": [],
                    "historial": [],
                    "margen_usado": 0.0
                })
                tg(f"🔄 Trading Futuros reseteado — Balance: ${PAPER_BALANCE:.2f} USDT")

            elif texto == '/ayuda' or texto == '/fut_ayuda':
                tg(
                    "📄 <b>Comandos Trading Futuros:</b>\n\n"
                    "/fut_estado — posiciones abiertas y balance\n"
                    "/fut_stats — estadísticas completas\n"
                    "/fut_cerrar — cerrar todas las posiciones\n"
                    "/fut_reset — reiniciar simulador\n"
                    "/fut_ayuda — esta lista\n\n"
                    f"⚙️ Config: {APALANCAMIENTO}x | SL {STOP_LOSS*100:.1f}% | TP {TAKE_PROFIT*100:.1f}% | ${MONTO_POR_TRADE}/trade"
                )
    except Exception as e:
        print(f"  [FUT CMD] Error: {e}")

# ============================================================
# REPORTE DIARIO
# ============================================================

def reporte_diario():
    """Envía reporte a las 8am UTC."""
    ultima_hora = -1
    while True:
        hora = datetime.utcnow().hour
        if hora == 8 and hora != ultima_hora:
            ultima_hora = hora
            stats = paper_stats()
            tg(
                f"📄 <b>Reporte diario — Trading Futuros</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 Balance: ${stats['balance_total']:.2f} ({stats['rendimiento']:+.2f}%)\n"
                f"🔢 Ops totales: {stats['ops_totales']}\n"
                f"🎯 Win rate: {stats['win_rate']:.1f}%\n"
                f"💵 P&L: {stats['pnl_total_usdt']:+.2f} USDT\n"
                f"📂 Posiciones abiertas: {stats['posiciones_abiertas']}"
            )
        time.sleep(3600)

# ============================================================
# THREADS
# ============================================================

def thread_scanner():
    """Escanea el mercado cada CICLO_SCAN segundos."""
    print("  [FUT] Thread scanner iniciado ✓")
    while True:
        try:
            escanear_mercado()
        except Exception as e:
            print(f"  [FUT] Error scanner: {e}")
        time.sleep(CICLO_SCAN)

def thread_posiciones():
    """Revisa posiciones cada CICLO_POSICIONES segundos."""
    print("  [FUT] Thread posiciones iniciado ✓")
    while True:
        try:
            revisar_posiciones()
        except Exception as e:
            print(f"  [FUT] Error posiciones: {e}")
        time.sleep(CICLO_POSICIONES)

def thread_comandos():
    """Escucha comandos de Telegram cada 5 segundos."""
    print("  [FUT] Thread comandos iniciado ✓")
    while True:
        try:
            procesar_comandos()
        except:
            pass
        time.sleep(5)

# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    tg(
        f"🚀 <b>Bot Trading Futuros</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 Balance: ${PAPER_BALANCE} USDT\n"
        f"⚡ Apalancamiento: {APALANCAMIENTO}x\n"
        f"📊 Max posiciones: {MAX_POSICIONES}\n"
        f"💰 Monto por trade: ${MONTO_POR_TRADE} (exposición ${MONTO_POR_TRADE * APALANCAMIENTO})\n"
        f"🛑 Stop loss: {STOP_LOSS*100:.1f}% | 🎯 Take profit: {TAKE_PROFIT*100:.1f}%\n"
        f"📈 Trailing: activa desde +{TRAILING_TRIGGER*100:.1f}%\n"
        f"🔄 Scan: cada {CICLO_SCAN}s\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Comandos: /fut_estado /fut_stats /fut_ayuda"
    )
    threading.Thread(target=thread_scanner, daemon=True).start()
    threading.Thread(target=thread_posiciones, daemon=True).start()
    # threading.Thread(target=thread_comandos, daemon=True).start()  # Comandos manejados por bot.py
    threading.Thread(target=reporte_diario, daemon=True).start()
    print("  [FUT] Todos los threads iniciados ✓")

    # Loop principal — mantener vivo
    while True:
        time.sleep(60)
