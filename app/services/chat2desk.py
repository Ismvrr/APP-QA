"""
Chat2Desk API Client - API_C2D

Cliente para la API REST de Chat2Desk (México).
Gestiona la extracción de conversaciones con paginación y reintentos.

Endpoints principales:
    GET /dialogs   - Lista de diálogos (conversaciones)
    GET /messages  - Mensajes de un diálogo

Notas de la API:
    - El filtro ?request_id=X en /messages NO funciona (API lo ignora)
    - Siempre usar order=desc para obtener mensajes recientes primero
    - La API puede limitar a ~200 mensajes por página
    - Token de autenticación en header Authorization
"""

import logging
import time
from datetime import datetime
from typing import Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

# Tipos de mensaje que se excluyen por defecto
EXCLUDED_TYPES = {"system", "comment"}

# Timeout por defecto (segundos)
DEFAULT_TIMEOUT = 15


class Chat2DeskError(Exception):
    """Excepción personalizada para errores de la API de Chat2Desk"""
    pass


class Chat2DeskClient:
    """
    Cliente para la API REST de Chat2Desk.

    Uso:
        client = Chat2DeskClient(token="tu_token")
        dialogs = client.get_dialogs()
        messages = client.get_messages(dialog_id=12345)
    """

    def __init__(self, token: Optional[str] = None):
        """
        Inicializa el cliente con un token de autenticación.

        Args:
            token: Token API de Chat2Desk. Si no se provee, usa C2D_DEFAULT_TOKEN de .env
        """
        self.base_url = settings.C2D_BASE_URL
        self.token = token or settings.C2D_DEFAULT_TOKEN
        self.session = self._create_session()

    def _create_session(self) -> requests.Session:
        """
        Crea una sesión HTTP con reintentos automáticos.

        Reintentos: 3 intentos con backoff exponencial (5s → 15s → 45s)
        """
        session = requests.Session()
        session.headers.update({
            "Authorization": self.token,
            "Content-Type": "application/json"
        })

        retry_strategy = Retry(
            total=3,
            backoff_factor=5,  # 5s, 10s, 15s (factor * 2^attempt)
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        return session

    def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        """
        Realiza una petición HTTP a la API de Chat2Desk.

        Args:
            method: Método HTTP (GET, POST, etc.)
            endpoint: Ruta del endpoint (ej: /dialogs)
            **kwargs: Argumentos adicionales para requests

        Returns:
            dict: Respuesta JSON de la API

        Raises:
            Chat2DeskError: Si la petición falla
        """
        url = f"{self.base_url}{endpoint}"
        timeout = kwargs.pop("timeout", DEFAULT_TIMEOUT)

        try:
            response = self.session.request(method, url, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.Timeout:
            logger.error(f"Timeout requesting {url}")
            raise Chat2DeskError(f"Timeout: {url}")
        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP error {e.response.status_code} on {url}: {e.response.text}")
            raise Chat2DeskError(f"HTTP {e.response.status_code}: {e.response.text}")
        except Exception as e:
            logger.error(f"Error requesting {url}: {e}")
            raise Chat2DeskError(str(e))

    # =========================================================================
    # DIALOGS
    # =========================================================================

    def get_dialogs(self, limit: int = 100, offset: int = 0) -> list:
        """
        Obtiene la lista de diálogos (conversaciones).

        Args:
            limit: Número máximo de diálogos por página (máx ~200)
            offset: Desplazamiento para paginación

        Returns:
            list: Lista de diálogos con sus datos
        """
        params = {"limit": limit, "offset": offset}
        data = self._request("GET", "/dialogs", params=params)
        return data if isinstance(data, list) else data.get("data", [])

    def get_all_dialogs(self) -> list:
        """
        Obtiene TODOS los diálogos usando paginación.

        Returns:
            list: Lista completa de diálogos
        """
        all_dialogs = []
        offset = 0
        limit = 200

        while True:
            dialogs = self.get_dialogs(limit=limit, offset=offset)
            if not dialogs:
                break

            all_dialogs.extend(dialogs)
            logger.info(f"Fetched {len(dialogs)} dialogs (total: {len(all_dialogs)})")

            if len(dialogs) < limit:
                break

            offset += limit

        return all_dialogs

    # =========================================================================
    # MESSAGES
    # =========================================================================

    def get_messages(
        self,
        dialog_id: Optional[int] = None,
        start_date: Optional[str] = None,
        finish_date: Optional[str] = None,
        limit: int = 200,
        offset: int = 0,
        order: str = "desc"
    ) -> list:
        """
        Obtiene mensajes de Chat2Desk.

        IMPORTANTE: El filtro ?request_id=X NO funciona (API lo ignora).

        Args:
            dialog_id: ID del diálogo (filtro más rápido)
            start_date: Fecha inicio (formato: "YYYY-MM-DD HH:MM")
            finish_date: Fecha fin (formato: "YYYY-MM-DD HH:MM")
            limit: Número máximo de mensajes por página
            offset: Desplazamiento para paginación
            order: Orden (asc o desc) - desc es recomendado

        Returns:
            list: Lista de mensajes
        """
        params = {"limit": limit, "offset": offset, "order": order}

        if dialog_id:
            params["dialog_id"] = dialog_id
        if start_date:
            params["start_date"] = start_date
        if finish_date:
            params["finish_date"] = finish_date

        data = self._request("GET", "/messages", params=params)
        return data if isinstance(data, list) else data.get("data", [])

    def get_messages_by_dialog(self, dialog_id: int) -> list:
        """
        Obtiene TODOS los mensajes de un diálogo específico.

        Args:
            dialog_id: ID del diálogo

        Returns:
            list: Lista completa de mensajes del diálogo
        """
        all_messages = []
        offset = 0
        limit = 200

        while True:
            messages = self.get_messages(
                dialog_id=dialog_id,
                limit=limit,
                offset=offset,
                order="asc"  # Orden cronológico para reconstruir la conversación
            )
            if not messages:
                break

            all_messages.extend(messages)
            logger.info(f"Dialog {dialog_id}: fetched {len(messages)} messages (total: {len(all_messages)})")

            if len(messages) < limit:
                break

            offset += limit

        return all_messages

    def get_messages_by_date_range(
        self,
        start_date: str,
        finish_date: str
    ) -> list:
        """
        Obtiene todos los mensajes en un rango de fechas.

        Args:
            start_date: Fecha inicio (formato: "YYYY-MM-DD HH:MM")
            finish_date: Fecha fin (formato: "YYYY-MM-DD HH:MM")

        Returns:
            list: Lista de mensajes en el rango
        """
        return list(self.iter_messages_by_date_range(start_date, finish_date))

    def iter_messages_by_date_range(
        self,
        start_date: str,
        finish_date: str
    ):
        """Yields message pages for a date range without per-dialog requests."""
        offset = 0
        limit = 200

        while True:
            messages = self.get_messages(
                start_date=start_date,
                finish_date=finish_date,
                limit=limit,
                offset=offset,
                order="desc"
            )
            if not messages:
                break

            logger.info(f"Date range: fetched page of {len(messages)} messages")
            yield messages

            if len(messages) < limit:
                break

            offset += limit

    # =========================================================================
    # UTILIDADES
    # =========================================================================

    @staticmethod
    def clean_message(raw_msg: dict, exclude_types: Optional[set] = None) -> Optional[dict]:
        """
        Limpia y formatea un mensaje crudo de Chat2Desk.

        Filtra mensajes según el tipo (system, comment, etc.) y
        formatea los campos para inserción en la BD.

        Args:
            raw_msg: Mensaje crudo de la API de C2D
            exclude_types: Tipos de mensaje a excluir (default: system, comment)

        Returns:
            dict: Mensaje limpio listo para BD, o None si se excluyó

        Ejemplo de salida:
            {
                "request_id": "113591968",
                "dialog_id": "31170845",
                "mensaje_id": "963360592",
                "client_id": "315533143",
                "operator_id": "31911",
                "tipo": "from_client",
                "texto": "Hola, necesito ayuda",
                "transport": "whatsapp",
                "fecha_creacion": "2026-07-10 17:57:56"
            }
        """
        if exclude_types is None:
            exclude_types = EXCLUDED_TYPES

        # Filtrar por tipo
        msg_type = raw_msg.get("type", "")
        if msg_type in exclude_types:
            return None

        # Extraer fecha y formatear para MySQL
        created = raw_msg.get("created", "")
        fecha_creacion = None
        if created:
            try:
                # Formato de C2D: "2026-07-10T17:57:56 UTC"
                dt = datetime.fromisoformat(created.replace(" UTC", "+00:00"))
                fecha_creacion = dt.strftime("%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                logger.warning(f"Invalid date format: {created}")

        return {
            "request_id": str(raw_msg.get("request_id", "")),
            "dialog_id": str(raw_msg.get("dialog_id", "")),
            "mensaje_id": str(raw_msg.get("id", "")),
            "client_id": str(raw_msg.get("client_id") or ""),
            "operator_id": str(raw_msg.get("operator_id") or ""),
            "tipo": msg_type,
            "texto": raw_msg.get("text", ""),
            "transport": raw_msg.get("transport", ""),
            "fecha_creacion": fecha_creacion,
        }

    @staticmethod
    def clean_messages(raw_messages: list, exclude_types: Optional[set] = None) -> list:
        """
        Limpia una lista de mensajes crudos.

        Args:
            raw_messages: Lista de mensajes crudos de la API
            exclude_types: Tipos a excluir

        Returns:
            list: Lista de mensajes limpios (sin None)
        """
        cleaned = []
        for msg in raw_messages:
            clean = Chat2DeskClient.clean_message(msg, exclude_types)
            if clean:
                cleaned.append(clean)
        return cleaned
