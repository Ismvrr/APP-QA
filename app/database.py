"""
Database connection module para MySQL - API_C2D

Gestiona la conexión a la base de datos MySQL remota.
Usa PyMySQL como driver y contextmanager para manejo seguro de conexiones.

Flujo de conexión:
    1. Lee credenciales de .env (via config.py)
    2. Crea conexión a MySQL remoto
    3. Usa DictCursor para retornar resultados como diccionarios
    4. Cierra conexión automáticamente al salir del context manager

Notas:
    - La BD se configura por entorno y no se expone en el código
    - Se usa charset utf8mb4 para soporte de emojis/unicode
    - autocommit=True para simplicidad (sin transacciones explícitas)
"""

import pymysql
from pymysql.cursors import DictCursor
from contextlib import contextmanager
from app.config import get_settings

settings = get_settings()


def get_connection():
    """
    Crea una conexión individual a la base de datos.

    Returns:
        pymysql.Connection: Conexión a MySQL con DictCursor

    Raises:
        pymysql.Error: Si falla la conexión

    Nota: Esta función NO cierra la conexión automáticamente.
          Usa get_db() para conexiones con auto-close.
    """
    return pymysql.connect(
        host=settings.DB_HOST,
        port=settings.DB_PORT,
        user=settings.DB_USER,
        password=settings.DB_PASS,
        database=settings.DB_NAME,
        charset='utf8mb4',
        cursorclass=DictCursor,  # Retorna rows como diccionarios
        autocommit=True
    )


@contextmanager
def get_db():
    """
    Context manager para conexiones a la BD.

    Uso recomendado:
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM users WHERE id = %s", (user_id,))
                user = cursor.fetchone()

    Garantiza que la conexión se cierra siempre, incluso si hay errores.

    Yields:
        pymysql.Connection: Conexión abierta a la BD
    """
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def check_db_connection():
    """
    Verifica si la base de datos está accesible.

    Ejecuta una consulta simple (SELECT 1) para probar la conexión.

    Returns:
        bool: True si la BD responde, False si hay error

    Uso:
        En /health endpoint para verificar el estado del sistema.
        Si retorna False, el sistema está "degraded".
    """
    try:
        with get_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                result = cursor.fetchone()
                return result is not None
    except Exception as e:
        print(f"Database connection error: {e}")
        return False
