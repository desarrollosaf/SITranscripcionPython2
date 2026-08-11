#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
transcribir_en_vivo.py
======================
Transcripción CASI EN TIEMPO REAL de sesiones legislativas transmitidas
por YouTube (en vivo o video ya terminado).

Flujo:
    YouTube ──> yt-dlp ──> ffmpeg (bloques WAV de N segundos)
            ──> faster-whisper (transcripción en español)
            ──> detección de oradores por fórmulas parlamentarias
            ──> pantalla + archivo .txt + base de datos SQLite

Uso básico:
    python transcribir_en_vivo.py "https://www.youtube.com/watch?v=XXXX"

Detener: Ctrl+C  (termina la captura, procesa lo pendiente y cierra bien)

Requiere: Python 3.9+, ffmpeg instalado y `pip install -r requirements.txt`
"""

import argparse
import difflib
import glob
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
from datetime import datetime
from types import SimpleNamespace

try:
    import numpy as np
except ImportError:
    np = None

# ---------------------------------------------------------------------------
# Configuración: fórmulas parlamentarias para identificar oradores
# ---------------------------------------------------------------------------

# Frases con las que la Mesa Directiva anuncia al siguiente orador
FRASES_ANUNCIO = [
    "uso de la palabra",
    "uso de la voz",
    "uso de la tribuna",
    "tiene la palabra",
    "se concede la palabra",
    "se le concede la palabra",
    "concedemos la palabra",
    "cedemos la palabra",
    "hara uso de la palabra",
    "hará uso de la palabra",
]

# Frases que SOLO cuentan como anuncio si van seguidas de "diputado/a + nombre"
FRASES_ANUNCIO_CON_DIPUTADO = [
    "adelante",
]

# Si alguna de estas palabras aparece justo antes de la fórmula, es una
# pregunta o condición ("¿alguien desea hacer uso de la palabra?"), no un
# anuncio real de orador. Se comparan palabras COMPLETAS: "solicita" y
# "solicitar" excluyen, pero "solicitó" y "ha solicitado" NO (esas sí
# anteceden a una cesión real: "Solicitó la palabra el diputado X...").
PATRON_EXCLUSION = re.compile(
    r"\b(desea|desean|deseen|quisiera|quisieran|alguien|"
    r"solicita|solicitan|solicitar|"
    r"har[áa]n|hacen)\b")  # plurales de agenda: "harán uso de la palabra
                           # los diputados..." lista turnos, no cede aún

# Si un candidato a nombre EMPIEZA con uno de estos cargos, es una
# referencia por cargo ("la diputada Presidenta de la Comisión..."),
# no un nombre propio
ROLES_NO_NOMBRE = {
    "Presidente", "Presidenta", "Presidencia",
    "Secretario", "Secretaria", "Secretaría",
    "Vicepresidente", "Vicepresidenta",
    "Coordinador", "Coordinadora",
    "Gobernador", "Gobernadora",
}

# Si un candidato a nombre EMPIEZA con una de estas palabras, es el título
# de un documento o trámite legislativo ("...para dar lectura al Dictamen
# de la Iniciativa..."), no una persona
DOCUMENTOS_NO_NOMBRE = {
    "Dictamen", "Iniciativa", "Iniciativas", "Proyecto", "Punto", "Puntos",
    "Acuerdo", "Decreto", "Minuta", "Lectura", "Orden", "Ley", "Leyes",
    "Código", "Reglamento", "Constitución", "Informe", "Exhorto", "Oficio",
    "Acta", "Actas", "Artículo", "Título", "Capítulo", "Sesión", "Gaceta",
    "Grupo", "Partido", "Comisión", "Comisiones", "Junta", "Pleno",
}

# Frases con las que un orador suele terminar su participación
FRASES_CIERRE = ["es cuanto", "es cuánto"]

# Palabras que NO son nombres propios aunque empiecen con mayúscula
# (títulos, cargos y partidos políticos)
TITULOS_IGNORAR = {
    "Diputado", "Diputada", "Presidente", "Presidenta", "Presidencia",
    "Secretario", "Secretaria", "Secretaría", "Ciudadano", "Ciudadana",
    "Licenciado", "Licenciada", "Doctor", "Doctora", "Maestro", "Maestra",
    "Ingeniero", "Ingeniera", "Honorable", "Congreso", "Asamblea",
    "Mesa", "Directiva", "Estado", "Legislatura",
    "Morena", "PAN", "PRI", "PRD", "PT", "PVEM", "MC",
    "Movimiento", "Ciudadano", "Verde", "Trabajo",
}

CONECTORES = {"de", "del", "la", "las", "los", "y", "e"}

ORADOR_MESA = "Presidencia / Mesa Directiva"
ORADOR_DESCONOCIDO = "Desconocido"
ORADOR_SECRETARIA = "Secretaría"   # etiqueta provisional mientras la voz
                                   # (o el pase de lista) revela quién es

# Frases con las que la Presidencia RETOMA la palabra sin anuncio formal
# (deben aparecer AL INICIO del segmento). "Gracias, diputado/a" es la Mesa
# agradeciendo al orador; ojo: "gracias, presidenta" es al revés (el orador
# agradeciendo a la Mesa) y por eso NO está aquí.
PATRON_RETOMA_MESA = re.compile(
    r"^\s*(?:"
    r"(?:muchas\s+)?gracias[,.]?\s+(?:señora?\s+)?diputad"
    r"|(?:se\s+)?abr[oe]\s+la\s+discusi[oó]n"
    r"|consulto\s+a\s+l[ao]s"
    r"|pregunto\s+a\s+l[ao]s\s+diputad"
    r"|pido\s+a\s+la\s+secretar[ií]a"
    r"|solicito\s+a\s+la\s+secretar[ií]a"
    r"|procedemos\s+a"
    r"|se\s+somete\s+a\s+votaci[oó]n"
    r"|se\s+declara\s+la\s+existencia"
    r")", re.IGNORECASE)


def retoma_presidencia(texto):
    """True si el segmento EMPIEZA con lenguaje inequívoco de la Mesa
    retomando la conducción ('Muchas gracias, señor diputado. Abro la
    discusión...')."""
    return bool(PATRON_RETOMA_MESA.match(texto))


# Fórmulas con las que la SECRETARÍA toma la palabra para rendir un informe
# a la Presidencia ("Presidenta, informo que ha sido verificado...", "Le
# informo, diputada Presidenta, que existe quórum..."). Suelen ser frases
# cortas cuya huella de voz es ruidosa, así que el texto es la mejor
# evidencia del cambio de orador. Deben aparecer AL INICIO del segmento.
PATRON_TOMA_SECRETARIA = re.compile(
    r"^\s*(?:"
    r"(?:señora?\s+|diputad[oa]\s+)?president[ea][,.]?\s+(?:le\s+)?informo"
    r"|(?:le\s+)?informo(?:,?\s+(?:señora?\s+|diputad[oa]\s+)?president[ea]"
    r"|\s+a\s+la\s+presidencia)"
    r"|ha\s+sido\s+verificad[oa]"
    r"|se\s+ha\s+verificado"
    r")", re.IGNORECASE)


def toma_secretaria(texto):
    """True si el segmento EMPIEZA con una fórmula de informe de la
    Secretaría a la Presidencia."""
    return bool(PATRON_TOMA_SECRETARIA.match(texto))


# ---------------------------------------------------------------------------
# Modo "pase de lista / votación nominal": el secretario llama por nombre y
# el diputado responde con una frase corta ("presente", "a favor"...). Esa
# respuesta, imposible de identificar por voz en 1 segundo, se atribuye al
# diputado que acaba de ser llamado.
# ---------------------------------------------------------------------------

PATRON_LISTA_ON = re.compile(
    r"(pasar\s+lista|pase\s+de\s+lista|registro\s+de\s+asistencia"
    r"|verifiqu\w*\s+el\s+qu[oó]rum|verificaci[oó]n\s+del\s+qu[oó]rum"
    r"|votaci[oó]n\s+nominal|sentido\s+de\s+su\s+voto"
    r"|recabar\s+la\s+votaci[oó]n)", re.IGNORECASE)

PATRON_LISTA_OFF = re.compile(
    r"(existe\s+qu[oó]rum|qu[oó]rum\s+legal|se\s+declara\s+la\s+existencia"
    r"|se\s+abre\s+la\s+(?:reuni[oó]n|sesi[oó]n)|aprobad[oa]\s+por"
    r"|uso\s+de\s+la\s+palabra)", re.IGNORECASE)

# Nombre de diputado AL FINAL del segmento (así llama el secretario)
PATRON_LLAMADO = re.compile(
    r"[Dd]iputad[oae]\s+([A-ZÁÉÍÓÚÜÑ][\wáéíóúüñ]*"
    r"(?:\s+(?:de|del|la|las|los|y|e|[A-ZÁÉÍÓÚÜÑ][\wáéíóúüñ]*)){0,6})"
    r"\s*[.,;!?]?\s*$")

SEG_LLAMADO_VIGENTE = 12.0   # segundos de validez de un nombre llamado
MAX_LARGO_RESPUESTA = 30     # caracteres máximos de una respuesta de lista


def detectar_llamado(texto):
    """Si el segmento TERMINA con el nombre de un diputado del catálogo
    ('...diputado Carlos Antonio Martínez Zurita.'), devuelve su etiqueta
    oficial; si no, None. Solo se usa en modo pase de lista."""
    m = PATRON_LLAMADO.search(texto.strip())
    if not m:
        return None
    nombre = limpiar_nombre(m.group(1))
    if not nombre:
        return None
    ajustado = ajustar_al_catalogo(nombre)
    if CATALOGO and _normalizar(ajustado) in {_normalizar(c)
                                              for c in CATALOGO}:
        return etiqueta_orador(ajustado)
    return None

# Llamamos a yt-dlp a través de Python para no depender del PATH del sistema
# (evita el error "yt-dlp is not recognized" en Windows)
YTDLP = [sys.executable, "-m", "yt_dlp"]

# Nombre propio: palabras con mayúscula inicial, admitiendo conectores
# ("María del Carmen Pérez López", "Juan de la Cruz", etc.)
PATRON_NOMBRE = re.compile(
    r"[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+"
    r"(?:[\s\-](?:(?:de|del|la|las|los|y|e)\s){0,2}[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+){0,5}"
)
PATRON_DIPUTADO = re.compile(r"diputad[oae]s?", re.IGNORECASE)

PROMPT_BASE = (
    "Sesión del Congreso. Intervenciones de diputadas y diputados en español. "
)


# ---------------------------------------------------------------------------
# Detección de oradores
# ---------------------------------------------------------------------------

def limpiar_nombre(nombre):
    """Quita títulos al inicio y conectores sueltos al final. Si el
    candidato empieza con un cargo (Presidenta, Secretaria...), es una
    referencia por cargo y no un nombre: devuelve None."""
    palabras = nombre.replace("-", " ").split()
    if palabras and (palabras[0] in ROLES_NO_NOMBRE
                     or palabras[0] in DOCUMENTOS_NO_NOMBRE):
        return None
    while palabras and palabras[0] in TITULOS_IGNORAR:
        palabras.pop(0)
    while palabras and palabras[-1].lower() in CONECTORES:
        palabras.pop()
    if not palabras:
        return None
    if len(palabras) == 1 and len(palabras[0]) < 3:
        return None
    return " ".join(palabras[:6])


def detectar_nuevo_orador(texto):
    """Si el texto contiene una fórmula tipo 'tiene el uso de la palabra la
    diputada Fulana de Tal' o 'adelante diputado Fulano', devuelve el nombre
    detectado; si no, None. Ignora preguntas retóricas como '¿alguien desea
    hacer uso de la palabra?'."""
    t = texto.lower()
    candidatas = ([(f, False) for f in FRASES_ANUNCIO]
                  + [(f, True) for f in FRASES_ANUNCIO_CON_DIPUTADO])
    for frase, requiere_dip in candidatas:
        p = t.find(frase)
        if p == -1:
            continue
        # ¿Es una pregunta o condición, no un anuncio real?
        previo = t[max(0, p - 35):p]
        if PATRON_EXCLUSION.search(previo):
            continue

        # Ventana de texto justo después de la fórmula
        pos = p + len(frase)
        ventana = texto[pos:pos + 140]

        # ¿Aparece "diputado/diputada"? (obligatorio para frases estrictas
        # como "adelante", y debe estar pegado a la fórmula)
        m_dip = PATRON_DIPUTADO.search(ventana[:40] if requiere_dip
                                       else ventana)
        if requiere_dip and not m_dip:
            continue
        zona = ventana[m_dip.end():] if m_dip else ventana

        # Reunir TODOS los candidatos a nombre dentro de la ventana
        candidatos = []
        for m_nombre in PATRON_NOMBRE.finditer(zona):
            nombre = limpiar_nombre(m_nombre.group(0))
            if nombre:
                candidatos.append((m_nombre.start(), nombre))
        if not candidatos:
            continue
        # 1) Si alguno coincide con el catálogo de diputados, ese gana.
        #    Resuelve casos como "...la diputada Presidenta de la Comisión
        #    de Salud, Jennifer González": el cargo se descarta y el nombre
        #    real, aunque venga lejos, sí está en el catálogo.
        if CATALOGO:
            oficiales = {_normalizar(c) for c in CATALOGO}
            for _, nombre in candidatos:
                ajustado = ajustar_al_catalogo(nombre)
                if _normalizar(ajustado) in oficiales:
                    return ajustado
        # 2) Sin catálogo o sin coincidencia: el primer candidato, y solo
        #    si viene pegado al anuncio (si está lejos, es otra palabra
        #    con mayúscula: un artículo de ley, un lugar, etc.)
        pos, nombre = candidatos[0]
        limite = 25 if m_dip else 45
        if pos <= limite:
            return nombre
    return None


def hay_cierre(texto):
    t = texto.lower()
    return any(f in t for f in FRASES_CIERRE)


# ---------------------------------------------------------------------------
# Catálogo de diputados (archivo diputados.txt, un nombre por línea)
# ---------------------------------------------------------------------------

CATALOGO = []


def _normalizar(texto):
    """minúsculas y sin acentos, para comparar nombres"""
    t = unicodedata.normalize("NFD", texto)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return t.lower()


def cargar_catalogo():
    base = os.path.dirname(os.path.abspath(__file__))
    for ruta in (os.path.join(base, "diputados.txt"), "diputados.txt"):
        if os.path.isfile(ruta):
            with open(ruta, encoding="utf-8") as f:
                return [ln.strip() for ln in f
                        if ln.strip() and not ln.strip().startswith("#")]
    return []


def ajustar_al_catalogo(nombre):
    """Si existe diputados.txt, corrige el nombre detectado al nombre oficial
    más parecido (arregla apellidos mal transcritos o incompletos)."""
    if not CATALOGO:
        return nombre
    objetivo = _normalizar(nombre)
    # 1) ¿Las palabras detectadas caben dentro de un único nombre del catálogo?
    #    (ej. "María Pérez" -> "María del Carmen Pérez López")
    palabras = set(objetivo.split())
    contenido = [c for c in CATALOGO
                 if palabras and palabras <= set(_normalizar(c).split())]
    if len(contenido) == 1:
        return contenido[0]
    # 2) Parecido general (tolera errores de transcripción de Whisper)
    normales = {_normalizar(c): c for c in CATALOGO}
    cerca = difflib.get_close_matches(objetivo, list(normales.keys()),
                                      n=1, cutoff=0.72)
    if cerca:
        return normales[cerca[0]]
    return nombre


def etiqueta_orador(nombre):
    """'Dip. Nombre' si está en el catálogo (o si no hay catálogo);
    el nombre tal cual si es un orador externo (Gobernadora, invitados,
    funcionarios que comparecen)."""
    if CATALOGO and _normalizar(nombre) not in {_normalizar(c)
                                                for c in CATALOGO}:
        return nombre
    return "Dip. " + nombre


# ---------------------------------------------------------------------------
# Reconocimiento de voz en vivo (opcional, requiere --voz)
# ---------------------------------------------------------------------------
# Cada segmento transcrito recibe una huella de voz. Los segmentos con la
# misma voz se agrupan en "tramos"; cuando la voz cambia (o el protocolo
# anuncia a otro orador), el tramo se cierra y se identifica contra las
# huellas conocidas. Así se detectan cambios de orador aunque no haya
# anuncio formal, y se corrigen incluso tramos mal atribuidos.

MIN_VOZ_FLUSH = 2.0        # segundos mínimos de voz para intentar identificar
MARGEN_MIN_VOZ = 0.05      # ventaja mínima del 1er sobre el 2o candidato
SEG_MIN_EMB = 0.8          # duración mínima de un segmento para sacar huella
SEG_MIN_CAMBIO = 2.0       # duración mínima de un segmento para poder
                           # DECLARAR un cambio de voz: las huellas de
                           # fragmentos de ~1 s son ruidosas y provocaban
                           # cortes falsos a media frase
MAX_SEG_HUERFANO = 3.0     # micro-tramos "Desconocido" de hasta esta
                           # duración se reabsorben si el mismo orador
                           # habla antes y después (efecto sándwich)
EXTRA_SI_ANUNCIADO = 0.05  # exigencia adicional para contradecir un anuncio
                           # formal (el protocolo es evidencia fuerte)


def nuevo_tramo_voz():
    """Un tramo agrupa segmentos consecutivos con la misma voz."""
    return {"suma": None, "n": 0, "peso": 0.0, "ids": [], "segundos": 0.0,
            "ini": None, "fin": None, "etiqueta": None, "resuelto": False,
            "protegido": False}


def huella_promedio(tramo):
    if tramo["n"] == 0 or tramo.get("peso", 0) <= 0:
        return None
    m = tramo["suma"] / tramo["peso"]
    n = np.linalg.norm(m)
    return m / n if n else m


def agregar_a_tramo(tramo, emb, seg_ini, seg_fin, row_id):
    """Suma un segmento al tramo. 'emb' puede ser None (segmento muy corto
    para sacar huella): igual se agrupa, pero no aporta a la voz promedio.
    Las huellas se ponderan por duración: un fragmento de 1 s ruidoso pesa
    poco frente a los segmentos largos y limpios."""
    tramo["ids"].append(row_id)
    tramo["segundos"] += max(0.0, seg_fin - seg_ini)
    if tramo["ini"] is None:
        tramo["ini"] = seg_ini
    tramo["fin"] = seg_fin
    if emb is not None:
        dur = max(0.2, seg_fin - seg_ini)
        vec = np.asarray(emb, dtype=np.float32) * dur
        if tramo["suma"] is None:
            tramo["suma"] = vec.copy()
        else:
            tramo["suma"] += vec
        tramo["peso"] = tramo.get("peso", 0.0) + dur
        tramo["n"] += 1


def es_cambio_de_voz(tramo, emb, umbral):
    """True si la huella del nuevo segmento se parece poco a la voz
    promedio del tramo en curso: habló alguien más."""
    if emb is None or tramo["n"] < 1:
        return False
    prom = huella_promedio(tramo)
    return float(np.dot(prom, emb)) < umbral


def identificar_huella(emb, nombres, matriz, umbral,
                       margen_min=MARGEN_MIN_VOZ):
    """Compara una huella contra los perfiles. Devuelve siempre el mejor
    candidato con su similitud y margen; la clave 'fuerte' indica si supera
    el umbral con ventaja clara sobre el segundo lugar."""
    if emb is None:
        return None
    sims = matriz @ emb
    orden = np.argsort(sims)[::-1]
    mejor = orden[0]
    segundo = orden[1] if len(orden) > 1 else orden[0]
    sim = float(sims[mejor])
    margen = float(sims[mejor] - sims[segundo])
    return {"candidato": nombres[mejor], "similitud": sim, "margen": margen,
            "fuerte": (sim >= umbral and margen >= margen_min)}


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def dividir_por_silencios(segmentos, pausa):
    """Whisper a veces entrega en UN solo segmento dos frases separadas por
    un silencio largo ('Ábrase el sistema electrónico... [3 min] Presidenta,
    informo que ha sido verificado'). Ese silencio interno es invisible para
    el corte por pausa y para las fórmulas de inicio de segmento, y además
    contamina la huella de voz (la rebanada incluye el silencio y mezcla dos
    voces). Con word_timestamps activado, aquí se parte cada segmento en la
    frontera de todo hueco entre palabras >= `pausa` segundos."""
    for s in segmentos:
        palabras = getattr(s, "words", None) or []
        if len(palabras) < 2:
            yield s
            continue
        grupos, grupo = [], [palabras[0]]
        for w in palabras[1:]:
            if (w.start is not None and grupo[-1].end is not None
                    and w.start - grupo[-1].end >= pausa):
                grupos.append(grupo)
                grupo = [w]
            else:
                grupo.append(w)
        grupos.append(grupo)
        if len(grupos) == 1:
            yield s
            continue
        for g in grupos:
            texto = "".join(w.word for w in g).strip()
            if texto:
                yield SimpleNamespace(start=g[0].start, end=g[-1].end,
                                      text=texto)


def hms(segundos):
    segundos = int(segundos)
    h, r = divmod(segundos, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def verificar_dependencias():
    faltan = []
    if shutil.which("ffmpeg") is None:
        faltan.append("ffmpeg (instálalo y agrégalo al PATH; ver README)")
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        faltan.append("yt-dlp (pip install yt-dlp)")
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        faltan.append("faster-whisper (pip install faster-whisper)")
    if faltan:
        print("FALTAN DEPENDENCIAS:")
        for f in faltan:
            print("  -", f)
        sys.exit(1)


def obtener_titulo(url):
    try:
        r = subprocess.run(
            YTDLP + ["--skip-download", "--no-warnings",
                     "--print", "%(title)s", url],
            capture_output=True, text=True, timeout=25,
        )
        titulo = r.stdout.strip().splitlines()
        return titulo[0] if titulo else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Captura del audio en vivo (yt-dlp -> ffmpeg -> bloques WAV)
# ---------------------------------------------------------------------------

def iniciar_captura(url, carpeta_audio, segundos_bloque):
    """Lanza la tubería de captura y devuelve (proceso_ytdlp, proceso_ffmpeg)."""
    patron_salida = os.path.join(carpeta_audio, "bloque_%06d.wav")

    p_ytdlp = subprocess.Popen(
        YTDLP + [
            "-q", "--no-warnings", "--no-part",
            "--hls-use-mpegts",          # clave para transmisiones EN VIVO
            "--wait-for-video", "30",    # si aún no empieza, reintenta cada 30 s
            "-f", "bestaudio/best",
            "-o", "-",                   # audio hacia la tubería
            url,
        ],
        stdout=subprocess.PIPE,
    )
    p_ffmpeg = subprocess.Popen(
        [
            "ffmpeg", "-loglevel", "error",
            "-i", "pipe:0",
            "-ac", "1", "-ar", "16000",  # mono, 16 kHz (lo que Whisper usa)
            "-f", "segment",
            "-segment_time", str(segundos_bloque),
            "-reset_timestamps", "1",
            patron_salida,
        ],
        stdin=p_ytdlp.stdout,
    )
    p_ytdlp.stdout.close()
    return p_ytdlp, p_ffmpeg


def indice_de_bloque(ruta):
    m = re.search(r"bloque_(\d+)\.wav$", os.path.basename(ruta))
    return int(m.group(1)) if m else 0


# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------

def abrir_db(ruta):
    con = sqlite3.connect(ruta)
    con.execute("""
        CREATE TABLE IF NOT EXISTS sesiones (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            url     TEXT,
            titulo  TEXT,
            inicio  TEXT,
            fin     TEXT
        )""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS participaciones (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            sesion_id   INTEGER REFERENCES sesiones(id),
            orador      TEXT,
            inicio_seg  REAL,
            fin_seg     REAL,
            inicio_hms  TEXT,
            fin_hms     TEXT,
            texto       TEXT
        )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_part_sesion "
                "ON participaciones(sesion_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_part_orador "
                "ON participaciones(orador)")
    con.commit()
    return con


# ---------------------------------------------------------------------------
# Programa principal
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Transcripción casi en tiempo real de sesiones "
                    "legislativas transmitidas por YouTube.")
    ap.add_argument("url", help="URL del video o transmisión en vivo")
    ap.add_argument("--modelo", default="small",
                    choices=["tiny", "base", "small", "medium", "large-v3"],
                    help="Tamaño del modelo Whisper (default: small)")
    ap.add_argument("--bloque", type=int, default=30,
                    help="Segundos de audio por bloque (default: 30)")
    ap.add_argument("--db", default="sesiones.db",
                    help="Ruta de la base de datos SQLite (default: sesiones.db)")
    ap.add_argument("--dispositivo", default="auto",
                    choices=["auto", "cpu", "cuda"],
                    help="Dónde correr el modelo (default: auto)")
    ap.add_argument("--conservar-audio", action="store_true",
                    help="No borrar los bloques WAV ya transcritos")
    ap.add_argument("--voz", action="store_true",
                    help="identificar por huella de voz los bloques sin "
                         "orador anunciado (requiere haber generado antes "
                         "las huellas con voz.py; ver README)")
    ap.add_argument("--perfiles", default="voces_perfiles.json",
                    help="archivo de huellas de voz "
                         "(default: voces_perfiles.json)")
    ap.add_argument("--umbral-voz", type=float, default=0.75,
                    help="similitud mínima para aceptar una identificación "
                         "por voz, 0-1 (default: 0.75)")
    ap.add_argument("--umbral-cambio-voz", type=float, default=0.50,
                    help="similitud mínima entre segmentos para considerar "
                         "que es la misma voz; por debajo se marca cambio "
                         "de orador (default: 0.50; bájalo si corta de más, "
                         "súbelo si no detecta cambios)")
    ap.add_argument("--pausa-voz", type=float, default=2.0,
                    help="pausa (en segundos) que cierra el tramo de voz en "
                         "curso; lo que siga se identifica por su cuenta "
                         "(default: 2.0)")
    args = ap.parse_args()

    # Consola en UTF-8 (evita errores de acentos en Windows)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    verificar_dependencias()

    CATALOGO[:] = cargar_catalogo()
    if CATALOGO:
        print(f"Catálogo cargado: {len(CATALOGO)} diputados (diputados.txt)")
    else:
        print("Sin catálogo (opcional): crea diputados.txt con un nombre "
              "por línea para corregir nombres automáticamente.")

    # Carpeta de trabajo de esta sesión
    marca = datetime.now().strftime("%Y%m%d_%H%M")
    carpeta = os.path.join("sesiones_en_vivo", f"sesion_{marca}")
    carpeta_audio = os.path.join(carpeta, "audio")
    os.makedirs(carpeta_audio, exist_ok=True)
    ruta_txt = os.path.join(carpeta, "transcripcion_en_vivo.txt")

    print("Obteniendo información del video...")
    titulo = obtener_titulo(args.url) or args.url

    print(f"Cargando modelo Whisper '{args.modelo}' "
          "(la primera vez se descarga, puede tardar)...")
    from faster_whisper import WhisperModel
    modelo = WhisperModel(args.modelo, device=args.dispositivo,
                          compute_type="auto")

    con = abrir_db(args.db)
    cur = con.execute(
        "INSERT INTO sesiones (url, titulo, inicio) VALUES (?, ?, ?)",
        (args.url, titulo, datetime.now().isoformat(timespec="seconds")))
    sesion_id = cur.lastrowid
    con.commit()

    # ---- Reconocimiento de voz (opcional) ----
    nombres_voz = matriz_voz = encoder_voz = None
    if args.voz:
        import voz as vz
        import numpy as np
        perfiles_voz = vz.cargar_perfiles(args.perfiles)
        if not perfiles_voz:
            print(f"[voz] No hay huellas en {args.perfiles}; la "
                  "identificación por voz se desactiva para esta sesión. "
                  "Genera huellas con voz.py primero (ver README).")
            args.voz = False
        else:
            print("Cargando modelo de huella de voz "
                  "(la primera vez puede tardar)...")
            encoder_voz = vz.obtener_encoder()
            nombres_voz = list(perfiles_voz.keys())
            matriz_voz = np.stack(
                [np.asarray(perfiles_voz[n]["embedding"], dtype=np.float32)
                 for n in nombres_voz])
            vz.asegurar_columnas_voz(con)
            print(f"[voz] {len(nombres_voz)} huellas cargadas; se vigilarán "
                  "los cambios de voz en todo el audio.")

    print(f"\nSesión #{sesion_id}: {titulo}")
    print(f"Base de datos : {os.path.abspath(args.db)}")
    print(f"Texto en vivo : {os.path.abspath(ruta_txt)}")
    print("Conectando a la transmisión... (Ctrl+C para detener)\n")

    p_ytdlp, p_ffmpeg = iniciar_captura(args.url, carpeta_audio, args.bloque)

    # ---- Estado de la transcripción ----
    estado = {
        "orador": ORADOR_MESA,      # quién habla ahora
        "pendiente": None,          # orador anunciado, aplica al sig. segmento
        "volver_mesa": False,       # tras "es cuanto" regresa la Presidencia
        "ultimo_impreso": None,
        "contexto": "",             # cola de texto para dar continuidad
        "stats": {},                # orador -> [segundos, num_segmentos]
        "voz": nuevo_tramo_voz(),   # tramo de voz en curso, para --voz
        "protegido": False,         # la etiqueta actual viene del protocolo
        "voz_ultimo_fin": None,     # fin del último segmento con voz
        "modo_lista": False,        # pase de lista / votación nominal
        "llamado": None,            # diputado recién llamado por nombre
        "llamado_hasta": 0.0,       # vigencia del llamado (seg de sesión)
        "secretario_lista": None,   # quién está pasando la lista
        "secretaria": None,         # identidad de la Secretaría de la
                                    # sesión, en cuanto se conozca
        "prev_resuelto": None,      # identidad del último tramo confiable
        "huerfanos": [],            # ids de un micro-Desconocido en espera
    }
    archivo_txt = open(ruta_txt, "a", encoding="utf-8")

    def emitir(orador, ini, fin, texto):
        """Imprime, escribe al .txt y guarda en la base de datos."""
        if orador != estado["ultimo_impreso"]:
            linea = f"\n[{hms(ini)}] >>> {orador}"
            print(linea)
            archivo_txt.write(linea + "\n")
            estado["ultimo_impreso"] = orador
        print("   " + texto)
        archivo_txt.write("   " + texto + "\n")
        archivo_txt.flush()
        cur = con.execute(
            "INSERT INTO participaciones "
            "(sesion_id, orador, inicio_seg, fin_seg, inicio_hms, fin_hms, texto) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sesion_id, orador, round(ini, 2), round(fin, 2),
             hms(ini), hms(fin), texto))
        s = estado["stats"].setdefault(orador, [0.0, 0])
        s[0] += max(fin - ini, 0)
        s[1] += 1
        return cur.lastrowid

    def calcular_huella_segmento(frag):
        """Huella de voz de un segmento individual, o None si es muy corto
        o el audio no sirve."""
        if frag is None or len(frag) < vz.FRECUENCIA * SEG_MIN_EMB:
            return None
        try:
            limpio = vz.preprocesar(frag)
            if len(limpio) < int(vz.FRECUENCIA * 0.6):
                return None
            emb = np.asarray(encoder_voz.embed_utterance(limpio),
                             dtype=np.float32)
        except Exception:
            return None
        n = np.linalg.norm(emb)
        return emb / n if n else emb

    def evaluar_tramo_voz(cerrar):
        """Identifica la voz del tramo en curso. Guarda siempre la evidencia
        (columnas voz_*); si la coincidencia es fuerte y contradice la
        etiqueta actual, corrige esos segmentos en la base de datos y, si el
        tramo sigue vivo, también al orador en curso (hacia adelante). Con
        cerrar=True, además arranca un tramo nuevo."""
        tramo = estado["voz"]
        try:
            if tramo["resuelto"] or tramo["n"] == 0 or not tramo["ids"]:
                return
            r = identificar_huella(huella_promedio(tramo), nombres_voz,
                                   matriz_voz, args.umbral_voz)
            if r is None:
                return
            etiqueta_nueva = etiqueta_orador(r["candidato"])
            etiqueta_actual = tramo["etiqueta"] or ORADOR_MESA
            marcas = ",".join("?" * len(tramo["ids"]))
            # Evidencia siempre (visible en revisar.py aunque sea débil)
            con.execute(
                f"UPDATE participaciones SET voz_orador=?, voz_similitud=? "
                f"WHERE id IN ({marcas})",
                [etiqueta_nueva, round(r["similitud"], 3)] + tramo["ids"])
            # Para contradecir una etiqueta respaldada por el protocolo se
            # exige más confianza que para bautizar un tramo sin respaldo
            exigencia = args.umbral_voz + (
                EXTRA_SI_ANUNCIADO if tramo["protegido"] else 0.0)
            if r["fuerte"] and r["similitud"] >= exigencia \
                    and etiqueta_nueva != etiqueta_actual:
                con.execute(
                    f"UPDATE participaciones SET orador=? "
                    f"WHERE id IN ({marcas})",
                    [etiqueta_nueva] + tramo["ids"])
                if etiqueta_actual == ORADOR_DESCONOCIDO:
                    aviso = (f"\n[voz] {hms(tramo['ini'])}–"
                             f"{hms(tramo['fin'])}: identificado: "
                             f"{etiqueta_nueva} (similitud "
                             f"{r['similitud']:.2f}); corregido en la base "
                             "de datos.\n")
                else:
                    aviso = (f"\n[voz] {hms(tramo['ini'])}–"
                             f"{hms(tramo['fin'])}: la voz corresponde a "
                             f"{etiqueta_nueva} (similitud "
                             f"{r['similitud']:.2f}), no a "
                             f"\"{etiqueta_actual}\"; corregido en la base "
                             "de datos.\n")
                print(aviso)
                archivo_txt.write(aviso)
                archivo_txt.flush()
                segs = tramo["segundos"]
                sa = estado["stats"].get(etiqueta_actual)
                if sa:
                    sa[0] = max(0.0, sa[0] - segs)
                sn = estado["stats"].setdefault(etiqueta_nueva, [0.0, 0])
                sn[0] += segs
                sn[1] += 1
                tramo["etiqueta"] = etiqueta_nueva
                tramo["resuelto"] = True
                # Si el tramo corregido era la "Secretaría" genérica, ya
                # sabemos quién es la Secretaria de esta sesión: los
                # próximos informes se le atribuyen directo por nombre
                if etiqueta_actual == ORADOR_SECRETARIA:
                    estado["secretaria"] = etiqueta_nueva
                    print(f"   [voz] Secretaría identificada: "
                          f"{etiqueta_nueva}")
                # Hacia adelante: si el tramo sigue vivo y el orador en
                # curso era el corregido, lo que sigue ya sale a su nombre
                if not cerrar and estado["orador"] == etiqueta_actual:
                    estado["orador"] = etiqueta_nueva
                    estado["protegido"] = True  # identidad respaldada por voz
                    estado["ultimo_impreso"] = None  # reimprime encabezado
            elif r["fuerte"] and etiqueta_nueva == etiqueta_actual:
                tramo["resuelto"] = True  # la voz confirma la etiqueta
                if not cerrar and estado["orador"] == etiqueta_actual:
                    estado["protegido"] = True
            elif cerrar and not tramo["protegido"] \
                    and etiqueta_actual != ORADOR_DESCONOCIDO \
                    and tramo["segundos"] >= MIN_VOZ_FLUSH:
                # Tramo sin respaldo del protocolo que la voz no pudo
                # identificar con confianza: queda como Desconocido para
                # que el administrador lo reclasifique en revisar.py
                con.execute(
                    f"UPDATE participaciones SET orador=? "
                    f"WHERE id IN ({marcas})",
                    [ORADOR_DESCONOCIDO] + tramo["ids"])
                aviso = (f"\n[voz] {hms(tramo['ini'])}–{hms(tramo['fin'])}: "
                         f"voz no identificada con confianza (lo más "
                         f"parecido: {etiqueta_nueva}, similitud "
                         f"{r['similitud']:.2f}); marcado como "
                         f"\"{ORADOR_DESCONOCIDO}\" para revisión.\n")
                print(aviso)
                archivo_txt.write(aviso)
                archivo_txt.flush()
                segs = tramo["segundos"]
                sa = estado["stats"].get(etiqueta_actual)
                if sa:
                    sa[0] = max(0.0, sa[0] - segs)
                sn = estado["stats"].setdefault(ORADOR_DESCONOCIDO, [0.0, 0])
                sn[0] += segs
                sn[1] += 1
                tramo["etiqueta"] = ORADOR_DESCONOCIDO
            con.commit()
        finally:
            if cerrar:
                # --- Sanador de sándwiches ---
                # Si este tramo tiene identidad confiable y coincide con la
                # del tramo anterior al micro-Desconocido pendiente, ese
                # micro-tramo era un corte falso a media frase: se reabsorbe.
                etiqueta_final = tramo["etiqueta"] or ORADOR_MESA
                confiable = tramo["resuelto"] or tramo["protegido"]
                if confiable and etiqueta_final != ORADOR_DESCONOCIDO:
                    if estado["huerfanos"] \
                            and etiqueta_final == estado["prev_resuelto"]:
                        marcas_h = ",".join("?" * len(estado["huerfanos"]))
                        con.execute(
                            f"UPDATE participaciones SET orador=? "
                            f"WHERE id IN ({marcas_h})",
                            [etiqueta_final] + estado["huerfanos"])
                        con.commit()
                        print(f"   [voz] micro-tramo reabsorbido: "
                              f"{len(estado['huerfanos'])} segmento(s) "
                              f"devueltos a {etiqueta_final}.")
                    estado["huerfanos"] = []
                    estado["prev_resuelto"] = etiqueta_final
                elif etiqueta_final == ORADOR_DESCONOCIDO \
                        and tramo["segundos"] <= MAX_SEG_HUERFANO \
                        and tramo["ids"]:
                    # micro-Desconocido: queda en espera por si el orador
                    # de antes reaparece justo después
                    estado["huerfanos"] = list(tramo["ids"])
                else:
                    estado["huerfanos"] = []
                    estado["prev_resuelto"] = None
                estado["voz"] = nuevo_tramo_voz()

    def procesar_bloque(ruta):
        idx = indice_de_bloque(ruta)
        offset = idx * args.bloque
        prompt = (PROMPT_BASE + estado["contexto"])[-800:]
        try:
            segmentos, _ = modelo.transcribe(
                ruta, language="es", vad_filter=True,
                beam_size=5, initial_prompt=prompt,
                word_timestamps=True)
        except Exception as e:
            print(f"   [aviso] no se pudo transcribir {ruta}: {e}")
            return
        # Partir los segmentos que traen un silencio largo adentro: así el
        # corte por pausa ve la frontera, las fórmulas de inicio de segmento
        # ("Presidenta, informo...") pueden dispararse y la huella de voz no
        # mezcla dos oradores en una misma rebanada
        segmentos = dividir_por_silencios(segmentos, args.pausa_voz)
        audio_bloque = None
        if args.voz:
            try:
                audio_bloque = vz.cargar_wav(ruta)
            except Exception as e:
                print(f"   [voz] aviso: no se pudo leer el audio de este "
                      f"bloque para identificación de voz ({e})")
        texto_bloque = []
        for s in segmentos:
            texto = s.text.strip()
            if not texto:
                continue
            # Aplicar cambios de orador decididos en el segmento anterior
            etiqueta_previa = estado["orador"]
            if estado["pendiente"]:
                estado["orador"] = estado["pendiente"]
                estado["pendiente"] = None
                estado["protegido"] = True   # respaldado por anuncio formal
            elif estado["volver_mesa"]:
                estado["orador"] = ORADOR_MESA
                estado["volver_mesa"] = False
                estado["protegido"] = False
            # La Presidencia retoma sin anuncio ("Muchas gracias, señor
            # diputado. Abro la discusión..."): aplica en ESTE segmento
            if estado["orador"] != ORADOR_MESA and retoma_presidencia(texto):
                estado["orador"] = ORADOR_MESA
                estado["protegido"] = False
            # La Secretaría rinde un informe a la Presidencia ("Presidenta,
            # informo que ha sido verificado..."): son frases cortas cuya
            # huella de voz es ruidosa, pero la fórmula es inequívoca. Se
            # cambia de orador por texto, aunque el segmento no alcance la
            # duración mínima para declarar cambio de voz
            if toma_secretaria(texto):
                sec = estado["secretaria"] or ORADOR_SECRETARIA
                if estado["orador"] != sec:
                    estado["orador"] = sec
                    # con identidad conocida es respaldo de protocolo; la
                    # etiqueta genérica queda abierta a que la voz la afine
                    estado["protegido"] = estado["secretaria"] is not None
            # Pase de lista / votación nominal: la respuesta corta va al
            # diputado llamado; llamar nombres o comentar largo es de quien
            # pasa la lista (el secretario)
            llamado_aqui = None
            if estado["modo_lista"]:
                llamado_aqui = detectar_llamado(texto)
                if estado["llamado"] \
                        and (offset + s.start) <= estado["llamado_hasta"] \
                        and len(texto) <= MAX_LARGO_RESPUESTA:
                    estado["orador"] = estado["llamado"]
                    estado["protegido"] = True   # respaldado por el llamado
                    estado["llamado"] = None
                elif estado["secretario_lista"] \
                        and (llamado_aqui
                             or len(texto) > MAX_LARGO_RESPUESTA):
                    estado["orador"] = estado["secretario_lista"]
                    estado["protegido"] = True
            # El protocolo cambió de orador: se cierra el tramo de voz
            # anterior y se evalúa antes de empezar el nuevo
            if args.voz and estado["orador"] != etiqueta_previa:
                evaluar_tramo_voz(cerrar=True)
            # ¿Este segmento anuncia a un nuevo orador?
            nombre = detectar_nuevo_orador(texto)
            if nombre:
                estado["pendiente"] = etiqueta_orador(
                    ajustar_al_catalogo(nombre))
                estado["orador"] = ORADOR_MESA  # quien anuncia es la Mesa
            if hay_cierre(texto):
                estado["volver_mesa"] = True
            row_id = emitir(estado["orador"], offset + s.start,
                            offset + s.end, texto)
            texto_bloque.append(texto)
            # Contabilidad del modo pase de lista / votación nominal
            t_min = texto.lower()
            if PATRON_LISTA_ON.search(t_min):
                if not estado["modo_lista"]:
                    estado["secretario_lista"] = None
                estado["modo_lista"] = True
            if estado["modo_lista"] and PATRON_LISTA_OFF.search(t_min):
                estado["modo_lista"] = False
                estado["llamado"] = None
                estado["secretario_lista"] = None
            if estado["modo_lista"]:
                if llamado_aqui is None:
                    llamado_aqui = detectar_llamado(texto)
                if llamado_aqui:
                    if estado["secretario_lista"] is None:
                        estado["secretario_lista"] = estado["orador"]
                        # quien pasa lista ES la Secretaría: si ya tiene
                        # nombre propio, se recuerda para los informes
                        if estado["orador"] not in (
                                ORADOR_MESA, ORADOR_DESCONOCIDO,
                                ORADOR_SECRETARIA):
                            estado["secretaria"] = estado["orador"]
                    estado["llamado"] = llamado_aqui
                    estado["llamado_hasta"] = (offset + s.end
                                               + SEG_LLAMADO_VIGENTE)
            # Seguimiento de voz: pausa larga o cambio de voz cierran el
            # tramo; el nuevo se identifica por su cuenta aunque arranque
            # con frases cortas
            if args.voz and audio_bloque is not None:
                ini_abs, fin_abs = offset + s.start, offset + s.end
                tramo = estado["voz"]
                # 1) Pausa prolongada: cierra el tramo; el que sigue hereda
                #    la etiqueta y su respaldo (pausar no cambia de persona)
                if tramo["ids"] and estado["voz_ultimo_fin"] is not None \
                        and ini_abs - estado["voz_ultimo_fin"] \
                        >= args.pausa_voz:
                    evaluar_tramo_voz(cerrar=True)
                    tramo = estado["voz"]
                frag = vz.rebanada(audio_bloque, s.start, s.end)
                emb = calcular_huella_segmento(frag)
                # 2) Cambio de voz: solo un segmento con duración decente
                #    puede declararlo (los de ~1 s dan huellas ruidosas y
                #    cortaban a media frase). El segmento que delató el
                #    cambio se atribuye al diputado llamado (si hay pase de
                #    lista en curso) o pasa a "Desconocido", y se verifica
                #    sin demora a quién corresponde la voz nueva
                if emb is not None \
                        and (fin_abs - ini_abs) >= SEG_MIN_CAMBIO \
                        and es_cambio_de_voz(
                            tramo, emb, args.umbral_cambio_voz):
                    evaluar_tramo_voz(cerrar=True)
                    tramo = estado["voz"]
                    anterior = estado["orador"]
                    if estado["modo_lista"] and estado["llamado"] \
                            and ini_abs <= estado["llamado_hasta"]:
                        nuevo = estado["llamado"]
                        estado["llamado"] = None
                        estado["protegido"] = True
                    else:
                        nuevo = ORADOR_DESCONOCIDO
                        estado["protegido"] = False
                    con.execute(
                        "UPDATE participaciones SET orador=? WHERE id=?",
                        (nuevo, row_id))
                    dur_seg = fin_abs - ini_abs
                    sa = estado["stats"].get(anterior)
                    if sa:
                        sa[0] = max(0.0, sa[0] - dur_seg)
                        sa[1] = max(0, sa[1] - 1)
                    sd = estado["stats"].setdefault(nuevo, [0.0, 0])
                    sd[0] += dur_seg
                    sd[1] += 1
                    estado["orador"] = nuevo
                    estado["ultimo_impreso"] = None
                    if nuevo == ORADOR_DESCONOCIDO:
                        print(f"\n[voz] {hms(ini_abs)}: cambio de voz "
                              "detectado; verificando a quién "
                              "corresponde...")
                    else:
                        print(f"\n[voz] {hms(ini_abs)}: cambio de voz; "
                              f"atribuido por pase de lista a {nuevo}.")
                if tramo["etiqueta"] is None:
                    tramo["etiqueta"] = estado["orador"]
                    tramo["protegido"] = estado["protegido"]
                agregar_a_tramo(tramo, emb, ini_abs, fin_abs, row_id)
                estado["voz_ultimo_fin"] = fin_abs
                # Verificación inmediata: en cuanto el tramo tiene huella se
                # intenta identificar, y se reintenta con cada segmento
                # nuevo hasta lograrlo o hasta que el tramo cierre
                if not tramo["resuelto"] and tramo["n"] >= 1:
                    evaluar_tramo_voz(cerrar=False)
        estado["contexto"] = (estado["contexto"] + " "
                              + " ".join(texto_bloque))[-300:]
        con.commit()
        if not args.conservar_audio:
            try:
                os.remove(ruta)
            except OSError:
                pass

    def terminar_captura():
        for p in (p_ffmpeg, p_ytdlp):
            if p.poll() is None:
                p.terminate()
        time.sleep(2)
        for p in (p_ffmpeg, p_ytdlp):
            if p.poll() is None:
                p.kill()

    # ---- Bucle principal ----
    procesados = set()
    deteniendo = False
    try:
        while True:
            try:
                bloques = sorted(glob.glob(
                    os.path.join(carpeta_audio, "bloque_*.wav")))
                captura_viva = (p_ffmpeg.poll() is None) and not deteniendo
                # El último bloque puede estar aún escribiéndose
                listos = bloques[:-1] if captura_viva else bloques
                nuevos = [b for b in listos if b not in procesados]
                if not nuevos:
                    if not captura_viva:
                        break  # transmisión terminada y todo procesado
                    time.sleep(2)
                    continue
                for b in nuevos:
                    procesar_bloque(b)
                    procesados.add(b)
            except KeyboardInterrupt:
                if not deteniendo:
                    deteniendo = True
                    print("\n[Ctrl+C] Deteniendo captura; procesando lo "
                          "pendiente... (Ctrl+C de nuevo para salir ya)")
                    terminar_captura()
                else:
                    print("\nSalida inmediata.")
                    break
    finally:
        terminar_captura()
        if args.voz:
            evaluar_tramo_voz(cerrar=True)  # el último tramo sin resolver
        con.execute("UPDATE sesiones SET fin = ? WHERE id = ?",
                    (datetime.now().isoformat(timespec="seconds"), sesion_id))
        con.commit()
        archivo_txt.close()

        # ---- Resumen ----
        print("\n" + "=" * 60)
        print("RESUMEN DE LA SESIÓN")
        print("=" * 60)
        orden = sorted(estado["stats"].items(),
                       key=lambda kv: kv[1][0], reverse=True)
        for orador, (seg, n) in orden:
            print(f"  {orador:<45} {hms(seg)}  ({n} segmentos)")
        print(f"\nTodo quedó guardado en: {os.path.abspath(args.db)} "
              f"(sesión #{sesion_id})")
        print(f"Transcripción en texto: {os.path.abspath(ruta_txt)}")
        con.close()


if __name__ == "__main__":
    main()
