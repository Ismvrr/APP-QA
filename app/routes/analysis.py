"""Manual Gemini analysis endpoints for conversations and periods."""

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.database import get_db
from app.services.gemini import (
    GeminiConfigurationError,
    GeminiDisabledError,
    GeminiQuotaError,
    analyze_text,
)
from app.services.jwt import get_current_user

router = APIRouter(prefix="/api/analyze", tags=["analysis"])
logger = logging.getLogger(__name__)
SERVER_KEY_MINUTE_LIMIT = 2
SERVER_KEY_DAILY_LIMIT = 30
ANALYSIS_BATCH_SIZE = 5
MAX_TRANSCRIPT_CHARS = 8000
def _batch_response_schema(expected: int) -> dict:
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "minItems": expected,
                "maxItems": expected,
                "items": {
                    "type": "object",
                    "properties": {
                        "conversation_index": {"type": "integer"},
                        "analysis": {"type": "string"},
                    },
                    "required": ["conversation_index", "analysis"],
                },
            },
            "block_summary": {"type": "string"},
        },
        "required": ["results", "block_summary"],
    }


class ConversationAnalysisRequest(BaseModel):
    company_id: int
    dialog_id: str
    request_id: Optional[str] = None
    year: int = Field(..., ge=2020, le=2030)
    month: int = Field(..., ge=1, le=12)
    client_prompt_id: Optional[int] = None
    prompt_text: str = Field(..., min_length=10, max_length=12000)


class PeriodAnalysisRequest(BaseModel):
    company_id: int
    year: int = Field(..., ge=2020, le=2030)
    month: int = Field(..., ge=1, le=12)
    client_prompt_id: Optional[int] = None
    prompt_text: str = Field(..., min_length=10, max_length=12000)
    max_conversations: Optional[int] = Field(default=1, ge=1, le=10000)
    full_month: bool = False
    consolidate: bool = False


def _transcript(cursor, request: ConversationAnalysisRequest) -> list[dict]:
    query = """SELECT id, request_id, dialog_id, tipo, texto, fecha_creacion
               FROM mensajes_request
               WHERE company_id=%s AND dialog_id=%s
                 AND YEAR(fecha_creacion)=%s AND MONTH(fecha_creacion)=%s"""
    params: list = [request.company_id, request.dialog_id, request.year, request.month]
    if request.request_id:
        query += " AND request_id=%s"
        params.append(request.request_id)
    query += " ORDER BY fecha_creacion ASC, id ASC"
    cursor.execute(query, params)
    return cursor.fetchall()


def _replace_prompt_variables(instructions: str, company: dict, transcript: str) -> str:
    company_name = company.get("name") or "No especificado"
    context = "; ".join(
        value for value in [
            f"Empresa: {company_name}",
            f"Estado: {company.get('status') or 'No especificado'}",
            f"Modo: {company.get('company_mode') or 'No especificado'}",
            f"Idioma: {company.get('lang') or 'No especificado'}",
            f"Zona horaria: {company.get('timezone') or 'No especificado'}",
        ]
    )
    values = {
        "company_name": company_name,
        "industry": company.get("company_mode") or "No especificada",
        "company_context": context,
        "company_services": company.get("subscription_addons") or "No especificados",
        "business_model": company.get("subscription_type") or "No especificado",
        "campaign_context": "No especificado",
        "bot_flow": "No especificado",
        "channels": "Chat2Desk; canal específico no indicado en la configuración",
        "analysis_objective": "Evaluar la conversación y detectar oportunidades de mejora.",
        "success_criteria": "Respuesta pertinente, continuidad del flujo y siguiente acción clara.",
        "fields_to_extract": "Intención, etapa, objeciones, resultado, fricciones y recomendaciones.",
        "conversation": transcript,
    }
    for key, value in values.items():
        instructions = instructions.replace("{{" + key + "}}", str(value))
    return instructions


