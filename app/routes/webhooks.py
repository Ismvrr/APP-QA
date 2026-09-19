"""
Webhook Routes - API_C2D

Receptor de webhooks de Chat2Desk para sincronizacion en tiempo real.

Diseno multi-cliente:
    - Todos los clientes usan la misma URL
    - La empresa se identifica por el token del header Authorization
    - El mensaje se guarda en mensajes_request con deduplicacion por mensaje_id

Modos de sincronizacion (companies.sync_mode):
    - local:    el payload se guarda en nuestra BD (mensajes_request)
    - external: el payload crudo se reenvia a companies.external_url

Eventos MVP soportados:
    - inbox
    - outbox
    - imported_message

Tras cada recepcion se registra en webhook_logs y se actualiza
companies.last_webhook_at para alimentar el estado de la UI.
"""

import logging
import hashlib
import json
from datetime import datetime, timezone

import httpx

from fastapi import APIRouter, Header, HTTPException, Request

from app.database import get_db

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)

SUPPORTED_EVENTS = {"inbox", "outbox", "imported_message"}

INSERT_MESSAGE_SQL = """
INSERT IGNORE INTO mensajes_request
    (company_id, request_id, dialog_id, mensaje_id, client_id, operator_id, tipo, texto, transport, fecha_creacion)
VALUES
    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


def _normalize_token(raw_token: str | None) -> str:
    """Normaliza el header Authorization para comparar contra companies.api_token."""
    if not raw_token:
        return ""

    token = raw_token.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def _parse_event_time(event_time: str | None) -> str | None:
    """Convierte el timestamp ISO del webhook al formato DATETIME de MySQL."""
    if not event_time:
        return None

    try:
        dt = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        logger.warning("Invalid webhook event_time", extra={"event_time": event_time})
        return None


def _map_webhook_message(payload: dict) -> dict | None:
    """Mapea el payload de C2D al schema de mensajes_request."""
    hook_type = payload.get("hook_type")
    if hook_type not in SUPPORTED_EVENTS:
        return None

    return {
        "request_id": str(payload.get("request_id", "")),
        "dialog_id": str(payload.get("dialog_id", "")),
        "mensaje_id": str(payload.get("message_id", "")),
        "client_id": str(payload.get("client_id", "")),
        "operator_id": str(payload.get("operator_id", "")),
        "tipo": payload.get("type", ""),
        "texto": payload.get("text", ""),
        "transport": payload.get("transport", ""),
        "fecha_creacion": _parse_event_time(payload.get("event_time")),
    }


def _log_reception(cursor, conn, company_id: int, hook_type: str | None,
                   status: str, payload: dict) -> None:
    """Registra la recepcion en webhook_logs."""
    cursor.execute(
        """
        INSERT INTO webhook_logs (company_id, hook_type, status, payload, received_at)
        VALUES (%s, %s, %s, %s, NOW())
        """,
        (company_id, hook_type, status, json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()


async def _forward_raw(payload: dict, target_url: str, timeout: float = 15.0) -> tuple[int | None, str]:
    """Reenvia el payload crudo al endpoint externo con httpx async.

    Returns:
        (status_code, body) del endpoint externo, o (None, error).
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(target_url, json=payload)
        body = response.text[:2000]
        return response.status_code, body
    except httpx.TimeoutException:
        return None, "Timeout al reenviar el webhook"
    except httpx.RequestError as e:
        return None, f"Sin conexion con el endpoint externo: {e}"
    except Exception as e:  # noqa: BLE001 - errores inesperados no deben romper el receptor
        return None, f"Error inesperado al reenviar: {e}"


@router.post("/c2d")
async def receive_c2d_webhook(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """
    Recibe un webhook de Chat2Desk. Segun sync_mode: guarda en BD (local)
    o reenvia el payload crudo a la URL externa de la empresa.

    Comportamiento:
        - 401 si el token no corresponde a ninguna empresa
        - 200 si el evento no es parte del MVP (se registra e ignora)
        - 200 si el mensaje se guardo o ya existia (deduplicacion)
    """
    payload = await request.json()
    token = _normalize_token(authorization)

    if not token:
        raise HTTPException(status_code=401, detail="Missing Authorization token")

    mapped_message = _map_webhook_message(payload)
    hook_type = payload.get("hook_type")

    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, name, sync_mode, external_url
                FROM companies
                WHERE api_token_hash = %s AND isdeleted = 0
                LIMIT 1
                """,
                (hashlib.sha256(token.encode()).hexdigest(),),
            )
            company = cursor.fetchone()

            if not company:
                raise HTTPException(status_code=401, detail="Invalid webhook token")

            company_id = company["id"]
            sync_mode = company["sync_mode"] or "local"
            external_url = company["external_url"]

            # Marcar fecha de ultima recepcion (siempre)
            cursor.execute(
                "UPDATE companies SET last_webhook_at = NOW() WHERE id = %s",
                (company_id,),
            )
            conn.commit()

            # Evento fuera del MVP: registrar e ignorar de forma segura
            if mapped_message is None:
                _log_reception(cursor, conn, company_id, hook_type, "ignored", payload)
                logger.info(
                    "webhook_ignored",
                    extra={"company_id": company_id, "hook_type": hook_type},
                )
                return {
                    "status": "ignored",
                    "reason": "unsupported_hook_type",
                    "hook_type": hook_type,
                }

            if sync_mode == "external":
                if not external_url:
                    _log_reception(cursor, conn, company_id, hook_type, "config_error", payload)
                    logger.error(
                        "webhook_external_missing_url",
                        extra={"company_id": company_id, "hook_type": hook_type},
                    )
                    return {
                        "status": "ok",
                        "mode": "external",
                        "error": "external_url_not_configured",
                        "hook_type": hook_type,
                    }

                status_code, forward_body = await _forward_raw(payload, external_url)
                forward_status = "forwarded" if status_code is not None and status_code < 300 else "forward_failed"

                _log_reception(cursor, conn, company_id, hook_type, forward_status, payload)
                logger.info(
                    "webhook_forwarded",
                    extra={
                        "company_id": company_id,
                        "hook_type": hook_type,
                        "target": external_url,
                        "target_status": status_code,
                        "inserted": False,
                    },
                )

                return {
                    "status": "ok",
                    "mode": "external",
                    "forwarded": forward_status == "forwarded",
                    "forward_http_status": status_code,
                    "forward_error": None if forward_status == "forwarded" else forward_body,
                    "hook_type": hook_type,
                    "message_id": mapped_message["mensaje_id"],
                }

            # Modo local: guardar en mensajes_request con deduplicacion
            cursor.execute(
                INSERT_MESSAGE_SQL,
                (
                    company_id,
                    mapped_message["request_id"],
                    mapped_message["dialog_id"],
                    mapped_message["mensaje_id"],
                    mapped_message["client_id"],
                    mapped_message["operator_id"],
                    mapped_message["tipo"],
                    mapped_message["texto"],
                    mapped_message["transport"],
                    mapped_message["fecha_creacion"],
                ),
            )
            conn.commit()

            inserted = cursor.rowcount > 0
            _log_reception(cursor, conn, company_id, hook_type,
                           "received" if inserted else "duplicate", payload)
            logger.info(
                "webhook_received",
                extra={
                    "company_id": company_id,
                    "hook_type": hook_type,
                    "message_id": mapped_message["mensaje_id"],
                    "inserted": inserted,
                },
            )

    return {
        "status": "ok",
        "mode": "local",
        "hook_type": hook_type,
        "message_id": mapped_message["mensaje_id"],
        "inserted": inserted,
    }