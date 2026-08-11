#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
voz.py — Reconocimiento de oradores por huella de voz (fase 2)
==============================================================

La idea: tus sesiones ya transcritas y CORREGIDAS en la interfaz son las
muestras de voz. De ellas se aprenden las huellas; con las huellas se
identifican las intervenciones que el protocolo no anunció.

Subcomandos:

  1) APRENDER voces de una sesión ya revisada:
        python voz.py perfiles --sesion 1
     Re-descarga el audio del video, corta los segmentos de cada diputado
     según la base de datos y guarda su huella en voces_perfiles.json.
     Cada sesión procesada mejora los perfiles.

  2) IDENTIFICAR quién habla en los bloques sin orador:
        python voz.py identificar --sesion 2
     Analiza los bloques etiquetados como "Presidencia / Mesa Directiva"
     (ahí se esconden las intervenciones sin anuncio), compara la voz con
     las huellas y muestra un reporte de sugerencias.
        ... --aplicar        guarda la evidencia en la base de datos
        ... --reemplazar     además sustituye el orador cuando la
                             coincidencia es fuerte

Requiere:  pip install -r requirements-voz.txt   (además de ffmpeg)
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import wave

import numpy as np

YTDLP = [sys.executable, "-m", "yt_dlp"]
ORADOR_MESA = "Presidencia / Mesa Directiva"
CARPETA_CACHE = os.path.join("sesiones_en_vivo", "voz_cache")
FRECUENCIA = 16000

# Parámetros de las huellas
SEG_VENTANA = 8.0        # tamaño de cada rebanada de voz analizada
SEG_MARGEN = 1.5         # segundos que se saltan al inicio de cada turno
                         # (suele traslaparse la voz de quien anuncia)
MAX_SEG_POR_ORADOR = 150 # tope de audio por diputado por sesión
MIN_SEG_PERFIL = 20      # mínimo para que un diputado obtenga huella
MIN_SEG_IDENT = 5        # duración mínima de un bloque para identificarlo


# ---------------------------------------------------------------------------
# Utilidades básicas
# ---------------------------------------------------------------------------

def hms(seg):
    seg = int(seg)
    h, r = divmod(seg, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


class _EncoderSpeechBrain:
    """Adaptador del modelo ECAPA-TDNN de SpeechBrain (huellas de 192
    dimensiones, precompilado para Windows/Mac/Linux)."""

    def __init__(self):
        try:
            from speechbrain.inference.speaker import EncoderClassifier
        except ImportError:  # versiones anteriores de speechbrain
            from speechbrain.pretrained import EncoderClassifier
        import torch
        self._torch = torch
        self._enc = EncoderClassifier.from_hparams(
            source="speechbrain/spkrec-ecapa-voxceleb",
            savedir=os.path.join("modelos_voz", "ecapa"))

    def embed_utterance(self, frag):
        t = self._torch.tensor(np.ascontiguousarray(frag),
                               dtype=self._torch.float32).unsqueeze(0)
        with self._torch.no_grad():
            emb = self._enc.encode_batch(t).squeeze().cpu().numpy()
        n = np.linalg.norm(emb)
        return emb / n if n else emb


def obtener_encoder():
    """Carga el modelo de huella de voz. La primera vez lo descarga
    (unos 80 MB) y lo guarda en la carpeta modelos_voz/."""
    try:
        print("Cargando modelo de huella de voz (la primera vez se "
              "descarga)...")
        return _EncoderSpeechBrain()
    except ImportError:
        print("Falta la librería de voz. Instálala con:\n"
              "    pip install -r requirements-voz.txt")
        sys.exit(1)


def preprocesar(fragmento):
    """Normaliza el volumen y recorta los silencios por energía (sin
    dependencias externas)."""
    frag = np.asarray(fragmento, dtype=np.float32)
    tope = float(np.max(np.abs(frag))) if len(frag) else 0.0
    if tope > 0:
        frag = frag / tope * 0.9
    ventana = int(0.03 * FRECUENCIA)          # marcos de 30 ms
    if len(frag) > ventana * 6:
        n = len(frag) // ventana
        marcos = frag[:n * ventana].reshape(n, ventana)
        energia = np.sqrt((marcos ** 2).mean(axis=1))
        umbral = max(float(energia.mean()) * 0.15, 1e-4)
        vivos = energia > umbral
        if vivos.any() and vivos.sum() >= 10:  # conservar si queda ≥0.3 s
            frag = marcos[vivos].reshape(-1)
    return frag


def coseno(a, b):
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


# ---------------------------------------------------------------------------
# Audio de la sesión (descarga con caché y lectura)
# ---------------------------------------------------------------------------

def ruta_audio_sesion(sesion_id):
    os.makedirs(CARPETA_CACHE, exist_ok=True)
    return os.path.join(CARPETA_CACHE, f"sesion_{sesion_id}.wav")


def descargar_audio(url, destino):
    """Descarga el audio del video y lo guarda como WAV 16 kHz mono."""
    if os.path.isfile(destino) and os.path.getsize(destino) > 100000:
        print(f"Audio ya descargado: {destino}")
        return
    print("Descargando el audio de la sesión (puede tardar)...")
    p1 = subprocess.Popen(
        YTDLP + ["-q", "--no-warnings", "-f", "bestaudio/best", "-o", "-",
                 url],
        stdout=subprocess.PIPE)
    p2 = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", "pipe:0",
         "-ac", "1", "-ar", str(FRECUENCIA), destino],
        stdin=p1.stdout)
    p1.stdout.close()
    p1.wait()
    if p2.returncode != 0 or not os.path.isfile(destino):
        print("No se pudo descargar el audio. ¿La URL de la sesión sigue "
              "disponible en YouTube?")
        sys.exit(1)


