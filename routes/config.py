from enum import Enum
from typing import Any, Optional
import json
import os
from pathlib import Path

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request
from pydantic import BaseModel

router = APIRouter(prefix="/config", tags=["config"])


class ExecutionMode(str, Enum):
    manual = "manual"
    auto = "auto"


class RiskMode(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


MAX_TRADES_BY_RISK = {
    RiskMode.low: 1,
    RiskMode.medium: 3,
    RiskMode.high: 5,
}


class ConfigStatus(BaseModel):
    execution_mode: ExecutionMode = ExecutionMode.manual
    risk_mode: RiskMode = RiskMode.low
    max_trades_per_day: int = 1
    trades_today: int = 0


config_state = ConfigStatus()


def _sync_max_trades() -> None:
    config_state.max_trades_per_day = int(MAX_TRADES_BY_RISK.get(config_state.risk_mode, 1))


def _norm(v: str) -> str:
    return v.strip().lower()


# =========================
# Seguridad para POST /config/*
# =========================
BDV_AGENT_SECRET = os.getenv("BDV_AGENT_SECRET", "").strip()


def _require_secret(x_bdv_secret: Optional[str]) -> None:
    """
    Si BDV_AGENT_SECRET está definido, exige header X-BDV-SECRET en endpoints POST.
    """
    if BDV_AGENT_SECRET:
        if not x_bdv_secret or x_bdv_secret.strip() != BDV_AGENT_SECRET:
            raise HTTPException(status_code=401, detail="Unauthorized: missing/invalid X-BDV-SECRET")


# =========================
# Persistencia en Disk (Render)
# =========================
PERSIST_DIR = os.getenv("BDV_PERSIST_DIR", "/var/data").strip() or "/var/data"
CONFIG_FILE = os.getenv("BDV_CONFIG_FILE", "bdv_config.json").strip() or "bdv_config.json"
CONFIG_PATH = Path(PERSIST_DIR) / CONFIG_FILE


def _load_config_from_disk() -> None:
    """
    Carga config persistida al iniciar el proceso.
    NO rompe el server si el archivo no existe o está corrupto.
    """
    try:
        if CONFIG_PATH.exists():
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                if "execution_mode" in raw:
                    config_state.execution_mode = ExecutionMode(str(raw["execution_mode"]).lower())
                if "risk_mode" in raw:
                    config_state.risk_mode = RiskMode(str(raw["risk_mode"]).lower())
                if "trades_today" in raw:
                    config_state.trades_today = int(raw["trades_today"])
    except Exception:
        pass
    finally:
        _sync_max_trades()


def _save_config_to_disk() -> None:
    """
    Guarda config de forma atómica para evitar corrupciones:
    escribe .tmp y luego os.replace()
    """
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        payload = {
            "execution_mode": config_state.execution_mode.value,
            "risk_mode": config_state.risk_mode.value,
            "trades_today": config_state.trades_today,
        }
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)  # atomic
    except Exception:
        pass


# Cargar estado al levantar el proceso (import time)
_load_config_from_disk()


async def _extract_mode(
    request: Request,
    query_mode: Optional[str],
    body_obj: Any,
    primary_key: str,
    alt_key: str,
    allowed: set[str],
) -> str:
    # 1) Querystring (lo más robusto para cron)
    if query_mode:
        m = _norm(str(query_mode))
        if m in allowed:
            return m

    # 2) Body ya parseado (dict / str)
    if isinstance(body_obj, dict):
        v = body_obj.get(primary_key) or body_obj.get(alt_key)
        if v is not None:
            m = _norm(str(v))
            if m in allowed:
                return m

    if isinstance(body_obj, str):
        m = _norm(body_obj)
        if m in allowed:
            return m

    # 3) Raw body fallback (si vino vacío / raro / no-JSON)
    raw = await request.body()
    if raw:
        text = raw.decode("utf-8", errors="ignore").strip()

        # raw: auto
        if text and not text.startswith("{") and not text.startswith("["):
            m = _norm(text.strip('"').strip("'"))
            if m in allowed:
                return m

        # JSON dict o JSON string
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                v = parsed.get(primary_key) or parsed.get(alt_key)
                if v is not None:
                    m = _norm(str(v))
                    if m in allowed:
                        return m
            elif isinstance(parsed, str):
                m = _norm(parsed)
                if m in allowed:
                    return m
        except Exception:
            pass

    raise HTTPException(
        status_code=422,
        detail=(
            f"mode is required and must be one of: {sorted(list(allowed))}. "
            f"Use ?mode=... or JSON body."
        ),
    )


@router.get("/status", response_model=ConfigStatus)
def get_config_status() -> ConfigStatus:
    _sync_max_trades()
    return config_state


@router.post("/execution-mode", response_model=ConfigStatus)
async def set_execution_mode(
    request: Request,
    mode: Optional[str] = Query(default=None),
    payload: Any = Body(default=None),
    x_bdv_secret: Optional[str] = Header(default=None),
) -> ConfigStatus:
    _require_secret(x_bdv_secret)

    m = await _extract_mode(
        request=request,
        query_mode=mode,
        body_obj=payload,
        primary_key="mode",
        alt_key="execution_mode",
        allowed={"auto", "manual"},
    )
    config_state.execution_mode = ExecutionMode(m)
    _sync_max_trades()
    _save_config_to_disk()
    return config_state


@router.post("/risk-mode", response_model=ConfigStatus)
async def set_risk_mode(
    request: Request,
    mode: Optional[str] = Query(default=None),
    payload: Any = Body(default=None),
    x_bdv_secret: Optional[str] = Header(default=None),
) -> ConfigStatus:
    _require_secret(x_bdv_secret)

    m = await _extract_mode(
        request=request,
        query_mode=mode,
        body_obj=payload,
        primary_key="mode",
        alt_key="risk_mode",
        allowed={"low", "medium", "high"},
    )
    config_state.risk_mode = RiskMode(m)
    _sync_max_trades()
    _save_config_to_disk()
    return config_state


@router.post("/reset-trades", response_model=ConfigStatus)
def reset_trades_today(
    x_bdv_secret: Optional[str] = Header(default=None),
) -> ConfigStatus:
    _require_secret(x_bdv_secret)

    config_state.trades_today = 0
    _sync_max_trades()
    _save_config_to_disk()
    return config_state


_sync_max_trades()
