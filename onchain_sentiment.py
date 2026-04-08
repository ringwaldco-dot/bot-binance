"""
onchain_sentiment.py
Módulo de sentiment on-chain — todas las fuentes son gratuitas, sin API key.
"""

import requests
import time
import logging

logger = logging.getLogger(__name__)

_fg_cache = {"data": None, "ts": 0}
_CACHE_TTL = 300
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; trading-bot/1.0)"}


def get_fear_greed() -> dict:
    global _fg_cache
    if _fg_cache["data"] and (time.time() - _fg_cache["ts"]) < _CACHE_TTL:
        return _fg_cache["data"]
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1&format=json", headers=HEADERS, timeout=6)
        d = r.json()["data"][0]
        result = {
            "value": int(d["value"]),
            "label": d["value_classification"],
            "normalized": round((int(d["value"]) - 50) / 50, 3),
        }
        _fg_cache = {"data": result, "ts": time.time()}
        return result
    except Exception as e:
        logger.warning(f"Fear&Greed error: {e}")
        return {"value": 50, "label": "Neutral", "normalized": 0.0}


def get_funding_rate(symbol: str) -> dict:
    try:
        r = requests.get(
            f"https://fapi.binance.com/fapi/v1/premiumIndex?symbol={symbol}USDT",
            headers=HEADERS, timeout=6
        )
        if r.status_code == 200:
            fr = float(r.json().get("lastFundingRate", 0))
            return {
                "rate_pct": round(fr * 100, 5),
                "normalized": round(max(-1.0, min(1.0, -fr / 0.0003)), 3),
                "signal": "bearish" if fr > 0.0001 else "bullish" if fr < -0.0001 else "neutral",
            }
    except Exception as e:
        logger.warning(f"Funding rate error {symbol}: {e}")
    return {"rate_pct": 0.0, "normalized": 0.0, "signal": "neutral"}


def get_long_short_ratio(symbol: str) -> dict:
    try:
        r = requests.get(
            "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
            params={"symbol": f"{symbol}USDT", "period": "1h", "limit": 1},
            headers=HEADERS, timeout=6
        )
        if r.status_code == 200:
            data = r.json()
            if data:
                ratio = float(data[0]["longShortRatio"])
                return {
                    "ratio": round(ratio, 3),
                    "long_pct": round(float(data[0]["longAccount"]), 3),
                    "normalized": round(max(-1.0, min(1.0, -(ratio - 1.0) / 0.5)), 3),
                    "signal": "bearish" if ratio > 1.5 else "bullish" if ratio < 0.7 else "neutral",
                }
    except Exception as e:
        logger.warning(f"Long/short error {symbol}: {e}")
    return {"ratio": 1.0, "long_pct": 0.5, "normalized": 0.0, "signal": "neutral"}


def get_taker_volume(symbol: str) -> dict:
    try:
        r = requests.get(
            "https://fapi.binance.com/futures/data/takerlongshortRatio",
            params={"symbol": f"{symbol}USDT", "period": "1h", "limit": 6},
            headers=HEADERS, timeout=6
        )
        if r.status_code == 200:
            data = r.json()
            if data:
                buy_vols  = [float(d["buyVol"])  for d in data]
                sell_vols = [float(d["sellVol"]) for d in data]
                total_buy  = sum(buy_vols)
                total_sell = sum(sell_vols)
                total   = total_buy + total_sell
                ratio   = total_buy / total_sell if total_sell > 0 else 1.0
                buy_pct = total_buy / total if total > 0 else 0.5
                return {
                    "ratio": round(ratio, 3),
                    "buy_pct": round(buy_pct, 3),
                    "normalized": round(max(-1.0, min(1.0, (buy_pct - 0.5) * 10)), 3),
                    "signal": "bullish" if ratio > 1.1 else "bearish" if ratio < 0.9 else "neutral",
                }
    except Exception as e:
        logger.warning(f"Taker volume error {symbol}: {e}")
    return {"ratio": 1.0, "buy_pct": 0.5, "normalized": 0.0, "signal": "neutral"}


