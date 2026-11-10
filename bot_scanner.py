import pandas as pd
import numpy as np
import requests
import json
import os
from datetime import datetime
import time
import warnings
from tvDatafeed import TvDatafeed, Interval
import ta

warnings.filterwarnings('ignore')

# ==========================================
# ⚙️ CONFIGURACIÓN DEL BOT Y CUENTA
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "TU_TOKEN_AQUI") 
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "TU_CHAT_ID_AQUI")
BALANCE_CUENTA = 1000                   
RIESGO_POR_OPERACION = 0.02             
CONTRATO_ORO = 100                      

TARGET_RR = 1.23
OB_LOOKBACK = 7
USE_RETRACEMENT = True 

# 🏆 TODAS LAS TEMPORALIDADES GANADORAS
CONFIG_TFS = [
    {"tf": "1m",  "interval": Interval.in_1_minute,  "minutes": 1},
    {"tf": "5m",  "interval": Interval.in_5_minute,  "minutes": 5},
    {"tf": "15m", "interval": Interval.in_15_minute, "minutes": 15},
    {"tf": "30m", "interval": Interval.in_30_minute, "minutes": 30},
    {"tf": "1h",  "interval": Interval.in_1_hour,    "minutes": 60},
    {"tf": "4h",  "interval": Interval.in_4_hour,    "minutes": 240}
]

ARCHIVO_MEMORIA = "last_signals_smc.json"

def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "HTML"}
    try:
        return requests.post(url, json=payload, timeout=10).status_code == 200
    except Exception as e:
        print(f"Error Telegram: {e}")
        return False

def calcular_lotaje(entry, sl, balance, riesgo):
    dist_sl = abs(entry - sl)
    if dist_sl == 0: return 0.01
    return round(max((balance * riesgo) / (dist_sl * CONTRATO_ORO), 0.01), 2)

def cargar_memoria():
    memoria_base = {cfg["tf"]: {"last_candle": "", "active_trade": None} for cfg in CONFIG_TFS}
    if os.path.exists(ARCHIVO_MEMORIA):
        try:
            with open(ARCHIVO_MEMORIA, 'r') as f:
                mem = json.load(f)
                for k in memoria_base:
                    if k not in mem: mem[k] = memoria_base[k]
                return mem
        except: pass
    return memoria_base

def guardar_memoria(memoria):
    with open(ARCHIVO_MEMORIA, 'w') as f:
        json.dump(memoria, f, indent=4)

