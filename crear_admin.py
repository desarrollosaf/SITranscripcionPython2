#!/usr/bin/env python3
"""Crea (o promueve) un usuario administrador en MySQL.

Uso:
    python crear_admin.py --email admin@ejemplo.com --password ****
"""
import argparse
import getpass

from api.db_mysql import conexion, inicializar_esquema
from api.security import hash_password


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", help="Si se omite, se pide de forma oculta")
    args = ap.parse_args()

    password = args.password or getpass.getpass("Contraseña: ")

    inicializar_esquema()
    with conexion() as con, con.cursor() as cur:
        cur.execute("SELECT id FROM usuarios WHERE email = %s", (args.email,))
        existente = cur.fetchone()
        if existente:
            cur.execute(
                "UPDATE usuarios SET password_hash = %s, es_admin = TRUE "
                "WHERE id = %s", (hash_password(password), existente["id"]))
            print(f"Usuario existente actualizado a admin: {args.email}")
        else:
            cur.execute(
                "INSERT INTO usuarios (email, password_hash, es_admin) "
                "VALUES (%s, %s, TRUE)", (args.email, hash_password(password)))
            print(f"Usuario admin creado: {args.email}")


if __name__ == "__main__":
    main()
