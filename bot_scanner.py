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
TELEGRAM_TOKEN = "8570417983:AAG_lqX9bzw2dZxQ-GPYCHqBYpAgtyZaD5s"
TELEGRAM_CHAT_ID = "8996462811"
BALANCE_CUENTA = 1000                   # ⚠️ CAMBIA ESTO POR TU BALANCE REAL
RIESGO_POR_OPERACION = 0.02             
CONTRATO_ORO = 100                      

# 🏆 CONFIGURACIONES GANADORAS (Horario México UTC-6)
# Estructura: (TF, Intervalo, Minutos_TF, %Caída, TP_Ratio, SL_Mult, H_Inicio, H_Fin, RSI_Min, RSI_Max, Cooldown_Velas)
CONFIG_OPTIMA = [
    ("15m", Interval.in_15_minute, 15, 0.0014, 0.49, 1.06, 19, 17, None, None, 5),
    ("1h",  Interval.in_1_hour,    60, 0.0039, 0.35, 0.81, 22, 11, 30, 70, 5)
]

ARCHIVO_MEMORIA = "last_signals.json"

# ==========================================
# 🧠 FUNCIONES DE APOYO
# ==========================================
def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload)
    except Exception as e:
        print(f"❌ Error Telegram: {e}")

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
    return {cfg[0]: {"last_candle": "", "last_trade": ""} for cfg in CONFIG_OPTIMA}

def guardar_memoria(memoria):
    with open(ARCHIVO_MEMORIA, 'w') as f:
        json.dump(memoria, f, indent=4)

