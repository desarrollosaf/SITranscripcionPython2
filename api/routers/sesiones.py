"""Revisión de sesiones (antes la pantalla principal de revisar.py):
oradores, textos, división de turnos, resúmenes, IA y exportación a Word."""
import json
import os
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

import revisar

from .. import revision
from ..revision import db
from ..security import usuario_actual, usuario_actual_o_query, usuario_admin_actual

router = APIRouter(tags=["sesiones"])


class CambioOrador(BaseModel):
    ids: list[int] = Field(min_length=1)
    orador: str = Field(min_length=1)


class Division(BaseModel):
    ids: list[int] = Field(min_length=1)
    corte: int = 0
    orador1: str = Field(min_length=1)
    orador2: str = Field(min_length=1)


class Renombre(BaseModel):
    de: str = Field(min_length=1)
    a: str = Field(min_length=1)


class CambioTexto(BaseModel):
    id: int
    texto: str = ""


class CambiosTexto(BaseModel):
    cambios: list[CambioTexto]


class PedidoResumen(BaseModel):
    orador: str = ""
    texto: str = Field(min_length=1)
    ancla_id: int = 0


class ResumenEditado(BaseModel):
    orador: str = ""
    resumen: str = ""


def _ahora():
    return datetime.now().isoformat(timespec="seconds")


def _limpiar_ia(con):
    """Una corrección manual manda sobre el veredicto del corrector de IA."""
    return (", revisado_ia=NULL, motivo_ia=NULL"
            if "revisado_ia" in revision.columnas_participaciones(con) else "")


def _sesion_o_404(con, sid):
    ses = con.execute("SELECT * FROM sesiones WHERE id=?", (sid,)).fetchone()
    if not ses:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sesión no encontrada")
    return ses


def _exigir_clave_ia():
    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "No hay clave API configurada (variable ANTHROPIC_API_KEY).")


@router.get("/catalogo", response_model=list[str])
def catalogo(_usuario=Depends(usuario_actual)):
    """Diputados de diputados.txt, sugerencias al corregir oradores."""
    return revisar.cargar_catalogo()


@router.get("/sesiones")
def listar_sesiones(_usuario=Depends(usuario_actual)):
    """Todas las sesiones con su número de segmentos. Cualquier usuario la
    ve (el corrector la usa para elegir sesión en el módulo estenográfico)."""
    with db() as con:
        return [dict(r) for r in con.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM participaciones p "
            " WHERE p.sesion_id = s.id) AS segmentos, "
            "(SELECT MAX(fin_seg) FROM participaciones p "
            " WHERE p.sesion_id = s.id) AS duracion_seg "
            "FROM sesiones s ORDER BY s.id DESC")]


@router.get("/sesiones/{sid}/participaciones")
def participaciones(sid: int, _admin=Depends(usuario_admin_actual)):
    with db() as con:
        ses = _sesion_o_404(con, sid)
        opcionales = [c for c in ("voz_orador", "voz_similitud", "revisado_ia",
                                  "motivo_ia")
                      if c in revision.columnas_participaciones(con)]
        extra = ("," + ",".join(opcionales)) if opcionales else ""
        filas = [dict(r) for r in con.execute(
            f"SELECT id, orador, inicio_seg, fin_seg, texto{extra} "
            "FROM participaciones WHERE sesion_id=? ORDER BY inicio_seg, id",
            (sid,))]
        resumenes = {r["ancla_id"]: r["resumen"] for r in con.execute(
            "SELECT ancla_id, resumen FROM resumenes WHERE sesion_id=?", (sid,))}
        estructura = None
        fila_est = con.execute("SELECT datos FROM estructura WHERE sesion_id=?",
                               (sid,)).fetchone()
        if fila_est and fila_est["datos"]:
            try:
                estructura = json.loads(fila_est["datos"])
            except (ValueError, TypeError):
                estructura = None
    return {"sesion": dict(ses), "filas": filas, "resumenes": resumenes,
            "estructura": estructura}


