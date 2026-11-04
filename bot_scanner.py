import yfinance as yf
import pandas as pd
import numpy as np
import requests
import json
import os
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

# ==========================================
# ⚙️ CONFIGURACIÓN DEL BOT Y CUENTA
# ==========================================
TELEGRAM_TOKEN = "8570417983:AAG_lqX9bzw2dZxQ-GPYCHqBYpAgtyZaD5s"
TELEGRAM_CHAT_ID = "8996462811"
TICKER = "GC=F"                         
BALANCE_CUENTA = 1000                   # ⚠️ CAMBIA ESTO POR TU BALANCE REAL
RIESGO_POR_OPERACION = 0.02             
CONTRATO_ORO = 100                      

# 🏆 LAS 4 TEMPORALIDADES GANADORAS
CONFIG_OPTIMA = [
    ("15m", "60d",  "15m", None,   0.002, 2, 0.50), 
    ("1h",  "730d", "1h",  None,   0.002, 1, 0.50), 
    ("2h",  "730d", "1h",  "2h",   0.004, 1, 0.40),
    ("1d",  "5y",   "1d",  None,   0.002, 3, 0.90) 
]

ARCHIVO_MEMORIA = "last_signals.json"

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

# 🛡️ FUNCIÓN BLINDADA CONTRA ARCHIVOS VACÍOS
def cargar_memoria():
    if os.path.exists(ARCHIVO_MEMORIA):
        try:
            with open(ARCHIVO_MEMORIA, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            # Si el archivo está vacío o corrupto, devuelve la memoria limpia
            return {cfg[0]: "" for cfg in CONFIG_OPTIMA}
    return {cfg[0]: "" for cfg in CONFIG_OPTIMA}

def guardar_memoria(memoria):
    with open(ARCHIVO_MEMORIA, 'w') as f:
        json.dump(memoria, f)

def escanear_mercado():
    print(f"\n[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC] Iniciando escaneo...")
    memoria = cargar_memoria()
    
    for cfg in CONFIG_OPTIMA:
        tf, periodo, interval, resample_rule, caida_min, n_conf, tp_ratio = cfg
        print(f"🔍 Analizando {tf.upper()}...")
        
        try:
            df = yf.download(TICKER, period=periodo, interval=interval, progress=False, auto_adjust=True)
            if df.empty or len(df) < 100: continue
                
            if isinstance(df.columns, pd.MultiIndex): 
                df.columns = df.columns.get_level_values(0)
            df.columns = [c.lower() for c in df.columns]
            
            if resample_rule:
                df = df.resample(resample_rule).agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'}).dropna()
            
            # 🛑 ELIMINAMOS LA VELA ACTUAL (Cero Trampas)
            df = df.iloc[:-1].copy()
            if len(df) < 60: continue
            
            df = df[['open', 'high', 'low', 'close']]
            
            idx_conf_final = len(df) - 1
            idx_low2 = idx_conf_final - n_conf
            vela2 = df.iloc[idx_low2]
            
            low1 = df.iloc[idx_low2 - 20 : idx_low2]['low'].min()
            p_low1 = low1
            p_low2 = vela2['low']
            
            # 1. Validar Higher Low
            if p_low2 > p_low1:
                # 2. Validar Caída Fuerte
                ventana_caida = df.iloc[idx_low2 - 26 : idx_low2 - 6]
                if len(ventana_caida) < 5: continue
                caida = (ventana_caida['open'].iloc[0] - ventana_caida['close'].iloc[-1]) / ventana_caida['open'].iloc[0]
                
                # 3. Validar Confirmaciones (Velas verdes)
                confirmacion_ok = True
                for c in range(1, n_conf + 1):
                    vela_check = df.iloc[idx_low2 + c]
                    if vela_check['close'] <= vela_check['open']:
                        confirmacion_ok = False
                        break
                        
                if caida >= caida_min and confirmacion_ok:
                    target = df.iloc[max(0, idx_low2 - 50) : idx_low2]['high'].max()
                    
                    vela_entrada = df.iloc[-1]
                    entrada = vela_entrada['close']
                    sl = p_low1
                    
                    if entrada <= sl or target <= entrada: continue
                    
                    tp = entrada + (target - entrada) * tp_ratio
                    id_senal = str(vela_entrada.name)
                    
                    if memoria.get(tf) != id_senal:
                        memoria[tf] = id_senal
                        guardar_memoria(memoria)
                        
                        lote = calcular_lotaje(entrada, sl, BALANCE_CUENTA, RIESGO_POR_OPERACION)
                        
                        mensaje = (
                            f"🚨 <b>¡NUEVA SEÑAL (XAUUSD)!</b> 🚨\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"📊 <b>Activo:</b> XAUUSD\n"
                            f"⏱️ <b>Temporalidad:</b> {tf.upper()}\n"
                            f"📈 <b>Dirección:</b> LONG\n"
                            f"🎯 <b>Entrada (Cierre Vela):</b> {entrada:.2f}\n"
                            f"🔴 <b>SL (Low 1):</b> {sl:.2f}\n"
                            f"✅ <b>TP ({int(tp_ratio*100)}%):</b> {tp:.2f}\n"
                            f"━━━━━━━━━━━━━━━━━━\n"
                            f"⚖️ <b>Lotaje Sugerido:</b> {lote} lots\n"
                            f"💰 <b>Riesgo:</b> ${BALANCE_CUENTA * RIESGO_POR_OPERACION:.2f}\n"
                            f"<i>Generado por AlgoBot</i>"
                        )
                        enviar_telegram(mensaje)
                        print(f"   ✅ ¡SEÑAL ENVIADA para {tf.upper()}! (Cierre: {entrada:.2f})")
                    else:
                        print(f"   ℹ️ Vela ya procesada en {tf.upper()}.")
        except Exception as e:
            print(f"   ❌ Error en {tf}: {e}")

if __name__ == "__main__":
    escanear_mercado()
