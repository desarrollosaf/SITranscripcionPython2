"""Conexión a la base de las sesiones transcritas (texto, oradores,
resúmenes, módulo estenográfico), en SQLite o en MySQL.

Todo el código del proyecto (transcriptor, voz.py, corregir.py, revisar.py y
la API) está escrito contra la interfaz de sqlite3: con.execute(sql, params),
filas por nombre (fila["orador"]) o por posición, cur.lastrowid, commit().
`conectar()` devuelve esa misma interfaz para cualquiera de los dos motores:

    SESIONES_DB=sqlite  (default)  -> sqlite3 sobre el archivo indicado
    SESIONES_DB=mysql              -> MySQL (mismas variables DB_* que la API)

Con MySQL, el SQL de SQLite se traduce al vuelo: "?" -> "%s",
PRAGMA table_info, INSERT OR REPLACE, ON CONFLICT ... DO UPDATE. Las tablas
NO se crean desde el código de cada script (sus CREATE TABLE se ignoran):
las crea crear_esquema_mysql(), con tipos e índices propios de MySQL.
"""
import os
import re
import sqlite3
import threading
import time


def motor():
    return os.environ.get("SESIONES_DB", "sqlite").strip().lower()


def usa_mysql():
    return motor() == "mysql"


def conectar(ruta_sqlite="sesiones.db", **opciones_sqlite):
    """Conexión según SESIONES_DB. Con SQLite, `opciones_sqlite` se pasan a
    sqlite3.connect tal cual (timeout, check_same_thread...)."""
    if usa_mysql():
        return ConexionMySQL()
    return sqlite3.connect(ruta_sqlite, **opciones_sqlite)


# Errores de base de datos de ambos motores, para los `except`.
try:
    import pymysql
    ERRORES_DB = (sqlite3.Error, pymysql.err.Error)
except ImportError:          # el transcriptor puede correr sin pymysql
    pymysql = None
    ERRORES_DB = (sqlite3.Error,)


# ---------------------------------------------------------------------------
# Esquema MySQL
# ---------------------------------------------------------------------------
# utf8mb4_bin: comparaciones exactas como en SQLite ("DESCONOCIDO" y
# "Desconocido" son oradores distintos; con la colación por default de
# MySQL serían iguales y un renombrado tocaría ambos).
_OPCIONES_TABLA = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin"

