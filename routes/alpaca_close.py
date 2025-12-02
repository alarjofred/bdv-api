@router.post("/close/{symbol}")
def close_symbol(symbol: str):
    """
    Cierra la posición abierta en un símbolo específico (si existe).
    Ejemplo: POST /alpaca/close/QQQ

    Paso A: intenta usar DELETE /v2/positions/{symbol}
    Paso B (fallback): si Alpaca responde 404, busca la posición
    en /v2/positions y envía una orden de venta MARKET con la qty
    encontrada (similar a vender manualmente en la web).
    """
    trading_url = os.getenv(
        "APCA_TRADING_URL",
        "https://paper-api.alpaca.markets/v2",
    ).rstrip("/")

    symbol = symbol.upper()
    headers = get_alpaca_headers()

    # ---------- Paso A: intento normal de cerrar la posición ----------
    url = f"{trading_url}/positions/{symbol}"

    try:
        r = requests.delete(url, headers=headers, timeout=10)
        body = r.json() if r.text else {}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error llamando a Alpaca (DELETE /positions/{symbol}): {e}",
        )

    # Si funcionó (2xx), devolvemos OK
    if 200 <= r.status_code < 300:
        return {
            "status": "ok",
            "method": "delete_endpoint",
            "closed": body,
        }

    # Si Alpaca dice 404, aplicamos el PLAN B
    if r.status_code == 404:
        # ---------- Paso B: fallback con orden de venta MARKET ----------
        try:
            # 1) Leer todas las posiciones
            pos_resp = requests.get(
                f"{trading_url}/positions",
                headers=headers,
                timeout=10,
            )

            if pos_resp.status_code == 404:
                # Alpaca no ve ninguna posición
                raise HTTPException(
                    status_code=404,
                    detail=f"No se encontró ninguna posición en Alpaca para {symbol} (lista vacía).",
                )

            if pos_resp.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "message": "Error leyendo posiciones en Alpaca (fallback)",
                        "alpaca_status": pos_resp.status_code,
                        "alpaca_body": pos_resp.text,
                    },
                )

            positions = pos_resp.json() or []

            # 2) Buscar la posición exacta por símbolo
            current = next(
                (p for p in positions if p.get("symbol") == symbol),
                None,
            )

            if not current:
                raise HTTPException(
                    status_code=404,
                    detail=f"No se encontró posición para {symbol} en la lista de /positions (fallback).",
                )

            # 3) Tomar la cantidad (qty viene como string)
            qty = current.get("qty") or current.get("quantity") or current.get("qty_available")
            if not qty:
                raise HTTPException(
                    status_code=500,
                    detail=f"No se pudo determinar la cantidad (qty) para {symbol} en el fallback.",
                )

            qty_str = str(qty)

            # 4) Enviar orden de venta MARKET
            order_payload = {
                "symbol": symbol,
                "qty": qty_str,
                "side": "sell",
                "type": "market",
                "time_in_force": "day",
            }

            order_resp = requests.post(
                f"{trading_url}/orders",
                headers=headers,
                json=order_payload,
                timeout=10,
            )
            order_body = order_resp.json() if order_resp.text else {}

            if order_resp.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "message": "Error cerrando posición vía orden de venta (fallback)",
                        "alpaca_status": order_resp.status_code,
                        "alpaca_body": order_body,
                    },
                )

            return {
                "status": "ok",
                "method": "order_fallback",
                "position_before": current,
                "order": order_body,
            }

        except HTTPException:
            # Re-lanzamos errores controlados
            raise
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Error en fallback de cierre para {symbol}: {e}",
            )

    # Otros errores distintos de 404
    raise HTTPException(
        status_code=502,
        detail={
            "message": "Error cerrando posición en Alpaca (DELETE /positions/{symbol})",
            "alpaca_status": r.status_code,
            "alpaca_body": body,
        },
    )