def cargar_wav(ruta):
    with wave.open(ruta, "rb") as w:
        assert w.getframerate() == FRECUENCIA and w.getnchannels() == 1, \
            "El WAV no está en 16 kHz mono"
        datos = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return datos.astype(np.float32) / 32768.0


def rebanada(audio, ini_seg, fin_seg):
    a = int(max(0, ini_seg) * FRECUENCIA)
    b = int(min(len(audio) / FRECUENCIA, fin_seg) * FRECUENCIA)
    return audio[a:b]


def a_segundos(txt):
    """'HH:MM:SS', 'MM:SS' o segundos -> segundos (float)."""
    try:
        partes = [float(p) for p in str(txt).split(":")]
    except ValueError:
        raise SystemExit(f"Tiempo no válido: {txt} "
                         "(usa HH:MM:SS, MM:SS o segundos)")
    seg = 0.0
    for p in partes:
        seg = seg * 60 + p
    return seg


def convertir_a_wav(origen, destino):
    """Cualquier audio/video local -> WAV 16 kHz mono."""
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", origen,
                        "-vn", "-ac", "1", "-ar", str(FRECUENCIA), destino])
    if r.returncode != 0 or not os.path.isfile(destino):
        raise SystemExit(f"ffmpeg no pudo convertir: {origen}")


def descargar_rango(url, desde, hasta, destino):
    """Descarga SOLO un rango del video de YouTube como WAV 16 kHz mono.
    Intenta la descarga por secciones (rápida); si falla, baja el audio
    completo y lo recorta."""
    tmp = destino + ".seccion.audio"
    r = subprocess.run(
        YTDLP + ["-q", "--no-warnings", "-f", "bestaudio/best",
                 "--download-sections", f"*{desde}-{hasta}",
                 "-o", tmp, url])
    if r.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
        convertir_a_wav(tmp, destino)
        os.remove(tmp)
        return
    print("  (descarga por secciones no disponible; bajando completo "
          "y recortando...)")
    p1 = subprocess.Popen(
        YTDLP + ["-q", "--no-warnings", "-f", "bestaudio/best", "-o", "-",
                 url],
        stdout=subprocess.PIPE)
    r2 = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", "pipe:0",
         "-ss", str(a_segundos(desde)), "-to", str(a_segundos(hasta)),
         "-ac", "1", "-ar", str(FRECUENCIA), destino],
        stdin=p1.stdout)
    p1.stdout.close()
    p1.wait()
    if r2.returncode != 0 or not os.path.isfile(destino):
        raise SystemExit("No se pudo descargar el rango del video.")


# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------

