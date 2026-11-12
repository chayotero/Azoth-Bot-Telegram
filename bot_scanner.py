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

warnings.filterwarnings('ignore')

# ==========================================
# ⚙️ CONFIGURACIÓN DEL BOT
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BALANCE_CUENTA = 1000                   
RIESGO_POR_OPERACION = 0.02             
TARGET_RR = 1.23
OB_LOOKBACK = 7
MAX_PENDING_BARS = 50
MAX_TRADE_AGE_MINUTES = 7 * 24 * 60

# 🏆 CONFIGURACIÓN GANADORA (IGUAL A TRADINGVIEW)
USE_SESSION = False        # False = Opera 24 horas
USE_HTF_BIAS = True        # True = Usa filtro de EMA
HTF_TF_MINUTES = 120       # Fijo en 2 horas

# 🏆 ACTIVOS OPTIMIZADOS (Sin EURUSD 30m)
ASSETS = [
    {"symbol": "XAUUSD", "exchange": "OANDA", "contract_size": 100,    "tfs": ["5m", "15m", "30m", "1h", "4h"]},
    {"symbol": "EURUSD", "exchange": "OANDA", "contract_size": 100000, "tfs": ["1h"]},
    {"symbol": "GBPUSD", "exchange": "OANDA", "contract_size": 100000, "tfs": ["30m", "1h"]},
    {"symbol": "USDJPY", "exchange": "OANDA", "contract_size": 100000, "tfs": ["30m", "1h"]},
    {"symbol": "AUDUSD", "exchange": "OANDA", "contract_size": 100000, "tfs": ["30m", "1h"]},
    {"symbol": "USDCAD", "exchange": "OANDA", "contract_size": 100000, "tfs": ["30m", "1h"]}
]

TF_MAP = {
    "5m":  {"interval": Interval.in_5_minute,  "minutes": 5},
    "15m": {"interval": Interval.in_15_minute, "minutes": 15},
    "30m": {"interval": Interval.in_30_minute, "minutes": 30},
    "1h":  {"interval": Interval.in_1_hour,    "minutes": 60},
    "4h":  {"interval": Interval.in_4_hour,    "minutes": 240}
}

ARCHIVO_MEMORIA = "last_signals_smc.json"

# ==========================================
# 🛠️ FUNCIONES DE APOYO
# ==========================================
def enviar_telegram(mensaje):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "HTML"}
    try:
        return requests.post(url, json=payload, timeout=10).status_code == 200
    except: return False

def calcular_lotaje(entry, sl, balance, riesgo, contract_size):
    dist_sl = abs(entry - sl)
    if dist_sl == 0: return 0.01
    return round(max((balance * riesgo) / (dist_sl * contract_size), 0.01), 2)

def cargar_memoria():
    memoria_base = {"init_sent": False}
    for asset in ASSETS:
        for tf in asset["tfs"]:
            memoria_base[f"{asset['symbol']}_{tf}"] = {"active_trade": None, "cooldown_until": None}
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
    with open(ARCHIVO_MEMORIA, 'w') as f: json.dump(memoria, f, indent=4)

