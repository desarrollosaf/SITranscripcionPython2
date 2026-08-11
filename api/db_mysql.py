from contextlib import contextmanager

import pymysql
import pymysql.cursors

from .config import settings


@contextmanager
def conexion():
    con = pymysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        database=settings.mysql_db,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )
    try:
        yield con
    finally:
        con.close()


def inicializar_esquema():
    with conexion() as con, con.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id INT AUTO_INCREMENT PRIMARY KEY,
                email VARCHAR(255) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                es_admin BOOLEAN NOT NULL DEFAULT FALSE,
                creado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS trabajos (
                id VARCHAR(36) PRIMARY KEY,
                usuario_id INT NOT NULL,
                url VARCHAR(1000) NOT NULL,
                participantes_pedidos JSON NOT NULL,
                participantes_encontrados JSON NOT NULL,
                participantes_no_encontrados JSON NOT NULL,
                modelo VARCHAR(20) NOT NULL,
                estado VARCHAR(20) NOT NULL DEFAULT 'ejecutando',
                pid INT,
                sesion_id INT,
                error TEXT,
                creado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                actualizado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,
                FOREIGN KEY (usuario_id) REFERENCES usuarios(id)
            )
        """)
        _agregar_columnas_si_faltan(cur, "trabajos", {
            "fuente": "VARCHAR(10) NOT NULL DEFAULT 'youtube'",
            "puerto": "INT NULL",
            "passphrase": "VARCHAR(64) NULL",
            "evento_id": "VARCHAR(64) NULL",
        })


def _agregar_columnas_si_faltan(cur, tabla, columnas):
    """Migración idempotente: agrega columnas nuevas a una tabla que ya
    puede existir en producción, sin tocar las que ya tiene."""
    cur.execute(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (tabla,))
    existentes = {r["COLUMN_NAME"] for r in cur.fetchall()}
    for columna, ddl in columnas.items():
        if columna not in existentes:
            cur.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {ddl}")
