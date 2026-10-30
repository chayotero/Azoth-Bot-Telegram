import yfinance as yf
import pandas as pd
import numpy as np
import requests
import os
import time
from datetime import datetime

# --- CREDENCIALES ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# --- ACTIVOS A MONITOREAR ---
ACTIVOS = {
    'GC=F': 'Oro (XAUUSD)',
    'BTC-USD': 'Bitcoin (BTCUSD)',
    'USDJPY=X': 'USD/JPY',
    'EURUSD=X': 'EUR/USD',
    'GBPUSD=X': 'GBP/USD',
    'AUDUSD=X': 'AUD/USD',
    'CL=F': 'Petróleo (USOIL)'
}

# --- PARÁMETROS GANADORES ---
EMA_PERIOD = 5
DISP_ATR_MULT = 0.8
DEEP_WICK_LEVEL = 0.67
SL_BUFFER_ATR = 0.4
TARGET_RR = 1.45

def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"Error Telegram: {e}")

def analizar_activo(ticker, nombre_activo):
    print(f"Analizando {nombre_activo} ({ticker})...")
    try:
        df = yf.download(ticker, period="1mo", interval="1h", progress=False)
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        df = df.dropna()

        if len(df) < 50: 
            return

        tr = pd.concat([df['High'] - df['Low'], (df['High'] - df['Close'].shift()).abs(), (df['Low'] - df['Close'].shift()).abs()], axis=1).max(axis=1)
        df['ATR'] = tr.ewm(alpha=1/33, adjust=False).mean()
        df['EMA'] = df['Close'].ewm(span=EMA_PERIOD, adjust=False).mean()

        # Evaluamos la última vela COMPLETAMENTE CERRADA
        current = df.iloc[-2]
        prev = df.iloc[-3]

        atr = current['ATR']
        htf_bull = current['Close'] > current['EMA']
        htf_bear = current['Close'] < current['EMA']
        strong_candle = (current['High'] - current['Low']) >= (atr * DISP_ATR_MULT)

        señal = None
        if prev['Close'] < prev['Open'] and current['High'] > prev['High'] and strong_candle and htf_bull:
            r = prev['High'] - prev['Low']
            entry = prev['High'] - (r * DEEP_WICK_LEVEL)
            sl = min(prev['Low'] - (atr * SL_BUFFER_ATR), entry - (atr * 0.5))
            tp = entry + (entry - sl) * TARGET_RR
            señal = "LONG"
        elif prev['Close'] > prev['Open'] and current['Low'] < prev['Low'] and strong_candle and htf_bear:
            r = prev['High'] - prev['Low']
            entry = prev['Low'] + (r * DEEP_WICK_LEVEL)
            sl = max(prev['High'] + (atr * SL_BUFFER_ATR), entry + (atr * 0.5))
            tp = entry - (sl - entry) * TARGET_RR
            señal = "SHORT"

        if señal:
            decimales = 4 if 'USD' in ticker and '=' in ticker or 'JPY' in ticker else 2
            mensaje = f"""
🚨 *¡NUEVA SEÑAL ({nombre_activo})!* 🚨
━━━━━━━━━━━━━━━━━━━━
📊 *Activo:* {nombre_activo}
📈 *Dirección:* {señal}
🎯 *Entrada:* {entry:.{decimales}f}
🛑 *SL:* {sl:.{decimales}f}
✅ *TP:* {tp:.{decimales}f}
━━━━━━━━━━━━━━━━━━━━
*Gestión recomendada:*
_1. Coloca la orden límite._
_2. Mueve el SL a Break-Even cuando vayas a favor._
━━━━━━━━━━━━━━━━━━━━
_Generado por Azoth Bot (GitHub)_
"""
            enviar_telegram(mensaje)
            print(f"✅ Señal {señal} enviada para {nombre_activo}.")
        else:
            print(f"Sin setups en {nombre_activo}.")
            
    except Exception as e:
        print(f"Error analizando {nombre_activo}: {e}")

def ejecutar_bot():
    # --- VENTANA DE BÚSQUEDA AMPLIADA (00 al 20) ---
    # Revisa justo al cerrar la vela y da 20 min de margen por si GitHub se retrasa
    minuto_actual = datetime.utcnow().minute
    if minuto_actual > 20:
        print(f"Minuto {minuto_actual}. Fuera de la ventana de búsqueda. Saliendo...")
        return

    print("Dentro de la ventana. Buscando setups en los activos...")
    for ticker, nombre in ACTIVOS.items():
        analizar_activo(ticker, nombre)
        time.sleep(1)

if __name__ == "__main__":
    ejecutar_bot()
