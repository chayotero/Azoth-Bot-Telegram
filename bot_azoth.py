import yfinance as yf
import pandas as pd
import numpy as np
import requests
import os
from datetime import datetime

# --- CREDENCIALES ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# --- PARÁMETROS GANADORES (Extraídos del Backtest) ---
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

def ejecutar_bot():
    # Filtro anti-spam para cron de 5 min
    minuto_actual = datetime.utcnow().minute
    if minuto_actual > 10:
        print(f"Minuto {minuto_actual}. Fuera de la ventana de alerta. Saliendo...")
        return

    print("Dentro de la ventana de evaluación. Descargando datos...")
    # Descargamos datos de 1H
    df = yf.download("GC=F", period="1mo", interval="1h", progress=False)
    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
    df = df.dropna()

    if len(df) < 50: 
        print("Datos insuficientes.")
        return

    # INDICADORES EXACTOS
    tr = pd.concat([df['High'] - df['Low'], (df['High'] - df['Close'].shift()).abs(), (df['Low'] - df['Close'].shift()).abs()], axis=1).max(axis=1)
    df['ATR'] = tr.ewm(alpha=1/33, adjust=False).mean()
    df['EMA'] = df['Close'].ewm(span=EMA_PERIOD, adjust=False).mean()

    # EVALUACIÓN DE LA VELA CERRADA (Iloc[-2] es la vela que acaba de cerrar)
    current = df.iloc[-2]
    prev = df.iloc[-3]

    atr = current['ATR']
    
    # Lógica de sesgo (EMA 5)
    htf_bull = current['Close'] > current['EMA']
    htf_bear = current['Close'] < current['EMA']
    
    # Lógica de Displacement (Fuerza de la vela)
    strong_candle = (current['High'] - current['Low']) >= (atr * DISP_ATR_MULT)

    señal = None
    
    # --- CONDICIÓN LONG ---
    if prev['Close'] < prev['Open'] and current['High'] > prev['High'] and strong_candle and htf_bull:
        r = prev['High'] - prev['Low']
        entry = prev['High'] - (r * DEEP_WICK_LEVEL)
        sl = min(prev['Low'] - (atr * SL_BUFFER_ATR), entry - (atr * 0.5))
        tp = entry + (entry - sl) * TARGET_RR
        señal = "LONG"
        
    # --- CONDICIÓN SHORT ---
    elif prev['Close'] > prev['Open'] and current['Low'] < prev['Low'] and strong_candle and htf_bear:
        r = prev['High'] - prev['Low']
        entry = prev['Low'] + (r * DEEP_WICK_LEVEL)
        sl = max(prev['High'] + (atr * SL_BUFFER_ATR), entry + (atr * 0.5))
        tp = entry - (sl - entry) * TARGET_RR
        señal = "SHORT"

    # ENVIAR ALERTA
    if señal:
        mensaje = f"""
🚨 *¡NUEVA SEÑAL (1H CERRADA)!* 🚨
━━━━━━━━━━━━━━━━━━━━
📊 *Activo:* Oro (GC=F)
📈 *Dirección:* {señal}
🎯 *Entrada:* ${entry:.2f}
🛑 *SL:* ${sl:.2f}
✅ *TP:* ${tp:.2f}
━━━━━━━━━━━━━━━━━━━━
*Gestión recomendada:*
_1. Coloca la orden límite._
_2. Cuando el precio llegue al 50% del camino al TP, mueve el SL a Break-Even (Entrada)._
━━━━━━━━━━━━━━━━━━━━
_Generado por Azoth Bot (GitHub)_
"""
        enviar_telegram(mensaje)
        print(f"✅ Señal {señal} enviada.")
    else:
        print("Sin setups en esta vela.")

if __name__ == "__main__":
    ejecutar_bot()
