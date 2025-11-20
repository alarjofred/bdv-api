from fastapi import APIRouter
import os
import requests

router = APIRouter()

@router.get("/test-alpaca")
def test_alpaca_connection():
    api_key = os.getenv("APCA_API_KEY_ID")
    secret_key = os.getenv("APCA_API_SECRET_KEY")
    data_url = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets/v2")

    if not api_key or not secret_key:
        return {
            "status": "error",
            "message": "Variables faltantes: APCA_API_KEY_ID o APCA_API_SECRET_KEY"
        }

    url = f"{data_url}/stocks/AAPL/trades/latest"
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
    }

    try:
        r = requests.get(url, headers=headers)
        return {
            "status": "success",
            "code": r.status_code,
            "preview": r.text[:200]
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }

