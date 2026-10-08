"""Módulo estenográfico colaborativo (antes /esteno en revisar.py): la
sesión se reparte en bloques de tiempo entre varios correctores, cada uno
toma un bloque, lo corrige escuchando su tramo y lo marca como terminado.

Quién es el corrector lo dice el token (usuario["email"]), nunca el cliente.
"""
import json
import mimetypes
import os
import re
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import revisar

from .. import lanzador, revision
from ..revision import db
from ..security import usuario_actual, usuario_actual_o_query, usuario_admin_actual

router = APIRouter(prefix="/esteno", tags=["esteno"])


class Configuracion(BaseModel):
    num_correctores: int = Field(1, ge=1, le=50)
    bloque_min: float = Field(10, gt=0, le=240)
    nombres: list[str] = []


class Reasignacion(BaseModel):
    slot: int = Field(ge=0)


class SegmentoEditado(BaseModel):
    id: int
    orador: str = ""
    texto: str = ""


class Guardado(BaseModel):
    segmentos: list[SegmentoEditado] = []
    terminar: bool = False


class RutaAudio(BaseModel):
    ruta: str = ""


def _ahora():
    return datetime.now().isoformat(timespec="seconds")


def _bloqueo_fresco(bloque):
    """True si quien tomó el bloque dio señales de vida hace poco."""
    if not bloque["tomado_en"]:
        return True
    try:
        t = datetime.fromisoformat(bloque["tomado_en"]).timestamp()
    except ValueError:
        return False
    return (time.time() - t) <= revisar.ESTENO_LOCK_STALE


def _bloque_o_404(con, sid, indice):
    b = con.execute("SELECT * FROM esteno_bloques WHERE sesion_id=? AND indice=?",
                    (sid, indice)).fetchone()
    if not b:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bloque inexistente")
    return b


