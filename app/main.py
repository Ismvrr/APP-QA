"""
API_C2D - FastAPI Application

Aplicación principal para la plataforma de análisis de conversaciones
de Chat2Desk con inteligencia artificial.

Arquitectura:
    - Laravel (Auth): Puerto 8080 - Maneja login y sesiones
    - FastAPI (API): Puerto 8000 - API REST + análisis AI
    - Nginx (Proxy): Puerto 443 - Enrutamiento HTTPS
    - MySQL: Puerto 3306 - Base de datos remota

Endpoints principales:
    GET  /health            - Health check del sistema
    GET  /                  - Info de la API
    GET  /api/docs          - Swagger UI
    GET  /api/redoc         - ReDoc documentation
    POST /api/auth/*        - Autenticación (JWT)
    POST /api/extract       - Extracción de mensajes por período
    GET  /api/sync/status   - Estado de sincronización
    GET  /api/sync/periods  - Períodos extraídos
    POST /api/webhooks/*    - Webhooks de Chat2Desk (post-V1)
    POST /api/analyze/*     - Análisis AI manual
"""

import logging

import hashlib
import httpx

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.database import check_db_connection, get_db
from app.logging_config import setup_logging
from app.routes.auth import router as auth_router
from app.routes.extraction import router as extraction_router
from app.routes.webhooks import router as webhooks_router
from app.routes.analysis import router as analysis_router
from app.routes.reports import router as reports_router
from app.routes.health import router as health_router
from app.routes.predict import router as predict_router

settings = get_settings()

# Configurar logging estructurado (JSON)
setup_logging(level="DEBUG" if settings.DEBUG else "INFO")
logger = logging.getLogger(__name__)

# Crear la aplicación FastAPI
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    docs_url="/api/docs",      # Swagger UI
    redoc_url="/api/redoc"     # ReDoc documentation
)

# Archivos estáticos (CSS, JS, imágenes)
app.mount("/static", StaticFiles(directory="static"), name="static")

# Templates Jinja2 para renderizado server-side
templates = Jinja2Templates(directory="app/templates")

# Registrar rutas
app.include_router(auth_router)
app.include_router(extraction_router)
app.include_router(webhooks_router)
app.include_router(analysis_router)
app.include_router(reports_router)
app.include_router(health_router)
app.include_router(predict_router)


@app.get("/health")
async def health_check():
    """
    Health check del sistema.

    Retorna solo un estado simple para el monitor externo.
    """
    db_status = check_db_connection()
    if not db_status:
        logger.error("health_check_failed", extra={"db_status": "disconnected"})
        return JSONResponse(status_code=503, content={"status": "error"})

    logger.info("health_check", extra={"db_status": "connected"})

    return {"status": "ok"}


@app.get("/health/company")
async def health_company(request: Request, authorization: str | None = Header(default=None)):
    """
    Estado por empresa, protegido con el token de Chat2Desk.

    Requiere el mismo token que configuro el cliente (header Authorization).
    Retorna configuracion de sync, si el webhook esta registrado en C2D,
    ultima recepcion y los ultimos eventos de webhook_logs.
    """
    token = (authorization or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing Authorization token")

    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, name, realtime_enabled, sync_mode, external_url, last_webhook_at
                FROM companies
                WHERE api_token_hash = %s AND isdeleted = 0
                LIMIT 1
                """,
                (hashlib.sha256(token.encode()).hexdigest(),),
            )
            company = cursor.fetchone()

            if not company:
                raise HTTPException(status_code=401, detail="Invalid token")

            cursor.execute(
                """
                SELECT hook_type, status, received_at
                FROM webhook_logs
                WHERE company_id = %s
                ORDER BY id DESC
                LIMIT 10
                """,
                (company["id"],),
            )
            last_logs = cursor.fetchall()

    webhook_registered = False
    try:
        response = httpx.get(
            "https://api.chat2desk.com.mx/v1/webhooks",
            headers={"Authorization": token},
            timeout=20,
        )
        if response.is_success:
            data = response.json().get("data", [])
            webhook_url = str(request.url).rsplit("/health/company", 1)[0] + "/api/webhooks/c2d"
            webhook_registered = any(w.get("url") == webhook_url for w in data)
    except Exception as e:  # noqa: BLE001 - el health no debe caer si C2D no responde
        logger.warning("health_company_c2d_lookup_failed", extra={"error": str(e)})

    return {
        "status": "ok",
        "name": company["name"],
        "realtime_enabled": bool(company["realtime_enabled"]),
        "sync_mode": company["sync_mode"],
        "external_url": company["external_url"],
        "last_webhook_at": company["last_webhook_at"],
        "webhook_registered_in_c2d": webhook_registered,
        "last_events": last_logs,
    }


@app.get("/")
async def root():
    """
    Endpoint raíz - Información básica de la API.

    Útil para verificar que la API está corriendo.
    Retorna nombre, versión y URL de documentación.
    """
    logger.info("root_request")
    return {
        "message": f"{settings.APP_NAME} API",
        "version": settings.APP_VERSION,
        "docs": "/api/docs"
    }