def _transcript_text(messages: list[dict]) -> str:
    return "\n".join(
        f"[{message['fecha_creacion']}] {message['tipo']}: {message['texto'] or ''}"
        for message in messages
    )


def _build_prompt(instructions: str, messages: list[dict], company: dict) -> str:
    transcript = _transcript_text(messages)
    return _build_transcript_prompt(instructions, transcript, company)


def _build_transcript_prompt(instructions: str, transcript: str, company: dict) -> str:
    prompt = _replace_prompt_variables(instructions.strip(), company, transcript)
    if "CONVERSACIÓN:" not in prompt and "CONVERSACION:" not in prompt:
        prompt = f"{prompt}\n\nCONVERSACION A ANALIZAR:\n{transcript}"
    return prompt


def _compact_transcript(messages: list[dict]) -> str:
    """Keep both ends of long conversations while bounding batch prompt size."""
    transcript = _transcript_text(messages)
    if len(transcript) <= MAX_TRANSCRIPT_CHARS:
        return transcript
    half = MAX_TRANSCRIPT_CHARS // 2
    return transcript[:half] + "\n[... conversación recortada ...]\n" + transcript[-half:]


def _build_batch_prompt(instructions: str, conversations: list[dict], company: dict) -> str:
    base = _replace_prompt_variables(instructions.strip(), company, "")
    entries = []
    for index, conversation in enumerate(conversations, start=1):
        entries.append(
            f"CONVERSACIÓN {index} (dialog_id={conversation['dialog_id']}, "
            f"request_id={conversation.get('request_id') or 'No identificado'}):\n"
            f"{conversation['transcript']}"
        )
    return (
        f"{base}\n\n"
        "INSTRUCCIÓN DE LOTE: analiza cada conversación por separado. "
        f"Debes generar exactamente {len(conversations)} resultados, uno por cada índice. "
        "Devuelve únicamente un JSON válido con esta forma: "
        '{"results":[{"conversation_index":1,"analysis":"texto Markdown"}],'
        '"block_summary":"resumen breve del bloque"}. '
        "Incluye exactamente un resultado por conversación, agrega un resumen del bloque "
        "y no escribas texto fuera del JSON.\n\n"
        + "\n\n---\n\n".join(entries)
    )


def _parse_batch_result(text: str, expected: int) -> tuple[list[str], str]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    payload = json.loads(cleaned)
    items = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(items, list) or len(items) != expected:
        raise ValueError(f"Gemini devolvió {len(items) if isinstance(items, list) else 0} resultados; se esperaban {expected}")
    ordered = [None] * expected
    for item in items:
        index = int(item.get("conversation_index", 0)) - 1
        analysis = item.get("analysis") or item.get("result") or item.get("text")
        if not 0 <= index < expected or not isinstance(analysis, str) or not analysis.strip():
            raise ValueError("Respuesta de lote con formato inválido")
        ordered[index] = analysis.strip()
    if any(value is None for value in ordered):
        raise ValueError("Falta un resultado de conversación en la respuesta de Gemini")
    summary = payload.get("block_summary", "") if isinstance(payload, dict) else ""
    if not isinstance(summary, str) or not summary.strip():
        # Some valid model responses contain all individual analyses but omit
        # the optional summary. Keep the batch usable without another Gemini call.
        summary = " ".join(value.replace("\n", " ")[:300] for value in ordered)
    return ordered, summary.strip()[:2000]


def _load_company(cursor, company_id: int) -> dict:
    cursor.execute(
        """SELECT name, status, company_mode, lang, timezone,
                  subscription_type, subscription_addons
           FROM companies WHERE id=%s""",
        (company_id,),
    )
    company = cursor.fetchone()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


