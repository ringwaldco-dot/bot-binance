"""
market_monitor.py
=================
Monitor amplio de mercado cripto — anti-bait por diseño.
Fuentes: Telegram channels, Reddit, CoinGecko trending, Binance volumen.

Una señal solo se considera REAL si:
  1. La mencionan 2+ fuentes independientes
  2. Hay volumen real en Binance que lo confirme
  3. Groq analiza el texto y no detecta patrones de pump artificial
  4. El coin tiene par USDT con liquidez suficiente en Binance

Uso:
    python market_monitor.py          # corre standalone
    from market_monitor import MonitorMercado
    monitor = MonitorMercado()
    señales = monitor.escanear()      # retorna lista de señales verificadas
"""

import os
import json
import time
import logging
import requests
import re
from datetime import datetime, timedelta
from collections import defaultdict
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

GROQ_API_KEY      = os.getenv('GROQ_API_KEY')
TELEGRAM_TOKEN    = os.getenv('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID  = os.getenv('TELEGRAM_CHAT_ID', '')
TELEGRAM_API_ID   = os.getenv('TELEGRAM_API_ID')
TELEGRAM_API_HASH = os.getenv('TELEGRAM_API_HASH')

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; trading-bot/1.0)"}
SEÑALES_FILE = "señales_monitor.json"

client_groq = Groq(api_key=GROQ_API_KEY)

logger = logging.getLogger(__name__)

# ─── Canales de Telegram públicos a monitorear ───────────────────────────────
# Solo canales públicos que no requieren unirse manualmente
TELEGRAM_CHANNELS = [
    "https://t.me/s/CryptoWhaleAlerts",
    "https://t.me/s/binance_news_channel",
    "https://t.me/s/CoinMarketCapAlerts",
    "https://t.me/s/crypto_pump_club",
    "https://t.me/s/AltcoinSherpa",
    "https://t.me/s/CryptoComAlerts",
    "https://t.me/s/whale_alert_io",
]

# ─── Patrones de bait / pump artificial ──────────────────────────────────────
BAIT_PATTERNS = [
    r'x\d{2,}',           # x100, x50, etc.
    r'\d+x garantiz',     # 100x garantizado
    r'insider',           # insider info
    r'compra (ya|ahora|urgente)',
    r'buy (now|urgent|fast)',
    r'pump (incoming|confirmed|signal)',
    r'guaranteed (profit|gains|moon)',
    r'secret (group|signal|call)',
    r'limited (time|slots|offer)',
    r'🚀🚀🚀',            # spam de cohetes
    r'100%\s*(safe|profit|guaranteed)',
    r'get rich',
    r'easy money',
    r'next (bitcoin|eth|solana)',  # "el próximo bitcoin"
]

# ─── Coins a ignorar (scams conocidos, muy volátiles sin liquidez) ────────────
COINS_IGNORAR = {
    'SHIB2', 'ELON2', 'SAFEMOON', 'SQUID', 'LUNA2CLASSIC',
    'TURBO2', 'PEPE2', 'BONK2',
}


def enviar_telegram(mensaje):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": mensaje,
            "parse_mode": "HTML"
        }, timeout=8)
    except:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# FUENTE 1: CoinGecko Trending
# ─────────────────────────────────────────────────────────────────────────────
def obtener_trending_coingecko() -> list:
    """
    Retorna los coins trending en CoinGecko ahora mismo.
    Completamente gratis, sin API key.
    """
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/search/trending",
            headers=HEADERS, timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            coins = []
            for item in data.get('coins', []):
                coin = item['item']
                coins.append({
                    'symbol': coin['symbol'].upper(),
                    'name': coin['name'],
                    'rank': coin.get('market_cap_rank', 999),
                    'score': coin.get('score', 0),
                    'fuente': 'coingecko_trending',
                })
            print(f"  CoinGecko trending: {[c['symbol'] for c in coins]}")
            return coins
    except Exception as e:
        logger.warning(f"CoinGecko trending error: {e}")
    return []


