"""Gemini adapter used only by explicit, user-triggered analysis requests."""

import logging
from dataclasses import dataclass

from google import genai
from google.genai import errors
from google.genai import types

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


class GeminiDisabledError(RuntimeError):
    """Raised when manual Gemini analysis has not been enabled."""


class GeminiQuotaError(RuntimeError):
    """Raised when the Google project has no available Gemini quota."""


class GeminiConfigurationError(RuntimeError):
    """Raised when the API key or Gemini configuration is invalid."""


@dataclass
class GeminiResult:
    text: str
    input_tokens: int
    output_tokens: int
    total_tokens: int


def analyze_text(
    prompt: str,
    api_key: str | None = None,
    response_schema: dict | None = None,
) -> GeminiResult:
    """
    Sends one explicit prompt to Gemini.

    This function never runs from extraction or webhooks. GEMINI_ENABLED must
    be enabled deliberately in the server environment before it can spend tokens.
    """
    if not settings.GEMINI_ENABLED:
        raise GeminiDisabledError("Gemini analysis is disabled")
    key = api_key or settings.GEMINI_API_KEY
    if not key:
        raise GeminiConfigurationError("No hay una API key de Gemini configurada")

    client = genai.Client(api_key=key)
    try:
        config = None
        if response_schema:
            config = types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=response_schema,
            )
        response = client.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=prompt,
            config=config,
        )
    except errors.ClientError as exc:
        logger.warning("Gemini API client error code=%s detail=%s", exc.code, str(exc))
        if exc.code == 429:
            raise GeminiQuotaError(
                "Gemini no tiene cuota disponible para este proyecto o modelo. "
                "Revisa billing, límites y la API key en Google AI Studio."
            ) from exc
        if exc.code in (400, 401, 403, 404):
            raise GeminiConfigurationError(
                f"La API key no tiene acceso o el modelo {settings.GEMINI_MODEL} no está disponible para este proyecto. "
                "Verifica el proyecto, la Generative Language API y las restricciones de la clave."
            ) from exc
        raise
    usage = getattr(response, "usage_metadata", None)

    input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
    output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
    total_tokens = int(getattr(usage, "total_token_count", input_tokens + output_tokens) or 0)

    return GeminiResult(
        text=response.text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )
