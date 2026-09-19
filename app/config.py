"""
Configuration settings para API_C2D

Carga variables desde el archivo .env usando Pydantic Settings.
Los valores se cachean con @lru_cache para evitar re-leer el archivo .env.

Uso:
    from app.config import get_settings
    settings = get_settings()
    print(settings.DB_HOST)

Notas de seguridad:
    - Las credenciales de BD están en .env (nunca en código fuente)
    - El .env NO se sube a git (excluido en .gitignore)
    - El .env.example tiene valores placeholder para referencia
"""

from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    """
    Configuración de la aplicación.

    Todos los campos se cargan desde variables de entorno o .env.
    El orden de prioridad es: ENV > .env > valor por defecto.
    """

    # =========================================================================
    # APP - Configuración general
    # =========================================================================
    APP_NAME: str = "QAIA"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False

    # =========================================================================
    # DATABASE - Conexión MySQL remota
    # =========================================================================
    # NOTA: Estos valores NO están en .env.example (son específicos del entorno)
    DB_HOST: str = ""          # Host de MySQL, configurado por entorno
    DB_PORT: int = 3306        # Puerto MySQL
    DB_USER: str = ""          # Usuario de BD
    DB_PASS: str = ""          # Contraseña de BD
    DB_NAME: str = ""          # Nombre de la BD

    # =========================================================================
    # Chat2Desk - API externa
    # =========================================================================
    C2D_BASE_URL: str = "https://api.chat2desk.com.mx/v1"  # API REST
    C2D_WEB_URL: str = "https://web.chat2desk.com.mx"      # Web login
    C2D_DEFAULT_TOKEN: str = ""  # Token API de Chat2Desk

    # =========================================================================
    # Gemini - IA para análisis
    # =========================================================================
    GEMINI_API_KEY: str = ""    # API key de Google Gemini
    GEMINI_MODEL: str = "gemini-3.5-flash-lite"  # Modelo a usar
    GEMINI_ENABLED: bool = False  # Se habilita solo para pruebas manuales

    # =========================================================================
    # JWT - Autenticación entre Laravel y FastAPI
    # =========================================================================
    # IMPORTANTE: Debe ser idéntico al JWT_SECRET en Laravel .env
    JWT_SECRET: str = "change-this-in-production"
    JWT_ALGORITHM: str = "HS256"       # Algoritmo de firma
    JWT_EXPIRY_MINUTES: int = 480      # Expiración: 8 horas

    # =========================================================================
    # SERVER - Configuración del servidor FastAPI
    # =========================================================================
    HOST: str = "0.0.0.0"   # Escuchar en todas las interfaces
    PORT: int = 8000         # Puerto de FastAPI

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    """
    Retorna la configuración cacheada.

    Usa @lru_cache para que solo se lea el .env una vez.
    En producción, esto es seguro porque la config no cambia.
    """
    return Settings()
