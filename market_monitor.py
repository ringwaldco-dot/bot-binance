# -*- coding: utf-8 -*-
"""
market_monitor.py
Monitor amplio de mercado cripto - anti-bait por diseno.
Fuentes: Telegram channels, Reddit, CoinGecko trending, Binance volumen.
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
from google import genai
from google.genai import types

load_dotenv()

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; trading-bot/1.0)"}
SENALES_FILE = "senales_monitor.json"

client_gemini = genai.Client(api_key=GEMINI_API_KEY)

logger = logging.getLogger(__name__)

TELEGRAM_CHANNELS = [
    "https://t.me/s/CryptoWhaleAlerts",
    "https://t.me/s/binance_news_channel",
    "https://t.me/s/CoinMarketCapAlerts",
    "https://t.me/s/AltcoinSherpa",
    "https://t.me/s/whale_alert_io",
]

BAIT_PATTERNS = [
    r'x\d{2,}',
    r'\d+x garantiz',
    r'insider',
    r'compra (ya|ahora|urgente)',
    r'buy (now|urgent|fast)',
    r'pump (incoming|confirmed|signal)',
    r'guaranteed (profit|gains|moon)',
    r'secret (group|signal|call)',
    r'limited (time|slots|offer)',
    r'100%\s*(safe|profit|guaranteed)',
    r'get rich',
    r'easy money',
    r'next (bitcoin|eth|solana)',
]

COINS_IGNORAR = {
    'SHIB2', 'ELON2', 'SAFEMOON', 'SQUID', 'LUNA2CLASSIC',
    'TURBO2', 'PEPE2', 'BONK2',
}

def enviar_telegram(mensaje):
    try:
        url = "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_TOKEN)
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": mensaje,
            "parse_mode": "HTML"
        }, timeout=8)
    except:
        pass

def obtener_trending_coingecko():
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/search/trending",
            headers=HEADERS, timeout=8
        )
        if r.status_code == 200:
            coins = []
            for item in r.json().get('coins', []):
                coin = item['item']
                coins.append({
                    'symbol': coin['symbol'].upper(),
                    'name': coin['name'],
                    'fuente': 'coingecko_trending',
                })
            print("  CoinGecko trending: {}".format([c['symbol'] for c in coins]))
            return coins
    except Exception as e:
        logger.warning("CoinGecko trending error: {}".format(e))
    return []

def obtener_top_gainers():
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
                if cambio > 5 and vol > 500000:
                    coins.append({
                        'symbol': coin['symbol'].upper(),
                        'name': coin['name'],
                        'cambio_24h': round(cambio, 2),
                        'volumen_usd': vol,
                        'fuente': 'coingecko_gainers',
                    })
            print("  Top gainers: {}".format([c['symbol'] for c in coins[:5]]))
            return coins[:10]
    except Exception as e:
        logger.warning("Top gainers error: {}".format(e))
    return []

def obtener_menciones_reddit():
    subreddits = ['CryptoMoonShots', 'altcoin', 'CryptoCurrency', 'SatoshiStreetBets']
    menciones = defaultdict(int)
    textos = defaultdict(list)
    for sub in subreddits:
        try:
            url = "https://www.reddit.com/r/{}/new.json?limit=25".format(sub)
            r = requests.get(url, headers={**HEADERS, "Accept": "application/json"}, timeout=8)
            if r.status_code == 200:
                posts = r.json().get('data', {}).get('children', [])
                for post in posts:
                    titulo = post['data'].get('title', '')
                    texto = post['data'].get('selftext', '')
                    contenido = "{} {}".format(titulo, texto).upper()
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
            logger.warning("Reddit {} error: {}".format(sub, e))
    resultado = []
    for symbol, count in sorted(menciones.items(), key=lambda x: x[1], reverse=True)[:15]:
        if count >= 2:
            resultado.append({
                'symbol': symbol,
                'menciones': count,
                'textos': textos[symbol][:3],
                'fuente': 'reddit',
            })
    print("  Reddit menciones: {}".format([c['symbol'] for c in resultado[:5]]))
    return resultado

def obtener_menciones_telegram():
    menciones = defaultdict(int)
    textos_raw = defaultdict(list)
    for channel_url in TELEGRAM_CHANNELS:
        try:
            r = requests.get(channel_url, headers=HEADERS, timeout=8)
            if r.status_code == 200:
                texto = r.text.upper()
                simbolos = re.findall(r'\$([A-Z]{2,6})\b', texto)
                simbolos += re.findall(r'#([A-Z]{2,6})\b', texto)
                for s in simbolos:
                    if s not in {'USD', 'USDT', 'THE', 'FOR', 'AND', 'NEW'}:
                        menciones[s] += 1
                        textos_raw[s].append(channel_url.split('/')[-1])
            time.sleep(0.3)
        except Exception as e:
            logger.warning("Telegram scraping {}: {}".format(channel_url, e))
    resultado = []
    for symbol, count in sorted(menciones.items(), key=lambda x: x[1], reverse=True)[:15]:
        if count >= 1:
            resultado.append({
                'symbol': symbol,
                'menciones': count,
                'canales': list(set(textos_raw[symbol]))[:3],
                'fuente': 'telegram_web',
            })
    print("  Telegram menciones: {}".format([c['symbol'] for c in resultado[:5]]))
    return resultado

def verificar_en_binance(symbol):
    if symbol in COINS_IGNORAR:
        return {"existe": False, "razon": "coin ignorado"}
    try:
        par = "{}USDT".format(symbol)
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr?symbol={}".format(par),
            headers=HEADERS, timeout=6
        )
        if r.status_code == 200:
            data = r.json()
            volumen = float(data.get('quoteVolume', 0))
            cambio = float(data.get('priceChangePercent', 0))
            precio = float(data.get('lastPrice', 0))
            if volumen < 500000:
                return {"existe": False, "razon": "volumen bajo"}
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

def analizar_texto_groq(symbol, textos, fuentes):
    """Análisis con Gemini 2.0 Flash (reemplaza Groq)."""
    texto_combinado = " ".join(textos).lower()
    for patron in BAIT_PATTERNS:
        if re.search(patron, texto_combinado, re.IGNORECASE):
            return {
                "legitimo": False,
                "confianza": 0,
                "razon": "patron de pump detectado",
            }
    try:
        prompt = """Sos un analista cripto experto en detectar manipulacion de mercado.
