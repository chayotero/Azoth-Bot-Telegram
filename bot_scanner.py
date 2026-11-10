import pandas as pd
import numpy as np
import requests
import json
import os
from datetime import datetime
import time
import warnings
warnings.filterwarnings('ignore')

from tvDatafeed import TvDatafeed, Interval

# ==========================================
# ⚙️ CONFIGURACIÓN DEL BOT Y CUENTA
# ==========================================
# Se leen de los Secrets de GitHub Actions (Settings -> Secrets -> Actions)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "TU_TOKEN_AQUI") 
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "TU_CHAT_ID_AQUI")
BALANCE_CUENTA = 1000                   
RIESGO_POR_OPERACION = 0.02             
CONTRATO_ORO = 100                      

# ==========================================
# 🏆 CONFIGURACIÓN GANADORA (El Monstruo de +158R)
# ==========================================
# Formato: (TF, Intervalo, Minutos, Dirección, Caída/Subida, TP_Ratio, SL_Mult, H_Inicio, H_Fin, RSI_Min, RSI_Max, Cooldown, Target_Min, Evitar_Dom, Evitar_Mad)
CONFIG_OPTIMA = [
    # MOTOR SHORT (30M): El francotirador de caídas (TP 20%)
    ("30m", Interval.in_30_minute, 30, "SHORT", 0.0025, 0.20, 0.95, 23, 17, None, 75, 13, 3.6, False, True),
    
    # MOTOR LONG (1H): El cazador de tendencias alcistas (TP 25%)
    ("1h",  Interval.in_1_hour,    60, "LONG",  0.0039, 0.25, 0.81, 23, 11, 45, 70, 13, 3.6, False, False)
]

ARCHIVO_MEMORIA = "last_signals.json"

def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print(f"   ❌ Error de red Telegram: {e}")
        return False

def calcular_lotaje(entry, sl, balance, riesgo):
    dist_sl = abs(entry - sl)
    if dist_sl == 0: return 0.01
    lotaje = (balance * riesgo) / (dist_sl * CONTRATO_ORO)
    return round(max(lotaje, 0.01), 2)

