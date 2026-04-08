import os
import json
import time
from datetime import datetime
from dotenv import load_dotenv
from binance.client import Client
from groq import Groq

load_dotenv()

BINANCE_API_KEY = os.getenv('BINANCE_API_KEY')
BINANCE_SECRET_KEY = os.getenv('BINANCE_SECRET_KEY')
GROQ_API_KEY = os.getenv('GROQ_API_KEY')

MONTO_POR_ORDEN = 5.0
TAKE_PROFIT = 0.012
STOP_LOSS = 0.008
MAX_POSICIONES = 1
HISTORIAL_FILE = "historial_binance.json"

client_binance = Client(BINANCE_API_KEY, BINANCE_SECRET_KEY)
client_groq = Groq(api_key=GROQ_API_KEY)

def cargar_historial():
    if os.path.exists(HISTORIAL_FILE):
        with open(HISTORIAL_FILE, "r") as f:
            return json.load(f)
    return []

def guardar_historial(historial):
    with open(HISTORIAL_FILE, "w") as f:
        json.dump(historial, f, indent=2)

def obtener_precio(par):
    try:
        ticker = client_binance.get_symbol_ticker(symbol=par)
        return float(ticker['price'])
    except:
        return None

def obtener_mejores_pares():
    try:
        tickers = client_binance.get_ticker()
        usdt_pares = [
            t for t in tickers
            if t['symbol'].endswith('USDT')
            and float(t['quoteVolume']) > 1000000
            and float(t['lastPrice']) > 0.001
            and float(t['lastPrice']) < 1000
        ]
        usdt_pares.sort(key=lambda x: float(x['quoteVolume']), reverse=True)
        return usdt_pares[:60]
    except Exception as e:
        print(f"Error obteniendo pares: {e}")
        return []

def obtener_datos_mercado(par):
    try:
        klines = client_binance.get_klines(symbol=par, interval='5m', limit=12)
        precios = [float(k[4]) for k in klines]
        precio_actual = precios[-1]
        precio_hace_1h = precios[0]
        cambio_1h = ((precio_actual - precio_hace_1h) / precio_hace_1h) * 100
        maximo = max(precios)
        minimo = min(precios)
        volumen = sum(float(k[5]) for k in klines[-3:])
        return {
            'par': par,
            'precio_actual': precio_actual,
            'cambio_1h': round(cambio_1h, 3),
            'maximo': maximo,
            'minimo': minimo,
            'volumen': volumen,
            'precios': precios
        }
    except:
        return None

def filtrar_candidatos(pares_tickers):
    candidatos = []
    for t in pares_tickers:
        cambio = float(t['priceChangePercent'])
        precio = float(t['lastPrice'])
        volumen = float(t['quoteVolume'])
        par = t['symbol']
        # Buscar caídas recientes con buen volumen
        if -8 <= cambio <= -1.5 and volumen > 2000000:
            candidatos.append({
                'par': par,
                'cambio_24h': cambio,
                'precio': precio,
                'volumen': volumen
            })
    candidatos.sort(key=lambda x: x['cambio_24h'])
    return candidatos[:10]

def analizar_con_groq(datos, cambio_24h):
    try:
        prompt = f"""Sos un trader experto en crypto scalping.
Analizá si este par tiene alta probabilidad de rebote en los proximos minutos.

Par: {datos['par']}
Precio actual: {datos['precio_actual']}
Cambio ultima hora: {datos['cambio_1h']}%
Cambio 24h: {cambio_24h}%
Maximo 1h: {datos['maximo']}
Minimo 1h: {datos['minimo']}
Precio cerca del minimo: {'SI' if datos['precio_actual'] <= datos['minimo'] * 1.003 else 'NO'}

Estrategia: comprar caidas para vender en rebote de 1-2%.

Respondé SOLO con JSON sin texto extra:
{{"comprar": true, "confianza": 8, "razon": "breve explicacion"}}

Si no hay ventaja clara, pon "comprar": false."""

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

def ejecutar_compra(par, monto):
    try:
        orden = client_binance.order_market_buy(symbol=par, quoteOrderQty=monto)
        qty = float(orden['executedQty'])
        precio = float(orden['fills'][0]['price']) if orden.get('fills') else obtener_precio(par)
        print(f"   COMPRA OK! {qty} {par} a ${precio}")
        return True, qty, precio
    except Exception as e:
        print(f"   Error comprando: {e}")
        return False, 0, 0