# ==========================================
# 🚀 LÓGICA PRINCIPAL
# ==========================================
def escanear_mercado():
    print(f"\n[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC] Iniciando escaneo (OANDA Spot)...")
    memoria = cargar_memoria()
    
    try:
        tv = TvDatafeed()
    except Exception as e:
        print(f"❌ Error conectando a TradingView: {e}")
        return

    for cfg in CONFIG_OPTIMA:
        tf, tv_interval, tf_minutes, caida_min, tp_ratio, sl_mult, h_start, h_end, rsi_min, rsi_max, cooldown_velas = cfg
        print(f"🔍 Analizando {tf.upper()}...")
        
        try:
            df = tv.get_hist(symbol='XAUUSD', exchange='OANDA', interval=tv_interval, n_bars=150)
            
            if df is None or df.empty or len(df) < 60:
                print(f"   ⚠️ Datos insuficientes para {tf.upper()}.")
                continue
            
            df = df[['open', 'high', 'low', 'close']]
            
            # 🌎 CONVERSIÓN DE ZONA HORARIA (UTC a México)
            df.index = pd.to_datetime(df.index).tz_localize('UTC').tz_convert('America/Mexico_City').tz_localize(None)
            
            # Calcular RSI
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            df['RSI'] = 100 - (100 / (1 + (gain / loss)))
            
            # 🛑 ELIMINAR VELA ACTUAL (En formación)
            df = df.iloc[:-1].copy()
            if len(df) < 60: continue
            
            opens = df['open'].values
            highs = df['high'].values
            lows = df['low'].values
            closes = df['close'].values
            rsis = df['RSI'].values
            hours = df.index.hour.values
            
            # Vela 3 (Confirmación) es la última vela cerrada
            idx_conf_final = len(df) - 1
            idx_low2 = idx_conf_final - 1
            vela2 = df.iloc[idx_low2]
            
            low1 = np.min(lows[idx_low2 - 20 : idx_low2])
            p_low1 = low1
            p_low2 = vela2['low']
            
            # 1. Validar Higher Low
            if p_low2 > p_low1:
                # 2. Validar Caída Fuerte
                ventana_caida = df.iloc[idx_low2 - 26 : idx_low2 - 6]
                if len(ventana_caida) < 5: continue
                caida = (ventana_caida['open'].iloc[0] - ventana_caida['close'].iloc[-1]) / ventana_caida['open'].iloc[0]
                
                # 3. Validar Confirmación (Vela Verde)
                vela_check = df.iloc[idx_low2 + 1]
                confirmacion_ok = vela_check['close'] > vela_check['open']
                        
                if caida >= caida_min and confirmacion_ok:
                    
                    vela_entrada = df.iloc[-1]
                    hora_actual = hours[-1]
                    rsi_actual = rsis[-1]
                    
                    # 🛡️ FILTRO HORARIO (Soporta rangos que cruzan medianoche)
                    if h_start <= h_end:
                        hora_valida = h_start <= hora_actual <= h_end
                    else:
                        hora_valida = hora_actual >= h_start or hora_actual <= h_end
                        
                    if not hora_valida:
                        continue
                        
                    # 🛡️ FILTRO RSI
                    if rsi_min is not None and rsi_actual < rsi_min:
                        continue
                    if rsi_max is not None and rsi_actual > rsi_max:
                        continue
                        
                    target = np.max(highs[max(0, idx_low2 - 50) : idx_low2])
                    entrada = vela_entrada['close']
                    
                    # 🛡️ CALCULO DEL SL Y TP
                    distancia_original = entrada - p_low1
                    sl = entrada - (distancia_original * sl_mult)
                    
                    if entrada <= sl or target <= entrada: continue
                    
                    tp = entrada + (target - entrada) * tp_ratio
                    id_senal = str(vela_entrada.name)
                    
                    # 🛑 VERIFICAR MEMORIA Y COOLDOWN
                    last_candle = memoria[tf].get("last_candle", "")
                    last_trade = memoria[tf].get("last_trade", "")
                    
                    # Si esta vela ya fue procesada, no hacemos nada
                    if last_candle == id_senal:
                        continue
                        
                    # Actualizar last_candle
                    memoria[tf]["last_candle"] = id_senal
                    
                    # Verificar Cooldown
                    cooldown_activo = False
                    if last_trade:
                        last_trade_dt = pd.to_datetime(last_trade)
                        current_dt = pd.to_datetime(id_senal)
                        minutes_diff = (current_dt - last_trade_dt).total_seconds() / 60
                        if minutes_diff < (cooldown_velas * tf_minutes):
                            cooldown_activo = True
                            
                    if cooldown_activo:
                        print(f"   🛑 {tf.upper()}: Cooldown activo (esperando {cooldown_velas} velas).")
                        guardar_memoria(memoria)
                        continue
                        
                    # 🚨 SI LLEGAMOS AQUÍ, ES UNA SEÑAL VÁLIDA
                    memoria[tf]["last_trade"] = id_senal
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
                        f"🔴 <b>SL (Optimizado):</b> {sl:.2f}\n"
                        f"✅ <b>TP ({int(tp_ratio*100)}%):</b> {tp:.2f}\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"⚖️ <b>Lotaje Sugerido:</b> {lote} lots\n"
                        f"💰 <b>Riesgo:</b> ${BALANCE_CUENTA * RIESGO_POR_OPERACION:.2f}\n"
                        f"<i>Generado por AlgoBot (Elite)</i>"
                    )
                    enviar_telegram(mensaje)
                    print(f"   ✅ ¡SEÑAL ENVIADA para {tf.upper()}! (Cierre: {entrada:.2f})")
                    
                else:
                    # Si no hay patrón pero la vela es nueva, guardamos el last_candle
                    memoria[tf]["last_candle"] = str(df.iloc[-1].name)
                    guardar_memoria(memoria)
                    print(f"   ⏳ Sin patrón válido en {tf.upper()}.")
            else:
                memoria[tf]["last_candle"] = str(df.iloc[-1].name)
                guardar_memoria(memoria)
                print(f"   ⏳ Sin patrón válido en {tf.upper()}.")
                
        except Exception as e:
            print(f"   ❌ Error procesando {tf}: {e}")
        
        time.sleep(1)

if __name__ == "__main__":
    escanear_mercado()