def cargar_memoria():
    if os.path.exists(ARCHIVO_MEMORIA):
        try:
            with open(ARCHIVO_MEMORIA, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {cfg[0]: {"last_candle": "", "last_trade": None} for cfg in CONFIG_OPTIMA}

def guardar_memoria(memoria):
    with open(ARCHIVO_MEMORIA, 'w') as f:
        json.dump(memoria, f, indent=4)

def escanear_mercado():
    print(f"\n[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC] Iniciando escaneo...")
    memoria = cargar_memoria()
    
    try:
        tv = TvDatafeed()
    except Exception as e:
        print(f"❌ Error conectando a TradingView: {e}")
        return

    for cfg in CONFIG_OPTIMA:
        (tf, tv_interval, tf_minutes, direccion, caida_min, tp_ratio, sl_mult, 
         h_start, h_end, rsi_min, rsi_max, cooldown_velas, min_target, 
         evitar_dom, evitar_mad) = cfg
        
        print(f"🔍 Analizando {tf.upper()} - Motor {direccion}...")
        
        try:
            # Pedimos 1000 velas para tener suficiente historial y no perder trades abiertos
            df = tv.get_hist(symbol='XAUUSD', exchange='OANDA', interval=tv_interval, n_bars=1000)
            
            if df is None or df.empty or len(df) < 100:
                print(f"   ⚠️ Datos insuficientes para {tf}.")
                continue
            
            df = df[['open', 'high', 'low', 'close']]
            df.index = pd.to_datetime(df.index).tz_localize('UTC').tz_convert('America/Mexico_City').tz_localize(None)
            
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            df['RSI'] = 100 - (100 / (1 + (gain / loss)))
            
            df = df.iloc[:-1].copy() # Quitar vela en formación
            if len(df) < 100: continue
            
            opens = df['open'].values
            highs = df['high'].values
            lows = df['low'].values
            closes = df['close'].values
            rsis = df['RSI'].values
            hours = df.index.hour.values
            days = df.index.dayofweek.values
            
            # ==========================================
            # PASO 1: ACTUALIZAR TRADE ANTERIOR Y TRAILING STOP
            # ==========================================
            last_trade_info = memoria[tf].get("last_trade")
            
            if last_trade_info and last_trade_info.get("status") == "OPEN":
                entry_time = pd.to_datetime(last_trade_info["entry_time"])
                
                if entry_time in df.index:
                    entry_idx = df.index.get_loc(entry_time)
                    
                    if entry_idx < len(df) - 1:
                        v_highs = highs[entry_idx+1:]
                        v_lows = lows[entry_idx+1:]
                        
                        tp_dist = abs(last_trade_info["tp"] - last_trade_info["entry"])
                        trigger_price = (last_trade_info["entry"] + (tp_dist * 0.40)) if direccion == "LONG" else (last_trade_info["entry"] - (tp_dist * 0.40))
                        
                        status = "OPEN"
                        exit_idx = None
                        
                        # Evaluación vela por vela (Trailing Stop Dinámico)
                        for j in range(len(v_highs)):
                            if direccion == "LONG":
                                if v_highs[j] >= trigger_price:
                                    nuevo_sl = v_highs[j] - (tp_dist * 0.10)
                                    if nuevo_sl > last_trade_info["sl"] and nuevo_sl >= last_trade_info["entry"]:
                                        last_trade_info["sl"] = float(nuevo_sl)
                            else:
                                if v_lows[j] <= trigger_price:
                                    nuevo_sl = v_lows[j] + (tp_dist * 0.10)
                                    if nuevo_sl < last_trade_info["sl"] and nuevo_sl <= last_trade_info["entry"]:
                                        last_trade_info["sl"] = float(nuevo_sl)

                            if direccion == "LONG":
                                hit_sl = v_lows[j] <= last_trade_info["sl"]
                                hit_tp = v_highs[j] >= last_trade_info["tp"]
                            else:
                                hit_sl = v_highs[j] >= last_trade_info["sl"]
                                hit_tp = v_lows[j] <= last_trade_info["tp"]
                            
                            if hit_sl or hit_tp:
                                status = "TRAIL" if (hit_sl and ((direccion=="LONG" and last_trade_info["sl"] > last_trade_info["entry"]) or (direccion=="SHORT" and last_trade_info["sl"] < last_trade_info["entry"]))) else ("LOSS" if hit_sl else "WIN")
                                exit_idx = entry_idx + 1 + j
                                break
                        
                        if status != "OPEN":
                            last_trade_info["status"] = status
                            last_trade_info["exit_time"] = str(df.index[exit_idx])
                            memoria[tf]["last_trade"] = last_trade_info
                            guardar_memoria(memoria)
                            print(f"   🔄 {tf.upper()}: Trade anterior cerrado como {status}.")
                        else:
                            print(f"   ⏳ {tf.upper()}: Trade en curso.")
                            continue
                    else:
                        continue
                else:
                    continue
            
            # ==========================================
            # PASO 2: COOLDOWN
            # ==========================================
            if last_trade_info and last_trade_info.get("status") == "LOSS":
                exit_time = pd.to_datetime(last_trade_info["exit_time"])
                minutes_diff = (df.index[-1] - exit_time).total_seconds() / 60
                if minutes_diff < (cooldown_velas * tf_minutes):
                    continue
            
            # ==========================================
            # PASO 3: BUSCAR NUEVO PATRÓN (GEOMETRÍA X=10)
            # ==========================================
            idx_conf_final = len(df) - 1
            idx_low2 = idx_conf_final - 1
            vela2 = df.iloc[idx_low2]
            x_dist = 10
            
            if direccion == "LONG":
                low1 = np.min(lows[idx_low2 - x_dist : idx_low2])
                low2 = vela2['low']
                
                if low2 > low1 and ((low2 - low1) / low1 <= 0.005):
                    caida = (opens[idx_low2 - 26] - closes[idx_low2 - 6]) / opens[idx_low2 - 26]
                    vela_check = df.iloc[idx_low2 + 1]
                    
                    if caida >= caida_min and vela_check['close'] > vela_check['open']:
                        entrada = vela_check['close']
                        target = np.max(highs[max(0, idx_low2 - 50) : idx_low2])
                        
                        if (target - entrada) >= min_target:
                            sl = entrada - ((entrada - low1) * sl_mult)
                            tp = entrada + ((target - entrada) * tp_ratio)
                            
                            if entrada > sl and target > entrada:
                                ejecutar_senal(df, tf, direccion, entrada, sl, tp, tp_ratio, hours, days, rsis, memoria)
                                
            else: # SHORT
                high1 = np.max(highs[idx_low2 - x_dist : idx_low2])
                high2 = vela2['high']
                
                if high2 < high1 and ((high1 - high2) / high1 <= 0.005):
                    subida = (closes[idx_low2 - 6] - opens[idx_low2 - 26]) / opens[idx_low2 - 26]
                    vela_check = df.iloc[idx_low2 + 1]
                    
                    if subida >= caida_min and vela_check['close'] < vela_check['open']:
                        entrada = vela_check['close']
                        target = np.min(lows[max(0, idx_low2 - 50) : idx_low2])
                        
                        if (entrada - target) >= min_target:
                            sl = entrada + ((high1 - entrada) * sl_mult)
                            tp = entrada - ((entrada - target) * tp_ratio)
                            
                            if entrada < sl and target < entrada:
                                ejecutar_senal(df, tf, direccion, entrada, sl, tp, tp_ratio, hours, days, rsis, memoria)
                                
        except Exception as e:
            print(f"   ❌ Error procesando {tf}: {e}")
        
        time.sleep(1)

def ejecutar_senal(df, tf, direccion, entrada, sl, tp, tp_ratio, hours, days, rsis, memoria):
    cfg_data = [cfg for cfg in CONFIG_OPTIMA if cfg[0] == tf][0]
    h_start, h_end = cfg_data[7], cfg_data[8]
    rsi_min, rsi_max = cfg_data[9], cfg_data[10]
    evitar_dom, evitar_mad = cfg_data[13], cfg_data[14]

    hora_actual, dia_actual, rsi_actual = hours[-1], days[-1], rsis[-1]

    if h_start <= h_end: h_ok = h_start <= hora_actual <= h_end
    else: h_ok = hora_actual >= h_start or hora_actual <= h_end
    if not h_ok: return
    if evitar_dom and dia_actual == 6: return
    if evitar_mad and (hora_actual in (2, 3)): return
    if rsi_min is not None and rsi_actual < rsi_min: return
    if rsi_max is not None and rsi_actual > rsi_max: return

    vela_entrada = df.iloc[-1]
    id_senal = str(vela_entrada.name)
    
    if memoria[tf].get("last_candle") == id_senal: return
    memoria[tf]["last_candle"] = id_senal
    guardar_memoria(memoria)

    memoria[tf]["last_trade"] = {
        "entry_time": id_senal, "entry": float(entrada), "sl": float(sl),
        "tp": float(tp), "status": "OPEN", "exit_time": None
    }
    guardar_memoria(memoria)

    lote = calcular_lotaje(entrada, sl, BALANCE_CUENTA, RIESGO_POR_OPERACION)
    hora_mx = vela_entrada.name.strftime('%H:%M')
    icono = "📈" if direccion == "LONG" else "📉"
    texto_dir = "COMPRA (LONG)" if direccion == "LONG" else "VENTA (SHORT)"
    
    mensaje = (
        f"🚨 <b>¡NUEVA SEÑAL (XAUUSD SPOT)!</b> 🚨\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Activo:</b> XAUUSD (OANDA)\n"
        f"⏱️ <b>Temporalidad:</b> {tf.upper()}\n"
        f"🕒 <b>Hora (MX):</b> {hora_mx}\n"
        f"{icono} <b>Dirección:</b> {texto_dir}\n"
        f"🎯 <b>Entrada:</b> {entrada:.2f}\n"
        f"🔴 <b>SL Inicial:</b> {sl:.2f}\n"
        f"✅ <b>TP ({int(tp_ratio*100)}%):</b> {tp:.2f}\n"
        f"🛡️ <b>Trailing Stop:</b> Activado al 40%\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚖️ <b>Lotaje Sugerido:</b> {lote} lots\n"
        f"💰 <b>Riesgo:</b> ${BALANCE_CUENTA * RIESGO_POR_OPERACION:.2f}\n"
        f"<i>Generado por AlgoBot (Elite)</i>"
    )
    
    if enviar_telegram(mensaje):
        print(f"   ✅ ¡SEÑAL {direccion} ENVIADA para {tf.upper()}!")

if __name__ == "__main__":
    escanear_mercado()