@router.post("/sesiones/{sid}/orador")
def cambiar_orador(sid: int, datos: CambioOrador, _admin=Depends(usuario_admin_actual)):
    """Asigna un orador a unas filas y une con sus vecinas del mismo orador."""
    orador = datos.orador.strip()
    with db() as con:
        marcas = ",".join("?" * len(datos.ids))
        cur = con.execute(
            f"UPDATE participaciones SET orador=?{_limpiar_ia(con)} "
            f"WHERE sesion_id=? AND id IN ({marcas})", [orador, sid] + datos.ids)
        con.commit()
        unidos = revisar.compactar(con, sid, solo_ids=datos.ids)
    return {"ok": True, "cambios": cur.rowcount, "unidos": unidos}


@router.post("/sesiones/{sid}/renombrar")
def renombrar(sid: int, datos: Renombre, _admin=Depends(usuario_admin_actual)):
    """Cambia un orador por otro en toda la sesión."""
    with db() as con:
        cur = con.execute("UPDATE participaciones SET orador=? "
                          "WHERE sesion_id=? AND orador=?",
                          (datos.a.strip(), sid, datos.de.strip()))
        con.commit()
        unidos = revisar.compactar(con, sid)
    return {"ok": True, "cambios": cur.rowcount, "unidos": unidos}


@router.post("/sesiones/{sid}/compactar")
def compactar(sid: int, _admin=Depends(usuario_admin_actual)):
    with db() as con:
        return {"ok": True, "unidos": revisar.compactar(con, sid)}


@router.post("/sesiones/{sid}/textos")
def guardar_textos(sid: int, datos: CambiosTexto, _admin=Depends(usuario_admin_actual)):
    n = 0
    with db() as con:
        for c in datos.cambios:
            n += con.execute("UPDATE participaciones SET texto=? "
                             "WHERE id=? AND sesion_id=?",
                             (c.texto, c.id, sid)).rowcount
        con.commit()
    return {"ok": True, "cambios": n}


