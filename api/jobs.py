import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timezone

from .config import settings
from .db_mysql import conexion

RUTA_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "transcribir_en_vivo_c3.py")

# job_id -> {"proc": Popen, "log": file}. Vive en memoria: solo funciona si
# la API corre con un único worker/proceso uvicorn.
_procesos = {}
_lock = threading.Lock()


def _normalizar(texto):
    s = unicodedata.normalize("NFKD", texto.strip().lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def cargar_catalogo_perfiles():
    if not os.path.isfile(settings.perfiles_path):
        return {}
    with open(settings.perfiles_path, encoding="utf-8") as f:
        return json.load(f)


def filtrar_participantes(nombres_pedidos):
    """Devuelve (encontrados, no_encontrados) comparando contra las huellas
    disponibles en voces_perfiles.json, sin importar mayúsculas/acentos."""
    catalogo = cargar_catalogo_perfiles()
    indice = {_normalizar(nombre): nombre for nombre in catalogo}
    encontrados, no_encontrados = [], []
    for pedido in nombres_pedidos:
        real = indice.get(_normalizar(pedido))
        (encontrados if real else no_encontrados).append(real or pedido)
    return encontrados, no_encontrados


def _escribir_perfiles_filtrados(job_id, nombres_encontrados):
    catalogo = cargar_catalogo_perfiles()
    subconjunto = {nombre: catalogo[nombre] for nombre in nombres_encontrados}
    carpeta = os.path.join(settings.jobs_dir, job_id)
    os.makedirs(carpeta, exist_ok=True)
    ruta = os.path.join(carpeta, "perfiles.json")
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(subconjunto, f, ensure_ascii=False)
    return ruta


def _max_sesion_id_actual():
    if not os.path.isfile(settings.db_path):
        return 0
    con = sqlite3.connect(settings.db_path, timeout=10)
    try:
        fila = con.execute("SELECT COALESCE(MAX(id), 0) FROM sesiones").fetchone()
        return fila[0]
    finally:
        con.close()


def _actualizar_trabajo(job_id, **campos):
    if not campos:
        return
    set_clause = ", ".join(f"{k} = %s" for k in campos)
    with conexion() as con, con.cursor() as cur:
        cur.execute(f"UPDATE trabajos SET {set_clause} WHERE id = %s",
                    (*campos.values(), job_id))


def _detectar_sesion_id(job_id, url, id_previo, log):
    for _ in range(60):  # hasta ~2 minutos
        time.sleep(2)
        with _lock:
            entrada = _procesos.get(job_id)
        if entrada is None:
            return
        try:
            con = sqlite3.connect(settings.db_path, timeout=5)
            fila = con.execute(
                "SELECT id FROM sesiones WHERE url = ? AND id > ? "
                "ORDER BY id DESC LIMIT 1", (url, id_previo)).fetchone()
            con.close()
        except sqlite3.OperationalError:
            continue
        if fila:
            _actualizar_trabajo(job_id, sesion_id=fila[0])
            return
        if entrada["proc"].poll() is not None:
            return  # el proceso ya terminó (probablemente con error) sin crear sesión


def _monitorear(job_id, proc, log_path):
    proc.wait()
    with conexion() as con, con.cursor() as cur:
        cur.execute("SELECT estado FROM trabajos WHERE id = %s", (job_id,))
        fila = cur.fetchone()
    estado_previo = fila["estado"] if fila else None

    if estado_previo == "deteniendo":
        estado_final = "detenido"
        error = None
    elif proc.returncode == 0:
        estado_final = "finalizado"
        error = None
    else:
        estado_final = "error"
        error = _cola_log(log_path)

    _actualizar_trabajo(job_id, estado=estado_final, error=error)
    with _lock:
        _procesos.pop(job_id, None)


def _cola_log(log_path, max_chars=4000):
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            contenido = f.read()
        return contenido[-max_chars:]
    except OSError:
        return None


def crear_trabajo(usuario_id, datos):
    if datos.modelo not in settings.modelos_permitidos:
        raise ValueError(f"modelo inválido: {datos.modelo}")

    encontrados, no_encontrados = filtrar_participantes(datos.participantes)
    if not encontrados:
        raise ValueError(
            "Ninguno de los participantes tiene huella de voz en "
            f"{settings.perfiles_path}: {', '.join(datos.participantes)}")

    job_id = str(uuid.uuid4())
    ruta_perfiles = _escribir_perfiles_filtrados(job_id, encontrados)
    id_previo = _max_sesion_id_actual()

    carpeta = os.path.join(settings.jobs_dir, job_id)
    log_path = os.path.join(carpeta, "salida.log")
    log = open(log_path, "w", encoding="utf-8")

    comando = [
        sys.executable, RUTA_SCRIPT, datos.url,
        "--modelo", datos.modelo,
        "--bloque", str(datos.bloque),
        "--db", settings.db_path,
        "--voz",
        "--perfiles", ruta_perfiles,
        "--umbral-voz", str(datos.umbral_voz),
        "--umbral-cambio-voz", str(datos.umbral_cambio_voz),
    ]

    proc = subprocess.Popen(comando, stdout=log, stderr=subprocess.STDOUT,
                             cwd=os.path.dirname(RUTA_SCRIPT) or ".")

    with conexion() as con, con.cursor() as cur:
        cur.execute(
            "INSERT INTO trabajos (id, usuario_id, url, participantes_pedidos, "
            "participantes_encontrados, participantes_no_encontrados, modelo, "
            "estado, pid) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (job_id, usuario_id, datos.url, json.dumps(datos.participantes),
             json.dumps(encontrados), json.dumps(no_encontrados), datos.modelo,
             "ejecutando", proc.pid))

    with _lock:
        _procesos[job_id] = {"proc": proc, "log": log}

    threading.Thread(target=_monitorear, args=(job_id, proc, log_path),
                      daemon=True).start()
    threading.Thread(target=_detectar_sesion_id,
                      args=(job_id, datos.url, id_previo, log_path),
                      daemon=True).start()

    return obtener_trabajo(job_id)


def obtener_trabajo(job_id):
    with conexion() as con, con.cursor() as cur:
        cur.execute("SELECT * FROM trabajos WHERE id = %s", (job_id,))
        return cur.fetchone()


def listar_trabajos(usuario_id=None):
    with conexion() as con, con.cursor() as cur:
        if usuario_id is None:
            cur.execute("SELECT * FROM trabajos ORDER BY creado_en DESC")
        else:
            cur.execute("SELECT * FROM trabajos WHERE usuario_id = %s "
                        "ORDER BY creado_en DESC", (usuario_id,))
        return cur.fetchall()


def detener_trabajo(job_id):
    with _lock:
        entrada = _procesos.get(job_id)
    if entrada is None:
        raise ValueError("El trabajo no está corriendo (ya terminó o no existe)")
    _actualizar_trabajo(job_id, estado="deteniendo")
    entrada["proc"].send_signal(signal.SIGINT)


def obtener_participaciones(sesion_id):
    con = sqlite3.connect(settings.db_path, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        filas = con.execute(
            "SELECT orador, inicio_hms, fin_hms, texto FROM participaciones "
            "WHERE sesion_id = ? ORDER BY inicio_seg", (sesion_id,)).fetchall()
        return [dict(f) for f in filas]
    finally:
        con.close()
