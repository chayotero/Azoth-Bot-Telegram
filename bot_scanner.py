import pandas as pd
import numpy as np
import requests
import json
import os
from datetime import datetime, timedelta
import time
import warnings
from tvDatafeed import TvDatafeed, Interval
import ta
import pytz

warnings.filterwarnings('ignore')

# ==========================================
# ⚙️ CONFIGURACIÓN DEL BOT Y CUENTA
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") 
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BALANCE_CUENTA = 1000                   
RIESGO_POR_OPERACION = 0.02             
TARGET_RR = 1.23
OB_LOOKBACK = 7
USE_RETRACEMENT = True 
COOLDOWN_VELAS = 13 

# 🏆 ACTIVOS Y TEMPORALIDADES (1m excluido para XAUUSD)
ASSETS = [
    {"symbol": "XAUUSD", "exchange": "OANDA", "contract_size": 100,    "tfs": ["5m", "15m", "30m", "1h", "4h"]},
    {"symbol": "EURUSD", "exchange": "OANDA", "contract_size": 100000, "tfs": ["30m", "1h"]},
    {"symbol": "GBPUSD", "exchange": "OANDA", "contract_size": 100000, "tfs": ["30m", "1h"]},
    {"symbol": "USDJPY", "exchange": "OANDA", "contract_size": 100000, "tfs": ["30m", "1h"]},
    {"symbol": "AUDUSD", "exchange": "OANDA", "contract_size": 100000, "tfs": ["30m", "1h"]},
    {"symbol": "USDCAD", "exchange": "OANDA", "contract_size": 100000, "tfs": ["30m", "1h"]}
]

# Mapeo de temporalidades
TF_MAP = {
    "5m":  {"interval": Interval.in_5_minute,  "minutes": 5},
    "15m": {"interval": Interval.in_15_minute, "minutes": 15},
    "30m": {"interval": Interval.in_30_minute, "minutes": 30},
    "1h":  {"interval": Interval.in_1_hour,    "minutes": 60},
    "4h":  {"interval": Interval.in_4_hour,    "minutes": 240}
}

ARCHIVO_MEMORIA = "last_signals_smc.json"

def enviar_telegram(mensaje):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Faltan los secretos de Telegram.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "HTML"}
    try:
        return requests.post(url, json=payload, timeout=10).status_code == 200
    except Exception as e:
        print(f"❌ Error de red Telegram: {e}")
        return False

def calcular_lotaje(entry, sl, balance, riesgo, contract_size):
    dist_sl = abs(entry - sl)
    if dist_sl == 0: return 0.01
    # Calculo estándar para MT4/MT5
    lotaje = (balance * riesgo) / (dist_sl * contract_size)
    return round(max(lotaje, 0.01), 2)

def cargar_memoria():
    memoria_base = {"init_sent": False}
    for asset in ASSETS:
        for tf in asset["tfs"]:
            key = f"{asset['symbol']}_{tf}"
            memoria_base[key] = {"active_trade": None, "cooldown_until": None}
            
    if os.path.exists(ARCHIVO_MEMORIA):
        try:
            with open(ARCHIVO_MEMORIA, 'r') as f:
                mem = json.load(f)
                for k in memoria_base:
                    if k not in mem: mem[k] = memoria_base[k]
                return mem
        except Exception as e:
            print(f"Error cargando memoria: {e}")
    return memoria_base

def guardar_memoria(memoria):
    with open(ARCHIVO_MEMORIA, 'w') as f:
        json.dump(memoria, f, indent=4)

