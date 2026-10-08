"""Acceso a la base SQLite de sesiones (sesiones.db) para los routers de
revisión, módulo estenográfico y lanzador.

La lógica de negocio (compactar, IA, bloques estenográficos, Word) vive en
revisar.py y se reutiliza tal cual: aquí solo se abre la conexión y se
agregan los ayudantes que antes dependían del manejador HTTP de revisar.py.
"""
from contextlib import contextmanager

import revisar

from .config import settings
from .db_mysql import conexion as conexion_mysql

# El transcriptor crea estas dos tablas al arrancar; se repiten aquí para
# que la interfaz funcione aunque todavía no se haya transcrito nada.
_ESQUEMA_BASE = (
    """CREATE TABLE IF NOT EXISTS sesiones (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        url     TEXT,
        titulo  TEXT,
        inicio  TEXT,
        fin     TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS participaciones (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        sesion_id   INTEGER REFERENCES sesiones(id),
        orador      TEXT,
        inicio_seg  REAL,
        fin_seg     REAL,
        inicio_hms  TEXT,
        fin_hms     TEXT,
        texto       TEXT
    )""",
)


@contextmanager
def db():
    """Conexión a sesiones.db con las tablas de revisión ya creadas."""
    con = revisar.conectar_db(settings.db_path)
    try:
        for ddl in _ESQUEMA_BASE:
            con.execute(ddl)
        con.commit()
        yield con
    finally:
        con.close()


def columnas_participaciones(con):
    return {r[1] for r in con.execute("PRAGMA table_info(participaciones)")}


def sesion_en_vivo(sesion_id):
    """True si algo sigue escribiendo en esa sesión: el lanzador local
    (cualquier transcripción en curso, igual que en revisar.py) o un
    trabajo de la API ligado a esa sesión."""
    from . import lanzador
    if lanzador.corriendo():
        return True
    try:
        with conexion_mysql() as con, con.cursor() as cur:
            cur.execute("SELECT 1 FROM trabajos WHERE sesion_id=%s AND "
                        "estado IN ('ejecutando','deteniendo') LIMIT 1",
                        (sesion_id,))
            return cur.fetchone() is not None
    except Exception:
        return False


def es_admin(usuario):
    return bool(usuario and usuario["es_admin"])
