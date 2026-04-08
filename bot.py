import os
import json
import time
import numpy as np
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from binance.client import Client
from groq import Groq

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
HISTORIAL_FILE = "historial_binance.json"
BLACKLIST_FILE = "blacklist.json"
REPORTE_FILE = "ultimo_reporte.json"

client_binance = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)
client_groq = Groq(api_key=GROQ_API_KEY)

def enviar_telegram(mensaje):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "HTML"})
    except:
        pass

def cargar_historial():
    if os.path.exists(HISTORIAL_FILE):
        with open(HISTORIAL_FILE, "r") as f:
            return json.load(f)
    return []

def guardar_historial(historial):
    with open(HISTORIAL_FILE, "w") as f:
        json.dump(historial, f, indent=2)

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
        reporte = f"""📊 <b>REPORTE DIARIO</b> {ayer}
━━━━━━━━━━━━━━━━━━━━
📈 Operaciones: {len(ops_ayer)}
✅ Ganancias: {len(ganancias)} (+{total_g:.2f}%)
🔴 Pérdidas: {len(perdidas)} ({total_p:.2f}%)
💰 Neto del día: {neto:+.2f}%
━━━━━━━━━━━━━━━━━━━━
📦 Acumulado total: {neto_total:+.2f}%
🚫 Pares en blacklist: {len(blacklist)}
━━━━━━━━━━━━━━━━━━━━
🤖 Bot operando normalmente"""
        enviar_telegram(reporte)
        with open(REPORTE_FILE, "w") as f:
            json.dump({'fecha': hoy}, f)
    except Exception as e:
        print(f"Error reporte: {e}")

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

def obtener_datos_mercado(par):
    try:
        klines = client_binance.get_klines(symbol=par, interval='5m', limit=50)
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

def filtrar_candidatos(pares_tickers):
    candidatos = []
    for t in pares_tickers:
        cambio = float(t['priceChangePercent'])
        volumen = float(t['quoteVolume'])
        par = t['symbol']
        if -10 <= cambio <= -1.0 and volumen > 2000000 and not esta_en_blacklist(par):
            candidatos.append({
                'par': par,
                'cambio_24h': cambio,
                'volumen': volumen
            })
    candidatos.sort(key=lambda x: x['cambio_24h'])
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
                    'volumen': volumen
                })
        except:
            continue
        time.sleep(0.1)
    pumps.sort(key=lambda x: x['ratio_volumen'], reverse=True)
    return pumps[:5]

def analizar_con_groq(datos, cambio_24h):
    try:
        rsi = datos['rsi']
        macd = datos['macd']
        signal = datos['macd_signal']
        precio = datos['precio_actual']
        bb_inf = datos['bb_inf']
        cerca_bb_inf = precio <= bb_inf * 1.005
        rsi_sobreventa = rsi < 35
        macd_alcista = macd > signal
        volumen_alto = datos['volumen_ratio'] > 1.2

        prompt = f"""Sos un trader experto en crypto scalping con análisis técnico avanzado.

Par: {datos['par']}
Precio: {precio}
Cambio 1h: {datos['cambio_1h']}%
Cambio 24h: {cambio_24h}%

INDICADORES:
- RSI: {rsi} {'(SOBREVENTA)' if rsi_sobreventa else '(neutral)' if rsi < 50 else '(sobrecompra)'}
- MACD: {'ALCISTA' if macd_alcista else 'BAJISTA'}
- Bollinger: {'CERCA DEL PISO' if cerca_bb_inf else 'zona media'}
- Volumen: {round(datos['volumen_ratio'], 2)}x {'(ALTO)' if volumen_alto else '(normal)'}

Señales positivas: {sum([rsi_sobreventa, macd_alcista, cerca_bb_inf, volumen_alto])}/4

Respondé SOLO con JSON:
{{"comprar": true, "confianza": 8, "razon": "1 linea"}}

Solo recomendá comprar si hay al menos 2 señales positivas."""

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