def _margen(cfg):
    dur_bloque = cfg["bloque_seg"] if cfg else 600
    return max(30, dur_bloque // 2)


def _segmentos(con, sid, b):
    return [dict(r) for r in revisar._esteno_segmentos(
        con, sid, b["inicio_seg"], b["fin_seg"])]


def _bloque_out(b):
    return {"indice": b["indice"], "inicio_seg": b["inicio_seg"],
            "fin_seg": b["fin_seg"], "slot": b["slot"]}


@router.get("/{sid}/estado")
def estado(sid: int, _usuario=Depends(usuario_actual)):
    with db() as con:
        revisar._esteno_extender(con, sid)      # refleja el crecimiento en vivo
        cfg = revisar._esteno_config(con, sid)
        total = revisar._esteno_dur_sesion(con, sid)
        en_vivo = revision.sesion_en_vivo(sid)
        margen = _margen(cfg)
        bloques = []
        for b in con.execute("SELECT * FROM esteno_bloques WHERE sesion_id=? "
                             "ORDER BY indice", (sid,)):
            cnt = con.execute("SELECT COUNT(*) c FROM participaciones WHERE "
                              "sesion_id=? AND inicio_seg>=? AND inicio_seg<?",
                              (sid, b["inicio_seg"], b["fin_seg"])).fetchone()["c"]
            if b["estado"] == "terminado" or not en_vivo:
                listo = True
            else:
                # En vivo: listo solo si la transcripción ya va 'margen'
                # segundos más allá del fin del bloque.
                listo = float(b["fin_seg"]) <= total - margen
            bloques.append({
                **_bloque_out(b), "estado": b["estado"],
                "tomado_por": b["tomado_por"], "terminado_por": b["terminado_por"],
                "vencido": b["estado"] == "editando" and not _bloqueo_fresco(b),
                "segmentos": cnt, "listo": listo})
        ruta_audio = revisar._esteno_ruta_audio(con, sid)
        ses = con.execute("SELECT id, titulo, inicio, url FROM sesiones WHERE id=?",
                          (sid,)).fetchone()
    conf = None
    if cfg:
        conf = {"num_correctores": cfg["num_correctores"],
                "bloque_seg": cfg["bloque_seg"],
                "nombres": json.loads(cfg["nombres"]) if cfg["nombres"] else []}
    terminados = sum(1 for b in bloques if b["estado"] == "terminado")
    return {"sesion": dict(ses) if ses else None, "config": conf,
            "total_seg": total, "bloques": bloques, "en_vivo": en_vivo,
            "progreso": {"terminados": terminados, "total": len(bloques)},
            "audio": bool(ruta_audio)}


@router.get("/{sid}/nombres")
def nombres(sid: int, _usuario=Depends(usuario_actual)):
    """Nombres para el selector del editor: catálogo de diputados, mesa
    directiva e integrantes de comisiones (contextos.json) y los oradores
    ya usados en la sesión."""
    encontrados = {n.strip() for n in revisar.cargar_catalogo() if n.strip()}

    def agregar(v):
        if isinstance(v, list):
            for x in v:
                agregar(x)
        elif isinstance(v, dict):
            for k in ("nombre", "name", "diputado", "integrante"):
                if isinstance(v.get(k), str) and v[k].strip():
                    encontrados.add(v[k].strip())
                    return
            for val in v.values():
                agregar(val)
        elif isinstance(v, str) and v.strip():
            encontrados.add(v.strip())

    try:
        if os.path.isfile(lanzador.RUTA_CONTEXTOS):
            with open(lanzador.RUTA_CONTEXTOS, encoding="utf-8") as f:
                cfg = json.load(f)
            agregar(cfg.get("mesa_directiva"))
            for com in (cfg.get("comisiones") or {}).values():
                agregar(com)
    except Exception:
        pass
    with db() as con:
        for r in con.execute("SELECT DISTINCT orador FROM participaciones WHERE "
                             "sesion_id=? AND orador IS NOT NULL AND "
                             "TRIM(orador)<>''", (sid,)):
            encontrados.add((r["orador"] or "").strip())
    for basura in ("", "DESCONOCIDO", "Desconocido", "desconocido"):
        encontrados.discard(basura)
    return {"nombres": sorted(encontrados, key=lambda s: s.lower())}


@router.get("/{sid}/bloques/{indice}")
def segmentos(sid: int, indice: int, _usuario=Depends(usuario_actual)):
    with db() as con:
        b = _bloque_o_404(con, sid, indice)
        return {"segmentos": _segmentos(con, sid, b), "bloque": _bloque_out(b)}


@router.get("/{sid}/audio")
def audio(sid: int, range_: Optional[str] = Header(None, alias="Range"),
          _usuario=Depends(usuario_actual_o_query)):
    """Audio de la sesión con soporte de rangos (para saltar a un minuto
    sin descargar todo). Acepta ?token= porque <audio> no manda headers."""
    with db() as con:
        ruta = revisar._esteno_ruta_audio(con, sid)
    if not ruta or not os.path.isfile(ruta):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No hay audio para esta sesión.")
    tam = os.path.getsize(ruta)
    tipo = mimetypes.guess_type(ruta)[0] or "application/octet-stream"
    inicio, fin, parcial = 0, tam - 1, False
    m = re.match(r"bytes=(\d*)-(\d*)", (range_ or "").strip())
    if m:
        g1, g2 = m.group(1), m.group(2)
        if g1 == "" and g2 != "":
            inicio, fin = max(0, tam - int(g2)), tam - 1
        else:
            inicio = int(g1) if g1 else 0
            fin = int(g2) if g2 else tam - 1
        inicio, fin = max(0, inicio), min(fin, tam - 1)
        if inicio > fin:
            inicio, fin = 0, tam - 1
        parcial = True
    longitud = fin - inicio + 1

    def trozos():
        with open(ruta, "rb") as f:
            f.seek(inicio)
            restante = longitud
            while restante > 0:
                trozo = f.read(min(65536, restante))
                if not trozo:
                    break
                restante -= len(trozo)
                yield trozo

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(longitud),
               "Cache-Control": "no-store"}
    if parcial:
        headers["Content-Range"] = f"bytes {inicio}-{fin}/{tam}"
    return StreamingResponse(trozos(), status_code=206 if parcial else 200,
                             media_type=tipo, headers=headers)


@router.post("/{sid}/configurar")
def configurar(sid: int, datos: Configuracion, _admin=Depends(usuario_admin_actual)):
    """Crea o actualiza el plan de bloques. Cambiar la duración reinicia
    los estados (los bloques se recalculan desde cero)."""
    dur = max(30, int(datos.bloque_min * 60))
    nombres_json = json.dumps([n.strip() for n in datos.nombres], ensure_ascii=False)
    with db() as con:
        old = revisar._esteno_config(con, sid)
        reset = old is None or old["bloque_seg"] != dur
        if old:
            con.execute("UPDATE esteno_config SET num_correctores=?, bloque_seg=?, "
                        "nombres=?, actualizado=? WHERE sesion_id=?",
                        (datos.num_correctores, dur, nombres_json, _ahora(), sid))
        else:
            con.execute("INSERT INTO esteno_config (sesion_id, num_correctores, "
                        "bloque_seg, nombres, creado, actualizado) "
                        "VALUES (?,?,?,?,?,?)",
                        (sid, datos.num_correctores, dur, nombres_json, _ahora(), _ahora()))
        n = revisar._esteno_generar(con, sid, datos.num_correctores, dur, reset=reset)
        con.commit()
    return {"ok": True, "bloques": n, "reinicio": reset}


