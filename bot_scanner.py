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
TELEGRAM_TOKEN = "8570417983:AAEE_9Bg5FaINA-O53tUefr86HW7AMPD-xc" # Token nuevo
TELEGRAM_CHAT_ID = "8996462811"
BALANCE_CUENTA = 1000                   
RIESGO_POR_OPERACION = 0.02             
CONTRATO_ORO = 100                      

# 🏆 CONFIGURACIÓN FINAL (Intacta)
CONFIG_OPTIMA = [
    ("15m", Interval.in_15_minute, 15, 0.0014, 0.49, 1.06, 19, 17, None, None, 13, 3.6, True, True),
    ("1h",  Interval.in_1_hour,    60, 0.0039, 0.35, 0.81, 23, 11, 45, 70, 13, 3.6, False, False)
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
    # Nueva estructura: guarda el ID de la vela y los datos de la operación
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
        tf, tv_interval, tf_minutes, caida_min, tp_ratio, sl_mult, h_start, h_end, rsi_min, rsi_max, cooldown_velas, min_target, evitar_dom, evitar_mad = cfg
        print(f"🔍 Analizando {tf.upper()}...")
        
        try:
            df = tv.get_hist(symbol='XAUUSD', exchange='OANDA', interval=tv_interval, n_bars=150)
            
            if df is None or df.empty or len(df) < 60:
                continue
            
            df = df[['open', 'high', 'low', 'close']]
            df.index = pd.to_datetime(df.index).tz_localize('UTC').tz_convert('America/Mexico_City').tz_localize(None)
            
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            df['RSI'] = 100 - (100 / (1 + (gain / loss)))
            
            df = df.iloc[:-1].copy()
            if len(df) < 60: continue
            
            opens = df['open'].values
            highs = df['high'].values
            lows = df['low'].values
            closes = df['close'].values
            rsis = df['RSI'].values
            hours = df.index.hour.values
            days = df.index.dayofweek.values
            
            idx_conf_final = len(df) - 1
            idx_low2 = idx_conf_final - 1
            vela2 = df.iloc[idx_low2]
            
            low1 = np.min(lows[idx_low2 - 20 : idx_low2])
            p_low1 = low1
            p_low2 = vela2['low']

            # 🧠 VERIFICACIÓN INTELIGENTE DEL COOLDOWN (Solo si la última operación perdió)
            last_trade_info = memoria[tf].get("last_trade")
            cooldown_activo = False
            
            if last_trade_info:
                # Si la operación anterior tocó el SL, aplicamos cooldown
                if last_trade_info.get("status") == "LOSS":
                    # Calcular cuánto tiempo ha pasado desde la señal
                    last_signal_time = pd.to_datetime(last_trade_info["time"])
                    current_time = df.index[-1]
                    minutes_diff = (current_time - last_signal_time).total_seconds() / 60
                    
                    if minutes_diff < (cooldown_velas * tf_minutes):
                        cooldown_activo = True
                        
                # Si la operación anterior ganó (TP) o sigue abierta, NO hay cooldown.
                # Podemos seguir buscando nuevas señales.
            
            if cooldown_activo:
                print(f"   🛑 {tf.upper()}: Cooldown activo (la última operación fue Loss).")
                continue
            
            # Lógica del patrón
            if p_low2 > p_low1:
                caida = (opens[idx_low2 - 26] - closes[idx_low2 - 6]) / opens[idx_low2 - 26]
                
                vela_check = df.iloc[idx_low2 + 1]
                confirmacion_ok = vela_check['close'] > vela_check['open']
                        
                if caida >= caida_min and confirmacion_ok:
                    
                    vela_entrada = df.iloc[-1]
                    id_senal = str(vela_entrada.name)
                    
                    # Guardar memoria de la vela ANTES de evaluar filtros
                    if memoria[tf].get("last_candle") == id_senal:
                        continue
                    memoria[tf]["last_candle"] = id_senal
                    
                    # Comprobar si la operación anterior (si existía) tocó el SL
                    if last_trade_info and last_trade_info.get("status") == "OPEN":
                        # Verificar si en las velas recientes el precio tocó el SL de la última operación
                        # Buscamos en todo el histórico reciente desde la señal anterior
                        idx_anterior = df.index.get_loc(pd.to_datetime(last_trade_info["time"]))
                        if idx_anterior < idx_conf_final: # Si la señal anterior fue en el pasado
                            v_lows = lows[idx_anterior+1 : idx_conf_final+1]
                            v_highs = highs[idx_anterior+1 : idx_conf_final+1]
                            
                            # ¿Tocó SL?
                            if len(v_lows) > 0 and np.min(v_lows) <= last_trade_info["sl"]:
                                last_trade_info["status"] = "LOSS"
                            # ¿Tocó TP?
                            elif len(v_highs) > 0 and np.max(v_highs) >= last_trade_info["tp"]:
                                last_trade_info["status"] = "WIN"
                    
                    memoria[tf]["last_trade"] = last_trade_info
                    guardar_memoria(memoria)

                    hora_actual = hours[-1]
                    dia_actual = days[-1]
                    rsi_actual = rsis[-1]
                    
                    if h_start <= h_end:
                        hora_valida = h_start <= hora_actual <= h_end
                    else:
                        hora_valida = hora_actual >= h_start or hora_actual <= h_end
                    if not hora_valida: continue
                        
                    if evitar_dom and dia_actual == 6: continue
                    if evitar_mad and (hora_actual == 2 or hora_actual == 3): continue
                        
                    if rsi_min is not None and rsi_actual < rsi_min: continue
                    if rsi_max is not None and rsi_actual > rsi_max: continue
                        
                    target = np.max(highs[max(0, idx_low2 - 50) : idx_low2])
                    entrada = vela_entrada['close']

                    if (target - entrada) < min_target: continue
                    
                    distancia_original = entrada - p_low1
                    sl = entrada - (distancia_original * sl_mult)
                    
                    if entrada <= sl or target <= entrada: continue
                    
                    tp = entrada + (target - entrada) * tp_ratio
                    
                    # 🚨 SEÑAL VÁLIDA: Guardamos los datos de ESTA operación
                    memoria[tf]["last_trade"] = {
                        "time": id_senal,
                        "entry": float(entrada),
                        "sl": float(sl),
                        "tp": float(tp),
                        "status": "OPEN"
                    }
                    guardar_memoria(memoria)
                    
                    lote = calcular_lotaje(entrada, sl, BALANCE_CUENTA, RIESGO_POR_OPERACION)
                    hora_mx = vela_entrada.name.strftime('%H:%M')
                    
                    mensaje = (
                        f"🚨 <b>¡NUEVA SEÑAL (XAUUSD SPOT)!</b> 🚨\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"📊 <b>Activo:</b> XAUUSD (OANDA)\n"
                        f"⏱️ <b>Temporalidad:</b> {tf.upper()}\n"
                        f"🕒 <b>Hora (MX):</b> {hora_mx}\n"
                        f"📈 <b>Dirección:</b> LONG\n"
                        f"🎯 <b>Entrada:</b> {entrada:.2f}\n"
                        f"🔴 <b>SL:</b> {sl:.2f}\n"
                        f"✅ <b>TP ({int(tp_ratio*100)}%):</b> {tp:.2f}\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"⚖️ <b>Lotaje Sugerido:</b> {lote} lots\n"
                        f"💰 <b>Riesgo:</b> ${BALANCE_CUENTA * RIESGO_POR_OPERACION:.2f}\n"
                        f"<i>Generado por AlgoBot (Elite)</i>"
                    )
                    
                    if enviar_telegram(mensaje):
                        print(f"   ✅ ¡SEÑAL ENVIADA para {tf.upper()}! (Cierre: {entrada:.2f})")
                    else:
                        print(f"   ⚠️ Falló Telegram.")
                        
        except Exception as e:
            print(f"   ❌ Error procesando {tf}: {e}")
        
        time.sleep(1)

if __name__ == "__main__":
    escanear_mercado()