ESQUEMA_MYSQL = (
    f"""CREATE TABLE IF NOT EXISTS sesiones (
        id      INT AUTO_INCREMENT PRIMARY KEY,
        url     VARCHAR(1000),
        titulo  VARCHAR(500),
        inicio  VARCHAR(32),
        fin     VARCHAR(32)
    ) {_OPCIONES_TABLA}""",
    # Las columnas opcionales que en SQLite agregan voz.py, corregir.py y la
    # corrección de estilo ya vienen creadas: así esos scripts nunca
    # necesitan un ALTER TABLE.
    f"""CREATE TABLE IF NOT EXISTS participaciones (
        id             INT AUTO_INCREMENT PRIMARY KEY,
        sesion_id      INT NOT NULL,
        orador         VARCHAR(255),
        inicio_seg     DOUBLE,
        fin_seg        DOUBLE,
        inicio_hms     VARCHAR(16),
        fin_hms        VARCHAR(16),
        texto          MEDIUMTEXT,
        voz_orador     VARCHAR(255),
        voz_similitud  DOUBLE,
        texto_original MEDIUMTEXT,
        fuente         VARCHAR(20) DEFAULT 'rapido',
        revisado_ia    VARCHAR(20),
        motivo_ia      TEXT,
        INDEX idx_part_sesion (sesion_id, inicio_seg),
        INDEX idx_part_orador (orador),
        CONSTRAINT fk_part_sesion FOREIGN KEY (sesion_id) REFERENCES sesiones(id)
    ) {_OPCIONES_TABLA}""",
    f"""CREATE TABLE IF NOT EXISTS resumenes (
        id         INT AUTO_INCREMENT PRIMARY KEY,
        sesion_id  INT,
        ancla_id   INT UNIQUE,
        orador     VARCHAR(255),
        resumen    MEDIUMTEXT,
        creado     VARCHAR(32),
        INDEX idx_res_sesion (sesion_id)
    ) {_OPCIONES_TABLA}""",
    f"""CREATE TABLE IF NOT EXISTS estructura (
        sesion_id  INT PRIMARY KEY,
        datos      MEDIUMTEXT,
        creado     VARCHAR(32)
    ) {_OPCIONES_TABLA}""",
    f"""CREATE TABLE IF NOT EXISTS esteno_config (
        sesion_id       INT PRIMARY KEY,
        num_correctores INT NOT NULL,
        bloque_seg      INT NOT NULL,
        nombres         TEXT,
        creado          VARCHAR(32),
        actualizado     VARCHAR(32)
    ) {_OPCIONES_TABLA}""",
    f"""CREATE TABLE IF NOT EXISTS esteno_bloques (
        sesion_id     INT NOT NULL,
        indice        INT NOT NULL,
        inicio_seg    DOUBLE,
        fin_seg       DOUBLE,
        slot          INT,
        estado        VARCHAR(20) DEFAULT 'pendiente',
        tomado_por    VARCHAR(255),
        tomado_en     VARCHAR(32),
        terminado_por VARCHAR(255),
        terminado_en  VARCHAR(32),
        PRIMARY KEY (sesion_id, indice)
    ) {_OPCIONES_TABLA}""",
    f"""CREATE TABLE IF NOT EXISTS esteno_audio (
        sesion_id INT PRIMARY KEY,
        ruta      VARCHAR(1000)
    ) {_OPCIONES_TABLA}""",
)

TABLAS = ("sesiones", "participaciones", "resumenes", "estructura",
          "esteno_config", "esteno_bloques", "esteno_audio")


def crear_esquema_mysql():
    """Crea las tablas de sesiones en MySQL si no existen (idempotente)."""
    con = ConexionMySQL()
    try:
        for ddl in ESQUEMA_MYSQL:
            con.ejecutar_crudo(ddl)
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Adaptador MySQL con la interfaz de sqlite3
# ---------------------------------------------------------------------------
class Fila(dict):
    """Como sqlite3.Row: fila["col"], fila[0], fila.keys() y dict(fila)."""

    def __getitem__(self, clave):
        if isinstance(clave, int):
            return list(self.values())[clave]
        return super().__getitem__(clave)


class _Resultado:
    """Resultado ya leído completo (se lee dentro del candado, así el
    transcriptor puede compartir la conexión entre hilos sin cruzarse)."""

    def __init__(self, filas, lastrowid, rowcount):
        self._filas = filas
        self._pos = 0
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    def fetchone(self):
        if self._pos >= len(self._filas):
            return None
        fila = self._filas[self._pos]
        self._pos += 1
        return fila

    def fetchall(self):
        resto = self._filas[self._pos:]
        self._pos = len(self._filas)
        return resto

    def __iter__(self):
        return iter(self.fetchall())


_RE_PRAGMA_TABLA = re.compile(r"^\s*PRAGMA\s+table_info\s*\(\s*(\w+)\s*\)\s*$", re.I)
_RE_PRAGMA = re.compile(r"^\s*PRAGMA\b", re.I)
_RE_CREATE = re.compile(r"^\s*CREATE\s+(TABLE|INDEX|UNIQUE\s+INDEX)\b", re.I)
_RE_ON_CONFLICT = re.compile(r"ON\s+CONFLICT\s*\([^)]*\)\s*DO\s+UPDATE\s+SET", re.I)
_RE_EXCLUDED = re.compile(r"\bexcluded\.(\w+)", re.I)
_RE_INSERT_OR_REPLACE = re.compile(r"\bINSERT\s+OR\s+REPLACE\b", re.I)


