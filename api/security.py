from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, Query, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt

from .config import settings
from .db_mysql import conexion

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login")
oauth2_opcional = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)


def hash_password(password: str) -> str:
    # bcrypt solo mira los primeros 72 bytes de la contraseña.
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("utf-8")


def verificar_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8")[:72], password_hash.encode("utf-8"))


def crear_token(email: str) -> str:
    expira = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expira_minutos)
    payload = {"sub": email, "exp": expira}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algoritmo)


def obtener_usuario_por_email(email: str):
    with conexion() as con, con.cursor() as cur:
        cur.execute("SELECT * FROM usuarios WHERE email = %s", (email,))
        return cur.fetchone()


async def usuario_actual(token: str = Depends(oauth2_scheme)):
    return _usuario_de_token(token)


async def usuario_actual_o_query(token_header: Optional[str] = Depends(oauth2_opcional),
                                 token: Optional[str] = Query(None)):
    """Como usuario_actual, pero también acepta ?token=... en la URL: un
    <audio src> o un enlace de descarga no pueden mandar el encabezado
    Authorization."""
    return _usuario_de_token(token_header or token or "")


def _usuario_de_token(token: str):
    credenciales_invalidas = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Credenciales inválidas",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algoritmo])
        email = payload.get("sub")
        if email is None:
            raise credenciales_invalidas
    except JWTError:
        raise credenciales_invalidas

    usuario = obtener_usuario_por_email(email)
    if usuario is None:
        raise credenciales_invalidas
    return usuario


async def usuario_admin_actual(usuario=Depends(usuario_actual)):
    if not usuario["es_admin"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Requiere permisos de administrador")
    return usuario
