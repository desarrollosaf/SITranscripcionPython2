"""Nueva transcripción desde la interfaz: URL manual (lanzador local) o la
lista de eventos del sistema de registro parlamentario."""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from .. import lanzador, parlamentario
from ..security import usuario_admin_actual

router = APIRouter(prefix="/lanzador", tags=["lanzador"])


class TranscripcionManual(BaseModel):
    url: str
    tipo: str = "pleno"           # "pleno" | "comision"
    comisiones: list[str] = []
    fecha: str = ""
    modelo: str = "small"


@router.get("/contextos")
def contextos(_admin=Depends(usuario_admin_actual)):
    return lanzador.contextos()


@router.get("/eventos")
def eventos(tipo: int = 1, _admin=Depends(usuario_admin_actual)):
    """Últimos eventos del Congreso (1 = Sesión/Diputación permanente,
    0 = Comisión), para elegir uno en vez de escribir URL y participantes."""
    try:
        return parlamentario.resumen_eventos(tipo)
    except Exception as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"No pude consultar el Congreso: {e}")


@router.post("/transcribir")
def transcribir(datos: TranscripcionManual, _admin=Depends(usuario_admin_actual)):
    try:
        return lanzador.lanzar(datos.url, datos.tipo, datos.comisiones,
                               datos.fecha, datos.modelo)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))


@router.post("/detener")
def detener(_admin=Depends(usuario_admin_actual)):
    return lanzador.detener()


@router.get("/estado")
def estado(_admin=Depends(usuario_admin_actual)):
    """Estado de la transcripción manual y la cola de su log."""
    return lanzador.estado()
