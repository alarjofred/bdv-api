# routes/telegram_notify.py

import os
import requests
from fastapi import APIRouter

router = APIRouter(prefix="/notify", tags=["notify"])

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "1").strip() not in ("0", "false", "False", "no", "NO")

# Recomendación: MarkdownV2 (mucho más estricto) + escape
TELEGRAM_PARSE_MODE = os.getenv("TELEGRAM_PARSE_MODE", "MarkdownV2")  # "MarkdownV2" o "" (sin parse)


def _escape_markdown_v2(text: str) -> str:
    """
    Escapa caracteres reservados para Telegram MarkdownV2.
    https://core.telegram.org/bots/api#markdownv2-style
    """
    if text is None:
        return ""
    s = str(text)
    for ch in r"_*[]()~`>#+-=|{}.!":
        s = s.replace(ch, f"\\{ch}")
    return s


def send_telegram_message(text: str):
    """
    Envía un mensaje de Telegram.
    Devuelve un dict con estado (ok / error) sin volcar payload gigante.
    """
    if not TELEGRAM_ENABLED:
        return {"status": "disabled", "message": "TELEGRAM_ENABLED=0"}

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {
            "status": "error",
            "message": "Falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID en variables de entorno.",
        }

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text if TELEGRAM_PARSE_MODE != "MarkdownV2" else _escape_markdown_v2(text),
    }

    # Solo agregar parse_mode si está activo
    if TELEGRAM_PARSE_MODE:
        payload["parse_mode"] = TELEGRAM_PARSE_MODE

    try:
        resp = requests.post(url, json=payload, timeout=10)
        ok = resp.status_code == 200
        return {
            "status": "ok" if ok else "telegram_error",
            "telegram_status": resp.status_code,
            "telegram_text": (resp.text[:500] + "...") if len(resp.text) > 500 else resp.text,
        }
    except Exception as e:
        return {"status": "exception", "error": str(e)}


def send_alert(event: str, data: dict):
    """
    Envía mensajes estructurados BDV.
    event: "signal", "execution", "close", "summary"
    """
    data = data or {}

    try:
        if event == "signal":
            text = (
                "📈 Nueva señal BDV\n"
                f"Símbolo: {data.get('symbol','')}\n"
                f"Sesgo: {data.get('bias','')}\n"
                f"Acción: {data.get('suggestion','')}\n"
                f"Target: {data.get('target','')} | Stop: {data.get('stop','')}\n"
                f"Nota: {data.get('note','')}"
            )

        elif event == "execution":
            side = str(data.get("side", "")).upper()
            qty = data.get("qty", "")
            text = (
                "✅ Orden ejecutada\n"
                f"{data.get('symbol','')} – {side} ({qty})\n"
                f"Entrada: {data.get('price','')}\n"
                f"Target: {data.get('target','')} | Stop: {data.get('stop','')}\n"
                f"Modo: {data.get('mode','')}"
            )

        elif event == "close":
            text = (
                "🔒 Cierre de posición\n"
                f"{data.get('symbol','')} – {data.get('reason','')}\n"
                f"P/L: {data.get('pl','n/a')} ({data.get('percent','n/a')}%)"
            )

        elif event == "summary":
            text = (
                "🧾 Resumen BDV\n"
                f"Operaciones: {data.get('trades','')}\n"
                f"Ganancia: {data.get('profit','')}\n"
                f"Riesgo: {data.get('risk_mode','')}\n"
                f"Modo: {data.get('execution_mode','')}"
            )

        else:
            text = f"ℹ️ Evento BDV: {event}\n{data}"

        return send_telegram_message(text)

    except Exception as e:
        return {"status": "error", "error": str(e)}


@router.get("/telegram-test")
def telegram_test():
    """
    Endpoint de prueba: envía un mensaje test.
    """
    return send_telegram_message("🔔 BDV — prueba de notificación desde Render.")
