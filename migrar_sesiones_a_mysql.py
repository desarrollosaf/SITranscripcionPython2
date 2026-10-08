#!/usr/bin/env python3
"""Copia las sesiones de un archivo SQLite (sesiones.db o un respaldo) a las
tablas de MySQL que usa SESIONES_DB=mysql (misma base que usuarios/trabajos).

- Conserva los mismos id (la sesión #50 sigue siendo la #50), así los
  trabajos de la API y los enlaces siguen apuntando a lo mismo.
- Por default salta las sesiones sin texto (arranques fallidos).
- Todo va en una sola transacción: si algo falla, MySQL queda como estaba.
- Al final compara cuántos registros hay de cada lado.

Uso (dentro de Docker, con las variables DB_* del .env):
    docker compose exec api python migrar_sesiones_a_mysql.py --sqlite respaldos/respaldo_sesiones.db --simular
    docker compose exec api python migrar_sesiones_a_mysql.py --sqlite respaldos/respaldo_sesiones.db
"""
import argparse
import os
import sqlite3
import sys

import db_sesiones

LOTE = 500


def columnas_sqlite(con, tabla):
    return [r[1] for r in con.execute(f"PRAGMA table_info({tabla})")]


def existe_sqlite(con, tabla):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (tabla,)).fetchone() is not None


def columnas_mysql(con, tabla):
    return [r[1] for r in con.execute(f"PRAGMA table_info({tabla})")]


def contar_mysql(con, tabla):
    return con.execute(f"SELECT COUNT(*) AS n FROM `{tabla}`").fetchone()["n"]


def filas_a_migrar(origen, tabla, conservar, saltar_audio_vacio):
    """(columnas, filas) de una tabla del SQLite, solo de las sesiones que
    se conservan."""
    if not existe_sqlite(origen, tabla):
        return [], []
    cols = columnas_sqlite(origen, tabla)
    clave = "id" if tabla == "sesiones" else "sesion_id"
    filas = []
    for f in origen.execute(f"SELECT * FROM {tabla}"):
        if f[clave] not in conservar:
            continue
        if tabla == "esteno_audio" and saltar_audio_vacio and not (f["ruta"] or "").strip():
            continue
        filas.append(f)
    return cols, filas


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sqlite", required=True, help="archivo SQLite de origen")
    ap.add_argument("--incluir-vacias", action="store_true",
                    help="migrar también las sesiones sin ningún texto")
    ap.add_argument("--reemplazar", action="store_true",
                    help="vaciar antes las tablas de sesiones en MySQL "
                         "(por default se niega si ya tienen datos)")
    ap.add_argument("--simular", action="store_true",
                    help="solo mostrar qué se migraría, sin escribir nada")
    args = ap.parse_args()

    if not os.path.isfile(args.sqlite):
        sys.exit(f"No existe {args.sqlite}")
    origen = sqlite3.connect(f"file:{args.sqlite}?mode=ro", uri=True)
    origen.row_factory = sqlite3.Row
    if origen.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        sys.exit("El archivo SQLite está dañado (integrity_check falló).")

    # Sesiones a conservar
    con_texto = {r[0] for r in origen.execute(
        "SELECT DISTINCT sesion_id FROM participaciones")}
    todas = [r["id"] for r in origen.execute("SELECT id FROM sesiones ORDER BY id")]
    conservar = set(todas) if args.incluir_vacias else (set(todas) & con_texto)
    vacias = sorted(set(todas) - conservar)
    print(f"Sesiones en el SQLite: {len(todas)}  ·  a migrar: {len(conservar)}"
          f"  ·  vacías que se saltan: {len(vacias)}")
    if vacias:
        print("   saltadas:", ", ".join(f"#{i}" for i in vacias))

    plan = {}
    for tabla in db_sesiones.TABLAS:
        plan[tabla] = filas_a_migrar(origen, tabla, conservar, saltar_audio_vacio=True)
        print(f"   {tabla:<16} {len(plan[tabla][1]):>6} registros")

    db_sesiones.crear_esquema_mysql()
    destino = db_sesiones.ConexionMySQL()
    try:
        # Trabajos de la API que apuntan a sesiones que no se migran.
        huerfanos = []
        if vacias:
            marcas = ",".join(["%s"] * len(vacias))
            huerfanos = destino.ejecutar_crudo(
                f"SELECT id, sesion_id, estado FROM trabajos WHERE sesion_id IN ({marcas})",
                vacias).fetchall()
        if huerfanos:
            print(f"\n⚠ {len(huerfanos)} trabajo(s) de la API apuntan a sesiones vacías "
                  "que no se migran (se dejan tal cual; no tienen texto que ver):")
            for t in huerfanos:
                print(f"   trabajo {t['id']} -> sesión #{t['sesion_id']} ({t['estado']})")

        ocupadas = {t: contar_mysql(destino, t) for t in db_sesiones.TABLAS}
        if any(ocupadas.values()) and not args.reemplazar:
            print("\nMySQL ya tiene datos de sesiones:",
                  ", ".join(f"{t}={n}" for t, n in ocupadas.items() if n))
            sys.exit("Usa --reemplazar para vaciarlas y volver a migrar.")

        if args.simular:
            print("\n(--simular) No se escribió nada.")
            return

        destino.ejecutar_crudo("SET FOREIGN_KEY_CHECKS=0")
        if args.reemplazar:
            for tabla in reversed(db_sesiones.TABLAS):
                destino.ejecutar_crudo(f"DELETE FROM `{tabla}`")

        for tabla in db_sesiones.TABLAS:
            cols_origen, filas = plan[tabla]
            if not filas:
                continue
            # Solo columnas que existen en ambos lados (las opcionales que el
            # SQLite no tiene toman su default en MySQL).
            cols = [c for c in cols_origen if c in set(columnas_mysql(destino, tabla))]
            faltan = set(cols_origen) - set(cols)
            if faltan:
                sys.exit(f"{tabla}: columnas sin equivalente en MySQL: {sorted(faltan)}")
            sql = (f"INSERT INTO `{tabla}` ({', '.join(f'`{c}`' for c in cols)}) "
                   f"VALUES ({', '.join('?' * len(cols))})")
            for i in range(0, len(filas), LOTE):
                destino.executemany(sql, [[f[c] for c in cols] for f in filas[i:i + LOTE]])
        destino.ejecutar_crudo("SET FOREIGN_KEY_CHECKS=1")

        # Verificación antes de confirmar
        errores = []
        print("\nVerificación (SQLite a migrar -> MySQL):")
        for tabla in db_sesiones.TABLAS:
            esperado, real = len(plan[tabla][1]), contar_mysql(destino, tabla)
            marca = "ok" if esperado == real else "DIFERENTE"
            print(f"   {tabla:<16} {esperado:>6} -> {real:>6}  {marca}")
            if esperado != real:
                errores.append(tabla)
        if errores:
            destino.rollback()
            sys.exit(f"Los conteos no coinciden en {errores}; se deshizo todo.")
        destino.commit()
    except BaseException:
        destino.rollback()
        raise
    finally:
        destino.close()

    print("\nMigración completa. Para usarla, pon SESIONES_DB=mysql en el .env y "
          "reinicia los servicios (docker compose up -d).")


if __name__ == "__main__":
    main()