def ejecutar_compra(par, monto, datos):
    try:
        orden = client_binance.order_market_buy(symbol=par, quoteOrderQty=monto)
        qty = float(orden['executedQty'])
        precio = float(orden['fills'][0]['price']) if orden.get('fills') else obtener_precio(par)
        print(f"   COMPRA OK! {qty} {par} a ${precio}")
        enviar_telegram(f"🟢 <b>COMPRA</b> {par}\n💰 Precio: ${precio}\n📊 RSI: {datos['rsi']} | MACD: {'alcista' if datos['macd'] > datos['macd_signal'] else 'bajista'}\n💵 Monto: ${monto}")
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

def revisar_posiciones():
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
        tp = TAKE_PROFIT_PUMP if estrategia == 'pump' else TAKE_PROFIT

        precio_maximo = float(pos.get('precio_maximo', precio_compra))
        if precio_actual > precio_maximo:
            precio_maximo = precio_actual
            historial[i]['precio_maximo'] = precio_maximo

        caida_desde_maximo = (precio_maximo - precio_actual) / precio_maximo
        ganancia_actual = (precio_actual - precio_compra) / precio_compra
        trailing_activado = ganancia_actual >= TAKE_PROFIT and caida_desde_maximo >= TRAILING_STOP

        print(f"  {pos['par']} [{estrategia}] | Compra: {precio_compra:.4f} | Actual: {precio_actual:.4f} | {pct:+.3f}% | Max: {precio_maximo:.4f}")

        if trailing_activado:
            print(f"  TRAILING STOP! Cayó {caida_desde_maximo*100:.2f}% desde máximo. Ganancia: +{pct}%")
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
        elif cambio <= -STOP_LOSS:
            print(f"  STOP LOSS {pct}%!")
            if ejecutar_venta(pos['par'], pos.get('cantidad', 0), precio_actual, pct, 'perdida'):
                historial[i]['estado'] = 'cerrada_perdida'
                historial[i]['precio_venta'] = precio_actual
                historial[i]['ganancia_pct'] = pct
                historial[i]['fecha_cierre'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cerradas += 1
        else:
            print(f"  Manteniendo... (trailing en {caida_desde_maximo*100:.2f}% desde max)")

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
    print(f"  Ganancias: {len(ganancias)} (+{g_pct:.2f}%) | Perdidas: {len(perdidas)} ({p_pct:.2f}%) | Abiertas: {len(abiertas)} | Neto: {g_pct+p_pct:+.2f}% | Blacklist: {len(blacklist)}")

def main():
    print("="*60)
    print("  BOT ELITE - Pump+Scalping+Trailing+Blacklist+Dinamico")
    print("="*60)
    print(f"  TP: {TAKE_PROFIT*100}% | TP Pump: {TAKE_PROFIT_PUMP*100}% | SL: {STOP_LOSS*100}% | Trail: {TRAILING_STOP*100}%")
    mostrar_resumen()
    print("="*60)

    enviar_reporte_diario()
    revisar_posiciones()

    historial = cargar_historial()
    posiciones_abiertas = len([p for p in historial if p.get('estado') == 'abierta'])

    if posiciones_abiertas >= MAX_POSICIONES:
        print(f"\nMaximo de posiciones abiertas ({MAX_POSICIONES}). Esperando cierres.")
        return

    capital_disponible = obtener_capital_disponible()
    print(f"\nCapital USDT disponible: ${capital_disponible:.2f}")

    if capital_disponible < MONTO_MIN:
        print("Capital insuficiente para operar.")
        return

    print(f"\nEscaneando mercado...")
    mejores_pares = obtener_mejores_pares()
    if not mejores_pares:
        return

    pares_en_uso = {p['par'] for p in historial if p.get('estado') == 'abierta'}

    print(f"\nDetectando pumps...")
    pumps = detectar_pumps(mejores_pares)
    print(f"{len(pumps)} pumps detectados\n")

    for p in pumps:
        if posiciones_abiertas >= MAX_POSICIONES:
            break
        if p['par'] in pares_en_uso:
            continue
        par = p['par']
        print(f"PUMP! {par} | +{p['cambio_5m']}% en 5min | Volumen {p['ratio_volumen']}x")
        datos = obtener_datos_mercado(par)
        if not datos or datos['rsi'] > 72:
            print(f"  RSI muy alto, saltando")
            continue
        monto = calcular_monto_diversificado(historial, capital_disponible)
        if monto == 0:
            print("  Capital insuficiente")
            continue
        print(f"  RSI: {datos['rsi']} OK - ENTRANDO con ${monto}!")
        exito, cantidad, precio = ejecutar_compra(par, monto, datos)
        if exito:
            historial.append({
                'par': par,
                'precio_compra': precio,
                'precio_maximo': precio,
                'cantidad': cantidad,
                'monto': monto,
                'rsi_entrada': datos['rsi'],
                'confianza': 9,
                'razon': f"PUMP +{p['cambio_5m']}% en 5min, volumen {p['ratio_volumen']}x",
                'estado': 'abierta',
                'estrategia': 'pump',
                'fecha': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            guardar_historial(historial)
            posiciones_abiertas += 1
            pares_en_uso.add(par)
            capital_disponible -= monto
            enviar_telegram(f"🚀 <b>PUMP DETECTADO</b> {par}\n📈 +{p['cambio_5m']}% en 5min\n📊 Volumen: {p['ratio_volumen']}x\n💰 Monto: ${monto}")

    candidatos = filtrar_candidatos(mejores_pares)
    print(f"{len(candidatos)} candidatos scalping encontrados\n")

    for c in candidatos:
        if posiciones_abiertas >= MAX_POSICIONES:
            break
        if c['par'] in pares_en_uso:
            continue
        par = c['par']
        print(f"Analizando {par} | Cambio 24h: {c['cambio_24h']}%")
        datos = obtener_datos_mercado(par)
        if not datos:
            continue
        print(f"  RSI: {datos['rsi']} | MACD: {'alcista' if datos['macd'] > datos['macd_signal'] else 'bajista'} | BB: {'cerca piso' if datos['precio_actual'] <= datos['bb_inf'] * 1.005 else 'normal'}")
        analisis = analizar_con_groq(datos, c['cambio_24h'])
        if not analisis:
            continue
        if analisis.get('comprar') and analisis.get('confianza', 0) >= 7:
            monto = calcular_monto_diversificado(historial, capital_disponible)
            if monto == 0:
                print("  Capital insuficiente")
                continue
            print(f"  ENTRADA! Confianza: {analisis['confianza']}/10 | {analisis.get('razon','')} | Monto: ${monto}")
            exito, cantidad, precio = ejecutar_compra(par, monto, datos)
            if exito:
                historial.append({
                    'par': par,
                    'precio_compra': precio,
                    'precio_maximo': precio,
                    'cantidad': cantidad,
                    'monto': monto,
                    'rsi_entrada': datos['rsi'],
                    'confianza': analisis.get('confianza'),
                    'razon': analisis.get('razon'),
                    'estado': 'abierta',
                    'estrategia': 'scalp',
                    'fecha': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
                guardar_historial(historial)
                posiciones_abiertas += 1
                pares_en_uso.add(par)
                capital_disponible -= monto
        else:
            print(f"  Descartado (confianza: {analisis.get('confianza','?')}/10)")
        time.sleep(0.5)

    print(f"\n{'='*60}")
    print(f"  Ciclo: {datetime.now().strftime('%H:%M:%S')}")

if __name__ == "__main__":
    enviar_telegram("🤖 <b>Bot Binance ELITE</b>\n🚀 Pump + Scalping + Trailing + Blacklist + Monto Dinámico\nReporte diario a las 8am 📊")
    while True:
        try:
            main()
        except Exception as e:
            print(f"Error: {e}")
            enviar_telegram(f"⚠️ Error: {e}")
        time.sleep(120)