def get_liquidations(symbol: str) -> dict:
    try:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/allForceOrders",
            params={"symbol": f"{symbol}USDT", "limit": 50},
            headers=HEADERS, timeout=6
        )
        if r.status_code == 200:
            data = r.json()
            if data:
                long_liqs  = [d for d in data if d["side"] == "SELL"]
                short_liqs = [d for d in data if d["side"] == "BUY"]
                long_usd  = sum(float(d["origQty"]) * float(d["price"]) for d in long_liqs)
                short_usd = sum(float(d["origQty"]) * float(d["price"]) for d in short_liqs)
                total_usd = long_usd + short_usd
                normalized = 0.0
                if total_usd > 0:
                    normalized = round(max(-1.0, min(1.0, (short_usd - long_usd) / total_usd * 2)), 3)
                return {
                    "long_usd":   round(long_usd),
                    "short_usd":  round(short_usd),
                    "total_usd":  round(total_usd),
                    "normalized": normalized,
                    "signal": "bullish" if short_usd > long_usd * 1.5
                              else "bearish" if long_usd > short_usd * 1.5
                              else "neutral",
                }
    except Exception as e:
        logger.warning(f"Liquidations error {symbol}: {e}")
    return {"long_usd": 0, "short_usd": 0, "total_usd": 0, "normalized": 0.0, "signal": "neutral"}


def get_onchain_signal(binance_symbol: str) -> dict:
    symbol = binance_symbol.replace("USDT", "")
    fg      = get_fear_greed()
    funding = get_funding_rate(symbol)
    ls      = get_long_short_ratio(symbol)
    taker   = get_taker_volume(symbol)
    liqs    = get_liquidations(symbol)

    score = round(
        taker["normalized"]   * 0.30 +
        funding["normalized"] * 0.25 +
        ls["normalized"]      * 0.20 +
        fg["normalized"]      * 0.15 +
        liqs["normalized"]    * 0.10,
        3
    )
    score = max(-1.0, min(1.0, score))

    if score >= 0.35:
        action, emoji = "FAVOR_LONG",   "🟢"
    elif score >= 0.15:
        action, emoji = "SLIGHT_LONG",  "🟡"
    elif score <= -0.35:
        action, emoji = "AVOID_LONG",   "🔴"
    elif score <= -0.15:
        action, emoji = "SLIGHT_SHORT", "🟠"
    else:
        action, emoji = "NEUTRAL",      "⚪"

    return {
        "symbol":  binance_symbol,
        "score":   score,
        "action":  action,
        "emoji":   emoji,
        "block":   score <= -0.35,
        "components": {
            "fear_greed":   fg,
            "funding_rate": funding,
            "long_short":   ls,
            "taker_volume": taker,
            "liquidations": liqs,
        },
    }


def format_signal_telegram(sig: dict) -> str:
    c = sig["components"]
    return (
        f"{sig['emoji']} <b>Sentiment On-Chain — {sig['symbol']}</b>\n"
        f"Score: <code>{sig['score']:+.3f}</code> | <b>{sig['action']}</b>\n\n"
        f"• Fear &amp; Greed: <code>{c['fear_greed']['value']}</code> ({c['fear_greed']['label']})\n"
        f"• Funding Rate:  <code>{c['funding_rate']['rate_pct']:+.4f}%</code> ({c['funding_rate']['signal']})\n"
        f"• Long/Short:    <code>{c['long_short']['ratio']:.2f}</code> ({c['long_short']['signal']})\n"
        f"• Taker Vol:     <code>{c['taker_volume']['ratio']:.2f}</code> ({c['taker_volume']['signal']})\n"
        f"• Liquidaciones: <code>${c['liquidations']['total_usd']:,}</code> ({c['liquidations']['signal']})\n"
    )


if __name__ == "__main__":
    for sym in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
        sig = get_onchain_signal(sym)
        print(format_signal_telegram(sig))
        print(f"  BLOCK: {sig['block']}\n" + "-"*50)
        time.sleep(1)