# ─────────────────────────────────────────────────────────────────────────────
# FUENTE 2: CoinGecko Top Gainers
# ─────────────────────────────────────────────────────────────────────────────
def obtener_top_gainers() -> list:
    """Top coins por ganancia en las últimas 24h."""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "price_change_percentage_24h_desc",
                "per_page": 20,
                "page": 1,
                "sparkline": False,
            },
            headers=HEADERS, timeout=8
        )
        if r.status_code == 200:
            coins = []
            for coin in r.json():
                cambio = coin.get('price_change_percentage_24h', 0) or 0
                vol = coin.get('total_volume', 0) or 0
                if cambio > 5 and vol > 500000:  # solo si subió >5% con volumen real
                    coins.append({
                        'symbol': coin['symbol'].upper(),
                        'name': coin['name'],
                        'cambio_24h': round(cambio, 2),
                        'volumen_usd': vol,
                        'fuente': 'coingecko_gainers',
                    })
            print(f"  Top gainers: {[c['symbol'] for c in coins[:5]]}")
            return coins[:10]
    except Exception as e:
        logger.warning(f"Top gainers error: {e}")
    return []


# ─────────────────────────────────────────────────────────────────────────────
# FUENTE 3: Reddit scraping (sin API key)
# ─────────────────────────────────────────────────────────────────────────────
def obtener_menciones_reddit() -> list:
    """
    Scraping liviano de Reddit sin API key.
    Busca coins mencionados en posts recientes de subs cripto.
    """
    subreddits = ['CryptoMoonShots', 'altcoin', 'CryptoCurrency', 'SatoshiStreetBets']
    menciones = defaultdict(int)
    textos = defaultdict(list)

    for sub in subreddits:
        try:
            url = f"https://www.reddit.com/r/{sub}/new.json?limit=25"
            r = requests.get(url, headers={**HEADERS, "Accept": "application/json"}, timeout=8)
            if r.status_code == 200:
                posts = r.json().get('data', {}).get('children', [])
                for post in posts:
                    titulo = post['data'].get('title', '')
                    texto = post['data'].get('selftext', '')
                    contenido = f"{titulo} {texto}".upper()
                    # Buscar símbolos cripto (palabras en mayúsculas de 2-6 chars)
                    simbolos = re.findall(r'\b[A-Z]{2,6}\b', contenido)
                    for s in simbolos:
                        if s not in {'THE', 'FOR', 'AND', 'BUT', 'NOT', 'ARE',
                                     'ALL', 'NEW', 'GET', 'HOW', 'WHY', 'NOW',
                                     'USD', 'BTC', 'ETH', 'THIS', 'WITH', 'FROM'}:
                            menciones[s] += 1
                            if titulo not in textos[s]:
                                textos[s].append(titulo[:100])
            time.sleep(0.5)
        except Exception as e:
            logger.warning(f"Reddit {sub} error: {e}")

    # Solo los más mencionados
    resultado = []
    for symbol, count in sorted(menciones.items(), key=lambda x: x[1], reverse=True)[:15]:
        if count >= 2:
            resultado.append({
                'symbol': symbol,
                'menciones': count,
                'textos': textos[symbol][:3],
                'fuente': 'reddit',
            })

    print(f"  Reddit menciones: {[c['symbol'] for c in resultado[:5]]}")
    return resultado


# ─────────────────────────────────────────────────────────────────────────────
# FUENTE 4: Telegram channels públicos (scraping web)
# ─────────────────────────────────────────────────────────────────────────────
def obtener_menciones_telegram() -> list:
    """
    Lee los últimos mensajes de canales públicos de Telegram via web.
    No requiere Telethon ni autenticación.
    """
    menciones = defaultdict(int)
    textos_raw = defaultdict(list)

    for channel_url in TELEGRAM_CHANNELS:
        try:
            r = requests.get(channel_url, headers=HEADERS, timeout=8)
            if r.status_code == 200:
                texto = r.text.upper()
                # Buscar símbolos cripto en el HTML
                simbolos = re.findall(r'\$([A-Z]{2,6})\b', texto)
                simbolos += re.findall(r'#([A-Z]{2,6})\b', texto)
                for s in simbolos:
                    if s not in {'USD', 'USDT', 'THE', 'FOR', 'AND', 'NEW'}:
                        menciones[s] += 1
                        textos_raw[s].append(channel_url.split('/')[-1])
            time.sleep(0.3)
        except Exception as e:
            logger.warning(f"Telegram scraping {channel_url}: {e}")

    resultado = []
    for symbol, count in sorted(menciones.items(), key=lambda x: x[1], reverse=True)[:15]:
        if count >= 1:
            resultado.append({
                'symbol': symbol,
                'menciones': count,
                'canales': list(set(textos_raw[symbol]))[:3],
                'fuente': 'telegram_web',
            })

    print(f"  Telegram menciones: {[c['symbol'] for c in resultado[:5]]}")
    return resultado