def escanear_tf(cfg, memoria):
    tf_name = cfg["tf"]
    print(f"🔍 Analizando SMC en {tf_name.upper()}...")
    
    try:
        tv = TvDatafeed()
        # Pedimos 500 velas (suficiente para ATR y estructura en cualquier TF)
        df = tv.get_hist(symbol='XAUUSD', exchange='OANDA', interval=cfg["interval"], n_bars=500)
    except Exception as e:
        print(f"Error TvDatafeed en {tf_name}: {e}")
        return

    if df is None or len(df) < 200: return

    df = df[['open', 'high', 'low', 'close']].copy()
    df.index = pd.to_datetime(df.index).tz_localize('UTC').tz_convert('America/New_York').tz_localize(None)
    df = df.iloc[:-1] 
    
    df['ATR'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=33)
    
    # HTF Dinámico
    htf_minutes = max(120, cfg["minutes"] * 4)
    htf_rule = f'{htf_minutes}min' if htf_minutes < 1440 else '1D'
    
    try:
        df_htf = df.resample(htf_rule).agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
        df_htf['HTF_EMA'] = ta.trend.ema_indicator(df_htf['close'], window=5)
        df = df.join(df_htf['HTF_EMA'], rsuffix='_htf').ffill()
    except Exception as e:
        print(f"Error resampleando HTF en {tf_name}: {e}")
        return

    opens, highs, lows, closes = df['open'].values, df['high'].values, df['low'].values, df['close'].values
    atrs, htf_emas = df['ATR'].values, df['HTF_EMA'].values
    hours, days = df.index.hour.values, df.index.dayofweek.values
    
    trend = 0
    last_pivot_high, last_pivot_low = np.nan, np.nan
    leg_high, leg_low = np.nan, np.nan
    
    # Solo analizamos las últimas 10 velas para no saturar
    for i in range(len(df) - 10, len(df)):
        ph = highs[i-1] if (highs[i-1] > highs[i-2] and highs[i-1] > highs[i]) else np.nan
        pl = lows[i-1] if (lows[i-1] < lows[i-2] and lows[i-1] < lows[i]) else np.nan
        
        if not np.isnan(ph): last_pivot_high = ph
        if not np.isnan(pl): last_pivot_low = pl
        
        is_high_broken = closes[i] > last_pivot_high if not np.isnan(last_pivot_high) else False
        is_low_broken = closes[i] < last_pivot_low if not np.isnan(last_pivot_low) else False
        
        choch_bull, bos_bull, choch_bear, bos_bear = False, False, False, False
        
        if is_high_broken:
            if trend == -1: choch_bull = True; leg_high = highs[i]; leg_low = last_pivot_low
            elif trend == 1: bos_bull = True
            trend = 1
        if is_low_broken:
            if trend == 1: choch_bear = True; leg_low = lows[i]; leg_high = last_pivot_high
            elif trend == -1: bos_bear = True
            trend = -1
            
        if trend == 1: leg_high = max(leg_high if not np.isnan(leg_high) else highs[i], highs[i])
        if trend == -1: leg_low = min(leg_low if not np.isnan(leg_low) else lows[i], lows[i])
        
        equilibrium = (leg_high + leg_low) / 2.0 if not np.isnan(leg_high) and not np.isnan(leg_low) else np.nan
        strong_candle = max(highs[i] - lows[i], highs[i-1] - lows[i-1]) >= atrs[i] * 0.6
        disp_bull = strong_candle and (choch_bull or bos_bull)
        disp_bear = strong_candle and (choch_bear or bos_bear)
        
        htf_bias_bull = closes[i] > htf_emas[i] if not np.isnan(htf_emas[i]) else True
        htf_bias_bear = closes[i] < htf_emas[i] if not np.isnan(htf_emas[i]) else True
        in_session = (7 <= hours[i] <= 16) and (days[i] != 6)
        
        if i >= len(df) - 3:
            if disp_bull and htf_bias_bull and in_session:
                ob_offset = 0; lowest_ob_low = 1e10
                for k in range(1, OB_LOOKBACK + 1):
                    if closes[i-k] <= opens[i-k] and lows[i-k] < lowest_ob_low:
                        lowest_ob_low = lows[i-k]; ob_offset = k
                if ob_offset > 0:
                    ob_top = highs[i-ob_offset]; ob_bottom = lows[i-ob_offset]
                    if np.isnan(equilibrium) or ob_top <= equilibrium:
                        entry_p = ob_top if USE_RETRACEMENT else closes[i]
                        sl_p = ob_bottom; risk_p = entry_p - sl_p
                        if risk_p > 0:
                            tp_p = entry_p + (risk_p * TARGET_RR)
                            id_senal = str(df.index[i])
                            if memoria[tf_name]["last_candle"] != id_senal and memoria[tf_name]["active_trade"] is None:
                                memoria[tf_name]["last_candle"] = id_senal
                                memoria[tf_name]["active_trade"] = {"dir": "LONG", "time": id_senal}
                                guardar_memoria(memoria)
                                lote = calcular_lotaje(entry_p, sl_p, BALANCE_CUENTA, RIESGO_POR_OPERACION)
                                msg = (f"🚨 <b>¡NUEVA SEÑAL SMC ({tf_name.upper()})!</b> 🚨\n"
                                       f"🟢 <b>Dirección:</b> COMPRA\n"
                                       f"🎯 <b>Entrada:</b> {entry_p:.2f}\n"
                                       f"🔴 <b>SL:</b> {sl_p:.2f}\n"
                                       f"✅ <b>TP:</b> {tp_p:.2f}\n"
                                       f"⚖️ <b>Lote:</b> {lote}")
                                enviar_telegram(msg)

            elif disp_bear and htf_bias_bear and in_session:
                ob_offset_s = 0; highest_ob_high = -1e10
                for k in range(1, OB_LOOKBACK + 1):
                    if closes[i-k] >= opens[i-k] and highs[i-k] > highest_ob_high:
                        highest_ob_high = highs[i-k]; ob_offset_s = k
                if ob_offset_s > 0:
                    ob_top_s = highs[i-ob_offset_s]; ob_bottom_s = lows[i-ob_offset_s]
                    if np.isnan(equilibrium) or ob_bottom_s >= equilibrium:
                        entry_ps = ob_bottom_s if USE_RETRACEMENT else closes[i]
                        sl_ps = ob_top_s; risk_ps = sl_ps - entry_ps
                        if risk_ps > 0:
                            tp_ps = entry_ps - (risk_ps * TARGET_RR)
                            id_senal = str(df.index[i])
                            if memoria[tf_name]["last_candle"] != id_senal and memoria[tf_name]["active_trade"] is None:
                                memoria[tf_name]["last_candle"] = id_senal
                                memoria[tf_name]["active_trade"] = {"dir": "SHORT", "time": id_senal}
                                guardar_memoria(memoria)
                                lote = calcular_lotaje(entry_ps, sl_ps, BALANCE_CUENTA, RIESGO_POR_OPERACION)
                                msg = (f"🚨 <b>¡NUEVA SEÑAL SMC ({tf_name.upper()})!</b> 🚨\n"
                                       f"🔴 <b>Dirección:</b> VENTA\n"
                                       f"🎯 <b>Entrada:</b> {entry_ps:.2f}\n"
                                       f"🔴 <b>SL:</b> {sl_ps:.2f}\n"
                                       f"✅ <b>TP:</b> {tp_ps:.2f}\n"
                                       f"⚖️ <b>Lote:</b> {lote}")
                                enviar_telegram(msg)

    if memoria[tf_name]["active_trade"] is not None:
        time_trade = pd.to_datetime(memoria[tf_name]["active_trade"]["time"])
        # Liberamos la memoria después de 10 velas de la TF
        if (df.index[-1] - time_trade).total_seconds() / 60 > (cfg["minutes"] * 10):
            print(f"🧹 Liberando memoria en {tf_name.upper()}")
            memoria[tf_name]["active_trade"] = None
            guardar_memoria(memoria)

def escanear_mercado():
    print(f"\n[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC] Iniciando escaneo SMC Multi-TF...")
    memoria = cargar_memoria()
    
    for cfg in CONFIG_TFS:
        escanear_tf(cfg, memoria)
        time.sleep(2) # Pausa para no saturar TradingView

if __name__ == "__main__":
    escanear_mercado()