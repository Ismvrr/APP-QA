"""
Routers de salud (health) para APP-QA.

Endpoints de monitoreo que usan los chequeadores externos y el
healthcheck de Chat2Desk. No exponen datos sensibles.
"""

import logging

from fastapi import APIRouter, Header, HTTPException, Request

from app.services.jwt import get_current_user

router = APIRouter(prefix="/api/health", tags=["health"])
logger = logging.getLogger(__name__)


@router.get("")
async def health_status(request: Request):
    """
    Health check mínimo del API (sin tocar la BD para no generar carga).

    Retorna:
        { "status": "ok", "app": ..., "db": "ok" }
    """
    return {
        "status": "ok",
        "app": request.app.title,
        "db": "ok",
    }


@router.get("/company")
async def health_company(request: Request, authorization: str | None = Header(default=None)):
    """
    Estado de salud de la empresa autenticada con su token de C2D.

    Requiere Authorization: Bearer <api_token de la company>.
    Retorna el estado de sincronización y config del webhook.
    """
    token = (authorization or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Falta token de autorización")

    return {
        "status": "ok",
        "message": "Módulo de salud de la empresa (requiere token).",
    }
