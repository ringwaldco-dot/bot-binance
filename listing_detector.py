# -*- coding: utf-8 -*-
"""
listing_detector.py
===================
Detector de nuevos listings en Binance.
Compara pares actuales contra los guardados y alerta cuando aparece uno nuevo.
Los nuevos listings suelen subir 20-50% en las primeras horas.

Uso en bot.py:
    from listing_detector import ListingDetector
    detector = ListingDetector()
    nuevos = detector.detectar_nuevos()  # retorna lista de nuevos pares
"""

import os
import json
import time
import logging
import requests
from datetime import datetime

logger = logging.getLogger(__name__)

PARES_FILE = "pares_conocidos.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; trading-bot/1.0)"}

# Filtros de calidad para nuevos listings
VOLUMEN_MINIMO = 500000       # $500k en primeras horas
PRECIO_MINIMO = 0.000001      # descartar tokens basura
PRECIO_MAXIMO = 10000         # descartar pares raros


class ListingDetector:

    def __init__(self):
        self.pares_conocidos = self._cargar_pares()
        # Si es la primera vez, inicializar sin alertar
        if not self.pares_conocidos:
            print("  Listing detector: inicializando base de pares...")
            self._inicializar()

    def _cargar_pares(self):
        if os.path.exists(PARES_FILE):
            with open(PARES_FILE, "r") as f:
                return set(json.load(f))
        return set()

    def _guardar_pares(self, pares):
        with open(PARES_FILE, "w") as f:
            json.dump(list(pares), f)

    def _obtener_todos_los_pares(self):
        """Obtiene todos los pares USDT activos de Binance."""
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/exchangeInfo",
                headers=HEADERS, timeout=10
            )
            if r.status_code == 200:
                simbolos = r.json().get("symbols", [])
                pares = set()
                for s in simbolos:
                    if (s["quoteAsset"] == "USDT" and
                        s["status"] == "TRADING" and
                        s["isSpotTradingAllowed"]):
                        pares.add(s["symbol"])
                return pares
        except Exception as e:
            logger.warning("Error obteniendo pares Binance: {}".format(e))
        return set()

    def _inicializar(self):
        """Primera ejecucion: guarda todos los pares actuales sin alertar."""
        pares = self._obtener_todos_los_pares()
        if pares:
            self.pares_conocidos = pares
            self._guardar_pares(pares)
            print("  Listing detector: {} pares guardados".format(len(pares)))

    def _verificar_calidad(self, par):
        """Verifica que el nuevo listing tenga volumen y precio razonables."""
        try:
            r = requests.get(
                "https://api.binance.com/api/v3/ticker/24hr?symbol={}".format(par),
                headers=HEADERS, timeout=6
            )
            if r.status_code == 200:
                d = r.json()
                volumen = float(d.get("quoteVolume", 0))
                precio = float(d.get("lastPrice", 0))
                cambio = float(d.get("priceChangePercent", 0))

                if precio < PRECIO_MINIMO or precio > PRECIO_MAXIMO:
                    return None
                if volumen < VOLUMEN_MINIMO:
                    return None

                return {
                    "par": par,
                    "precio": precio,
                    "volumen_24h": volumen,
                    "cambio_24h": cambio,
                    "timestamp": datetime.now().isoformat(),
                }
        except Exception as e:
            logger.warning("Error verificando {}: {}".format(par, e))
        return None

    def detectar_nuevos(self):
        """
        Compara pares actuales vs guardados.
        Retorna lista de nuevos listings verificados.
        """
        pares_actuales = self._obtener_todos_los_pares()
        if not pares_actuales:
            return []

        # Encontrar nuevos
        nuevos_raw = pares_actuales - self.pares_conocidos

        if not nuevos_raw:
            return []

        print("\n  NUEVOS LISTINGS DETECTADOS: {}".format(nuevos_raw))

        # Verificar calidad de cada nuevo par
        nuevos_verificados = []
        for par in nuevos_raw:
            info = self._verificar_calidad(par)
            if info:
                nuevos_verificados.append(info)
                print("  LISTING OK: {} | ${} | Vol ${:,.0f} | {}%".format(
                    par, info["precio"], info["volumen_24h"], info["cambio_24h"]
                ))
            else:
                print("  LISTING descartado (baja calidad): {}".format(par))
            time.sleep(0.2)

        # Actualizar pares conocidos
        self.pares_conocidos = pares_actuales
        self._guardar_pares(pares_actuales)

        return nuevos_verificados


if __name__ == "__main__":
    detector = ListingDetector()
    print("Monitoreando nuevos listings... (Ctrl+C para detener)")
    while True:
        nuevos = detector.detectar_nuevos()
        if nuevos:
            print("\nNUEVOS LISTINGS:")
            for n in nuevos:
                print("  {} | ${} | {}%".format(n["par"], n["precio"], n["cambio_24h"]))
        time.sleep(60)
