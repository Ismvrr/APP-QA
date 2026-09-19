"""
Router de predicción IA (predict) para APP-QA.

Cara pública de la ingeniería IA real del módulo. Expone un único
endpoint `POST /api/predict` que analiza un texto/feedback con la
configuración de IA (correcta para el esquema de producción del jefe,
punto 9 del plan de despliegue).

No duplica lógica: reutiliza `app/services/gemini.py` que es la
misma pieza de IA que ya corre en el módulo Tools, solo expuesta
aquí como endpoint de predicción.

Se habilita cuando GEMINI_ENABLED=true y GEMINI_API_KEY están
configuradas en el .env. Sin la key, retorna 503 con mensaje claro.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.gemini import (
    GeminiConfigurationError,
    GeminiDisabledError,
    GeminiQuotaError,
    analyze_text,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/predict", tags=["predict"])


class PredictRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=12000)


class PredictResponse(BaseModel):
    prediction: str
    model: str
    enabled: bool


@router.post("", response_model=PredictResponse)
async def predict(payload: PredictRequest):
    """
    POST /api/predict —{"text": "..."}

    Analiza un texto (feedback o instrucción) con la IA y retorna el
    resultado en texto.

    Args:
        payload: { "text": "..." }

    Retorna:
        { "prediction": "<resumen>", "model": ..., "enabled": true }

    Errores:
        503 - IA deshabilitada o config mal (Gemini)
        429 - Sin cuota de Gemini
    """
    try:
        result = analyze_text(prompt=payload.text)
    except GeminiDisabledError as exc:
        logger.warning("predict_disabled", extra={"detail": str(exc)})
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except GeminiConfigurationError as exc:
        logger.warning("predict_config_error", extra={"detail": str(exc)})
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except GeminiQuotaError as exc:
        logger.warning("predict_quota", extra={"detail": str(exc)})
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    finally:
        logger.info("predict_ok", extra={"model": getattr(result, "model", "gemini")})

    return PredictResponse(
        prediction=result.text,
        model="gemini",
        enabled=True,
    )
