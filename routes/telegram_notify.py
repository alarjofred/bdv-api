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


@router.get("/telegram-test")
def telegram_test():
    """
    Endpoint de prueba: envía un mensaje de test a tu Telegram.
    """
    result = send_telegram_message("🔔 *BDV OPTIONS LIVE* — prueba de notificación desde Render.")
    return result
