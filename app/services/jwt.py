"""
JWT Service para FastAPI - API_C2D

Este módulo maneja la autenticación JWT entre Laravel (fronted) y FastAPI (API).
El flujo es:
  1. El usuario se autentica en Laravel vía Chat2Desk
  2. Laravel genera un JWT con la información del usuario
  3. El JWT se guarda en una cookie httpOnly
  4. FastAPI valida ese JWT en cada petición API

La JWT se comparte mediante el mismo JWT_SECRET configurado en ambos servicios.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


def generate_token(user_data: dict) -> str:
    """
    Genera un token JWT (usado por FastAPI cuando necesita crear tokens).

    Args:
        user_data: Diccionario con datos del usuario:
            - email: Email del usuario (se usa como 'sub')
            - id: ID del usuario en la BD
            - company_id: ID de la empresa
            - role: Rol del usuario (admin, user, shadow)

    Returns:
        str: Token JWT codificado

    Nota: La expiración se configura en JWT_EXPIRY_MINUTES (default: 480 min = 8h)
    """
    payload = {
        "sub": user_data.get("email"),      # Subject: identificador principal
        "user_id": user_data.get("id"),     # ID interno de la BD
        "company_id": user_data.get("company_id"),  # Multi-tenant: ID de empresa
        "role": user_data.get("role"),      # Rol para autorización
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_EXPIRY_MINUTES),
        "iat": datetime.now(timezone.utc),  # Issued at: timestamp de creación
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def validate_token(token: str) -> Optional[dict]:
    """
    Valida un token JWT y retorna el payload o None si es inválido.

    Args:
        token: String del token JWT a validar

    Returns:
        dict: Payload del token si es válido, None si falla

    Maneja errores comunes:
        - Token expirado
        - Firma inválida
        - Token malformado
    """
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        return payload
    except JWTError as e:
        logger.warning(f"JWT validation failed: {e}")
        return None


async def get_current_user(request: Request) -> dict:
    """
    Extrae y valida el usuario actual desde la cookie JWT.

    Uso como dependency en endpoints protegidos:
        @router.get("/protected")
        async def protected(user: dict = Depends(get_current_user)):
            ...

    Args:
        request: Request HTTP de FastAPI

    Returns:
        dict: Payload del token JWT con datos del usuario

    Raises:
        HTTPException 401: Si no hay token o es inválido
    """
    token = request.cookies.get("token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    payload = validate_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return payload


async def require_auth(request: Request) -> dict:
    """
    Middleware de autenticación que redirige a /login si no está autenticado.

    A diferencia de get_current_user, este retorna un redirect (307) en vez
    de un error 401. Útil para endpoints web que deben redirigir al login.

    Args:
        request: Request HTTP de FastAPI

    Returns:
        dict: Payload del token JWT

    Raises:
        HTTPException 307: Redirect a /login si no está autenticado
    """
    token = request.cookies.get("token")
    if not token:
        raise HTTPException(
            status_code=307,
            headers={"Location": "/login"}
        )

    payload = validate_token(token)
    if not payload:
        raise HTTPException(
            status_code=307,
            headers={"Location": "/login"}
        )

    return payload