def _save_consolidation(
    request: PeriodAnalysisRequest,
    results: list[dict],
    api_key: Optional[str],
    user: dict,
) -> dict:
    source = "byok" if api_key else "server"
    source_text = "\n\n--- RESULTADO ---\n".join(item["result"] for item in results)
    prompt = (
        "Actúa como analista senior. Consolida los resultados de análisis de conversaciones "
        "del periodo indicado. No inventes datos. Entrega patrones, hallazgos repetidos, "
        "problemas prioritarios y recomendaciones accionables.\n\n"
        f"PERIODO: {request.year}-{request.month:02d}\n"
        f"RESULTADOS DE CONVERSACIONES:\n{source_text}"
    )

    with get_db() as conn:
        with conn.cursor() as cursor:
            _load_company(cursor, request.company_id)
            if not api_key:
                _enforce_server_key_rate_limit(cursor, request.company_id)
            cursor.execute(
                """INSERT INTO analysis_jobs
                   (company_id, client_prompt_id, year, month, status, prompt_snapshot,
                    gemini_key_source, started_at)
                   VALUES (%s, %s, %s, %s, 'running', %s, %s, NOW())""",
                (request.company_id, request.client_prompt_id, request.year, request.month, prompt, source),
            )
            job_id = cursor.lastrowid
            conn.commit()

    try:
        result = analyze_text(prompt, api_key)
    except GeminiDisabledError as exc:
        _mark_job_error(job_id, str(exc))
        raise HTTPException(status_code=409, detail=str(exc))
    except GeminiQuotaError as exc:
        _mark_job_error(job_id, str(exc))
        raise HTTPException(status_code=429, detail=str(exc))
    except GeminiConfigurationError as exc:
        _mark_job_error(job_id, str(exc))
        raise HTTPException(status_code=401, detail=str(exc))

    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """UPDATE analysis_jobs
                   SET status='completed', prompt2_result=%s, gemini_tokens_used=%s,
                       input_tokens=%s, output_tokens=%s, completed_at=NOW()
                   WHERE id=%s""",
                (
                    json.dumps({"text": result.text}, ensure_ascii=False),
                    result.total_tokens,
                    result.input_tokens,
                    result.output_tokens,
                    job_id,
                ),
            )
            conn.commit()

    return {
        "job_id": job_id,
        "result": result.text,
        "tokens": {
            "input": result.input_tokens,
            "output": result.output_tokens,
            "total": result.total_tokens,
        },
    }


def _update_batch(batch_id: int, **values) -> None:
    if not values:
        return
    assignments = ", ".join(f"{key}=%s" for key in values)
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"UPDATE analysis_batches SET {assignments} WHERE id=%s",
                (*values.values(), batch_id),
            )
            conn.commit()


