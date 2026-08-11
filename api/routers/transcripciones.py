import json

from fastapi import APIRouter, Depends, HTTPException, status

from .. import jobs, parlamentario
from ..schemas import (ParticipacionOut, TrabajoCrear, TrabajoDesdeEvento,
                       TrabajoOut)
from ..security import usuario_actual

router = APIRouter(prefix="/transcripciones", tags=["transcripciones"])


def _a_trabajo_out(fila):
    def _campo(nombre):
        v = fila[nombre]
        return json.loads(v) if isinstance(v, str) else (v or [])

    return TrabajoOut(
        id=fila["id"], url=fila["url"], estado=fila["estado"],
        fuente=fila["fuente"], puerto=fila["puerto"],
        passphrase=fila["passphrase"],
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


@router.post("/desde-evento", response_model=TrabajoOut,
            status_code=status.HTTP_201_CREATED)
def crear_desde_evento(datos: TrabajoDesdeEvento, usuario=Depends(usuario_actual)):
    """Crea un trabajo a partir de un evento real del sistema de registro
    parlamentario: saca participantes y URL (si trae liga de YouTube)
    automáticamente, sin que nadie tenga que escribirlos a mano."""
    try:
        evento = parlamentario.obtener_evento(datos.evento_id, datos.tipo)
    except Exception as e:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"No pude consultar el sistema de registro parlamentario: {e}")
    if not evento:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Ese evento ya no está entre los últimos de ese tipo; refresca "
            "la lista e inténtalo de nuevo")
    participantes = parlamentario.participantes_de_evento(evento)
    if not participantes:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "El evento no trae integrantes")

    trabajo = TrabajoCrear(
        url=evento.get("liga") or (evento.get("descripcion") or "")[:200].strip(),
        participantes=participantes,
        modelo=datos.modelo, bloque=datos.bloque,
        fuente="youtube" if evento.get("liga") else "srt")
    try:
        fila = jobs.crear_trabajo(usuario["id"], trabajo)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e))
    return _a_trabajo_out(fila)


@router.get("", response_model=list[TrabajoOut])
def listar_transcripciones(usuario=Depends(usuario_actual)):
    usuario_id = None if usuario["es_admin"] else usuario["id"]
    return [_a_trabajo_out(f) for f in jobs.listar_trabajos(usuario_id)]


@router.get("/esperando-audio", response_model=list[TrabajoOut])
def transcripciones_esperando_audio(usuario=Depends(usuario_actual)):
    """Cola compartida de trabajos SRT activos: cualquier usuario logueado
    ve TODOS (no solo los suyos) — la app de la consola de audio necesita
    ver qué comisiones están esperando que alguien les mande el audio."""
    return [_a_trabajo_out(f) for f in jobs.listar_trabajos_srt_activos()]


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