Coin: {}
Fuentes: {}
Textos: {}
Determina si es senal ORGANICA o pump/manipulacion.
MANIPULACION: ganancias garantizadas, urgencia, next 100x, insider info.
LEGITIMA: desarrollo real, volumen organico, analisis tecnico.
Responde SOLO con JSON sin texto adicional:
{{"legitimo": true, "confianza": 7, "razon": "1 linea"}}""".format(
            symbol,
            ', '.join(fuentes),
            '\n'.join(['- ' + t for t in textos[:5]])
        )

        response = client_gemini.models.generate_content(
            model='gemini-2.0-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=100,
            )
        )
        texto = response.text.strip().replace('```json', '').replace('```', '').strip()
        inicio = texto.find('{')
        fin = texto.rfind('}')
        if inicio != -1 and fin != -1:
            return json.loads(texto[inicio:fin+1])
    except Exception as e:
        logger.warning("Gemini analisis error: {}".format(e))
    return {"legitimo": True, "confianza": 5, "razon": "sin analisis disponible"}


class MonitorMercado:
    def __init__(self):
        self.senales_enviadas = self._cargar_senales()

    def _cargar_senales(self):
        if os.path.exists(SENALES_FILE):
            with open(SENALES_FILE, 'r') as f:
                return json.load(f)
        return {}

    def _guardar_senales(self):
        with open(SENALES_FILE, 'w') as f:
            json.dump(self.senales_enviadas, f, indent=2)

    def _ya_procesado(self, symbol):
        if symbol not in self.senales_enviadas:
            return False
        ultima = datetime.fromisoformat(self.senales_enviadas[symbol])
        return datetime.now() - ultima < timedelta(hours=2)

    def _marcar_procesado(self, symbol):
        self.senales_enviadas[symbol] = datetime.now().isoformat()
        self._guardar_senales()

    def escanear(self):
        print("\n" + "="*50)
        print("  MONITOR AMPLIO - {}".format(datetime.now().strftime('%H:%M:%S')))
        print("="*50)

        trending = obtener_trending_coingecko()
        gainers = obtener_top_gainers()
        reddit = obtener_menciones_reddit()
        telegram = obtener_menciones_telegram()

        mapa = defaultdict(lambda: {"fuentes": [], "textos": [], "datos": {}})

        for coin in trending:
            s = coin['symbol']
            mapa[s]["fuentes"].append("coingecko_trending")
        for coin in gainers:
            s = coin['symbol']
            mapa[s]["fuentes"].append("coingecko_gainers")
        for coin in reddit:
            s = coin['symbol']
            mapa[s]["fuentes"].append("reddit({}x)".format(coin['menciones']))
            mapa[s]["textos"].extend(coin.get('textos', []))
        for coin in telegram:
            s = coin['symbol']
            mapa[s]["fuentes"].append("telegram({}x)".format(coin['menciones']))
            mapa[s]["textos"].extend(coin.get('canales', []))

        senales_verificadas = []

        for symbol, info in mapa.items():
            fuentes = info["fuentes"]
            n_fuentes = len(fuentes)
            if n_fuentes < 2:
                continue
            if self._ya_procesado(symbol):
                continue
            binance = verificar_en_binance(symbol)
            if not binance["existe"]:
                print("  {} descartado - {}".format(symbol, binance['razon']))
                continue
            textos = info["textos"] or ["{} mencionado en {}".format(symbol, ', '.join(fuentes))]
            analisis = analizar_texto_groq(symbol, textos, fuentes)
            if not analisis["legitimo"] or analisis["confianza"] < 6:
                print("  {} BAIT - {}".format(symbol, analisis['razon']))
                enviar_telegram(
                    "<b>BAIT detectado</b> - {}\nRazon: {}\nFuentes: {}".format(
                        symbol, analisis['razon'], ', '.join(fuentes)
                    )
                )
                self._marcar_procesado(symbol)
                continue
            senal = {
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
            senales_verificadas.append(senal)
            self._marcar_procesado(symbol)
            print("  SENAL VERIFICADA: {} | {} fuentes | confianza {}/10".format(
                symbol, n_fuentes, analisis['confianza']
            ))
            enviar_telegram(
                "<b>SENAL VERIFICADA</b> - {}\n"
                "Fuentes ({}): {}\n"
                "Gemini: {}/10 - {}\n"
                "Binance 24h: {:+.2f}% | Vol: ${:,.0f}".format(
                    symbol,
                    n_fuentes, ', '.join(fuentes),
                    analisis['confianza'], analisis['razon'],
                    binance['cambio_24h'], binance['volumen_24h']
                )
            )

        print("\n  Senales verificadas: {}".format(len(senales_verificadas)))
        return senales_verificadas


if __name__ == "__main__":
    monitor = MonitorMercado()
    senales = monitor.escanear()
    print("\nTotal: {}".format(len(senales)))
    for s in senales:
        print("  -> {} | {} fuentes | {}/10".format(s['symbol'], s['n_fuentes'], s['confianza_groq']))