async def _run_period_analysis(
    batch_id: int,
    company_id: int,
    year: int,
    month: int,
    client_prompt_id: Optional[int],
    prompt_text: str,
    conversations: list[dict],
    consolidate: bool,
    api_key: Optional[str],
):
    try:
        _update_batch(batch_id, status="running", started_at=datetime.now())
        with get_db() as conn:
            with conn.cursor() as cursor:
                company = _load_company(cursor, company_id)

        results = []
        block_summaries = []
        total_tokens = 0
        last_call_at = None

        for start in range(0, len(conversations), ANALYSIS_BATCH_SIZE):
            block = conversations[start:start + ANALYSIS_BATCH_SIZE]
            if not api_key and last_call_at:
                wait_seconds = 30 - (datetime.now() - last_call_at).total_seconds()
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)

            with get_db() as conn:
                with conn.cursor() as cursor:
                    for conversation in block:
                        cursor.execute(
                            """SELECT id, request_id, dialog_id, tipo, texto, fecha_creacion
                               FROM mensajes_request
                               WHERE company_id=%s AND dialog_id=%s
                                 AND YEAR(fecha_creacion)=%s AND MONTH(fecha_creacion)=%s
                                 AND (%s IS NULL OR request_id=%s)
                               ORDER BY fecha_creacion ASC, id ASC""",
                            (company_id, conversation["dialog_id"], year, month,
                             conversation.get("request_id"), conversation.get("request_id")),
                        )
                        conversation["transcript"] = _compact_transcript(cursor.fetchall())

            with get_db() as conn:
                with conn.cursor() as cursor:
                    if not api_key:
                        _enforce_server_key_rate_limit(cursor, company_id)

            prompt = _build_batch_prompt(prompt_text, block, company)
            last_call_at = datetime.now()
            try:
                result = await asyncio.to_thread(
                    analyze_text,
                    prompt,
                    api_key,
                    _batch_response_schema(len(block)),
                )
                # Count the API response even if its structure needs fallback handling.
                total_tokens += result.total_tokens
                analyses, block_summary = _parse_batch_result(result.text, len(block))
            except ValueError as batch_error:
                # Some Gemini responses still collapse a multi-conversation
                # request into one result. Fall back to reliable individual
                # calls for this block instead of losing the whole analysis.
                logger.warning("Batch %s returned invalid structure; falling back to individual calls: %s", batch_id, batch_error)
                analyses = []
                for conversation in block:
                    if not api_key and last_call_at:
                        wait_seconds = 30 - (datetime.now() - last_call_at).total_seconds()
                        if wait_seconds > 0:
                            await asyncio.sleep(wait_seconds)
                    single_prompt = _build_transcript_prompt(
                        prompt_text, conversation["transcript"], company
                    )
                    if not api_key:
                        with get_db() as conn:
                            with conn.cursor() as cursor:
                                _enforce_server_key_rate_limit(cursor, company_id)
                    last_call_at = datetime.now()
                    single_result = await asyncio.to_thread(analyze_text, single_prompt, api_key)
                    analyses.append(single_result.text.strip())
                    total_tokens += single_result.total_tokens
                block_summary = " ".join(value.replace("\n", " ")[:300] for value in analyses)[:2000]
            block_summaries.append(block_summary)

            with get_db() as conn:
                with conn.cursor() as cursor:
                    for conversation, analysis in zip(block, analyses):
                        cursor.execute(
                            """INSERT INTO analysis_jobs
                               (company_id, request_id, client_prompt_id, prompt_snapshot,
                                gemini_key_source, year, month, status, prompt1_result,
                                gemini_tokens_used, started_at, completed_at, analysis_batch_id)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, 'completed', %s, %s, NOW(), NOW(), %s)""",
                            (
                                company_id, conversation.get("request_id"), client_prompt_id,
                                prompt_text, "byok" if api_key else "server", year, month,
                                json.dumps({"text": analysis}, ensure_ascii=False),
                                0, batch_id,
                            ),
                        )
                        results.append({"result": analysis, "tokens": {"total": 0}})
                    conn.commit()

            _update_batch(
                batch_id,
                processed_conversations=start + len(block),
                total_tokens=total_tokens,
            )

        consolidation_result = None
        if consolidate and len(results) > 1:
            if not api_key and last_call_at:
                wait_seconds = 30 - (datetime.now() - last_call_at).total_seconds()
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
            consolidation = await asyncio.to_thread(
                _save_consolidation,
                PeriodAnalysisRequest(
                    company_id=company_id, year=year, month=month,
                    client_prompt_id=client_prompt_id, prompt_text=prompt_text,
                    max_conversations=len(conversations), full_month=True,
                    consolidate=True,
                ),
                [{"result": summary, "tokens": {"total": 0}} for summary in block_summaries],
                api_key, {"company_id": company_id},
            )
            consolidation_result = consolidation["result"]
            total_tokens += consolidation["tokens"]["total"]

        _update_batch(
            batch_id,
            status="completed",
            total_tokens=total_tokens,
            consolidation_result=json.dumps({"text": consolidation_result}, ensure_ascii=False)
            if consolidation_result else None,
            completed_at=datetime.now(),
        )
    except Exception as exc:
        logger.exception("Period analysis batch %s failed", batch_id)
        _update_batch(
            batch_id,
            status="error",
            total_tokens=total_tokens,
            error_message=str(exc)[:500],
        )


