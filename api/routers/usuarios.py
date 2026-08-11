from fastapi import APIRouter, Depends, HTTPException, status

from ..db_mysql import conexion
from ..schemas import UsuarioCrear, UsuarioOut
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


@router.get("", response_model=list[UsuarioOut])
def listar_usuarios(_admin=Depends(usuario_admin_actual)):
    with conexion() as con, con.cursor() as cur:
        cur.execute("SELECT * FROM usuarios ORDER BY id")
        return cur.fetchall()
