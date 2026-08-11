from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm

from ..schemas import Token, UsuarioOut
from ..security import (crear_token, obtener_usuario_por_email,
                         usuario_actual, verificar_password)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=Token)
def login(form: OAuth2PasswordRequestForm = Depends()):
    usuario = obtener_usuario_por_email(form.username)
    if not usuario or not verificar_password(form.password, usuario["password_hash"]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Email o contraseña incorrectos")
    return Token(access_token=crear_token(usuario["email"]))


@router.get("/me", response_model=UsuarioOut)
def yo(usuario=Depends(usuario_actual)):
    return usuario
