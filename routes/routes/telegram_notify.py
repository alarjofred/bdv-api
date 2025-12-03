from fastapi import APIRouter
import os
import requests

router = APIRouter(prefix="/telegram", tags=["telegram"])

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send_telegram_message(text: str) -> bool:
    """
    Envía un mensaje de texto simple a tu chat de Telegram.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID en variables de entorno")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }

    try:
        resp = requests.post(url, json=payload, timeout=10)
        print("Telegram resp:", resp.status_code, resp.text)
        return resp.ok
    except Exception as e:
        print("❌ Error enviando a Telegram:", e)
        return False


@router.get("/test")
def telegram_test():
    """
    Endpoint de prueba: envía un mensaje de test a tu Telegram.
    """
    ok = send_telegram_message("🔔 Test BDV: notificación desde el servidor Render (BDV-API).")
    return {"status": "sent" if ok else "failed"}
