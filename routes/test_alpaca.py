from fastapi import APIRouter
import os
from alpaca.trading.client import TradingClient

router = APIRouter()

@router.get("/test-alpaca")
def test_alpaca_connection():
    try:
        api_key = os.getenv("ALPACA_API_KEY")
        secret_key = os.getenv("ALPACA_SECRET_KEY")

        client = TradingClient(api_key, secret_key, paper=True)
        account = client.get_account()

        return {
            "status": "success",
            "account_status": account.status
        }
    except Exception as e:
        return {
            "status": "error",
            "message": str(e)
        }