# ==========================================
# 🧠 GESTIÓN DE TRADE ACTIVO (ALERTA AL TOCAR)
# ==========================================
def gestionar_trade_activo(df, key, memoria, tf_cfg, symbol, tf_name, contract_size):
    active = memoria[key]["active_trade"]
    if active is None: return None
    
    entry_time = pd.to_datetime(active["time"])
    current_time = df.index[-1]
    age_minutes = (current_time - entry_time).total_seconds() / 60
    
    # Anti-bloqueo
    if age_minutes > MAX_TRADE_AGE_MINUTES or entry_time not in df.index:
        memoria[key]["active_trade"] = None; guardar_memoria(memoria); return None
        
    entry_idx = df.index.get_loc(entry_time)
    if entry_idx >= len(df) - 1: return active
    
    if active["status"] == "PENDING" and (len(df) - 1 - entry_idx) > MAX_PENDING_BARS:
        memoria[key]["active_trade"] = None; guardar_memoria(memoria); return None

    v_highs, v_lows = df['high'].values[entry_idx+1:], df['low'].values[entry_idx+1:]
    is_long = active["dir"] == "LONG"
    triggered = active["status"] == "TRIGGERED"

    for j in range(len(v_highs)):
        if not triggered:
            # Lógica estricta de TradingView (toca entrada pero no SL en la misma vela)
            hit_entry = (is_long and v_lows[j] <= active["entry"] and v_lows[j] > active["sl"]) or \
                        (not is_long and v_highs[j] >= active["entry"] and v_highs[j] < active["sl"])
            
            hit_tp_before = (is_long and v_highs[j] >= active["tp"]) or (not is_long and v_lows[j] <= active["tp"])
            
            if hit_entry:
                triggered = True
                active["status"] = "TRIGGERED"
                memoria[key]["active_trade"] = active
                guardar_memoria(memoria)
                
                # 🚨 ENVIAR ALERTA A TELEGRAM JUSTO AHORA
                lote = calcular_lotaje(active['entry'], active['sl'], BALANCE_CUENTA, RIESGO_POR_OPERACION, contract_size)
                next_candle_time = (df.index[entry_idx] + timedelta(minutes=tf_cfg["minutes"])).strftime('%Y-%m-%d %H:%M')
                
                emoji = "🟢" if is_long else "🔴"
                dir_str = "COMPRA" if is_long else "VENTA"
                msg = (f"🚨 <b>¡ENTRADA ACTIVADA SMC ({symbol} - {tf_name.upper()})!</b> 🚨\n"
                       f"{emoji} <b>Dirección:</b> {dir_str}\n"
                       f"🎯 <b>El precio tocó el Order Block.</b>\n"
                       f"⏱️ <b>Setup generado en vela de (MX):</b> {next_candle_time}\n"
                       f"🎯 <b>Entrada (Límite TOCADA):</b> {active['entry']:.5f}\n"
                       f"🔴 <b>SL:</b> {active['sl']:.5f}\n"
                       f"✅ <b>TP:</b> {active['tp']:.5f}\n"
                       f"⚖️ <b>Lote:</b> {lote}\n"
                       f"<i>AlgoBot SMC (Elite)</i>")
                enviar_telegram(msg)
                print(f"   ✅ {key}: ¡Retroceso tocado! Alerta enviada a Telegram.")
                
            elif hit_tp_before:
                memoria[key]["active_trade"] = None; guardar_memoria(memoria); return None

        if triggered:
            hit_sl = (is_long and v_lows[j] <= active["sl"]) or (not is_long and v_highs[j] >= active["sl"])
            hit_tp = (is_long and v_highs[j] >= active["tp"]) or (not is_long and v_lows[j] <= active["tp"])
            if hit_sl or hit_tp:
                print(f"   🔄 {key}: Trade cerrado ({'TP' if hit_tp else 'SL'}).")
                memoria[key]["active_trade"] = None
                if hit_sl:
                    cd_time = df.index[-1] + timedelta(minutes=tf_cfg["minutes"] * 13)
                    memoria[key]["cooldown_until"] = str(cd_time)
                guardar_memoria(memoria); return None
    return active