# ─────────────────────────────────────────────────────────────────────────────
# VERIFICADOR: ¿Existe en Binance con liquidez?
# ─────────────────────────────────────────────────────────────────────────────
def verificar_en_binance(symbol: str) -> dict:
    """Chequea si el coin tiene par USDT en Binance con volumen real."""
    if symbol in COINS_IGNORAR:
        return {"existe": False, "razon": "coin ignorado"}
    try:
        par = f"{symbol}USDT"
        r = requests.get(
            f"https://api.binance.com/api/v3/ticker/24hr?symbol={par}",
            headers=HEADERS, timeout=6
        )
        if r.status_code == 200:
            data = r.json()
            volumen = float(data.get('quoteVolume', 0))
            cambio = float(data.get('priceChangePercent', 0))
            precio = float(data.get('lastPrice', 0))
            if volumen < 500000:
                return {"existe": False, "razon": f"volumen bajo ${volumen:,.0f}"}
            return {
                "existe": True,
                "par": par,
                "volumen_24h": volumen,
                "cambio_24h": cambio,
                "precio": precio,
            }
        return {"existe": False, "razon": "no encontrado en Binance"}
    except:
        return {"existe": False, "razon": "error Binance"}


# ─────────────────────────────────────────────────────────────────────────────
# ANTI-BAIT: Groq analiza el texto
# ─────────────────────────────────────────────────────────────────────────────
def analizar_texto_groq(symbol: str, textos: list, fuentes: list) -> dict:
    """
    Groq analiza si la señal es orgánica o pump artificial.
    Retorna: legitimo (bool), confianza (0-10), razon (str)
    """
    # Pre-filtro rápido por patrones de bait
    texto_combinado = " ".join(textos).lower()
    for patron in BAIT_PATTERNS:
        if re.search(patron, texto_combinado, re.IGNORECASE):
            return {
                "legitimo": False,
                "confianza": 0,
                "razon": f"patrón de pump detectado: {patron}",
            }

    try:
        prompt = f"""Sos un analista cripto experto en detectar manipulación de mercado.

Coin: {symbol}
Fuentes que lo mencionan: {', '.join(fuentes)}
Textos encontrados:
{chr(10).join(['- ' + t for t in textos[:5]])}

Tu tarea: determinar si esta es una señal ORGÁNICA y LEGÍTIMA o un intento de pump/manipulación.

Señales de MANIPULACIÓN: promesas de ganancias garantizadas, urgencia artificial, lenguaje exagerado, 
sin fundamento técnico, solo hype, "next 100x", "insider info", muchos emojis de cohete.

Señales LEGÍTIMAS: mención de desarrollo real, volumen orgánico, análisis técnico, 
noticias de proyecto, sin promesas exageradas.

Respondé SOLO con JSON:
{{"legitimo": true, "confianza": 7, "razon": "1 linea corta"}}"""

        respuesta = client_groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=100
        )
        texto = respuesta.choices[0].message.content.strip()
        texto = texto.replace('```json', '').replace('```', '').strip()
        inicio = texto.find('{')
        fin = texto.rfind('}')
        if inicio != -1 and fin != -1:
            return json.loads(texto[inicio:fin+1])
    except Exception as e:
        logger.warning(f"Groq análisis error: {e}")

    return {"legitimo": True, "confianza": 5, "razon": "sin análisis disponible"}


