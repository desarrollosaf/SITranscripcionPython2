import secrets

from fastapi import APIRouter, Depends, HTTPException, status

from ..db_mysql import conexion
from ..schemas import (UsuarioConPassword, UsuarioCrear, UsuarioEditar,
                       UsuarioGenerar, UsuarioOut)
from ..security import hash_password, obtener_usuario_por_email, usuario_admin_actual

router = APIRouter(prefix="/usuarios", tags=["usuarios"])


@router.post("", response_model=UsuarioOut, status_code=status.HTTP_201_CREATED)
def crear_usuario(datos: UsuarioCrear, _admin=Depends(usuario_admin_actual)):
    if obtener_usuario_por_email(datos.email):
        raise HTTPException(status.HTTP_409_CONFLICT, "Ese email ya está registrado")
    with conexion() as con, con.cursor() as cur:
        cur.execute(
            "INSERT INTO usuarios (email, password_hash, es_admin) VALUES (%s,%s,%s)",
            (datos.email, hash_password(datos.password), datos.es_admin))
        cur.execute("SELECT * FROM usuarios WHERE id = %s", (cur.lastrowid,))
        return cur.fetchone()


@router.post("/generar", response_model=UsuarioConPassword,
             status_code=status.HTTP_201_CREATED)
def crear_usuario_con_password(datos: UsuarioGenerar,
                               _admin=Depends(usuario_admin_actual)):
    """Crea una cuenta (agente de captura o corrector) con contraseña
    generada aquí. Se devuelve en claro UNA sola vez: la base solo guarda
    el hash."""
    if obtener_usuario_por_email(datos.email):
        raise HTTPException(status.HTTP_409_CONFLICT, "Ese email ya está registrado")
    password = _password_aleatoria()
    with conexion() as con, con.cursor() as cur:
        cur.execute(
            "INSERT INTO usuarios (email, password_hash, es_admin, nombre) "
            "VALUES (%s,%s,%s,%s)",
            (datos.email, hash_password(password), datos.es_admin,
             _limpiar_nombre(datos.nombre)))
        cur.execute("SELECT * FROM usuarios WHERE id = %s", (cur.lastrowid,))
        fila = cur.fetchone()
    return {**fila, "password": password}


@router.get("", response_model=list[UsuarioOut])
def listar_usuarios(_admin=Depends(usuario_admin_actual)):
    with conexion() as con, con.cursor() as cur:
        cur.execute("SELECT * FROM usuarios ORDER BY id")
        return cur.fetchall()


@router.put("/{usuario_id}", response_model=UsuarioOut)
def editar_usuario(usuario_id: int, datos: UsuarioEditar,
                   admin=Depends(usuario_admin_actual)):
    """Cambia el nombre y el rol. Un admin no puede quitarse a sí mismo el
    rol (se quedaría sin poder administrar)."""
    _usuario_o_404(usuario_id)
    if usuario_id == admin["id"] and not datos.es_admin:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "No puedes quitarte a ti mismo el rol de administrador")
    with conexion() as con, con.cursor() as cur:
        cur.execute("UPDATE usuarios SET nombre=%s, es_admin=%s WHERE id=%s",
                    (_limpiar_nombre(datos.nombre), datos.es_admin, usuario_id))
        cur.execute("SELECT * FROM usuarios WHERE id = %s", (usuario_id,))
        return cur.fetchone()


@router.post("/{usuario_id}/restablecer", response_model=UsuarioConPassword)
def restablecer_password(usuario_id: int, _admin=Depends(usuario_admin_actual)):
    """Genera una contraseña nueva (se muestra una sola vez). La anterior
    deja de funcionar."""
    fila = _usuario_o_404(usuario_id)
    password = _password_aleatoria()
    with conexion() as con, con.cursor() as cur:
        cur.execute("UPDATE usuarios SET password_hash=%s WHERE id=%s",
                    (hash_password(password), usuario_id))
    return {**fila, "password": password}


def _usuario_o_404(usuario_id):
    with conexion() as con, con.cursor() as cur:
        cur.execute("SELECT * FROM usuarios WHERE id = %s", (usuario_id,))
        fila = cur.fetchone()
    if not fila:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Usuario no encontrado")
    return fila


def _password_aleatoria(longitud=10):
    """Sin 0/O ni 1/l/I, para poder dictarla sin confusiones."""
    alfabeto = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alfabeto) for _ in range(longitud))


def _limpiar_nombre(nombre):
    nombre = " ".join((nombre or "").split())
    return nombre or None
