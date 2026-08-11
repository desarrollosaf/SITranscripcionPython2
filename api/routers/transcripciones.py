import json

from fastapi import APIRouter, Depends, HTTPException, status

from .. import jobs
from ..schemas import ParticipacionOut, TrabajoCrear, TrabajoOut
from ..security import usuario_actual

router = APIRouter(prefix="/transcripciones", tags=["transcripciones"])


def _a_trabajo_out(fila):
    def _campo(nombre):
        v = fila[nombre]
        return json.loads(v) if isinstance(v, str) else (v or [])

    return TrabajoOut(
        id=fila["id"], url=fila["url"], estado=fila["estado"],
        participantes_pedidos=_campo("participantes_pedidos"),
        participantes_encontrados=_campo("participantes_encontrados"),
        participantes_no_encontrados=_campo("participantes_no_encontrados"),
        sesion_id=fila["sesion_id"], error=fila["error"],
        creado_en=str(fila["creado_en"]))


def _trabajo_o_404(job_id, usuario):
    fila = jobs.obtener_trabajo(job_id)
    if not fila:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trabajo no encontrado")
    if not usuario["es_admin"] and fila["usuario_id"] != usuario["id"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No es tu trabajo")
    return fila


@router.post("", response_model=TrabajoOut, status_code=status.HTTP_201_CREATED)
def crear_transcripcion(datos: TrabajoCrear, usuario=Depends(usuario_actual)):
    try:
        fila = jobs.crear_trabajo(usuario["id"], datos)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return _a_trabajo_out(fila)


@router.get("", response_model=list[TrabajoOut])
def listar_transcripciones(usuario=Depends(usuario_actual)):
    usuario_id = None if usuario["es_admin"] else usuario["id"]
    return [_a_trabajo_out(f) for f in jobs.listar_trabajos(usuario_id)]


@router.get("/{job_id}", response_model=TrabajoOut)
def ver_transcripcion(job_id: str, usuario=Depends(usuario_actual)):
    return _a_trabajo_out(_trabajo_o_404(job_id, usuario))


@router.post("/{job_id}/detener", response_model=TrabajoOut)
def detener_transcripcion(job_id: str, usuario=Depends(usuario_actual)):
    _trabajo_o_404(job_id, usuario)
    try:
        jobs.detener_trabajo(job_id)
    except ValueError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e))
    return _a_trabajo_out(jobs.obtener_trabajo(job_id))


@router.get("/{job_id}/participaciones", response_model=list[ParticipacionOut])
def ver_participaciones(job_id: str, usuario=Depends(usuario_actual)):
    fila = _trabajo_o_404(job_id, usuario)
    if not fila["sesion_id"]:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Todavía no hay sesión asociada (el proceso está arrancando o falló "
            "antes de crear la sesión); vuelve a intentar en unos segundos")
    return jobs.obtener_participaciones(fila["sesion_id"])