# ─────────────────────────────────────────────────────────────────────────────
# MOTOR PRINCIPAL: cruzar fuentes y filtrar
# ─────────────────────────────────────────────────────────────────────────────
class MonitorMercado:

    def __init__(self):
        self.señales_enviadas = self._cargar_señales()

    def _cargar_señales(self) -> dict:
        if os.path.exists(SEÑALES_FILE):
            with open(SEÑALES_FILE, 'r') as f:
                return json.load(f)
        return {}

    def _guardar_señales(self):
        with open(SEÑALES_FILE, 'w') as f:
            json.dump(self.señales_enviadas, f, indent=2)

    def _ya_procesado(self, symbol: str) -> bool:
        """Evita procesar el mismo coin dos veces en menos de 2 horas."""
        if symbol not in self.señales_enviadas:
            return False
        ultima = datetime.fromisoformat(self.señales_enviadas[symbol])
        return datetime.now() - ultima < timedelta(hours=2)

    def _marcar_procesado(self, symbol: str):
        self.señales_enviadas[symbol] = datetime.now().isoformat()
        self._guardar_señales()

    def escanear(self) -> list:
        """
        Escanea todas las fuentes y retorna señales verificadas.
        Solo pasan el filtro las que tienen: 2+ fuentes + Binance confirmado + Groq aprueba.
        """
        print("\n" + "="*50)
        print(f"  MONITOR AMPLIO — {datetime.now().strftime('%H:%M:%S')}")
        print("="*50)

        # Recolectar de todas las fuentes
        trending   = obtener_trending_coingecko()
        gainers    = obtener_top_gainers()
        reddit     = obtener_menciones_reddit()
        telegram   = obtener_menciones_telegram()

        # Construir mapa de menciones por símbolo
        mapa = defaultdict(lambda: {"fuentes": [], "textos": [], "datos": {}})

        for coin in trending:
            s = coin['symbol']
            mapa[s]["fuentes"].append("coingecko_trending")
            mapa[s]["datos"]["coingecko"] = coin

        for coin in gainers:
            s = coin['symbol']
            mapa[s]["fuentes"].append("coingecko_gainers")
            mapa[s]["datos"]["gainer"] = coin

        for coin in reddit:
            s = coin['symbol']
            mapa[s]["fuentes"].append(f"reddit({coin['menciones']}x)")
            mapa[s]["textos"].extend(coin.get('textos', []))

        for coin in telegram:
            s = coin['symbol']
            mapa[s]["fuentes"].append(f"telegram({coin['menciones']}x)")
            mapa[s]["textos"].extend(coin.get('canales', []))

        # Filtrar y verificar
        señales_verificadas = []

        for symbol, info in mapa.items():
            fuentes = info["fuentes"]
            n_fuentes = len(fuentes)

            # FILTRO 1: mínimo 2 fuentes independientes
            if n_fuentes < 2:
                continue

            # FILTRO 2: no procesado recientemente
            if self._ya_procesado(symbol):
                continue

            # FILTRO 3: existe en Binance con liquidez
            binance = verificar_en_binance(symbol)
            if not binance["existe"]:
                print(f"  {symbol} descartado — {binance['razon']}")
                continue

            # FILTRO 4: Groq analiza y aprueba
            textos = info["textos"] or [f"{symbol} mencionado en {', '.join(fuentes)}"]
            analisis = analizar_texto_groq(symbol, textos, fuentes)

            if not analisis["legitimo"] or analisis["confianza"] < 6:
                print(f"  {symbol} BAIT detectado — {analisis['razon']}")
                enviar_telegram(
                    f"🚫 <b>BAIT detectado</b> — {symbol}\n"
                    f"Razón: {analisis['razon']}\n"
                    f"Fuentes: {', '.join(fuentes)}"
                )
                self._marcar_procesado(symbol)
                continue

            # ✅ PASÓ TODOS LOS FILTROS
            señal = {
                "symbol": symbol,
                "par_binance": binance["par"],
                "n_fuentes": n_fuentes,
                "fuentes": fuentes,
                "confianza_groq": analisis["confianza"],
                "razon_groq": analisis["razon"],
                "cambio_24h": binance["cambio_24h"],
                "volumen_24h": binance["volumen_24h"],
                "timestamp": datetime.now().isoformat(),
            }
            señales_verificadas.append(señal)
            self._marcar_procesado(symbol)

            print(f"  ✅ SEÑAL VERIFICADA: {symbol} | {n_fuentes} fuentes | confianza {analisis['confianza']}/10")

            # Notificar por Telegram
            enviar_telegram(
                f"📡 <b>SEÑAL VERIFICADA</b> — {symbol}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🔍 Fuentes ({n_fuentes}): {', '.join(fuentes)}\n"
                f"🤖 Groq: {analisis['confianza']}/10 — {analisis['razon']}\n"
                f"📊 Binance 24h: {binance['cambio_24h']:+.2f}% | Vol: ${binance['volumen_24h']:,.0f}\n"
                f"⚡ Evaluando entrada automática..."
            )

        print(f"\n  Señales verificadas: {len(señales_verificadas)}")
        return señales_verificadas


# ─────────────────────────────────────────────────────────────────────────────
# TEST STANDALONE
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    monitor = MonitorMercado()
    señales = monitor.escanear()
    print(f"\nTotal señales verificadas: {len(señales)}")
    for s in señales:
        print(f"  → {s['symbol']} | {s['n_fuentes']} fuentes | {s['confianza_groq']}/10")