# ==========================================
# 🔍 ESCANEO DE ACTIVO
# ==========================================
def escanear_activo(tv, symbol, exchange, contract_size, tf_name, memoria):
    tf_cfg = TF_MAP[tf_name]
    key = f"{symbol}_{tf_name}"
    try:
        df = tv.get_hist(symbol=symbol, exchange=exchange, interval=tf_cfg["interval"], n_bars=500)
    except: return

    if df is None or len(df) < 200: return

    df = df[['open', 'high', 'low', 'close']].copy()
    df.index = pd.to_datetime(df.index).tz_localize('UTC').tz_convert('America/Mexico_City').tz_localize(None)
    df = df.iloc[:-1] # Quitar vela en formación

    # 1. Gestionar trade activo (Dispara Telegram si toca la entrada)
    active = gestionar_trade_activo(df, key, memoria, tf_cfg, symbol, tf_name, contract_size)
    if active is not None: return

    # 2. Cooldown
    if memoria[key]["cooldown_until"] is not None:
        cd_time = pd.to_datetime(memoria[key]["cooldown_until"])
        if df.index[-1] < cd_time: return
        memoria[key]["cooldown_until"] = None

    # 3. Indicadores
    df['ATR'] = ta.volatility.average_true_range(df['high'], df['low'], df['close'], window=33)
    
    htf_rule = f'{HTF_TF_MINUTES}min'
    try:
        df_htf = df.resample(htf_rule).agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
        df_htf['HTF_EMA'] = ta.trend.ema_indicator(df_htf['close'], window=5)
        df = df.join(df_htf['HTF_EMA'], rsuffix='_htf').ffill()
    except: return

    opens, highs, lows, closes = df['open'].values, df['high'].values, df['low'].values, df['close'].values
    atrs, htf_emas = df['ATR'].values, df['HTF_EMA'].values
    hours, days = df.index.hour.values, df.index.dayofweek.values

    # 4. Buscar Setup
    trend, last_ph, last_pl, leg_h, leg_l = 0, np.nan, np.nan, np.nan, np.nan
    scan_start = max(70, len(df) - 100) # Escanea 100 velas para contexto
    fire_threshold = len(df) - 2        # Solo dispara en las últimas 2 velas

    for i in range(scan_start, len(df)):
        ph = highs[i-1] if (highs[i-1] > highs[i-2] and highs[i-1] > highs[i]) else np.nan
        pl = lows[i-1]  if (lows[i-1]  < lows[i-2]  and lows[i-1]  < lows[i])  else np.nan
        if not np.isnan(ph): last_ph = ph
        if not np.isnan(pl): last_pl = pl

        is_hb = closes[i] > last_ph if not np.isnan(last_ph) else False
        is_lb = closes[i] < last_pl if not np.isnan(last_pl) else False

        cb, bb, cs, bs = False, False, False, False
        if is_hb:
            if trend == -1: cb = True; leg_h = highs[i]; leg_l = last_pl
            elif trend == 1: bb = True
            trend = 1
        if is_lb:
            if trend == 1: cs = True; leg_l = lows[i]; leg_h = last_ph
            elif trend == -1: bs = True
            trend = -1

        if trend == 1: leg_h = max(leg_h if not np.isnan(leg_h) else highs[i], highs[i])
        if trend == -1: leg_l = min(leg_l if not np.isnan(leg_l) else lows[i], lows[i])

        eq = (leg_h + leg_l) / 2.0 if not np.isnan(leg_h) and not np.isnan(leg_l) else np.nan
        strong = max(highs[i]-lows[i], highs[i-1]-lows[i-1]) >= atrs[i] * 0.6
        db, ds = strong and (cb or bb), strong and (cs or bs)
        
        hb = closes[i] > htf_emas[i] if USE_HTF_BIAS and not np.isnan(htf_emas[i]) else True
        hs = closes[i] < htf_emas[i] if USE_HTF_BIAS and not np.isnan(htf_emas[i]) else True
        
        ins = True
        if USE_SESSION:
            ins = (7 <= hours[i] <= 16) and (days[i] != 6)

        if i < fire_threshold: continue

        if db and hb and ins:
            ob_o = 0; lo_ob = 1e10
            for k in range(1, OB_LOOKBACK + 1):
                if closes[i-k] <= opens[i-k] and lows[i-k] < lo_ob: lo_ob = lows[i-k]; ob_o = k
            if ob_o > 0:
                ob_top = highs[i-ob_o]; ob_bot = lows[i-ob_o]
                if np.isnan(eq) or ob_top <= eq:
                    entry = ob_top; sl = ob_bot; risk = entry - sl
                    if risk > 0:
                        # Guardamos el Setup en memoria (PENDING) y salimos. El Telegram se enviará cuando se toque.
                        memoria[key]["active_trade"] = {
                            "dir": "LONG", "entry": float(entry), "sl": float(sl), "tp": float(entry + (risk * TARGET_RR)),
                            "time": str(df.index[i]), "status": "PENDING"
                        }
                        guardar_memoria(memoria)
                        return

        elif ds and hs and ins:
            ob_o = 0; hi_ob = -1e10
            for k in range(1, OB_LOOKBACK + 1):
                if closes[i-k] >= opens[i-k] and highs[i-k] > hi_ob: hi_ob = highs[i-k]; ob_o = k
            if ob_o > 0:
                ob_top = highs[i-ob_o]; ob_bot = lows[i-ob_o]
                if np.isnan(eq) or ob_bot >= eq:
                    entry = ob_bot; sl = ob_top; risk = sl - entry
                    if risk > 0:
                        memoria[key]["active_trade"] = {
                            "dir": "SHORT", "entry": float(entry), "sl": float(sl), "tp": float(entry - (risk * TARGET_RR)),
                            "time": str(df.index[i]), "status": "PENDING"
                        }
                        guardar_memoria(memoria)
                        return

def escanear_mercado():
    print(f"\n[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC] Escaneo SMC 24h (Lógica TradingView)")
    memoria = cargar_memoria()
    
    if not memoria.get("init_sent", False):
        msg_init = "✅ <b>¡Bot SMC v3.0 Conectado!</b> ✅\nLógica 24h, HTF 120m, alerta solo al tocar Retroceso. Motor multi-activo arrancado."
        if enviar_telegram(msg_init):
            memoria["init_sent"] = True
            guardar_memoria(memoria)

    try: tv = TvDatafeed()
    except Exception as e: 
        print(f"❌ Error TvDatafeed: {e}")
        return

    for asset in ASSETS:
        for tf in asset["tfs"]:
            try: 
                escanear_activo(tv, asset["symbol"], asset["exchange"], asset["contract_size"], tf, memoria)
            except Exception as e: 
                print(f"   ❌ Error en {asset['symbol']}_{tf}: {e}")
            time.sleep(1)

if __name__ == "__main__":
    escanear_mercado()