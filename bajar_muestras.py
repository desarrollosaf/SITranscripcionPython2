#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bajar_muestras.py — Descarga y organiza muestras de voz desde YouTube
=====================================================================

Para juntar las muestras de voz de los diputados (ej. 3 clips de 1 minuto
por diputado) sin teclear cientos de comandos.

Flujo recomendado:

  1) Generar la plantilla (usa los nombres de diputados.txt, 3 filas
     por diputado):
        python bajar_muestras.py --plantilla

  2) Abrir muestras.csv en Excel y llenar url, desde y hasta de cada fila
     (deja vacías las que no tengas; se ignoran). Guardar como CSV.

  3) Descargar todo el lote:
        python bajar_muestras.py --lista muestras.csv

     Los clips quedan organizados en:
        muestras_voz/<Nombre Del Diputado>/<video>_<rango>.mp3
     Puedes abrirlos con doble clic para verificar de oído que sí es la
     persona correcta. Las filas ya descargadas se saltan, así que puedes
     correrlo las veces que quieras conforme llenes más filas.

  4) Crear las huellas de voz:
        python voz.py muestras

También sirve para una muestra suelta:
    python bajar_muestras.py --nombre "Zaira Cedillo Silva" --url "https://youtube.com/watch?v=XXX" --desde 00:15:20 --hasta 00:16:20
