from fastapi import APIRouter, Depends

from .. import jobs
from ..security import usuario_actual

router = APIRouter(prefix="/participantes", tags=["participantes"])


@router.get("", response_model=list[str])
def listar_participantes(_usuario=Depends(usuario_actual)):
    """Nombres disponibles en voces_perfiles.json, para validar antes de
    crear un trabajo cuáles integrantes ya tienen huella de voz."""
    return sorted(jobs.cargar_catalogo_perfiles().keys())
