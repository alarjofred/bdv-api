from fastapi import APIRouter
import json
import os

router = APIRouter()

CONFIG_FILE = "bdv_config.json"

# Valores por defecto
DEFAULT_CONFIG = {
    "execution_mode": "manual",      # manual / semi / auto
    "risk_mode": "medium",           # low / medium / high
    "max_trades_per_day": 3,
    "trades_today": 0
}

def load_config():
    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

@router.get("/config/status")
def get_config():
    return load_config()

@router.post("/config/execution-mode")
def set_execution_mode(mode: str):
    if mode not in ["manual", "semi", "auto"]:
        return {"status": "error", "message": "Modo inválido"}

    config = load_config()
    config["execution_mode"] = mode
    save_config(config)

    return {
        "status": "ok",
        "execution_mode": mode,
        "message": f"Modo de ejecución cambiado a {mode}"
    }

@router.post("/config/risk-mode")
def set_risk_mode(mode: str):
    if mode not in ["low", "medium", "high"]:
        return {"status": "error", "message": "Modo de riesgo inválido"}

    config = load_config()
    config["risk_mode"] = mode
    save_config(config)

    return {
        "status": "ok",
        "risk_mode": mode,
        "message": f"Modo de riesgo cambiado a {mode}"
    }