def _enforce_server_key_rate_limit(cursor, company_id: int) -> None:
    """Limit shared server-key usage without affecting BYOK requests."""
    cursor.execute(
        """SELECT COUNT(DISTINCT COALESCE(analysis_batch_id, id)) AS total
           FROM analysis_jobs
           WHERE company_id=%s AND gemini_key_source='server'
             AND started_at >= DATE_SUB(NOW(), INTERVAL 1 MINUTE)""",
        (company_id,),
    )
    if cursor.fetchone()["total"] >= SERVER_KEY_MINUTE_LIMIT:
        raise HTTPException(
            status_code=429,
            detail="Límite de 2 análisis por minuto alcanzado para la clave del servidor.",
            headers={"Retry-After": "60"},
        )

    cursor.execute(
        """SELECT COUNT(DISTINCT COALESCE(analysis_batch_id, id)) AS total
           FROM analysis_jobs
           WHERE company_id=%s AND gemini_key_source='server'
             AND started_at >= DATE_SUB(NOW(), INTERVAL 1 DAY)""",
        (company_id,),
    )
    if cursor.fetchone()["total"] >= SERVER_KEY_DAILY_LIMIT:
        raise HTTPException(
            status_code=429,
            detail="Límite de 30 análisis diarios alcanzado para la clave del servidor.",
            headers={"Retry-After": "3600"},
        )


