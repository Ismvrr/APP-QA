"""
Extraction Routes - API_C2D

Endpoints para extracción de conversaciones de Chat2Desk.

Flujo de extracción:
    1. Usuario selecciona cliente + mes/año
    2. POST /api/extract inicia la extracción
    3. Backend consulta dialogs de C2D
    4. Para cada dialog, obtiene mensajes del período
    5. Guarda en BD con deduplicación (INSERT IGNORE)
    6. Actualiza estado en extracted_periods

Endpoints:
    POST /api/extract          - Iniciar extracción por período
    GET  /api/sync/status      - Estado de sincronización
    GET  /api/sync/periods     - Períodos extraídos de un cliente
"""

import logging
import threading
from datetime import datetime
from typing import Optional
from concurrent.futures import ThreadPoolExecutor
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field

from app.services.jwt import get_current_user
from app.services.chat2desk import Chat2DeskClient, Chat2DeskError
from app.database import get_db

router = APIRouter(prefix="/api", tags=["extraction"])
logger = logging.getLogger(__name__)

# Batch size para inserciones (cada 500 filas)
BATCH_SIZE = 500

# Consultas concurrentes a la API de C2D (3 diálogos a la vez)
MAX_WORKERS = 3

# SQL para insertar mensaje con deduplicación
INSERT_MESSAGE_SQL = """
INSERT IGNORE INTO mensajes_request 
    (company_id, request_id, dialog_id, mensaje_id, client_id, operator_id, tipo, texto, transport, fecha_creacion)
VALUES 
    (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


class ExtractRequest(BaseModel):
    """Modelo para petición de extracción"""
    company_id: int = Field(..., description="ID de la empresa en nuestra BD")
    year: int = Field(..., ge=2020, le=2030, description="Año a extraer")
    month: int = Field(..., ge=1, le=12, description="Mes a extraer (1-12)")
    c2d_token: Optional[str] = Field(None, description="Token C2D (opcional, usa el de la empresa)")
    exclude_autoreply: bool = Field(False, description="Excluir mensajes autoreply")


class ExtractStatus(BaseModel):
    """Estado de una extracción"""
    company_id: int
    year: int
    month: int
    status: str
    total_dialogs: int
    total_messages: int
    error_message: Optional[str]
    created_at: Optional[str]
    completed_at: Optional[str]


# =============================================================================
# POST /api/extract - Iniciar extracción
# =============================================================================

def _dialog_may_have_activity(dialog: dict, month: int, year: int) -> bool:
    """
    Determina si un dialog pudo tener mensajes en el período solicitado
    usando el campo 'last_message.created' que entrega /dialogs.

    Si el último mensaje del dialog es anterior al primer día del mes,
    es imposible que tenga mensajes en ese mes → se descarta sin consultar
    la API. Si no hay información (created vacío o formato inválido), se
    conserva el dialog por seguridad.
    """
    last_message = dialog.get("last_message") or {}
    created = last_message.get("created") or ""
    if not created:
        return True

    try:
        created_dt = datetime.fromisoformat(created.replace(" UTC", "+00:00"))
    except (ValueError, TypeError):
        return True

    first_day = datetime(year, month, 1)
    return created_dt.replace(tzinfo=None) >= first_day


def _run_extraction(
    company_id: int,
    year: int,
    month: int,
    c2d_token: str,
    exclude_autoreply: bool
):
    """
    Función BackgroundTask que ejecuta la extracción completa.

    Flujo:
        1. Crear/actualizar registro en extracted_periods (status=extracting)
        2. Obtener todos los dialogs de C2D
        3. Filtrar localmente los dialogs con posible actividad en el mes
           (usa last_message.created → evita consultar miles de dialogs inactivos)
        4. Consultar mensajes del mes por dialog en paralelo (MAX_WORKERS=hilos)
        5. Limpiar y guardar en BD (batch de 500)
        6. Actualizar extracted_periods con totales
    """
    with get_db() as conn:
        cursor = conn.cursor()
        try:
            # 1. Registrar inicio de extracción
            logger.info(f"Starting extraction: company={company_id}, period={year}-{month:02d}")
            cursor.execute(
                """INSERT INTO extracted_periods (company_id, year, month, status)
                   VALUES (%s, %s, %s, 'extracting')
                   ON DUPLICATE KEY UPDATE status='extracting', error_message=NULL""",
                (company_id, year, month)
            )
            conn.commit()

            # 2. Obtener todos los dialogs
            logger.info("Fetching all dialogs from C2D...")
            dialogs = Chat2DeskClient(token=c2d_token).get_all_dialogs()
            logger.info(f"Found {len(dialogs)} dialogs total")

            # 3. Filtrar dialogs con posible actividad en el período
            candidates = []
            seen_candidate_ids = set()
            for dialog in dialogs:
                dialog_id = dialog.get("id")
                if not dialog_id or dialog_id in seen_candidate_ids:
                    continue
                if _dialog_may_have_activity(dialog, month, year):
                    candidates.append(dialog)
                    seen_candidate_ids.add(dialog_id)
            skipped = len(dialogs) - len(candidates)
            logger.info(f"Candidates for {year}-{month:02d}: {len(candidates)} (skipped {skipped} inactive)")

            cursor.execute(
                """UPDATE extracted_periods
                   SET dialogs_total=%s, dialogs_processed=0, total_dialogs=0, total_messages=0
                   WHERE company_id=%s AND year=%s AND month=%s""",
                (len(candidates), company_id, year, month)
            )
            conn.commit()

            # 4. Calcular rango de fechas del mes
            # C2D requires date filters in dd-mm-yyyy format.
            start_date = f"01-{month:02d}-{year}"
            if month == 12:
                end_date = f"01-01-{year + 1}"
            else:
                end_date = f"01-{month + 1:02d}-{year}"

            # Conjunto de tipos a excluir
            exclude_types = {"system", "comment"}
            if exclude_autoreply:
                exclude_types.add("autoreply")

            # Cliente C2D por hilo (requests.Session no es thread-safe)
            thread_local = threading.local()

            def get_thread_client():
                client = getattr(thread_local, "client", None)
                if client is None:
                    client = Chat2DeskClient(token=c2d_token)
                    thread_local.client = client
                return client

            def fetch_dialog(dialog: dict):
                """Consulta los mensajes del mes de un dialog. Devuelve (dialog_id, cleaned_msgs)."""
                dialog_id = dialog.get("id")
                if not dialog_id:
                    return None, []
                try:
                    messages = get_thread_client().get_messages(
                        dialog_id=dialog_id,
                        start_date=start_date,
                        finish_date=end_date,
                        order="asc"
                    )
                    cleaned = [
                        clean for m in messages
                        if (clean := Chat2DeskClient.clean_message(m, exclude_types))
                    ]
                    return dialog_id, cleaned
                except Chat2DeskError as e:
                    logger.warning(f"Error fetching messages for dialog {dialog_id}: {e}")
                    return dialog_id, []
                except Exception as e:
                    logger.error(f"Unexpected error fetching dialog {dialog_id}: {e}")
                    return dialog_id, []

            # 5. Extraer mensajes de cada dialog candidato en paralelo.
            # IMPORTANTE: la API de C2D ignora el parámetro offset en /messages,
            # por lo que NO se puede paginar con offset (causa bucles infinitos).
            # Una sola llamada por dialog corta naturalmente al devolver < limit.
            total_dialogs = 0
            total_messages = 0
            processed_dialogs = 0
            batch_messages = []

            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                for dialog_id, cleaned in executor.map(fetch_dialog, candidates):
                    processed_dialogs += 1

                    if not cleaned:
                        continue

                    total_dialogs += 1
                    for clean in cleaned:
                        clean["company_id"] = company_id
                        batch_messages.append(clean)
                        total_messages += 1
                        if len(batch_messages) >= BATCH_SIZE:
                            _insert_batch(cursor, batch_messages)
                            conn.commit()
                            batch_messages = []
                            _update_progress(
                                cursor, conn, processed_dialogs, total_dialogs, total_messages,
                                company_id, year, month
                            )
                            logger.info(f"Progress: {processed_dialogs} dialogs, {total_messages} messages")

            # Insertar mensajes restantes
            if batch_messages:
                _insert_batch(cursor, batch_messages)
                conn.commit()

            _update_progress(
                cursor, conn, processed_dialogs, total_dialogs, total_messages,
                company_id, year, month
            )

            # 6. Actualizar extracted_periods con éxito
            cursor.execute(
                """UPDATE extracted_periods 
                   SET status='completed', dialogs_processed=%s, total_dialogs=%s,
                       total_messages=%s, completed_at=NOW()
                   WHERE company_id=%s AND year=%s AND month=%s""",
                (processed_dialogs, total_dialogs, total_messages, company_id, year, month)
            )
            conn.commit()

            logger.info(f"Extraction completed: {total_dialogs} dialogs, {total_messages} messages")

        except Exception as e:
            logger.error(f"Extraction failed: {e}")
            cursor.execute(
                """UPDATE extracted_periods 
                   SET status='error', error_message=%s
                   WHERE company_id=%s AND year=%s AND month=%s""",
                (str(e)[:500], company_id, year, month)
            )
            conn.commit()
        finally:
            cursor.close()


def _insert_batch(cursor, messages: list):
    """
    Inserta un batch de mensajes en la BD.

    Usa INSERT IGNORE para deduplicación por mensaje_id.
    """
    if not messages:
        return

    values = [
        (
            m["company_id"], m["request_id"], m["dialog_id"], m["mensaje_id"],
            m["client_id"], m["operator_id"], m["tipo"],
            m["texto"], m["transport"], m["fecha_creacion"]
        )
        for m in messages
    ]

    cursor.executemany(INSERT_MESSAGE_SQL, values)
    logger.debug(f"Inserted batch of {len(values)} messages")


def _update_progress(cursor, conn, processed_dialogs, total_dialogs, total_messages,
                     company_id, year, month):
    """Persist extraction progress so clients can display live status."""
    cursor.execute(
        """UPDATE extracted_periods
           SET dialogs_processed=%s, total_dialogs=%s, total_messages=%s
           WHERE company_id=%s AND year=%s AND month=%s""",
        (processed_dialogs, total_dialogs, total_messages, company_id, year, month)
    )
    conn.commit()


@router.post("/extract")
async def start_extraction(
    request: ExtractRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user)
):
    """
    Inicia la extracción de mensajes de Chat2Desk para un período específico.

    La extracción se ejecuta en background (BackgroundTasks).
    El endpoint retorna inmediatamente con el estado "extracting".

    Flujo:
        1. Valida que el usuario tenga acceso al cliente
        2. Verifica si ya se extrajo ese período
        3. Crea registro en extracted_periods (status=extracting)
        4. Lanza BackgroundTask con la extracción
        5. Retorna 202 Accepted

    Args:
        request: company_id, year, month, c2d_token (opcional)
        background_tasks: FastAPI BackgroundTasks
        user: Usuario autenticado (JWT)

    Returns:
        202 Accepted con mensaje de inicio

    Errores:
        400: Período ya extraído (status=completed)
        403: Usuario no tiene acceso al cliente
        500: Error interno
    """
    try:
        with get_db() as conn:
            with conn.cursor() as cursor:
                # Verificar acceso del usuario al cliente
                cursor.execute(
                    "SELECT id FROM companies WHERE id = %s AND isdeleted = 0",
                    (request.company_id,)
                )
                company = cursor.fetchone()
                if not company:
                    raise HTTPException(status_code=404, detail="Company not found")

                # Verificar si ya se extrajo
                cursor.execute(
                    """SELECT status, total_messages FROM extracted_periods
                       WHERE company_id=%s AND year=%s AND month=%s""",
                    (request.company_id, request.year, request.month)
                )
                existing = cursor.fetchone()
                # Permite reintentar periodos que terminaron sin mensajes por un error de filtros.
                if (
                    existing
                    and existing["status"] == "completed"
                    and existing["total_messages"] > 0
                    and request.exclude_autoreply
                ):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Period {request.year}-{request.month:02d} already extracted"
                    )

                # Obtener token C2D de la empresa si no se proporcionó
                c2d_token = request.c2d_token
                if not c2d_token:
                    cursor.execute(
                        "SELECT api_token FROM companies WHERE id = %s",
                        (request.company_id,)
                    )
                    result = cursor.fetchone()
                    if result and result["api_token"]:
                        c2d_token = result["api_token"]
                    else:
                        raise HTTPException(
                            status_code=400,
                            detail="No C2D token provided and none configured for company"
                        )

        # Lanzar extracción en background
        background_tasks.add_task(
            _run_extraction,
            company_id=request.company_id,
            year=request.year,
            month=request.month,
            c2d_token=c2d_token,
            exclude_autoreply=request.exclude_autoreply
        )

        return {
            "message": "Extraction started",
            "company_id": request.company_id,
            "period": f"{request.year}-{request.month:02d}",
            "status": "extracting"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting extraction: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# GET /api/sync/status - Estado de sincronización
# =============================================================================

@router.get("/sync/status")
async def get_sync_status(
    company_id: int,
    user: dict = Depends(get_current_user)
):
    """
    Obtiene el estado de sincronización de un cliente.

    Retorna los últimos períodos extraídos y su estado.

    Args:
        company_id: ID de la empresa
        user: Usuario autenticado (JWT)

    Returns:
        Lista de períodos con su estado
    """
    try:
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """SELECT id, company_id, year, month, dialogs_total,
                              dialogs_processed, total_dialogs, total_messages,
                              status, error_message,
                              created_at, completed_at
                       FROM extracted_periods 
                       WHERE company_id = %s
                       ORDER BY year DESC, month DESC
                       LIMIT 12""",
                    (company_id,)
                )
                periods = cursor.fetchall()

                return {
                    "company_id": company_id,
                    "periods": periods
                }
    except Exception as e:
        logger.error(f"Error getting sync status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# GET /api/sync/periods - Períodos extraídos
# =============================================================================

@router.get("/sync/periods")
async def get_extracted_periods(
    company_id: int,
    user: dict = Depends(get_current_user)
):
    """
    Lista todos los períodos extraídos de un cliente.

    Útil para mostrar en la UI qué meses ya están disponibles
    para análisis.

    Args:
        company_id: ID de la empresa
        user: Usuario autenticado (JWT)

    Returns:
        Lista de períodos con estado y totales
    """
    try:
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """SELECT year, month, status, total_dialogs, total_messages,
                              completed_at
                       FROM extracted_periods 
                       WHERE company_id = %s AND status = 'completed'
                       ORDER BY year DESC, month DESC""",
                    (company_id,)
                )
                periods = cursor.fetchall()

                return {
                    "company_id": company_id,
                    "extracted_periods": periods
                }
    except Exception as e:
        logger.error(f"Error getting periods: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/messages")
async def get_extracted_messages(
    company_id: int,
    year: int,
    month: int,
    limit: int = 100,
    dialog_id: Optional[str] = None,
    request_id: Optional[str] = None,
    client_id: Optional[str] = None,
    message_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    user: dict = Depends(get_current_user),
):
    """Returns messages extracted for one company and calendar period."""
    if not 1 <= month <= 12 or not 1 <= limit <= 500:
        raise HTTPException(status_code=400, detail="Invalid month or limit")

    try:
        filters = [
            "company_id = %s",
            "YEAR(fecha_creacion) = %s",
            "MONTH(fecha_creacion) = %s",
        ]
        params = [company_id, year, month]

        if dialog_id:
            filters.append("dialog_id = %s")
            params.append(dialog_id)
        if request_id:
            filters.append("request_id = %s")
            params.append(request_id)
        if client_id:
            filters.append("client_id = %s")
            params.append(client_id)
        if message_type:
            filters.append("tipo = %s")
            params.append(message_type)
        if date_from:
            filters.append("DATE(fecha_creacion) >= %s")
            params.append(date_from)
        if date_to:
            filters.append("DATE(fecha_creacion) <= %s")
            params.append(date_to)

        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""SELECT id, request_id, dialog_id, mensaje_id, client_id,
                              operator_id, tipo, texto, transport, fecha_creacion
                       FROM mensajes_request
                       WHERE {' AND '.join(filters)}
                       ORDER BY fecha_creacion ASC, id ASC
                       LIMIT %s""",
                    (*params, limit),
                )
                return {
                    "company_id": company_id,
                    "year": year,
                    "month": month,
                    "messages": cursor.fetchall(),
                }
    except Exception as e:
        logger.error(f"Error getting extracted messages: {e}")
        raise HTTPException(status_code=500, detail="Could not retrieve messages")


