"""Lanzador de transcripción "URL manual" (antes en revisar.py): arranca
transcribir_en_vivo_c3.py como proceso aparte, uno a la vez, escribiendo en
sesiones.db. Los trabajos "Evento del Congreso" van por jobs.py.

Igual que jobs._procesos, el proceso vive en memoria: requiere un único
worker de uvicorn.
"""
import json
import os
import re
import subprocess
import sys
import threading

from . import jobs
from .config import settings

RUTA_CONTEXTOS = os.environ.get("CONTEXTOS_PATH", "contextos.json")
LOG = os.path.join(os.path.dirname(os.path.abspath(jobs.RUTA_SCRIPT)),
                   "transcripcion_en_curso.log")

_lock = threading.Lock()
_proc = None
_args_soportados = None
_args_leidos = False


def corriendo():
    return bool(_proc and _proc.poll() is None)


def _argumentos_transcriptor():
    """Opciones largas (--x) que acepta el transcriptor, leídas de su
    --help una sola vez. None si no se pudo leer (se pasan todas)."""
    global _args_soportados, _args_leidos
    if _args_leidos:
        return _args_soportados
    soportadas = None
    try:
        out = subprocess.run([sys.executable, jobs.RUTA_SCRIPT, "--help"],
                             capture_output=True, text=True, timeout=30)
        texto = (out.stdout or "") + (out.stderr or "")
        soportadas = set(re.findall(r"--[a-zA-Z][\w-]*", texto)) or None
    except Exception:
        soportadas = None
    _args_soportados, _args_leidos = soportadas, True
    return soportadas


def contextos():
    """Comisiones de contextos.json para el formulario. Sin archivo, el
    lanzador sigue sirviendo para el Pleno."""
    info = {"disponible": False, "comisiones": [],
            "tiene_transcriptor": os.path.isfile(jobs.RUTA_SCRIPT),
            "archivo": RUTA_CONTEXTOS}
    try:
        if os.path.isfile(RUTA_CONTEXTOS):
            with open(RUTA_CONTEXTOS, encoding="utf-8") as f:
                cfg = json.load(f)
            info["disponible"] = True
            info["comisiones"] = sorted((cfg.get("comisiones") or {}).keys())
            info["tiene_mesa"] = bool(cfg.get("mesa_directiva"))
    except Exception as e:
        info["error"] = str(e)
    return info


def lanzar(url, tipo="pleno", comisiones=None, fecha="", modelo=""):
    """Devuelve {"ok", "pid", "comando", ["aviso"]}. Lanza ValueError con
    un mensaje para el usuario si algo no cuadra."""
    global _proc
    url = (url or "").strip()
    tipo = (tipo or "pleno").strip()
    comisiones = [c for c in (comisiones or []) if c]
    fecha = (fecha or "").strip()
    modelo = (modelo or "").strip()

    if not url.startswith("http"):
        raise ValueError("Pega una URL de video válida (http…).")
    if tipo == "comision" and not comisiones:
        raise ValueError("Elige al menos una comisión (o cambia a Pleno).")
    if modelo and modelo not in settings.modelos_permitidos:
        raise ValueError(f"Modelo no permitido: {modelo}")
    if not os.path.isfile(jobs.RUTA_SCRIPT):
        raise ValueError(f"No encuentro el transcriptor ({jobs.RUTA_SCRIPT}).")

    with _lock:
        if corriendo():
            raise ValueError("Ya hay una transcripción en curso. Deténla "
                             "antes de iniciar otra.")
        soportadas = _argumentos_transcriptor()

        def acepta(flag):
            return soportadas is None or flag in soportadas

        cmd = [sys.executable, "-u", jobs.RUTA_SCRIPT, url]
        omitidas = []
        if acepta("--voz"):
            cmd += ["--voz"]
        if acepta("--conservar-audio"):
            cmd += ["--conservar-audio"]
        else:
            omitidas.append("--conservar-audio")
        if acepta("--db"):
            cmd += ["--db", settings.db_path]
        if acepta("--contextos"):
            cmd += ["--contextos", RUTA_CONTEXTOS]
        else:
            omitidas.append("--contextos")
        if modelo and acepta("--modelo"):
            cmd += ["--modelo", modelo]
        if acepta("--tipo"):
            cmd += ["--tipo", tipo]
        else:
            omitidas.append("--tipo")
        if acepta("--comision"):
            for c in comisiones:
                cmd += ["--comision", c]
        elif comisiones:
            omitidas.append("--comision")
        if fecha:
            if acepta("--fecha"):
                cmd += ["--fecha", fecha]
            else:
                omitidas.append("--fecha")

        legible = " ".join((f'"{a}"' if " " in a else a) for a in cmd)
        carpeta = os.path.dirname(os.path.abspath(jobs.RUTA_SCRIPT))
        entorno = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        flog = open(LOG, "wb")
        if omitidas:
            flog.write(("[aviso] Tu transcriptor no acepta estos argumentos y "
                        "se omitieron: " + ", ".join(omitidas) + ".\n\n")
                       .encode("utf-8"))
        flog.write(("$ " + legible + "\n\n").encode("utf-8"))
        flog.flush()
        _proc = subprocess.Popen(cmd, stdout=flog, stderr=subprocess.STDOUT,
                                 cwd=carpeta or None, env=entorno)

    respuesta = {"ok": True, "pid": _proc.pid, "comando": legible}
    if omitidas:
        respuesta["aviso"] = ("Tu transcriptor no acepta " + ", ".join(omitidas)
                              + "; se omitieron y la sesión no quedará "
                              "etiquetada con esos datos.")
    return respuesta


def detener():
    proc = _proc
    if not (proc and proc.poll() is None):
        return {"ok": True, "detenida": False,
                "nota": "No había ninguna transcripción en curso."}
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
    return {"ok": True, "detenida": True}


def cola_de_log(ruta, max_bytes=8000):
    """Últimos max_bytes de un log, sin la primera línea si quedó partida."""
    if not ruta or not os.path.isfile(ruta):
        return ""
    try:
        with open(ruta, "rb") as f:
            try:
                f.seek(-max_bytes, os.SEEK_END)
                partido = True
            except OSError:
                f.seek(0)
                partido = False
            texto = f.read().decode("utf-8", errors="replace")
        if partido and "\n" in texto:
            texto = texto.split("\n", 1)[1]
        return texto
    except Exception:
        return ""


def estado():
    proc = _proc
    vivo = corriendo()
    return {"corriendo": vivo,
            "pid": proc.pid if proc else None,
            "codigo": None if vivo else (proc.returncode if proc else None),
            "lineas": cola_de_log(LOG)}