def abrir_db(ruta):
    if not os.path.isfile(ruta):
        print(f"No encuentro la base de datos: {ruta}")
        sys.exit(1)
    con = sqlite3.connect(ruta)
    con.row_factory = sqlite3.Row
    return con


def datos_sesion(con, sesion_id):
    ses = con.execute("SELECT * FROM sesiones WHERE id=?",
                      (sesion_id,)).fetchone()
    if not ses:
        print(f"No existe la sesión #{sesion_id} en la base de datos.")
        sys.exit(1)
    filas = con.execute(
        "SELECT id, orador, inicio_seg, fin_seg, texto "
        "FROM participaciones WHERE sesion_id=? ORDER BY inicio_seg, id",
        (sesion_id,)).fetchall()
    return ses, filas


def agrupar_turnos(filas):
    """Filas consecutivas del mismo orador -> turnos."""
    turnos = []
    actual = None
    for f in filas:
        if not actual or actual["orador"] != f["orador"]:
            actual = {"orador": f["orador"], "inicio": f["inicio_seg"],
                      "fin": f["fin_seg"], "ids": [], "texto": f["texto"]}
            turnos.append(actual)
        actual["ids"].append(f["id"])
        actual["fin"] = f["fin_seg"]
    return turnos


def asegurar_columnas_voz(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(participaciones)")}
    if "voz_orador" not in cols:
        con.execute("ALTER TABLE participaciones ADD COLUMN voz_orador TEXT")
    if "voz_similitud" not in cols:
        con.execute("ALTER TABLE participaciones "
                    "ADD COLUMN voz_similitud REAL")
    con.commit()


# ---------------------------------------------------------------------------
# Perfiles (huellas de voz)
# ---------------------------------------------------------------------------

def cargar_perfiles(ruta):
    if os.path.isfile(ruta):
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    return {}


def guardar_perfiles(perfiles, ruta):
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(perfiles, f, ensure_ascii=False)
    print(f"\nPerfiles guardados en: {os.path.abspath(ruta)}")


def elegir_ventanas(turnos_orador):
    """Devuelve ventanas (ini, fin) de voz limpia para un orador, saltando
    el arranque de cada turno y respetando el tope por sesión."""
    ventanas, total = [], 0.0
    for t in turnos_orador:
        ini = t["inicio"] + SEG_MARGEN
        fin = t["fin"] - 0.5
        while fin - ini >= SEG_VENTANA and total < MAX_SEG_POR_ORADOR:
            ventanas.append((ini, ini + SEG_VENTANA))
            total += SEG_VENTANA
            ini += SEG_VENTANA
        if total >= MAX_SEG_POR_ORADOR:
            break
    return ventanas, total


def fusionar_perfil(anterior, nuevo_emb, nuevos_seg):
    """Promedio ponderado por segundos entre el perfil viejo y el nuevo.
    Si la huella vieja es de otro motor (distinta dimensión), se descarta
    y se empieza de cero con la nueva."""
    nuevo_emb = np.asarray(nuevo_emb, dtype=np.float32)
    if anterior and len(anterior.get("embedding", [])) != len(nuevo_emb):
        print("       (huella previa de un motor anterior: se reemplaza)")
        anterior = None
    if not anterior:
        emb = nuevo_emb
    else:
        va = np.asarray(anterior["embedding"], dtype=np.float32)
        sa = float(anterior.get("segundos", 0))
        emb = (va * sa + nuevo_emb * nuevos_seg) / (sa + nuevos_seg)
    n = np.linalg.norm(emb)
    if n > 0:
        emb = emb / n
    return emb


def cmd_perfiles(args):
    con = abrir_db(args.db)
    ses, filas = datos_sesion(con, args.sesion)
    turnos = agrupar_turnos(filas)

    por_orador = {}
    for t in turnos:
        if t["orador"].startswith("Dip. "):
            por_orador.setdefault(t["orador"][5:], []).append(t)

    if not por_orador:
        print("Esta sesión no tiene oradores 'Dip. ...' de los que aprender. "
              "¿Ya la transcribiste y revisaste?")
        return

    destino = ruta_audio_sesion(args.sesion)
    descargar_audio(ses["url"], destino)
    audio = cargar_wav(destino)
    encoder = obtener_encoder()
    perfiles = cargar_perfiles(args.perfiles)

    print(f"\nAprendiendo voces de la sesión #{args.sesion}: {ses['titulo']}\n")
    aprendidos, sin_material = [], []
    for nombre, ts in sorted(por_orador.items()):
        if args.sesion in perfiles.get(nombre, {}).get("sesiones", []):
            print(f"  [=] {nombre:<40} ya aprendido de la sesión "
                  f"#{args.sesion}, se salta")
            continue
        ventanas, total = elegir_ventanas(ts)
        if total < MIN_SEG_PERFIL:
            sin_material.append((nombre, total))
            continue
        embs = []
        for ini, fin in ventanas:
            frag = rebanada(audio, ini, fin)
            if len(frag) < FRECUENCIA:  # menos de 1 s útil
                continue
            embs.append(encoder.embed_utterance(preprocesar(frag)))
        if not embs:
            sin_material.append((nombre, total))
            continue
        emb_sesion = np.mean(np.stack(embs), axis=0)
        emb_final = fusionar_perfil(perfiles.get(nombre), emb_sesion, total)
        previo = perfiles.get(nombre, {})
        perfiles[nombre] = {
            "embedding": [round(float(x), 6) for x in emb_final],
            "segundos": float(previo.get("segundos", 0)) + total,
            "sesiones": sorted(set(previo.get("sesiones", [])
                                   + [args.sesion])),
        }
        aprendidos.append((nombre, total))
        print(f"  [OK] {nombre:<40} {total:5.0f} s de voz")

    for nombre, total in sin_material:
        print(f"  [--] {nombre:<40} {total:5.0f} s (insuficiente, "
              f"mínimo {MIN_SEG_PERFIL} s)")

    guardar_perfiles(perfiles, args.perfiles)
    print(f"Huellas en el archivo: {len(perfiles)} diputados "
          f"({len(aprendidos)} aprendidos/actualizados en esta corrida)")


# ---------------------------------------------------------------------------
# Muestras directas de voz (archivos, rangos de YouTube o carpetas)
# ---------------------------------------------------------------------------

def huella_de_audio(encoder, audio):
    """Rebana un audio completo en ventanas y devuelve
    (embedding_promedio, segundos_útiles)."""
    embs, total, ini = [], 0.0, 0.0
    dur = len(audio) / FRECUENCIA
    while dur - ini >= 3.0 and total < MAX_SEG_POR_ORADOR:
        fin = min(ini + SEG_VENTANA, dur)
        frag = rebanada(audio, ini, fin)
        if len(frag) >= FRECUENCIA:
            embs.append(encoder.embed_utterance(preprocesar(frag)))
            total += fin - ini
        ini += SEG_VENTANA
    if not embs:
        return None, 0.0
    return np.mean(np.stack(embs), axis=0), total


def _normalizar(s):
    import unicodedata
    t = unicodedata.normalize("NFD", s)
    return "".join(c for c in t if unicodedata.category(c) != "Mn").lower()


def avisar_si_no_esta_en_catalogo(nombre):
    """Si existe diputados.txt, avisa cuando el nombre no empata tal cual
    (evita crear perfiles duplicados por un error de dedo)."""
    base = os.path.dirname(os.path.abspath(__file__))
    for ruta in (os.path.join(base, "diputados.txt"), "diputados.txt"):
        if os.path.isfile(ruta):
            with open(ruta, encoding="utf-8") as f:
                cat = [_normalizar(l.strip()) for l in f
                       if l.strip() and not l.startswith("#")]
            if _normalizar(nombre) not in cat:
                print(f"  [AVISO] '{nombre}' no aparece tal cual en "
                      "diputados.txt; usa el nombre oficial exacto para "
                      "que las sugerencias empaten con el catálogo.")
            return


def registrar_muestra(perfiles, nombre, emb, segundos, origen):
    """Suma una muestra al perfil. Recuerda las fuentes ya procesadas:
    volver a correr el comando con los mismos archivos no duplica nada."""
    previo = perfiles.get(nombre, {})
    if origen in previo.get("fuentes", []):
        print(f"  [=] {nombre:<40} {origen} (ya registrada, se salta)")
        return False
    emb_final = fusionar_perfil(previo or None, emb, segundos)
    perfiles[nombre] = {
        "embedding": [round(float(x), 6) for x in emb_final],
        "segundos": float(previo.get("segundos", 0)) + segundos,
        "sesiones": previo.get("sesiones", []),
        "fuentes": previo.get("fuentes", []) + [origen],
    }
    print(f"  [OK] {nombre:<40} +{segundos:4.0f} s  ({origen})")
    return True


def cmd_muestra(args):
    import tempfile
    if not args.audio and not args.url:
        raise SystemExit("Indica --audio archivo(s), o --url con "
                         "--desde y --hasta.")
    if args.url and not (args.desde and args.hasta):
        raise SystemExit("Con --url necesitas --desde y --hasta "
                         "(ej. --desde 00:05:30 --hasta 00:06:30).")
    avisar_si_no_esta_en_catalogo(args.nombre)
    encoder = obtener_encoder()
    perfiles = cargar_perfiles(args.perfiles)
    total = 0.0
    with tempfile.TemporaryDirectory() as tmp:
        fuentes = []
        if args.url:
            destino = os.path.join(tmp, "rango.wav")
            print("Descargando el fragmento del video...")
            descargar_rango(args.url, args.desde, args.hasta, destino)
            fuentes.append((destino,
                            f"{args.desde}-{args.hasta} de {args.url}"[:90]))
        for i, ruta in enumerate(args.audio or []):
            if not os.path.isfile(ruta):
                raise SystemExit(f"No existe el archivo: {ruta}")
            destino = os.path.join(tmp, f"m{i}.wav")
            convertir_a_wav(ruta, destino)
            fuentes.append((destino, os.path.basename(ruta)))
        for ruta, origen in fuentes:
            emb, seg = huella_de_audio(encoder, cargar_wav(ruta))
            if emb is None:
                print(f"  [--] {origen}: audio demasiado corto (mínimo ~3 s)")
                continue
            registrar_muestra(perfiles, args.nombre, emb, seg, origen)
            total += seg
    guardar_perfiles(perfiles, args.perfiles)
    if total and total < 15:
        print("Consejo: con 30-60 segundos de voz limpia por diputado la "
              "huella es mucho más confiable; agrega más muestras cuando "
              "puedas.")


EXTENSIONES_AUDIO = (".mp3", ".m4a", ".wav", ".ogg", ".opus", ".aac",
                     ".flac", ".mp4", ".webm", ".mkv")


def cmd_muestras(args):
    import tempfile
    if not os.path.isdir(args.carpeta):
        raise SystemExit(
            f"No existe la carpeta '{args.carpeta}'. Créala con una "
            "subcarpeta por diputado (el nombre de la subcarpeta es el "
            "nombre oficial) y adentro sus audios.\n"
            "Ejemplo:  muestras_voz/Zaira Cedillo Silva/entrevista.mp3")
    encoder = obtener_encoder()
    perfiles = cargar_perfiles(args.perfiles)
    procesados = 0
    with tempfile.TemporaryDirectory() as tmp:
        for nombre in sorted(os.listdir(args.carpeta)):
            sub = os.path.join(args.carpeta, nombre)
            if not os.path.isdir(sub):
                continue
            if getattr(args, "solo", None) \
                    and _normalizar(nombre) != _normalizar(args.solo):
                continue
            archivos = [a for a in sorted(os.listdir(sub))
                        if a.lower().endswith(EXTENSIONES_AUDIO)]
            if not archivos:
                print(f"  [--] {nombre}: sin archivos de audio")
                continue
            avisar_si_no_esta_en_catalogo(nombre)
            for i, a in enumerate(archivos):
                destino = os.path.join(tmp, f"{procesados}_{i}.wav")
                convertir_a_wav(os.path.join(sub, a), destino)
                emb, seg = huella_de_audio(encoder, cargar_wav(destino))
                if emb is None:
                    print(f"  [--] {nombre} / {a}: demasiado corto")
                    continue
                registrar_muestra(perfiles, nombre, emb, seg, a)
            procesados += 1
    guardar_perfiles(perfiles, args.perfiles)
    print(f"Diputados procesados desde la carpeta: {procesados}")


# ---------------------------------------------------------------------------
# Administración de huellas
# ---------------------------------------------------------------------------

def cmd_ver(args):
    """Lista las huellas existentes con sus segundos y fuentes."""
    perfiles = cargar_perfiles(args.perfiles)
    if not perfiles:
        print(f"No hay huellas en {args.perfiles}.")
        return
    print(f"Huellas en {os.path.abspath(args.perfiles)}: "
          f"{len(perfiles)} diputados\n")
    for nombre in sorted(perfiles):
        p = perfiles[nombre]
        extras = []
        if p.get("fuentes"):
            extras.append(f"{len(p['fuentes'])} muestra(s)")
        if p.get("sesiones"):
            extras.append("sesiones " + ", ".join(
                f"#{s}" for s in p["sesiones"]))
        print(f"  {nombre:<44} {p.get('segundos', 0):5.0f} s"
              + ("   " + " | ".join(extras) if extras else ""))
    print("\nPara rehacer una huella:  python voz.py olvidar --nombre "
          "\"Nombre Completo\"")


def cmd_olvidar(args):
    """Elimina la huella de un diputado (para rehacerla desde cero)."""
    perfiles = cargar_perfiles(args.perfiles)
    clave = next((k for k in perfiles
                  if _normalizar(k) == _normalizar(args.nombre)), None)
    if not clave:
        print(f"No hay huella de '{args.nombre}' en {args.perfiles}.")
        if perfiles:
            print("Usa  python voz.py ver  para listar las existentes.")
        return
    del perfiles[clave]
    guardar_perfiles(perfiles, args.perfiles)
    print(f"Huella de {clave} eliminada ({len(perfiles)} restantes).")
    print(f"Para rehacerla:  python voz.py muestras --solo \"{clave}\"")


# ---------------------------------------------------------------------------
# Identificación
# ---------------------------------------------------------------------------

def cmd_identificar(args):
    con = abrir_db(args.db)
    ses, filas = datos_sesion(con, args.sesion)
    perfiles = cargar_perfiles(args.perfiles)
    if not perfiles:
        print(f"No hay huellas en {args.perfiles}. Primero corre:\n"
              f"    python voz.py perfiles --sesion <una sesión revisada>")
        return

    turnos = agrupar_turnos(filas)
    objetivo = [t for t in turnos
                if (args.todos or t["orador"] == ORADOR_MESA)
                and (t["fin"] - t["inicio"]) >= MIN_SEG_IDENT]
    if not objetivo:
        print("No hay bloques que analizar con esos criterios.")
        return

    destino = ruta_audio_sesion(args.sesion)
    descargar_audio(ses["url"], destino)
    audio = cargar_wav(destino)
    encoder = obtener_encoder()

    nombres = list(perfiles.keys())
    matriz = np.stack([np.asarray(perfiles[n]["embedding"], dtype=np.float32)
                       for n in nombres])

    print(f"\nSesión #{args.sesion}: {ses['titulo']}")
    print(f"Analizando {len(objetivo)} bloques contra "
          f"{len(nombres)} huellas (umbral {args.umbral})\n")

    if args.aplicar or args.reemplazar:
        asegurar_columnas_voz(con)

    sugerencias = reemplazos = 0
    for t in objetivo:
        ini = t["inicio"] + 0.8
        fin = min(t["fin"], ini + 20.0)   # bastan ~20 s para identificar
        frag = rebanada(audio, ini, fin)
        if len(frag) < FRECUENCIA * 2:
            continue
        emb = encoder.embed_utterance(preprocesar(frag))
        emb = emb / (np.linalg.norm(emb) or 1.0)
        sims = matriz @ emb
        orden = np.argsort(sims)[::-1]
        mejor, segundo = orden[0], (orden[1] if len(orden) > 1 else orden[0])
        sim, margen = float(sims[mejor]), float(sims[mejor] - sims[segundo])
        candidato = nombres[mejor]
        fuerte = sim >= args.umbral and margen >= 0.05

        marca = "***" if fuerte else "   "
        extracto = (t["texto"] or "")[:38]
        print(f"{marca} [{hms(t['inicio'])}] {t['orador'][:22]:<22} "
              f"-> ¿{candidato}?  sim={sim:.2f} margen={margen:.2f}"
              f"  | {extracto}")

        if fuerte:
            sugerencias += 1
        if args.aplicar or (args.reemplazar and fuerte):
            marcas = ",".join("?" * len(t["ids"]))
            con.execute(
                f"UPDATE participaciones SET voz_orador=?, voz_similitud=? "
                f"WHERE id IN ({marcas})",
                ["Dip. " + candidato, round(sim, 3)] + t["ids"])
        if args.reemplazar and fuerte and t["orador"] == ORADOR_MESA:
            marcas = ",".join("?" * len(t["ids"]))
            con.execute(
                f"UPDATE participaciones SET orador=? WHERE id IN ({marcas})",
                ["Dip. " + candidato] + t["ids"])
            reemplazos += 1
    con.commit()

    print(f"\nBloques con coincidencia fuerte (***): {sugerencias}")
    if args.reemplazar:
        print(f"Oradores reemplazados en la base de datos: {reemplazos}")
    elif args.aplicar:
        print("Evidencia guardada en las columnas voz_orador / "
              "voz_similitud (el orador NO se modificó).")
    else:
        print("Modo reporte: no se modificó la base de datos. "
              "Usa --aplicar o --reemplazar para escribir.")
    print("Revisa siempre los resultados en revisar.py: la voz sugiere, "
          "tú confirmas.")


# ---------------------------------------------------------------------------
# Programa principal
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Huellas de voz para identificar oradores.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("perfiles",
                        help="aprender voces de una sesión ya revisada")
    p1.add_argument("--db", default="sesiones.db")
    p1.add_argument("--sesion", type=int, required=True)
    p1.add_argument("--perfiles", default="voces_perfiles.json")

    p2 = sub.add_parser("identificar",
                        help="identificar bloques sin orador por su voz")
    p2.add_argument("--db", default="sesiones.db")
    p2.add_argument("--sesion", type=int, required=True)
    p2.add_argument("--perfiles", default="voces_perfiles.json")
    p2.add_argument("--umbral", type=float, default=0.75,
                    help="similitud mínima para sugerencia fuerte (0-1)")
    p2.add_argument("--todos", action="store_true",
                    help="analizar TODOS los bloques, no solo los de "
                         "Presidencia (útil para auditar)")
    p2.add_argument("--aplicar", action="store_true",
                    help="guardar la evidencia en columnas voz_*")
    p2.add_argument("--reemplazar", action="store_true",
                    help="además sustituir el orador en coincidencias "
                         "fuertes de bloques de Presidencia")

    p3 = sub.add_parser("muestra",
                        help="agregar muestra de voz de UN diputado")
    p3.add_argument("--nombre", required=True,
                    help="nombre oficial (igual que en diputados.txt)")
    p3.add_argument("--audio", nargs="+",
                    help="archivo(s) de audio o video locales")
    p3.add_argument("--url", help="video de YouTube donde habla")
    p3.add_argument("--desde", help="inicio del fragmento (HH:MM:SS)")
    p3.add_argument("--hasta", help="fin del fragmento (HH:MM:SS)")
    p3.add_argument("--perfiles", default="voces_perfiles.json")

    p4 = sub.add_parser("muestras",
                        help="agregar muestras por carpetas: una subcarpeta "
                             "por diputado con sus audios")
    p4.add_argument("--carpeta", default="muestras_voz")
    p4.add_argument("--perfiles", default="voces_perfiles.json")
    p4.add_argument("--solo",
                    help="procesar solo la subcarpeta de este diputado")

    p5 = sub.add_parser("ver", help="listar las huellas existentes")
    p5.add_argument("--perfiles", default="voces_perfiles.json")

    p6 = sub.add_parser("olvidar",
                        help="eliminar la huella de un diputado para "
                             "rehacerla")
    p6.add_argument("--nombre", required=True)
    p6.add_argument("--perfiles", default="voces_perfiles.json")

    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if args.cmd == "perfiles":
        cmd_perfiles(args)
    elif args.cmd == "muestra":
        cmd_muestra(args)
    elif args.cmd == "muestras":
        cmd_muestras(args)
    elif args.cmd == "ver":
        cmd_ver(args)
    elif args.cmd == "olvidar":
        cmd_olvidar(args)
    else:
        cmd_identificar(args)


if __name__ == "__main__":
    main()