def traducir(sql):
    """SQL de SQLite -> MySQL (solo lo que usa este proyecto)."""
    sql = _RE_INSERT_OR_REPLACE.sub("REPLACE", sql)
    if _RE_ON_CONFLICT.search(sql):
        sql = _RE_ON_CONFLICT.sub("ON DUPLICATE KEY UPDATE", sql)
        sql = _RE_EXCLUDED.sub(r"VALUES(\1)", sql)
    return sql.replace("%", "%%").replace("?", "%s")


class ConexionMySQL:
    # Tras este tiempo sin usarse se comprueba la conexión antes de la
    # siguiente consulta (el transcriptor puede pasar minutos callado).
    _PING_TRAS_SEG = 60

    def __init__(self):
        if pymysql is None:
            raise RuntimeError("SESIONES_DB=mysql requiere pymysql "
                               "(pip install -r requirements-api.txt)")
        self._con = pymysql.connect(
            host=os.environ.get("DB_HOST", "host.docker.internal"),
            port=int(os.environ.get("DB_PORT", "3306")),
            user=os.environ.get("DB_USER", "root"),
            password=os.environ.get("DB_PASS", ""),
            database=os.environ.get("DB_NAME", "transcripcion_api"),
            charset="utf8mb4",
            autocommit=False,
            # rowcount = filas que coinciden (como SQLite), no solo las que
            # cambiaron de valor.
            client_flag=pymysql.constants.CLIENT.FOUND_ROWS,
            cursorclass=pymysql.cursors.DictCursor,
        )
        with self._con.cursor() as cur:
            # Cada consulta ve lo que otros ya guardaron (el transcriptor
            # escribe mientras la interfaz lee).
            cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
        self._lock = threading.RLock()
        self._ultimo_uso = time.monotonic()
        self.row_factory = None   # se acepta y se ignora (siempre Fila)

    def _cursor(self):
        if time.monotonic() - self._ultimo_uso > self._PING_TRAS_SEG:
            self._con.ping(reconnect=True)
        self._ultimo_uso = time.monotonic()
        return self._con.cursor()

    def execute(self, sql, params=()):
        if _RE_CREATE.match(sql):
            return _Resultado([], None, 0)   # el esquema lo maneja MySQL
        m = _RE_PRAGMA_TABLA.match(sql)
        if m:
            return self._columnas(m.group(1))
        if _RE_PRAGMA.match(sql):
            return _Resultado([], None, 0)   # journal_mode, busy_timeout…
        with self._lock:
            with self._cursor() as cur:
                cur.execute(traducir(sql), tuple(params) if params else None)
                filas = [Fila(f) for f in cur.fetchall()] if cur.description else []
                return _Resultado(filas, cur.lastrowid, cur.rowcount)

    def executemany(self, sql, secuencia):
        with self._lock:
            with self._cursor() as cur:
                cur.executemany(traducir(sql), [tuple(p) for p in secuencia])
                return _Resultado([], cur.lastrowid, cur.rowcount)

    def ejecutar_crudo(self, sql, params=None):
        """SQL de MySQL sin traducir (DDL, migraciones)."""
        with self._lock:
            with self._cursor() as cur:
                cur.execute(sql, params)
                filas = [Fila(f) for f in cur.fetchall()] if cur.description else []
                return _Resultado(filas, cur.lastrowid, cur.rowcount)

    def _columnas(self, tabla):
        """Equivalente de PRAGMA table_info: (cid, name, type, ...), con el
        nombre en la posición 1 como lo leen los scripts."""
        with self._lock:
            with self._cursor() as cur:
                cur.execute(
                    "SELECT ORDINAL_POSITION - 1 AS cid, COLUMN_NAME AS name, "
                    "DATA_TYPE AS type FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
                    "ORDER BY ORDINAL_POSITION", (tabla,))
                return _Resultado([Fila(f) for f in cur.fetchall()], None, 0)

    def commit(self):
        with self._lock:
            self._con.commit()

    def rollback(self):
        with self._lock:
            self._con.rollback()

    def close(self):
        try:
            self._con.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, tipo, *_):
        if tipo is None:
            self.commit()
        else:
            self.rollback()