"""

import argparse
import csv
import os
import re
import subprocess
import sys
import unicodedata

YTDLP = [sys.executable, "-m", "yt_dlp"]
CARPETA = "muestras_voz"
ARCHIVO_LISTA = "muestras.csv"
FRECUENCIA = 16000


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _normalizar(s):
    t = unicodedata.normalize("NFD", s)
    return "".join(c for c in t if unicodedata.category(c) != "Mn").lower()


def cargar_catalogo():
    base = os.path.dirname(os.path.abspath(__file__))
    for ruta in (os.path.join(base, "diputados.txt"), "diputados.txt"):
        if os.path.isfile(ruta):
            with open(ruta, encoding="utf-8") as f:
                return [ln.strip() for ln in f
                        if ln.strip() and not ln.strip().startswith("#")]
    return []


def limpiar_tiempo(txt):
    """Quita los sufijos 'a. m.' / 'p. m.' que Excel agrega cuando
    interpreta un tiempo como hora del día."""
    t = str(txt).strip().lower()
    return re.sub(r"\s*[ap]\.?\s*m\.?\s*$", "", t).strip()


def a_segundos(txt):
    try:
        partes = [float(p) for p in limpiar_tiempo(txt).split(":")]
    except ValueError:
        return None
    seg = 0.0
    for p in partes:
        seg = seg * 60 + p
    return seg


def id_video(url):
    m = re.search(r"(?:v=|youtu\.be/|/live/|/shorts/)([\w\-]{6,})", url)
    return m.group(1) if m else "video"


def nombre_archivo(url, desde, hasta):
    limpio = lambda t: str(t).strip().replace(":", "-")
    return f"{id_video(url)}_{limpio(desde)}_a_{limpio(hasta)}.mp3"


def verificar_dependencias():
    faltan = []
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        faltan.append("yt-dlp  ->  pip install yt-dlp")
    import shutil
    if shutil.which("ffmpeg") is None:
        faltan.append("ffmpeg  ->  ver README")
    if faltan:
        print("FALTAN DEPENDENCIAS:")
        for f in faltan:
            print("  -", f)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Descarga de un clip
# ---------------------------------------------------------------------------

def descargar_clip(url, desde, hasta, destino):
    """Descarga solo el rango indicado y lo guarda como MP3 mono.
    Intenta la descarga por secciones (rápida); si falla, baja el audio
    completo y recorta. Devuelve True si quedó el archivo."""
    tmp = destino + ".parcial"
    try:
        r = subprocess.run(
            YTDLP + ["-q", "--no-warnings", "-f", "bestaudio/best",
                     "--download-sections", f"*{desde}-{hasta}",
                     "-o", tmp, url],
            timeout=600)
        if r.returncode == 0 and os.path.isfile(tmp) \
                and os.path.getsize(tmp) > 0:
            r2 = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", tmp, "-vn",
                 "-ac", "1", "-ar", str(FRECUENCIA), "-b:a", "64k",
                 destino])
            os.remove(tmp)
            if r2.returncode == 0 and os.path.isfile(destino):
                return True
        # Reserva: bajar completo y recortar
        p1 = subprocess.Popen(
            YTDLP + ["-q", "--no-warnings", "-f", "bestaudio/best",
                     "-o", "-", url],
            stdout=subprocess.PIPE)
        r3 = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", "pipe:0",
             "-ss", str(a_segundos(desde)), "-to", str(a_segundos(hasta)),
             "-vn", "-ac", "1", "-ar", str(FRECUENCIA), "-b:a", "64k",
             destino],
            stdin=p1.stdout, timeout=1800)
        p1.stdout.close()
        p1.wait()
        return r3.returncode == 0 and os.path.isfile(destino)
    except subprocess.TimeoutExpired:
        return False
    finally:
        if os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def procesar_fila(nombre, url, desde, hasta, catalogo):
    """Descarga un clip a la carpeta del diputado. Devuelve
    'ok' | 'ya_estaba' | 'error' | 'fila_mala'."""
    nombre = nombre.strip()
    if not nombre or not url.strip():
        return "fila_mala"
    if a_segundos(desde) is None or a_segundos(hasta) is None \
            or a_segundos(hasta) <= a_segundos(desde):
        print(f"  [??] {nombre}: tiempos no válidos "
              f"('{desde}' a '{hasta}') — fila saltada")
        return "fila_mala"
    if catalogo and _normalizar(nombre) not in [_normalizar(c)
                                                for c in catalogo]:
        print(f"  [AVISO] '{nombre}' no está tal cual en diputados.txt "
              "(revisa el nombre para que empate con el catálogo)")
    carpeta = os.path.join(CARPETA, nombre)
    os.makedirs(carpeta, exist_ok=True)
    destino = os.path.join(carpeta, nombre_archivo(url, desde, hasta))
    if os.path.isfile(destino) and os.path.getsize(destino) > 10000:
        print(f"  [=] {nombre}: {os.path.basename(destino)} (ya estaba)")
        return "ya_estaba"
    print(f"  [↓] {nombre}: {desde} a {hasta} ...", flush=True)
    if descargar_clip(url.strip(), desde.strip(), hasta.strip(), destino):
        print(f"  [OK] {nombre}: {os.path.basename(destino)}")
        return "ok"
    print(f"  [X] {nombre}: falló la descarga (¿URL correcta? "
          "¿yt-dlp actualizado?)")
    return "error"


# ---------------------------------------------------------------------------
# Plantilla y lista
# ---------------------------------------------------------------------------

def cmd_plantilla(args):
    if os.path.isfile(args.lista) and not args.forzar:
        print(f"Ya existe {args.lista}; no lo sobrescribo para no borrar "
              "tu trabajo. Usa --forzar si de verdad quieres regenerarlo.")
        return
    catalogo = cargar_catalogo()
    with open(args.lista, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["nombre", "url", "desde", "hasta"])
        if catalogo:
            for nombre in catalogo:
                for _ in range(args.clips):
                    w.writerow([nombre, "", "", ""])
        else:
            w.writerow(["Zaira Cedillo Silva",
                        "https://youtube.com/watch?v=XXXX",
                        "00:15:20", "00:16:20"])
    n = len(catalogo) * args.clips if catalogo else 1
    print(f"Plantilla creada: {os.path.abspath(args.lista)} "
          f"({n} filas{', ' + str(args.clips) + ' por diputado' if catalogo else ''})")
    print("Ábrela en Excel, llena url/desde/hasta (HH:MM:SS) y guarda "
          "como CSV.\nLuego:  python bajar_muestras.py --lista "
          f"{args.lista}")


PATRON_TIEMPO = re.compile(r"^\d{1,3}:\d{2}(:\d{2})?$|^\d+(\.\d+)?$")


def interpretar_fila(fila):
    """Acomoda las celdas aunque la fila traiga columnas extra (numeración,
    notas) o en otro orden: la URL es la celda con http/youtube, los
    tiempos son las celdas con formato de reloj, y el nombre es la celda
    con más letras. Devuelve (nombre, url, desde, hasta)."""
    celdas = [c.strip() for c in fila if c.strip()]
    url = next((c for c in celdas
                if "youtu" in c.lower() or c.lower().startswith("http")), "")
    # Tiempos: primero los que traen ':' (evita confundir una columna de
    # numeración '1' con segundos); se limpian los 'a. m.' de Excel
    tiempos = [limpiar_tiempo(c) for c in celdas
               if ":" in c and PATRON_TIEMPO.match(limpiar_tiempo(c))][:2]
    if len(tiempos) < 2:
        sueltos = [limpiar_tiempo(c) for c in celdas
                   if c != url and limpiar_tiempo(c) not in tiempos
                   and ":" not in c and PATRON_TIEMPO.match(limpiar_tiempo(c))]
        tiempos += sueltos[:2 - len(tiempos)]
    nombre = ""
    for c in celdas:
        if c == url or limpiar_tiempo(c) in tiempos:
            continue
        if sum(ch.isalpha() for ch in c) > sum(ch.isalpha() for ch in nombre):
            nombre = c
    desde, hasta = (tiempos + ["", ""])[:2]
    sa, sb = a_segundos(desde), a_segundos(hasta)
    if sa is not None and sb is not None and sa > sb:
        desde, hasta = hasta, desde   # orden natural: el menor es 'desde'
    return nombre, url, desde, hasta


def leer_lista(ruta):
    # Excel en Windows suele guardar el CSV en cp1252 (no UTF-8); probamos
    # las codificaciones más comunes hasta que una funcione
    import io
    contenido = None
    for codif in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            with open(ruta, encoding=codif, newline="") as f:
                contenido = f.read()
            break
        except UnicodeDecodeError:
            continue
    if contenido is None:
        raise SystemExit(f"No pude leer {ruta}: codificación desconocida. "
                         "Guárdalo desde Excel como 'CSV UTF-8'.")
    try:
        dialecto = csv.Sniffer().sniff(contenido[:4096], delimiters=",;\t")
    except csv.Error:
        dialecto = csv.excel
    filas = []
    for fila in csv.reader(io.StringIO(contenido), dialecto):
        if not fila or not any(c.strip() for c in fila):
            continue
        if any(c.strip().lower() == "nombre" for c in fila):
            continue  # encabezado (en cualquier columna)
        filas.append(fila)
    return filas


def cmd_lista(args):
    if not os.path.isfile(args.lista):
        print(f"No existe {args.lista}. Genera la plantilla primero:\n"
              "    python bajar_muestras.py --plantilla")
        sys.exit(1)
    catalogo = cargar_catalogo()
    filas = leer_lista(args.lista)
    interpretadas = [interpretar_fila(f) for f in filas]
    pendientes = [t for t in interpretadas if t[1]]
    print(f"Filas en la lista: {len(filas)} | con URL para descargar: "
          f"{len(pendientes)}\n")
    cuenta = {"ok": 0, "ya_estaba": 0, "error": 0, "fila_mala": 0}
    for nombre, url, desde, hasta in pendientes:
        cuenta[procesar_fila(nombre, url, desde, hasta, catalogo)] += 1

    # Resumen por diputado (lo que ya hay en disco)
    print("\n" + "=" * 58)
    print("MUESTRAS EN DISCO POR DIPUTADO")
    print("=" * 58)
    if os.path.isdir(CARPETA):
        for nombre in sorted(os.listdir(CARPETA)):
            sub = os.path.join(CARPETA, nombre)
            if os.path.isdir(sub):
                n = len([a for a in os.listdir(sub)
                         if a.lower().endswith(".mp3")])
                marca = "OK " if n >= args.meta else f"{n}/{args.meta}"
                print(f"  [{marca}] {nombre}: {n} clip(s)")
    print(f"\nDescargados ahora: {cuenta['ok']} | ya estaban: "
          f"{cuenta['ya_estaba']} | fallidos: {cuenta['error']} | "
          f"filas con datos incompletos: {cuenta['fila_mala']}")
    print("\nConsejo: abre los mp3 con doble clic y verifica de oído que "
          "cada clip sea la persona correcta.")
    print("Siguiente paso:  python voz.py muestras")


def main():
    ap = argparse.ArgumentParser(
        description="Descarga y organiza muestras de voz desde YouTube.")
    ap.add_argument("--plantilla", action="store_true",
                    help="generar muestras.csv con los nombres de "
                         "diputados.txt")
    ap.add_argument("--clips", type=int, default=3,
                    help="filas por diputado en la plantilla (default: 3)")
    ap.add_argument("--forzar", action="store_true",
                    help="sobrescribir la plantilla si ya existe")
    ap.add_argument("--lista", default=ARCHIVO_LISTA,
                    help=f"archivo CSV a procesar (default: {ARCHIVO_LISTA})")
    ap.add_argument("--meta", type=int, default=3,
                    help="clips deseados por diputado en el resumen")
    ap.add_argument("--nombre", help="modo de una sola muestra")
    ap.add_argument("--url")
    ap.add_argument("--desde")
    ap.add_argument("--hasta")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.plantilla:
        cmd_plantilla(args)
        return
    verificar_dependencias()
    if args.nombre:
        if not (args.url and args.desde and args.hasta):
            print("Modo de una muestra: necesitas --url, --desde y --hasta.")
            sys.exit(1)
        procesar_fila(args.nombre, args.url, args.desde, args.hasta,
                      cargar_catalogo())
        print("\nSiguiente paso:  python voz.py muestras")
        return
    cmd_lista(args)


if __name__ == "__main__":
    main()