def ejecutar_venta(par, cantidad):
    try:
        # Redondear cantidad segun reglas de Binance
        info = client_binance.get_symbol_info(par)
        step = next(f['stepSize'] for f in info['filters'] if f['filterType'] == 'LOT_SIZE')
        decimales = len(step.rstrip('0').split('.')[-1]) if '.' in step else 0
        cantidad = round(cantidad, decimales)
        orden = client_binance.order_market_sell(symbol=par, quantity=cantidad)
        print(f"   VENTA OK! ID: {orden['orderId']}")
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
        print(f"  {pos['par']} | Compra: {precio_compra} | Actual: {precio_actual} | {pct:+.3f}%")
        if cambio >= TAKE_PROFIT:
            print(f"  TAKE PROFIT +{pct}% VENDIENDO!")
            if ejecutar_venta(pos['par'], pos.get('cantidad', 0)):
                historial[i]['estado'] = 'cerrada_ganancia'
                historial[i]['precio_venta'] = precio_actual
                historial[i]['ganancia_pct'] = pct
                historial[i]['fecha_cierre'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cerradas += 1
        elif cambio <= -STOP_LOSS:
            print(f"  STOP LOSS {pct}% VENDIENDO!")
            if ejecutar_venta(pos['par'], pos.get('cantidad', 0)):
                historial[i]['estado'] = 'cerrada_perdida'
                historial[i]['precio_venta'] = precio_actual
                historial[i]['ganancia_pct'] = pct
                historial[i]['fecha_cierre'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cerradas += 1
        else:
            print(f"  Manteniendo...")
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
    neto = g_pct + p_pct
    print(f"  Ganancias: {len(ganancias)} (+{g_pct:.2f}%) | Perdidas: {len(perdidas)} ({p_pct:.2f}%) | Abiertas: {len(abiertas)} | Neto: {neto:+.2f}%")

def main():
    print("="*60)
    print("  BOT SCALPING INTELIGENTE - Binance + Groq")
    print("="*60)
    print(f"  Take Profit: {TAKE_PROFIT*100}% | Stop Loss: {STOP_LOSS*100}%")
    print(f"  Monto: ${MONTO_POR_ORDEN} | Max posiciones: {MAX_POSICIONES}")
    mostrar_resumen()
    print("="*60)

    revisar_posiciones()

    historial = cargar_historial()
    posiciones_abiertas = len([p for p in historial if p.get('estado') == 'abierta'])

    if posiciones_abiertas >= MAX_POSICIONES:
        print(f"\nMaximo de posiciones abiertas. Esperando cierres.")
        return

    print(f"\nEscaneando mercado Binance en tiempo real...")
    mejores_pares = obtener_mejores_pares()
    if not mejores_pares:
        print("No se pudo obtener datos del mercado.")
        return

    candidatos = filtrar_candidatos(mejores_pares)
    print(f"{len(candidatos)} candidatos con caidas recientes encontrados\n")

    if not candidatos:
        print("No hay caidas interesantes ahora. Esperando...")
        return

    for c in candidatos:
        if posiciones_abiertas >= MAX_POSICIONES:
            break

        par = c['par']
        print(f"Analizando {par} | Cambio 24h: {c['cambio_24h']}% | Vol: ${c['volumen']:,.0f}")

        datos = obtener_datos_mercado(par)
        if not datos:
            continue

        analisis = analizar_con_groq(datos, c['cambio_24h'])
        if not analisis:
            continue

        if analisis.get('comprar') and analisis.get('confianza', 0) >= 7:
            print(f"  ENTRADA! Confianza: {analisis['confianza']}/10 | {analisis.get('razon','')}")
            exito, cantidad, precio = ejecutar_compra(par, MONTO_POR_ORDEN)
            if exito:
                historial.append({
                    'par': par,
                    'precio_compra': precio,
                    'cantidad': cantidad,
                    'monto': MONTO_POR_ORDEN,
                    'confianza': analisis.get('confianza'),
                    'razon': analisis.get('razon'),
                    'estado': 'abierta',
                    'fecha': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
                guardar_historial(historial)
                posiciones_abiertas += 1
        else:
            print(f"  Descartado (confianza: {analisis.get('confianza','?')}/10)")

        time.sleep(0.3)

    print(f"\n{'='*60}")
    print(f"  Ciclo: {datetime.now().strftime('%H:%M:%S')}")

if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as e:
            print(f"Error: {e}")
        time.sleep(120)