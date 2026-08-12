import json
import os
import secrets
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


CONECTORES = {"de", "del", "la", "las", "los", "y", "e"}


def _palabras_significativas(nombre):
    """Conjunto de palabras del nombre (sin acentos/mayúsculas, sin
    conectores). Compara por CONTENIDO, no por orden — necesario porque
    voces_perfiles.json usa 'NOMBRE APELLIDOS' y el sistema de registro
    parlamentario manda 'Apellidos Nombre'."""
    s = unicodedata.normalize("NFKD", nombre.strip().lower())
    sin_acentos = "".join(c for c in s if not unicodedata.combining(c))
    return frozenset(w for w in sin_acentos.split() if w not in CONECTORES)


def cargar_catalogo_perfiles():
    if not os.path.isfile(settings.perfiles_path):
        return {}
    with open(settings.perfiles_path, encoding="utf-8") as f:
        return json.load(f)


def filtrar_participantes(nombres_pedidos):
    """Devuelve (encontrados, no_encontrados) comparando contra las huellas
    disponibles en voces_perfiles.json, por conjunto de palabras (sin
    importar mayúsculas/acentos/orden)."""
    catalogo = cargar_catalogo_perfiles()
    indice = {_palabras_significativas(nombre): nombre for nombre in catalogo}
    encontrados, no_encontrados = [], []
    for pedido in nombres_pedidos:
        real = indice.get(_palabras_significativas(pedido))
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


def _asignar_puerto_srt():
    """Primer puerto libre del rango reservado para audio SRT entrante.
    'Libre' = ningún trabajo srt en estado ejecutando lo está usando."""
    with conexion() as con, con.cursor() as cur:
        cur.execute(
            "SELECT puerto FROM trabajos WHERE fuente='srt' "
            "AND estado='ejecutando' AND puerto IS NOT NULL")
        ocupados = {r["puerto"] for r in cur.fetchall()}
    for puerto in range(settings.srt_puerto_base, settings.srt_puerto_fin + 1):
        if puerto not in ocupados:
            return puerto
    total = settings.srt_puerto_fin - settings.srt_puerto_base + 1
    raise ValueError(
        f"No hay puertos SRT disponibles (máximo {total} transmisiones "
        "simultáneas); espera a que termine otra o sube SRT_PUERTO_FIN.")


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


def trabajo_activo_por_evento(evento_id):
    """Trabajo ya corriendo (ejecutando) para este evento_id, si existe."""
    with conexion() as con, con.cursor() as cur:
        cur.execute(
            "SELECT * FROM trabajos WHERE evento_id = %s AND estado = 'ejecutando' "
            "ORDER BY creado_en DESC LIMIT 1", (evento_id,))
        return cur.fetchone()


def crear_trabajo(usuario_id, datos, evento_id=None):
    if datos.modelo not in settings.modelos_permitidos:
        raise ValueError(f"modelo inválido: {datos.modelo}")
    if datos.fuente not in ("youtube", "srt"):
        raise ValueError(f"fuente inválida: {datos.fuente} (usa youtube o srt)")
    if datos.fuente == "youtube" and not datos.url:
        raise ValueError("falta 'url' (requerido con fuente=youtube)")

    encontrados, no_encontrados = filtrar_participantes(datos.participantes)
    if not encontrados:
        raise ValueError(
            "Ninguno de los participantes tiene huella de voz en "
            f"{settings.perfiles_path}: {', '.join(datos.participantes)}")

    job_id = str(uuid.uuid4())
    ruta_perfiles = _escribir_perfiles_filtrados(job_id, encontrados)

    carpeta = os.path.join(settings.jobs_dir, job_id)
    log_path = os.path.join(carpeta, "salida.log")
    log = open(log_path, "w", encoding="utf-8")

    puerto = passphrase = None
    if datos.fuente == "srt":
        url_guardada = datos.url or "Sesión SRT"
        puerto = _asignar_puerto_srt()
        passphrase = secrets.token_hex(16)
    else:
        url_guardada = datos.url

    id_previo = _max_sesion_id_actual()

    comando = [
        sys.executable, RUTA_SCRIPT, url_guardada,
        "--modelo", datos.modelo,
        "--bloque", str(datos.bloque),
        "--db", settings.db_path,
        "--voz",
        "--perfiles", ruta_perfiles,
        "--umbral-voz", str(datos.umbral_voz),
        "--umbral-cambio-voz", str(datos.umbral_cambio_voz),
    ]
    if datos.fuente == "srt":
        comando += ["--puerto-srt", str(puerto), "--srt-passphrase", passphrase]

    proc = subprocess.Popen(comando, stdout=log, stderr=subprocess.STDOUT,
                             cwd=os.path.dirname(RUTA_SCRIPT) or ".")

    with conexion() as con, con.cursor() as cur:
        cur.execute(
            "INSERT INTO trabajos (id, usuario_id, url, participantes_pedidos, "
            "participantes_encontrados, participantes_no_encontrados, modelo, "
            "fuente, puerto, passphrase, evento_id, estado, pid) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (job_id, usuario_id, url_guardada, json.dumps(datos.participantes),
             json.dumps(encontrados), json.dumps(no_encontrados), datos.modelo,
             datos.fuente, puerto, passphrase, evento_id, "ejecutando", proc.pid))

    with _lock:
        _procesos[job_id] = {"proc": proc, "log": log}

    threading.Thread(target=_monitorear, args=(job_id, proc, log_path),
                      daemon=True).start()
    threading.Thread(target=_detectar_sesion_id,
                      args=(job_id, url_guardada, id_previo, log_path),
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


def limpiar_trabajos_huerfanos():
    """Al arrancar la API, _procesos siempre está vacío — así que cualquier
    trabajo que la base diga 'ejecutando'/'deteniendo' en ese momento es
    forzosamente un fantasma de un reinicio anterior (su proceso real ya no
    existe). Se cierran solos para no bloquear puertos SRT ni confundir al
    operador de audio o el panel de revisar.py."""
    with conexion() as con, con.cursor() as cur:
        cur.execute(
            "UPDATE trabajos SET estado='detenido', "
            "error='Proceso perdido (la API se reinició); cerrado "
            "automáticamente al arrancar.' "
            "WHERE estado IN ('ejecutando', 'deteniendo')")
        return cur.rowcount


def listar_trabajos_srt_activos():
    """Trabajos con fuente SRT que siguen corriendo — la cola compartida
    que ve la app de escritorio del operador (cualquier usuario, no solo
    quien los creó)."""
    with conexion() as con, con.cursor() as cur:
        cur.execute(
            "SELECT * FROM trabajos WHERE fuente='srt' AND estado='ejecutando' "
            "ORDER BY creado_en")
        return cur.fetchall()


def detener_trabajo(job_id):
    with _lock:
        entrada = _procesos.get(job_id)
    if entrada is not None:
        _actualizar_trabajo(job_id, estado="deteniendo")
        entrada["proc"].send_signal(signal.SIGINT)
        return

    # No hay proceso en memoria — normalmente porque el contenedor de la
    # API se reinició después de crear este trabajo (el proceso real ya no
    # existe, pero nadie actualizó su estado). Si la base todavía lo marca
    # como activo, es un "fantasma": lo cerramos a mano en vez de fallar.
    fila = obtener_trabajo(job_id)
    if not fila or fila["estado"] not in ("ejecutando", "deteniendo"):
        raise ValueError("El trabajo no está corriendo (ya terminó o no existe)")
    _actualizar_trabajo(
        job_id, estado="detenido",
        error="Proceso perdido (el contenedor de la API se reinició "
              "mientras corría); marcado como detenido manualmente.")


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