@router.get("/conversations")
async def get_conversations(
    company_id: int,
    year: int,
    month: int,
    page: int = 1,
    page_size: int = 20,
    user: dict = Depends(get_current_user),
):
    """Returns conversations grouped by dialog for a selected period."""
    if page < 1 or not 1 <= month <= 12 or not 1 <= page_size <= 100:
        raise HTTPException(status_code=400, detail="Invalid pagination or month")

    offset = (page - 1) * page_size
    try:
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """SELECT dialog_id, request_id, client_id,
                              COUNT(*) AS total_messages,
                              MIN(fecha_creacion) AS first_message,
                              MAX(fecha_creacion) AS last_message
                       FROM mensajes_request
                       WHERE company_id = %s
                         AND YEAR(fecha_creacion) = %s
                         AND MONTH(fecha_creacion) = %s
                       GROUP BY dialog_id, request_id, client_id
                       ORDER BY last_message DESC
                       LIMIT %s OFFSET %s""",
                    (company_id, year, month, page_size, offset),
                )
                conversations = cursor.fetchall()

                cursor.execute(
                    """SELECT COUNT(*) AS total FROM (
                           SELECT dialog_id, request_id, client_id
                           FROM mensajes_request
                           WHERE company_id = %s
                             AND YEAR(fecha_creacion) = %s
                             AND MONTH(fecha_creacion) = %s
                           GROUP BY dialog_id, request_id, client_id
                       ) grouped""",
                    (company_id, year, month),
                )
                total = cursor.fetchone()["total"]

                return {
                    "company_id": company_id,
                    "year": year,
                    "month": month,
                    "page": page,
                    "page_size": page_size,
                    "total": total,
                    "conversations": conversations,
                }
    except Exception as e:
        logger.error(f"Error getting conversations: {e}")
        raise HTTPException(status_code=500, detail="Could not retrieve conversations")


@router.get("/conversations/{dialog_id}")
async def get_conversation_detail(
    dialog_id: str,
    company_id: int,
    year: int,
    month: int,
    request_id: Optional[str] = None,
    user: dict = Depends(get_current_user),
):
    """Returns the chronological timeline for one conversation."""
    filters = [
        "company_id = %s",
        "dialog_id = %s",
        "YEAR(fecha_creacion) = %s",
        "MONTH(fecha_creacion) = %s",
    ]
    params = [company_id, dialog_id, year, month]
    if request_id:
        filters.append("request_id = %s")
        params.append(request_id)

    try:
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""SELECT id, request_id, dialog_id, mensaje_id, client_id,
                              operator_id, tipo, texto, transport, fecha_creacion
                       FROM mensajes_request
                       WHERE {' AND '.join(filters)}
                       ORDER BY fecha_creacion ASC, id ASC""",
                    params,
                )
                messages = cursor.fetchall()
                return {
                    "dialog_id": dialog_id,
                    "request_id": request_id,
                    "messages": messages,
                }
    except Exception as e:
        logger.error(f"Error getting conversation detail: {e}")
        raise HTTPException(status_code=500, detail="Could not retrieve conversation")
