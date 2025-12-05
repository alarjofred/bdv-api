# routes/telegram_notify.py

import os
import requests
from fastapi import APIRouter

router = APIRouter(prefix="/notify", tags=["notify"])

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send_telegram_message(text: str):
    """
    Envía un mensaje de texto simple a tu chat de Telegram.

    Devuelve un dict con el estado de la llamada a Telegram.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {
            "status": "error",
            "message": "Falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID en variables de entorno.",
        }

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        return {
            "status": "ok" if resp.status_code == 200 else "telegram_error",
            "telegram_status": resp.status_code,
            "telegram_body": resp.json(),
        }
    except Exception as e:
        return {
            "status": "exception",
            "error": str(e),
        }


# 🧩 NUEVA FUNCIÓN AÑADIDA AQUÍ
def send_alert(event: str, data: dict):
    """
    Envía mensajes estructurados a Telegram según el tipo de evento BDV.
    event: "signal", "execution", "close", "summary"
    """
    try:
        if event == "signal":
            text = (
                f"📈 *Nueva señal IA BDV*\n"
                f"Símbolo: {data.get('symbol')}\n"
                f"Sesgo: {data.get('bias')}\n"
                f"Acción sugerida: {data.get('suggestion')}\n"
                f"Target: {data.get('target')} / Stop: {data.get('stop')}\n"
                f"🧠 {data.get('note', '')}"
            )

        elif event == "execution":
            text = (
                f"✅ *Orden ejecutada*\n"
                f"{data.get('symbol')} – {data.get('side').upper()} ({data.get('qty')} contratos)\n"
                f"Precio entrada: {data.get('price')}\n"
                f"Target: {data.get('target')} / Stop: {data.get('stop')}\n"
                f"Modo: {data.get('mode', 'Auto/Paper')}"
            )

        elif event == "close":
            text = (
                f"🔒 *Cierre de posición*\n"
                f"{data.get('symbol')} – {data.get('reason')}\n"
                f"P/L: {data.get('pl', 'n/a')} ({data.get('percent', 'n/a')}%)"
            )

        elif event == "summary":
            text = (
                f"🧾 *Resumen BDV del día*\n"
                f"Operaciones: {data.get('trades')}\n"
                f"Ganancia total: {data.get('profit')}\n"
                f"Riesgo: {data.get('risk_mode')}\n"
                f"Modo: {data.get('execution_mode')}"
            )

        else:
            text = f"ℹ️ Evento BDV: {event}\n{data}"

        return send_telegram_message(text)

    except Exception as e:
        print(f"[ERR] send_alert: {e}")
        return {"status": "error", "error": str(e)}


@router.get("/telegram-test")
def telegram_test():
    """
    Endpoint de prueba: envía un mensaje de test a tu Telegram.
    """
    result = send_telegram_message("🔔 *BDV OPTIONS LIVE* — prueba de notificación desde Render.")
    return result