@router.post("/conversation")
async def analyze_conversation(
    request: ConversationAnalysisRequest,
    x_gemini_api_key: Optional[str] = Header(default=None),
    user: dict = Depends(get_current_user),
):
    """Analyzes one conversation only when explicitly requested by the user."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            messages = _transcript(cursor, request)
            if not messages:
                raise HTTPException(status_code=404, detail="Conversation has no extracted messages")
            company = _load_company(cursor, request.company_id)
            if not x_gemini_api_key:
                _enforce_server_key_rate_limit(cursor, request.company_id)
            prompt = _build_prompt(request.prompt_text, messages, company)

            cursor.execute(
                """INSERT INTO analysis_jobs
                   (company_id, request_id, client_prompt_id, year, month, status, prompt_snapshot,
                    gemini_key_source, started_at)
                   VALUES (%s, %s, %s, %s, %s, 'running', %s, %s, NOW())""",
                (
                    request.company_id,
                    request.request_id,
                    request.client_prompt_id,
                    request.year,
                    request.month,
                    prompt,
                    "byok" if x_gemini_api_key else "server",
                ),
            )
            job_id = cursor.lastrowid
            conn.commit()

    try:
        result = await asyncio.to_thread(analyze_text, prompt, x_gemini_api_key)
    except GeminiDisabledError as exc:
        _mark_job_error(job_id, str(exc))
        raise HTTPException(status_code=409, detail=str(exc))
    except GeminiQuotaError as exc:
        _mark_job_error(job_id, str(exc))
        raise HTTPException(status_code=429, detail=str(exc))
    except GeminiConfigurationError as exc:
        _mark_job_error(job_id, str(exc))
        raise HTTPException(status_code=401, detail=str(exc))
    except Exception as exc:
        logger.exception("Gemini analysis failed")
        _mark_job_error(job_id, str(exc)[:500])
        raise HTTPException(status_code=502, detail="Gemini analysis failed")

    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """UPDATE analysis_jobs
                   SET status='completed', prompt1_result=%s, gemini_tokens_used=%s,
                       input_tokens=%s, output_tokens=%s, completed_at=NOW()
                   WHERE id=%s""",
                (
                    json.dumps({"text": result.text}, ensure_ascii=False),
                    result.total_tokens,
                    result.input_tokens,
                    result.output_tokens,
                    job_id,
                ),
            )
            conn.commit()

    return {
        "job_id": job_id,
        "status": "completed",
        "dialog_id": request.dialog_id,
        "request_id": request.request_id,
        "result": result.text,
        "tokens": {
            "input": result.input_tokens,
            "output": result.output_tokens,
            "total": result.total_tokens,
        },
    }


@router.post("/period", status_code=202)
async def analyze_period(
    request: PeriodAnalysisRequest,
    background_tasks: BackgroundTasks,
    x_gemini_api_key: Optional[str] = Header(default=None),
    user: dict = Depends(get_current_user),
):
    """
    Analyzes a manually selected period with either an explicit conversation
    limit or every conversation extracted for that month.

    The default limit is one conversation so a first test cannot accidentally
    process a full month and spend tokens unexpectedly. ``full_month`` is an
    explicit opt-in for the complete extracted period.
    """
    with get_db() as conn:
        with conn.cursor() as cursor:
            # A dialog is the lead conversation; include all request sessions
            # from the selected month instead of analyzing each request alone.
            query = """SELECT dialog_id, NULL AS request_id
                       FROM mensajes_request
                       WHERE company_id=%s AND YEAR(fecha_creacion)=%s AND MONTH(fecha_creacion)=%s
                       GROUP BY dialog_id
                       ORDER BY MAX(fecha_creacion) DESC"""
            params = [request.company_id, request.year, request.month]
            if not request.full_month:
                query += " LIMIT %s"
                params.append(request.max_conversations or 1)
            cursor.execute(query, params)
            conversations = cursor.fetchall()
            if not conversations:
                raise HTTPException(status_code=404, detail="No hay conversaciones extraídas para este periodo")
            cursor.execute(
                """INSERT INTO analysis_batches
                   (company_id, client_prompt_id, year, month, status,
                    total_conversations, consolidate)
                   VALUES (%s, %s, %s, %s, 'pending', %s, %s)""",
                (
                    request.company_id, request.client_prompt_id, request.year,
                    request.month, len(conversations), request.consolidate or request.full_month,
                ),
            )
            batch_id = cursor.lastrowid
            conn.commit()

    background_tasks.add_task(
        _run_period_analysis,
        batch_id=batch_id,
        company_id=request.company_id,
        year=request.year,
        month=request.month,
        client_prompt_id=request.client_prompt_id,
        prompt_text=request.prompt_text,
        conversations=conversations,
        consolidate=request.consolidate or request.full_month,
        api_key=x_gemini_api_key,
    )
    return {
        "status": "running",
        "batch_id": batch_id,
        "total_conversations": len(conversations),
        "batch_size": ANALYSIS_BATCH_SIZE,
    }


@router.get("/period/{batch_id}")
async def analysis_period_status(
    batch_id: int,
    user: dict = Depends(get_current_user),
):
    """Returns progress for a background period analysis."""
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """SELECT id, company_id, year, month, status,
                          total_conversations, processed_conversations,
                          failed_conversations, total_tokens, consolidation_result,
                          error_message, completed_at
                   FROM analysis_batches WHERE id=%s""",
                (batch_id,),
            )
            batch = cursor.fetchone()
    token_company_id = user.get("company_id")
    if not batch or (
        token_company_id is not None
        and int(batch["company_id"]) != int(token_company_id)
    ):
        raise HTTPException(status_code=404, detail="Analysis batch not found")
    if batch.get("consolidation_result"):
        try:
            batch["consolidation"] = json.loads(batch.pop("consolidation_result")).get("text", "")
        except (TypeError, json.JSONDecodeError):
            batch["consolidation"] = ""
    else:
        batch.pop("consolidation_result", None)
    return batch


def _mark_job_error(job_id: int, message: str) -> None:
    with get_db() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE analysis_jobs SET status='error', error_message=%s WHERE id=%s",
                (message[:500], job_id),
            )
            conn.commit()
