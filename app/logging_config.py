"""
Structured JSON logging configuration - API_C2D

Configura logging en formato JSON para facilitar:
    - Análisis de logs con herramientas (ELK, Datadog, etc.)
    - Búsquedas por campo (level, logger, message)
    - Alertas basadas en contenido estructurado
    - Correlación de requests (request_id)

Uso:
    from app.logging_config import setup_logging
    setup_logging(level="INFO")

    logger = logging.getLogger(__name__)
    logger.info("user_login", extra={"user_id": 123})

Salida JSON:
    {
        "timestamp": "2026-07-20T12:00:00Z",
        "level": "INFO",
        "logger": "app.routes.auth",
        "message": "user_login",
        "module": "auth",
        "function": "login",
        "line": 42,
        "request_id": "abc-123"
    }
"""

import logging
import json
import sys
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    """
    Formatter personalizado que convierte logs a JSON estructurado.

    Soporta:
        - Timestamps en UTC ISO 8601
        - Información de traceback (exc_info)
        - request_id para correlación de requests
    """

    def format(self, record: logging.LogRecord) -> str:
        """
        Convierte un LogRecord a JSON string.

        Args:
            record: Registro de log estándar de Python

        Returns:
            str: JSON string con el log formateado
        """
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Agregar traceback si hay excepción
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)

        # Agregar request_id si está disponible (para correlación)
        if hasattr(record, "request_id"):
            log_entry["request_id"] = record.request_id

        return json.dumps(log_entry, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    """
    Configura el logging estructurado para la aplicación.

    Args:
        level: Nivel de log (DEBUG, INFO, WARNING, ERROR, CRITICAL)

    Configuración:
        - Console handler con formato JSON
        - Logs de terceros reducidos (uvicorn, pymysql)
        - Salida a stdout para Docker/containers
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Limpiar handlers existentes para evitar duplicados
    root_logger.handlers.clear()

    # Console handler con formato JSON
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(JSONFormatter())
    root_logger.addHandler(console_handler)

    # Reducir ruido de librerías externas
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("pymysql").setLevel(logging.WARNING)