@router.post("/{sid}/bloques/{indice}/reasignar")
def reasignar(sid: int, indice: int, datos: Reasignacion,
              _admin=Depends(usuario_admin_actual)):
    with db() as con:
        _bloque_o_404(con, sid, indice)
        con.execute("UPDATE esteno_bloques SET slot=? WHERE sesion_id=? AND indice=?",
                    (datos.slot, sid, indice))
        con.commit()
    return {"ok": True}


@router.post("/{sid}/bloques/{indice}/tomar")
def tomar(sid: int, indice: int, usuario=Depends(usuario_actual)):
    """Abre un bloque para editarlo (o lo refresca si ya es tuyo)."""
    corrector = usuario["email"]
    with db() as con:
        b = _bloque_o_404(con, sid, indice)
        if b["estado"] != "terminado" and revision.sesion_en_vivo(sid):
            total = revisar._esteno_dur_sesion(con, sid)
            if float(b["fin_seg"]) > total - _margen(revisar._esteno_config(con, sid)):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Este bloque aún está siendo grabado. Espera a que la "
                    f"transcripción pase del minuto {int(b['fin_seg'] // 60)} "
                    "para abrirlo.")
        if (b["estado"] == "editando" and b["tomado_por"]
                and b["tomado_por"] != corrector and _bloqueo_fresco(b)):
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"Este bloque lo está editando {b['tomado_por']} "
                                "en este momento.")
        con.execute("UPDATE esteno_bloques SET estado='editando', tomado_por=?, "
                    "tomado_en=? WHERE sesion_id=? AND indice=?",
                    (corrector, _ahora(), sid, indice))
        con.commit()
        return {"ok": True, "segmentos": _segmentos(con, sid, b),
                "bloque": _bloque_out(b)}


@router.post("/{sid}/bloques/{indice}/latido")
def latido(sid: int, indice: int, usuario=Depends(usuario_actual)):
    """Mantiene el bloqueo mientras el corrector sigue editando."""
    with db() as con:
        con.execute("UPDATE esteno_bloques SET tomado_en=? WHERE sesion_id=? AND "
                    "indice=? AND tomado_por=? AND estado='editando'",
                    (_ahora(), sid, indice, usuario["email"]))
        con.commit()
    return {"ok": True}


@router.post("/{sid}/bloques/{indice}/soltar")
def soltar(sid: int, indice: int, usuario=Depends(usuario_actual)):
    with db() as con:
        con.execute("UPDATE esteno_bloques SET estado='pendiente', tomado_por=NULL, "
                    "tomado_en=NULL WHERE sesion_id=? AND indice=? AND tomado_por=? "
                    "AND estado='editando'", (sid, indice, usuario["email"]))
        con.commit()
    return {"ok": True}


@router.post("/{sid}/bloques/{indice}/guardar")
def guardar(sid: int, indice: int, datos: Guardado, usuario=Depends(usuario_actual)):
    """Guarda orador y texto de los segmentos del bloque; con terminar=True
    lo marca como terminado y lo suelta."""
    corrector = usuario["email"]
    with db() as con:
        b = _bloque_o_404(con, sid, indice)
        if (b["estado"] == "editando" and b["tomado_por"]
                and b["tomado_por"] != corrector and _bloqueo_fresco(b)):
            raise HTTPException(status.HTTP_409_CONFLICT,
                                f"Otro corrector ({b['tomado_por']}) tomó el bloque "
                                "mientras editabas. Recarga para no pisar su trabajo.")
        # Solo se aceptan segmentos que caen dentro del bloque.
        ids_validos = {s["id"] for s in _segmentos(con, sid, b)}
        n = 0
        for s in datos.segmentos:
            if s.id not in ids_validos:
                continue
            con.execute("UPDATE participaciones SET orador=?, texto=? "
                        "WHERE id=? AND sesion_id=?",
                        (s.orador.strip(), s.texto, s.id, sid))
            n += 1
        if datos.terminar:
            con.execute("UPDATE esteno_bloques SET estado='terminado', tomado_por=NULL, "
                        "tomado_en=NULL, terminado_por=?, terminado_en=? "
                        "WHERE sesion_id=? AND indice=?",
                        (corrector, _ahora(), sid, indice))
        else:
            con.execute("UPDATE esteno_bloques SET estado='editando', tomado_por=?, "
                        "tomado_en=? WHERE sesion_id=? AND indice=?",
                        (corrector, _ahora(), sid, indice))
        con.commit()
    return {"ok": True, "guardados": n, "terminado": datos.terminar}


@router.post("/{sid}/audio-ruta")
def vincular_audio(sid: int, datos: RutaAudio, _admin=Depends(usuario_admin_actual)):
    ruta = datos.ruta.strip()
    with db() as con:
        con.execute("INSERT OR REPLACE INTO esteno_audio (sesion_id, ruta) VALUES (?, ?)",
                    (sid, ruta))
        con.commit()
    return {"ok": True, "existe": bool(ruta) and os.path.isfile(ruta)}