@router.post("/sesiones/{sid}/dividir")
def dividir(sid: int, datos: Division, usuario=Depends(usuario_actual)):
    """Separa dos oradores dentro de un bloque. 'corte' es el desplazamiento
    en caracteres sobre el texto del bloque unido con espacios. Un corrector
    (no admin) solo puede dividir dentro del bloque estenográfico que tiene
    abierto."""
    ids = datos.ids
    orador1, orador2 = datos.orador1.strip(), datos.orador2.strip()
    corte = datos.corte
    marcas = ",".join("?" * len(ids))
    with db() as con:
        if not revision.es_admin(usuario):
            filas_chk = con.execute(
                f"SELECT inicio_seg FROM participaciones WHERE id IN ({marcas}) "
                "AND sesion_id=?", ids + [sid]).fetchall()
            bloques = con.execute(
                "SELECT inicio_seg, fin_seg FROM esteno_bloques WHERE sesion_id=? "
                "AND tomado_por=? AND estado='editando'",
                (sid, usuario["email"])).fetchall()
            propios = len(filas_chk) == len(ids) and all(
                any(b["inicio_seg"] <= f["inicio_seg"] < b["fin_seg"] for b in bloques)
                for f in filas_chk)
            if not propios:
                raise HTTPException(status.HTTP_403_FORBIDDEN,
                                    "Solo puedes dividir segmentos del bloque "
                                    "que tienes abierto")
        cols = revision.columnas_participaciones(con)
        limpiar = _limpiar_ia(con)
        filas = {r["id"]: r for r in con.execute(
            f"SELECT * FROM participaciones WHERE sesion_id=? AND id IN ({marcas})",
            [sid] + ids)}
        orden = [filas[i] for i in ids if i in filas]
        if not orden:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Filas no encontradas")

        # En qué fila (y en qué punto de ella) cae el corte.
        pos, fila_k, off_local = 0, None, 0
        for k, f in enumerate(orden):
            txt = f["texto"] or ""
            if corte <= pos:
                fila_k, off_local = k, 0
                break
            if corte < pos + len(txt):
                fila_k, off_local = k, corte - pos
                break
            pos += len(txt) + 1          # +1 por el espacio de unión
        if fila_k is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "El corte deja la segunda parte vacía")

        antes = [orden[j]["id"] for j in range(fila_k)]
        if antes:
            con.execute(f"UPDATE participaciones SET orador=?{limpiar} "
                        f"WHERE id IN ({','.join('?' * len(antes))})",
                        [orador1] + antes)
        nuevo_id = None
        if off_local > 0:
            # El corte cae DENTRO de una fila: se parte en dos.
            f = orden[fila_k]
            txt = f["texto"] or ""
            t1, t2 = txt[:off_local].strip(), txt[off_local:].strip()
            ini_f = f["inicio_seg"] or 0
            fin_f = f["fin_seg"] or ini_f
            corte_seg = ini_f + (fin_f - ini_f) * (off_local / len(txt))
            con.execute(f"UPDATE participaciones SET orador=?, texto=?, fin_seg=?, "
                        f"fin_hms=?{limpiar} WHERE id=?",
                        (orador1, t1, corte_seg, revisar.hms(corte_seg), f["id"]))
            campos = "sesion_id, orador, inicio_seg, fin_seg, inicio_hms, fin_hms, texto"
            valores = [sid, orador2, corte_seg, fin_f, revisar.hms(corte_seg),
                       revisar.hms(fin_f), t2]
            if "fuente" in cols:
                campos += ", fuente"
                valores.append("manual")
            nuevo_id = con.execute(
                f"INSERT INTO participaciones ({campos}) VALUES "
                f"({','.join('?' * len(valores))})", valores).lastrowid
            despues = [orden[j]["id"] for j in range(fila_k + 1, len(orden))]
        else:
            despues = [orden[j]["id"] for j in range(fila_k, len(orden))]
        if despues:
            con.execute(f"UPDATE participaciones SET orador=?{limpiar} "
                        f"WHERE id IN ({','.join('?' * len(despues))})",
                        [orador2] + despues)
        con.commit()
    return {"ok": True, "nuevo_id": nuevo_id}


@router.post("/sesiones/{sid}/corregir-estilo")
def corregir_estilo(sid: int, _admin=Depends(usuario_admin_actual)):
    """Pasa todos los textos por la IA (ortografía, puntuación, párrafos).
    Guarda el original en texto_original la primera vez."""
    _exigir_clave_ia()
    with db() as con:
        revisar.compactar(con, sid)
        if "texto_original" not in revision.columnas_participaciones(con):
            con.execute("ALTER TABLE participaciones ADD COLUMN texto_original TEXT")
            con.commit()
        catalogo = revisar.cargar_catalogo()
        filas = con.execute("SELECT id, texto, texto_original FROM participaciones "
                            "WHERE sesion_id=? AND texto IS NOT NULL", (sid,)).fetchall()
        modificados, errores = 0, []
        for f in filas:
            texto_actual = (f["texto"] or "").strip()
            if len(texto_actual) < 10:
                continue
            res = revisar.corregir_texto_ia(texto_actual, catalogo)
            if "texto_corregido" in res:
                if not (f["texto_original"] or "").strip():
                    con.execute("UPDATE participaciones SET texto_original=? WHERE id=?",
                                (texto_actual, f["id"]))
                con.execute("UPDATE participaciones SET texto=? WHERE id=?",
                            (res["texto_corregido"], f["id"]))
                modificados += 1
            elif res.get("error"):
                errores.append(res["error"])
        con.commit()
    resp = {"ok": True, "cambios": modificados}
    if errores:
        resp["aviso"] = (f"{len(errores)} segmento(s) no se pudieron corregir. "
                         f"Ej.: {errores[0]}")
    return resp


