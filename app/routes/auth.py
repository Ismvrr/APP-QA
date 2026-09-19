"""
Rutas de autenticación para FastAPI - API_C2D

Estas rutas permiten a FastAPI verificar la identidad del usuario
que fue autenticado previamente en Laravel.

Flujo típico:
  1. Usuario hace login en Laravel → se crea JWT en cookie
  2. Frontend llama a /api/auth/me para obtener datos del usuario
  3. FastAPI valida el JWT y retorna info fresca desde la BD

Endpoints:
  GET  /api/auth/me     - Obtiene datos del usuario autenticado
  POST /api/auth/logout - Limpia la cookie de autenticación
"""

import logging
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.services.jwt import get_current_user
from app.database import get_db

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger(__name__)


@router.get("/me")
async def get_me(request: Request, user: dict = Depends(get_current_user)):
    """
    Obtiene los datos del usuario actual autenticado.

    Este endpoint es el principal para que el frontend verifique
    si el usuario está autenticado y obtenga sus datos.

    Flujo:
        1. Valida el JWT de la cookie (via Depends)
        2. Extrae el email del payload
        3. Consulta la BD para datos frescos (no cache del JWT)
        4. Retorna información del usuario + empresa

    Args:
        request: Request HTTP
        user: Payload del JWT (inyectado por Depends)

    Returns:
        JSON con datos del usuario:
        {
            "id": 1,
            "email": "user@example.com",
            "first_name": "John",
            "last_name": "Doe",
            "role": "admin",
            "company_id": 1,
            "company_name": "Mi Empresa"
        }

    Errores:
        401: Token inválido o usuario no encontrado en BD
        500: Error interno de base de datos
    """
    email = user.get("sub")
    user_id = user.get("user_id")

    if not email:
        return JSONResponse(status_code=401, content={"error": "Invalid token"})

    # Consulta fresca a la BD (no usar datos del JWT por si cambiaron)
    try:
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """SELECT u.id, u.email, u.first_name, u.last_name, u.role, 
                              u.company_id, c.name as company_name
                       FROM users u 
                       LEFT JOIN companies c ON u.company_id = c.id 
                       WHERE u.email = %s AND u.isdeleted = 0""",
                    (email,)
                )
                db_user = cursor.fetchone()

                if not db_user:
                    return JSONResponse(status_code=401, content={"error": "User not found"})

                return {
                    "id": db_user["id"],
                    "email": db_user["email"],
                    "first_name": db_user["first_name"],
                    "last_name": db_user["last_name"],
                    "role": db_user["role"],
                    "company_id": db_user["company_id"],
                    "company_name": db_user["company_name"],
                }
    except Exception as e:
        logger.error(f"Error fetching user: {e}")
        return JSONResponse(status_code=500, content={"error": "Internal error"})


@router.post("/logout")
async def logout():
    """
    Limpia la cookie de autenticación (token JWT).

    Este endpoint es llamado por el frontend cuando el usuario
    cierra sesión. Elimina la cookie 'token' del navegador.

    Nota: La sesión de Laravel se maneja por separado en /logout de Laravel.
    Este endpoint solo limpia la cookie JWT de FastAPI.
    """
    response = JSONResponse(content={"message": "Logged out"})
    response.delete_cookie(key="token", path="/")
    return response