def escanear_activo(tv, symbol, exchange, contract_size, tf_name, memoria):
    tf_cfg = TF_MAP[tf_name]
    key = f"{symbol}_{tf_name}"
    
    print(f"🔍 Analizando {symbol} en {tf_name.upper()}...")
    
    try:
        df = tv.get_hist(symbol=symbol, exchange=exchange, interval=tf_cfg["interval"], n_bars=500)
    except Exception as e:
        print(f"Error TvDatafeed en {symbol} {tf_name}: {e}")
        return

    if df is None or len(df) < 200: return

    df = df[['open', 'high', 'low', 'close']].copy()
    df.index = pd.to_datetime(df.index).tz_localize('UTC').tz_convert('America/Mexico_City').tz_localize(None)
    df = df.iloc[:-1] # Quitamos la vela en formación
    
    # ==========================================
    # 🛡️ 1. VERIFICAR SI EL TRADE ACTIVO YA CERRÓ
    # ==========================================
    active = memoria[key]["active_trade"]
    if active is not None:
        entry_time = pd.to_datetime(active["time"])
        if entry_time in df.index:
            entry_idx = df.index.get_loc(entry_time)
            
            if entry_idx < len(df) - 1:
                v_highs = df['high'].values[entry_idx+1:]
                v_lows = df['low'].values[entry_idx+1:]
                
                is_long = active["dir"] == "LONG"
                hit_sl, hit_tp, triggered = False, False, active["status"] == "TRIGGERED"
                
                for j in range(len(v_highs)):
                    if not triggered:
                        if (is_long and v_lows[j] <= active["entry"]) or (not is_long and v_highs[j] >= active["entry"]):
                            triggered = True
                        elif (is_long and v_highs[j] >= active["tp"]) or (not is_long and v_lows[j] <= active["tp"]):
                            print(f"⚪ {key}: Trade Cancelado (TP antes de entrada).")
                            memoria[key]["active_trade"] = None
                            guardar_memoria(memoria)
                            break
                            
                    if triggered:
                        if (is_long and v_lows[j] <= active["sl"]) or (not is_long and v_highs[j] >= active["sl"]):
                            hit_sl = True
                        elif (is_long and v_highs[j] >= active["tp"]) or (not is_long and v_lows[j] <= active["tp"]):
                            hit_tp = True
                            
                        if hit_sl or hit_tp:
                            print(f"🔄 {key}: Trade Cerrado (SL: {hit_sl}, TP: {hit_tp}).")
                            memoria[key]["active_trade"] = None
                            if hit_sl: 
                                cd_time = df.index[-1] + timedelta(minutes=tf_cfg["minutes"] * COOLDOWN_VELAS)
                                memoria[key]["cooldown_until"] = str(cd_time)
                            guardar_memoria(memoria)
                            break
        else:
            memoria[key]["active_trade"] = None
            guardar_memoria(memoria)

    # ==========================================
    # 🛑 2. VERIFICAR COOLDOWN
    # ==========================================
    if memoria[key]["cooldown_until"] is not None:
        cd_time = pd.to_datetime(memoria[key]["cooldown_until"])
        if df.index[-1] < cd_time:
            print(f"   🛑 {key}: En cooldown hasta {cd_time}.")
            return 
        else:
            memoria[key]["cooldown_until"] = None

    if memoria[key]["active_trade"] is not None:
        return # Si sigue activo, no buscamos más

    # ==========================================
    # 🎯 3. BUSCAR NUEVOS SETUPS
    # ==========================================
    df['ATR'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=33)
    
    htf_minutes = max(120, tf_cfg["minutes"] * 4)
    htf_rule = f'{htf_minutes}min' if htf_minutes < 1440 else '1D'
    
    try:
        df_htf = df.resample(htf_rule).agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
        df_htf['HTF_EMA'] = ta.trend.ema_indicator(df_htf['close'], window=5)
        df = df.join(df_htf['HTF_EMA'], rsuffix='_htf').ffill()
    except Exception as e:
        print(f"Error resampleando HTF en {key}: {e}")
        return

    opens, highs, lows, closes = df['open'].values, df['high'].values, df['low'].values, df['close'].values
    atrs, htf_emas = df['ATR'].values, df['HTF_EMA'].values
    hours, days = df.index.hour.values, df.index.dayofweek.values
    
    trend = 0
    last_pivot_high, last_pivot_low = np.nan, np.nan
    leg_high, leg_low = np.nan, np.nan
    
    # Buscamos en las últimas 3 velas cerradas
    for i in range(len(df) - 3, len(df)):
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
                        
                        status_init = "PENDING" if USE_RETRACEMENT else "TRIGGERED"
                        memoria[key]["active_trade"] = {
                            "dir": "LONG", "entry": float(entry_p), "sl": float(sl_p), "tp": float(tp_p), 
                            "time": str(df.index[i]), "status": status_init
                        }
                        guardar_memoria(memoria)
                        
                        lote = calcular_lotaje(entry_p, sl_p, BALANCE_CUENTA, RIESGO_POR_OPERACION, contract_size)
                        
                        # ⏱️ Calcular la hora de la vela que se va a operar (la actual)
                        next_candle_time = (df.index[i] + timedelta(minutes=tf_cfg["minutes"])).strftime('%Y-%m-%d %H:%M')
                        
                        msg = (f"🚨 <b>¡NUEVA SEÑAL SMC ({symbol} - {tf_name.upper()})!</b> 🚨\n"
                               f"🟢 <b>Dirección:</b> COMPRA\n"
                               f"⏱️ <b>Vela de Operar (MX):</b> {next_candle_time}\n"
                               f"🎯 <b>Entrada:</b> {entry_p:.5f}\n"
                               f"🔴 <b>SL:</b> {sl_p:.5f}\n"
                               f"✅ <b>TP:</b> {tp_p:.5f}\n"
                               f"⚖️ <b>Lote:</b> {lote}\n"
                               f"<i>AlgoBot SMC (Elite)</i>")
                        enviar_telegram(msg)
                        return

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
                        
                        status_init_s = "PENDING" if USE_RETRACEMENT else "TRIGGERED"
                        memoria[key]["active_trade"] = {
                            "dir": "SHORT", "entry": float(entry_ps), "sl": float(sl_ps), "tp": float(tp_ps), 
                            "time": str(df.index[i]), "status": status_init_s
                        }
                        guardar_memoria(memoria)
                        
                        lote = calcular_lotaje(entry_ps, sl_ps, BALANCE_CUENTA, RIESGO_POR_OPERACION, contract_size)
                        
                        # ⏱️ Calcular la hora de la vela que se va a operar (la actual)
                        next_candle_time = (df.index[i] + timedelta(minutes=tf_cfg["minutes"])).strftime('%Y-%m-%d %H:%M')
                        
                        msg = (f"🚨 <b>¡NUEVA SEÑAL SMC ({symbol} - {tf_name.upper()})!</b> 🚨\n"
                               f"🔴 <b>Dirección:</b> VENTA\n"
                               f"⏱️ <b>Vela de Operar (MX):</b> {next_candle_time}\n"
                               f"🎯 <b>Entrada:</b> {entry_ps:.5f}\n"
                               f"🔴 <b>SL:</b> {sl_ps:.5f}\n"
                               f"✅ <b>TP:</b> {tp_ps:.5f}\n"
                               f"⚖️ <b>Lote:</b> {lote}\n"
                               f"<i>AlgoBot SMC (Elite)</i>")
                        enviar_telegram(msg)
                        return

def escanear_mercado():
    print(f"\n[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC] Iniciando escaneo SMC Multi-Asset...")
    memoria = cargar_memoria()
    
    if not memoria.get("init_sent", False):
        msg_init = "✅ <b>¡Bot SMC Conectado!</b> ✅\nEl motor multi-activo y multi-temporalidad ha arrancado. Te avisaré cuando detecte la primera señal."
        if enviar_telegram(msg_init):
            memoria["init_sent"] = True
            guardar_memoria(memoria)
            print("📡 Mensaje de conexión inicial enviado a Telegram.")

    try:
        tv = TvDatafeed()
    except Exception as e:
        print(f"❌ Error conectando a TvDatafeed: {e}")
        return

    for asset in ASSETS:
        for tf in asset["tfs"]:
            escanear_activo(tv, asset["symbol"], asset["exchange"], asset["contract_size"], tf, memoria)
            time.sleep(1) # Pausa de 1 seg entre peticiones para no saturar

if __name__ == "__main__":
    escanear_mercado()