@router.post("/sesiones/{sid}/estructurar")
def estructurar(sid: int, _admin=Depends(usuario_admin_actual)):
    """La IA detecta y numera los puntos del orden del día."""
    _exigir_clave_ia()
    with db() as con:
        res = revisar.estructurar_ia(revisar.turnos_de_sesion(con, sid))
        if res.get("error"):
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, res["error"])
        con.execute("INSERT INTO estructura (sesion_id, datos, creado) VALUES (?,?,?) "
                    "ON CONFLICT(sesion_id) DO UPDATE SET datos=excluded.datos, "
                    "creado=excluded.creado",
                    (sid, json.dumps(res, ensure_ascii=False), _ahora()))
        con.commit()
    return {"ok": True, "secciones": len(res["secciones"]), "estructura": res}


@router.delete("/sesiones/{sid}/estructura")
def borrar_estructura(sid: int, _admin=Depends(usuario_admin_actual)):
    with db() as con:
        con.execute("DELETE FROM estructura WHERE sesion_id=?", (sid,))
        con.commit()
    return {"ok": True}


def _guardar_resumen(con, sid, ancla, orador, resumen):
    con.execute("INSERT INTO resumenes (sesion_id, ancla_id, orador, resumen, creado) "
                "VALUES (?,?,?,?,?) ON CONFLICT(ancla_id) DO UPDATE SET "
                "resumen=excluded.resumen, orador=excluded.orador, "
                "creado=excluded.creado", (sid, ancla, orador, resumen, _ahora()))
    con.commit()


@router.post("/sesiones/{sid}/resumen")
def pedir_resumen(sid: int, datos: PedidoResumen, _admin=Depends(usuario_admin_actual)):
    """Con clave API genera y guarda el resumen; sin ella devuelve el prompt
    para copiarlo a Claude ({"modo": "copiar", "prompt": ...})."""
    res = revisar.generar_resumen(datos.orador.strip(), datos.texto.strip())
    if res.get("modo") == "auto" and res.get("resumen") and datos.ancla_id:
        with db() as con:
            _guardar_resumen(con, sid, datos.ancla_id, datos.orador.strip(),
                             res["resumen"])
    return res


@router.put("/sesiones/{sid}/resumenes/{ancla_id}")
def editar_resumen(sid: int, ancla_id: int, datos: ResumenEditado,
                   _admin=Depends(usuario_admin_actual)):
    with db() as con:
        if datos.resumen.strip():
            _guardar_resumen(con, sid, ancla_id, datos.orador.strip(),
                             datos.resumen.strip())
        else:
            con.execute("DELETE FROM resumenes WHERE ancla_id=?", (ancla_id,))
            con.commit()
    return {"ok": True}


@router.delete("/sesiones/{sid}/resumenes/{ancla_id}")
def borrar_resumen(sid: int, ancla_id: int, _admin=Depends(usuario_admin_actual)):
    with db() as con:
        con.execute("DELETE FROM resumenes WHERE sesion_id=? AND ancla_id=?",
                    (sid, ancla_id))
        con.commit()
    return {"ok": True}


@router.get("/sesiones/{sid}/word")
def exportar_word(sid: int, corrector: Optional[str] = None,
                  marcar_autores: bool = False,
                  usuario=Depends(usuario_actual_o_query)):
    if not revision.es_admin(usuario):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Requiere permisos de administrador")
    with db() as con:
        _sesion_o_404(con, sid)
        datos, nombre = revisar.generar_word(con, sid, (corrector or "").strip(),
                                             marcar_autores)
    return Response(
        content=datos,
        media_type="application/vnd.openxmlformats-officedocument."
                   "wordprocessingml.document",
        headers={"Content-Disposition":
                 f"attachment; filename*=UTF-8''{quote(nombre)}"})
