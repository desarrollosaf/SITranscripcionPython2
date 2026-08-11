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

Máxima precisión (2ª pasada con retraso de ~8 s; requiere haber generado
las huellas con voz.py):
    python transcribir_en_vivo.py "URL" --voz --corrector

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
    # 'quórum' sale mal transcrito con frecuencia (cuoro, córum, cuórum...)
    r"|verifi\w*\s+(?:el\s+|del\s+)?(?:qu[oó]ru?m?|cu[oó]ru?m?o?|c[oó]rum)"
    r"|verificaci[oó]n\s+del\s+qu[oó]rum"
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
MAX_LARGO_LLAMADO = 80       # caracteres máximos de un segmento para que
                             # el nombre a su final cuente como LLAMADO de
                             # lista; más largo = lectura de un listado

# Una respuesta de pase de lista / votación nominal tiene forma conocida
# ("presente", "a favor", "en contra"...). Exigirla evita que cualquier
# texto corto tras la mención de un nombre (p. ej. la mitad de un apellido
# cortado por Whisper, o "la Comisión" al leer integraciones) se atribuya
# falsamente al diputado mencionado.
PATRON_RESPUESTA_LISTA = re.compile(
    r"^\s*(?:s[ií]|presente|presenta|a\s+favor|en\s+contra"
    r"|abstenci[oó]n|me\s+abstengo|por\s+la\s+afirmativa"
    r"|por\s+la\s+negativa|aqu[ií](?:\s+estoy)?|de\s+acuerdo)"
    r"[\s.,;!]*$", re.IGNORECASE)


def es_respuesta_lista(texto):
    """True si el texto tiene la forma de una respuesta de pase de lista o
    votación nominal."""
    return (len(texto) <= MAX_LARGO_RESPUESTA
            and bool(PATRON_RESPUESTA_LISTA.match(texto)))


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
    "Sesión del Congreso. Se verifica el quórum, se pasa lista de "
    "asistencia, votación nominal, se somete a votación el dictamen de la "
    "iniciativa, tiene la palabra la diputada, hará uso de la tribuna, es "
    "cuanto. Intervenciones de diputadas y diputados en español. "
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
# Corrección difusa de la transcripción (errores de oído de Whisper)
# ---------------------------------------------------------------------------
# Whisper deforma palabras poco comunes: "quórum" sale como cuoro, kuoru,
# córum...; los nombres con grafías inusuales ("Gabriel Kalid Mohamed Báez")
# salen irreconocibles. Como los patrones del protocolo y el catálogo
# dependen de esas palabras, aquí se corrigen ANTES de cualquier detección
# (y quedan corregidas también en la transcripción guardada).

# Términos protocolarios que Whisper suele deformar. Solo palabras RARAS:
# corregir palabras comunes sería peligroso.
TERMINOS_ASR = ["quórum", "dictamen", "legislatura", "unanimidad"]

# Palabras reales del español que se parecen a algún término y JAMÁS deben
# tocarse aunque el parecido difuso supere el umbral
PALABRAS_INTOCABLES = {
    "coro", "foro", "cloro", "toro", "oro", "cuota", "cuero", "fuero",
    "curo", "curó", "duro", "muro", "puro", "quiero", "quema", "cuarto",
    "cuanto", "cuánto", "dictaron", "dictar", "dictado", "dictados",
    "certamen", "examen", "unidad", "univer", "universidad",
}

UMBRAL_TERMINO_ASR = 0.80   # parecido mínimo palabra <-> término (global)
UMBRAL_TERMINO_MARCO = 0.45  # dentro de un marco protocolario el contexto
                             # ya desambigua, se tolera mucha deformación
UMBRAL_NOMBRE_ASR = 0.80    # parecido mínimo secuencia <-> nombre oficial

_TERMINOS_NORM = [(_normalizar(t), t) for t in TERMINOS_ASR]
_PATRON_PALABRA = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{4,}")

# Marcos protocolarios donde la palabra que sigue solo puede ser "quórum":
# ahí se acepta cualquier deformación razonable (cuoro, kuoru, córum...)
_PATRON_MARCO_QUORUM = re.compile(
    r"((?:verifi\w*|existencia)\s+(?:el|del|de el)\s+|existe\s+)"
    r"([CcKkQq][\wáéíóúüñ]{3,7})")

_QUORUM_NORM = _normalizar("quórum")


def corregir_terminos(texto):
    """Corrige deformaciones de términos protocolarios. Dos niveles:
    (1) dentro de un marco protocolario ('verificar el cuoro', 'existe
    córum legal') el contexto desambigua y se tolera mucha deformación;
    (2) fuera de marco, solo deformaciones de parecido altísimo
    ('cuorum', 'kuorum', 'quorun'), nunca palabras reales protegidas."""
    def repl_marco(m):
        pal = m.group(2)
        n = _normalizar(pal)
        if n == _QUORUM_NORM:
            return m.group(0)
        if difflib.SequenceMatcher(None, n, _QUORUM_NORM).ratio() \
                >= UMBRAL_TERMINO_MARCO:
            q = "Quórum" if pal[0].isupper() else "quórum"
            return m.group(1) + q
        return m.group(0)
    texto = _PATRON_MARCO_QUORUM.sub(repl_marco, texto)

    def repl(m):
        palabra = m.group(0)
        n = _normalizar(palabra)
        if n in PALABRAS_INTOCABLES:
            return palabra
        for tn, t in _TERMINOS_NORM:
            if n == tn:
                return palabra   # ya está bien (con o sin acento raro)
            if difflib.SequenceMatcher(None, n, tn).ratio() \
                    >= UMBRAL_TERMINO_ASR:
                return t.capitalize() if palabra[0].isupper() else t
        return palabra
    return _PATRON_PALABRA.sub(repl, texto)


# Secuencia de palabras con mayúscula, tolerando conectores y las comas
# espurias que Whisper mete dentro de un nombre ("Gabriel Calid, Mohammed
# Baez")
PATRON_NOMBRE_LAX = re.compile(
    r"[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+"
    r"(?:(?:,?\s+(?:de|del|la|las|los|y|e)\s+|,?\s+)"
    r"[A-ZÁÉÍÓÚÜÑ][a-záéíóúüñ]+){1,7}")


def _mejor_del_catalogo(cand):
    """(nombre oficial más parecido, ratio). Si el candidato ya es un
    nombre del catálogo o un fragmento correcto de uno, devuelve
    (None, 1.0): no hay nada que corregir."""
    n = _normalizar(cand)
    mejor, ratio = None, 0.0
    for c in CATALOGO:
        nc = _normalizar(c)
        if n == nc or n in nc:
            return None, 1.0
        r_ = difflib.SequenceMatcher(None, n, nc).ratio()
        if r_ > ratio:
            mejor, ratio = c, r_
    return mejor, ratio


def formatear_nombre(oficial):
    """Nombre del catálogo en formato de texto corrido ('GABRIEL KALID
    MOHAMED BAEZ' -> 'Gabriel Kalid Mohamed Baez'), con conectores en
    minúscula. Si el catálogo ya viene en mayúsculas/minúsculas mixtas,
    se respeta tal cual."""
    if oficial != oficial.upper():
        return oficial            # el catálogo ya trae formato propio
    partes = []
    for w in oficial.split():
        wl = w.lower()
        partes.append(wl if wl in CONECTORES else wl.capitalize())
    return " ".join(partes)


# La sustitución por el nombre oficial puede dejar colgando la cola del
# nombre original ("...Zurita Trejo Trejo"). Solo se deduplican palabras
# que forman parte de algún nombre del catálogo: repetir "muy muy" o
# "que que" es legítimo y no se toca.
def _quitar_colas_duplicadas(texto):
    palabras_cat = {w for c in CATALOGO for w in _normalizar(c).split()
                    if len(w) >= 4 and w not in CONECTORES}
    def repl(m):
        return (m.group(1)
                if _normalizar(m.group(1)) in palabras_cat
                else m.group(0))
    return re.sub(r"\b([\wáéíóúüñÁÉÍÓÚÜÑ]{4,})(?:\s+\1\b)+",
                  repl, texto, flags=re.IGNORECASE)


def _primer_nombre_compatible(cand, oficial):
    """True si la primera palabra del candidato se parece a la primera del
    nombre oficial. Evita que una secuencia con un nombre EXTRA al frente
    ('Héctor Susana Estrada Rojas', producto de audio perdido entre dos
    personas) se empareje con un nombre más corto del catálogo borrando
    palabras ('Susana Estrada Rojas')."""
    p_c = _normalizar(cand.split()[0])
    p_o = _normalizar(oficial.split()[0])
    return (p_c == p_o
            or difflib.SequenceMatcher(None, p_c, p_o).ratio() >= 0.5)


def corregir_nombres(texto):
    """Corrige nombres de diputados mal transcritos hacia su forma oficial
    del catálogo ('Gabriel Calid, Mohammed Baez' -> 'Gabriel Kalid Mohamed
    Báez'). No toca nombres ya correctos ni nombres de personas externas
    (invitados, funcionarios) que no se parezcan lo suficiente a nadie del
    catálogo."""
    if not CATALOGO:
        return texto

    def repl(m):
        cand = m.group(0)
        palabras = cand.split()
        prefijo = []
        while palabras and palabras[0].rstrip(",") in TITULOS_IGNORAR:
            prefijo.append(palabras.pop(0))
        cuerpo = " ".join(palabras)
        if len(palabras) < 2:
            return cand
        mejor, ratio = _mejor_del_catalogo(cuerpo.replace(",", ""))
        if mejor is None:
            return cand                      # ya correcto
        if ratio >= UMBRAL_NOMBRE_ASR \
                and _primer_nombre_compatible(cuerpo, mejor):
            return " ".join(prefijo + [formatear_nombre(mejor)])
        # La secuencia completa no cuadró (quizá son dos nombres unidos por
        # coma): intentar corregir cada parte por separado
        if "," in cuerpo:
            partes, cambio = [], False
            for p in (x.strip() for x in cuerpo.split(",")):
                mj, rt = (_mejor_del_catalogo(p)
                          if len(p.split()) >= 2 else (None, 0.0))
                if mj is not None and rt >= UMBRAL_NOMBRE_ASR \
                        and _primer_nombre_compatible(p, mj):
                    partes.append(formatear_nombre(mj))
                    cambio = True
                else:
                    partes.append(p)
            if cambio:
                pref = " ".join(prefijo)
                return (pref + " " if pref else "") + ", ".join(partes)
        return cand

    texto = PATRON_NOMBRE_LAX.sub(repl, texto)
    # Quitar la cola duplicada que puede dejar la sustitución
    # ("...Zurita Trejo Trejo" -> "...Zurita Trejo")
    return _quitar_colas_duplicadas(texto)


def corregir_transcripcion(texto):
    """Corrección completa de un segmento recién transcrito."""
    return corregir_nombres(corregir_terminos(texto))


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
MARGEN_INTRUSO = 0.10      # para atribuir una intervención breve a OTRO
                           # diputado (mejora C) se exige el doble del
                           # margen normal: la evidencia corta debe ser
                           # contundente
MAX_SEG_HUERFANO = 3.0     # micro-tramos "Desconocido" de hasta esta
                           # duración se reabsorben si el mismo orador
                           # habla antes y después (efecto sándwich)
SIM_SANDWICH = 0.55        # un tramo Desconocido LARGO también se reabsorbe
                           # si sus vecinos coinciden Y su propia voz apunta
                           # al mismo orador con al menos esta similitud
                           # (evidencia moderada + contexto = suficiente)
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
            "segundo": nombres[segundo],
            "sim_segundo": float(sims[segundo]),
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
            texto       TEXT,
            fuente      TEXT DEFAULT 'rapido'
        )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_part_sesion "
                "ON participaciones(sesion_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_part_orador "
                "ON participaciones(orador)")
    # Migración para bases creadas antes de existir la columna 'fuente'
    # ('rapido' = 1ª pasada en vivo; 'corregido' = 2ª pasada del corrector)
    cols = {r[1] for r in con.execute("PRAGMA table_info(participaciones)")}
    if "fuente" not in cols:
        con.execute("ALTER TABLE participaciones ADD COLUMN fuente TEXT "
                    "DEFAULT 'rapido'")
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
    ap.add_argument("--umbral-intruso", type=float, default=0.82,
                    help="similitud mínima para atribuir una intervención "
                         "BREVE (interjección, moción) a otro diputado sin "
                         "cortar el tramo del orador principal (default: "
                         "0.82; súbelo si reatribuye de más, 1.01 lo "
                         "desactiva)")
    ap.add_argument("--pausa-voz", type=float, default=2.0,
                    help="pausa (en segundos) que cierra el tramo de voz en "
                         "curso; lo que siga se identifica por su cuenta "
                         "(default: 2.0)")
    ap.add_argument("--corrector", action="store_true",
                    help="activa una SEGUNDA pasada con retraso que reevalúa "
                         "los tramos ya consolidados y corrige en la base los "
                         "que quedaron como Mesa/Presidencia/Secretaría/"
                         "Desconocido, usando una huella de voz limpia sobre "
                         "el audio completo del turno y el contexto de los "
                         "vecinos (requiere --voz)")
    ap.add_argument("--retraso", type=float, default=8.0,
                    help="segundos que el corrector se queda por detrás del "
                         "directo antes de dar un tramo por asentado; más "
                         "retraso = más contexto y precisión, menos "
                         "inmediatez (default: 8.0)")
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

    # prompt_extra.txt (opcional): palabras o nombres que Whisper suele
    # escribir mal ("Gabriel Kalid Mohamed Báez"); se anteponen al prompt
    # para sesgar la transcripción hacia la grafía correcta. Mantenerlo
    # corto (el prompt total se recorta a ~800 caracteres).
    prompt_sesion = PROMPT_BASE
    for ruta_pe in (os.path.join(os.path.dirname(
            os.path.abspath(__file__)), "prompt_extra.txt"),
            "prompt_extra.txt"):
        if os.path.isfile(ruta_pe):
            with open(ruta_pe, encoding="utf-8") as f:
                extra = " ".join(f.read().split())
            if extra:
                prompt_sesion = extra + ". " + PROMPT_BASE
                print(f"Vocabulario extra para Whisper: "
                      f"{len(extra)} caracteres (prompt_extra.txt)")
            break

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
    perfil_por_etiqueta = {}
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
            # etiqueta oficial ("Dip. Fulano") -> fila en matriz_voz, para
            # que el corrector pueda medir la voz de un tramo contra el
            # perfil de un vecino concreto
            perfil_por_etiqueta = {etiqueta_orador(n): i
                                   for i, n in enumerate(nombres_voz)}
            print(f"[voz] {len(nombres_voz)} huellas cargadas; se vigilarán "
                  "los cambios de voz en todo el audio.")

    # El corrector necesita las huellas de voz: si --voz no quedó activo
    # (sin perfiles), se desactiva también el corrector.
    if args.corrector and not args.voz:
        print("[corr] El corrector requiere --voz con huellas válidas; "
              "se desactiva para esta sesión.")
        args.corrector = False
    elif args.corrector:
        print(f"[corr] Corrector de ventana retardada activo "
              f"(retraso {args.retraso:g}s): los tramos Mesa/Presidencia/"
              "Secretaría/Desconocido se reevaluarán al asentarse.")

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
        "huerfanos": [],            # ids de un tramo Desconocido en espera
        "huerfano_micro": False,    # True si el huérfano fue un micro-corte
        "huerfano_voz": (None, 0.0),  # (candidato, similitud) del huérfano
        "huerfano_segundos": 0.0,   # duración del huérfano (para stats)
        # --- Corrector de ventana retardada (2ª pasada) ---
        "tramos_hist": [],          # tramos cerrados, en orden temporal
        "corr_idx": 0,              # próximo tramo pendiente de corregir
        "bloques_retenidos": set(),  # WAV en espera de liberar tras corregir
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
            # El sanador de sándwiches usa esta evidencia al cerrar
            tramo["voz_candidato"] = etiqueta_nueva
            tramo["voz_sim"] = r["similitud"]
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
                    and etiqueta_actual != ORADOR_SECRETARIA \
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
                         f"{r['similitud']:.2f}; después {r['segundo']} "
                         f"con {r['sim_segundo']:.2f}); marcado como "
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
            elif cerrar and etiqueta_nueva != etiqueta_actual \
                    and r["similitud"] >= args.umbral_voz:
                # Similitud alta que NO se aplicó automáticamente: decir
                # por qué, para poder calibrar. La evidencia queda visible
                # en revisar.py con el botón "aplicar".
                razones = []
                if r["margen"] < MARGEN_MIN_VOZ:
                    razones.append(
                        f"margen de solo {r['margen']:.2f} sobre "
                        f"{r['segundo']} ({r['sim_segundo']:.2f}); esos "
                        "dos perfiles de voz son casi gemelos, conviene "
                        "regenerarlos con voz.py usando audio limpio y "
                        "sin traslapes")
                if r["similitud"] < exigencia:
                    razones.append(
                        f"similitud {r['similitud']:.2f} por debajo de la "
                        f"exigencia {exigencia:.2f} para contradecir una "
                        "etiqueta respaldada por el protocolo")
                if razones:
                    aviso = (f"   [voz] {hms(tramo['ini'])}–"
                             f"{hms(tramo['fin'])}: la voz apunta a "
                             f"{etiqueta_nueva} "
                             f"({r['similitud']:.2f}) pero no se aplicó: "
                             + "; ".join(razones)
                             + ". Puedes aplicarla en revisar.py.")
                    print(aviso)
                    archivo_txt.write(aviso + "\n")
                    archivo_txt.flush()
            con.commit()
        finally:
            if cerrar:
                # --- Sanador de sándwiches (generalizado) ---
                # Un tramo Desconocido queda en espera; cuando el tramo
                # siguiente resulta confiable y su identidad coincide con
                # la del tramo ANTERIOR al Desconocido, éste se reabsorbe
                # si (a) era un micro-corte (<= MAX_SEG_HUERFANO), o
                # (b) su propia voz apuntaba a ese mismo orador con
                # similitud moderada (>= SIM_SANDWICH): estar rodeado por
                # la misma persona + parecerse a ella es evidencia
                # suficiente aunque no alcance el umbral por sí sola.
                etiqueta_final = tramo["etiqueta"] or ORADOR_MESA
                confiable = tramo["resuelto"] or tramo["protegido"]
                if confiable and etiqueta_final != ORADOR_DESCONOCIDO:
                    hv_cand, hv_sim = estado["huerfano_voz"]
                    procede = estado["huerfanos"] \
                        and etiqueta_final == estado["prev_resuelto"] \
                        and (estado["huerfano_micro"]
                             or (hv_cand == etiqueta_final
                                 and hv_sim >= SIM_SANDWICH))
                    if procede:
                        marcas_h = ",".join("?" * len(estado["huerfanos"]))
                        con.execute(
                            f"UPDATE participaciones SET orador=? "
                            f"WHERE id IN ({marcas_h})",
                            [etiqueta_final] + estado["huerfanos"])
                        con.commit()
                        segs_h = estado["huerfano_segundos"]
                        sd = estado["stats"].get(ORADOR_DESCONOCIDO)
                        if sd:
                            sd[0] = max(0.0, sd[0] - segs_h)
                        sf = estado["stats"].setdefault(
                            etiqueta_final, [0.0, 0])
                        sf[0] += segs_h
                        detalle = ("micro-corte" if estado["huerfano_micro"]
                                   else f"voz {hv_sim:.2f} concuerda con "
                                        "los vecinos")
                        aviso_h = (f"   [voz] tramo Desconocido "
                                   f"reabsorbido ({detalle}): "
                                   f"{len(estado['huerfanos'])} "
                                   f"segmento(s) devueltos a "
                                   f"{etiqueta_final}.")
                        print(aviso_h)
                        archivo_txt.write(aviso_h + "\n")
                        archivo_txt.flush()
                    estado["huerfanos"] = []
                    estado["prev_resuelto"] = etiqueta_final
                elif etiqueta_final == ORADOR_DESCONOCIDO and tramo["ids"]:
                    # Desconocido en espera: micro-corte, o tramo largo
                    # cuya voz al menos apunta a alguien (el veredicto
                    # llega cuando cierre el tramo siguiente)
                    estado["huerfanos"] = list(tramo["ids"])
                    estado["huerfano_micro"] = (
                        tramo["segundos"] <= MAX_SEG_HUERFANO)
                    estado["huerfano_voz"] = (
                        tramo.get("voz_candidato"),
                        tramo.get("voz_sim") or 0.0)
                    estado["huerfano_segundos"] = tramo["segundos"]
                else:
                    estado["huerfanos"] = []
                    estado["prev_resuelto"] = None
                # El corrector reevaluará este tramo cuando se asiente. Se
                # guarda su rastro (ids + tiempos + evidencia). La etiqueta
                # definitiva la vuelve a leer de la base al corregir, por si
                # el sanador de sándwiches ya la tocó entretanto.
                if args.corrector and tramo["ids"]:
                    estado["tramos_hist"].append({
                        "ids": list(tramo["ids"]),
                        "ini": tramo["ini"], "fin": tramo["fin"],
                        "etiqueta": tramo["etiqueta"] or ORADOR_MESA,
                        "protegido": tramo["protegido"],
                        "resuelto": tramo["resuelto"],
                        "segundos": tramo["segundos"],
                    })
                estado["voz"] = nuevo_tramo_voz()

    # -----------------------------------------------------------------
    # Corrector de ventana retardada (2ª pasada)
    # -----------------------------------------------------------------
    # Reevalúa cada tramo unos segundos DESPUÉS, cuando ya está asentado y
    # se conoce su vecino derecho. Sobre el audio COMPLETO del turno saca
    # una huella limpia (mucho mejor que el promedio de fragmentos de ~1 s)
    # y, con el contexto de ambos vecinos, decide si un tramo que quedó como
    # Mesa/Presidencia/Secretaría/Desconocido era en realidad de alguien
    # identificable. Nunca pisa una identidad fuerte ni un anuncio formal.
    _wav_cache = {}
    GRISES = {ORADOR_MESA, ORADOR_DESCONOCIDO, ORADOR_SECRETARIA}

    def _audio_de_bloque(idx):
        if idx in _wav_cache:
            return _wav_cache[idx]
        ruta = os.path.join(carpeta_audio, f"bloque_{idx:06d}.wav")
        a = None
        if os.path.isfile(ruta):
            try:
                a = vz.cargar_wav(ruta)
            except Exception:
                a = None
        _wav_cache[idx] = a
        return a

    def _audio_consolidado(ini, fin):
        """Concatena el audio real de [ini, fin] a través de los bloques que
        lo cubren (un turno puede cruzar la frontera de un bloque)."""
        if ini is None or fin is None or fin <= ini:
            return None
        partes = []
        for idx in range(int(ini // args.bloque), int(fin // args.bloque) + 1):
            a = _audio_de_bloque(idx)
            if a is None:
                continue
            base = idx * args.bloque
            il = max(0.0, ini - base)
            fl = min(float(args.bloque), fin - base)
            if fl <= il:
                continue
            try:
                frag = vz.rebanada(a, il, fl)
            except Exception:
                frag = None
            if frag is not None and len(frag):
                partes.append(frag)
        if not partes:
            return None
        return partes[0] if len(partes) == 1 else np.concatenate(partes)

    def _huella_consolidada(ini, fin):
        audio = _audio_consolidado(ini, fin)
        if audio is None or len(audio) < int(vz.FRECUENCIA * MIN_VOZ_FLUSH):
            return None
        try:
            limpio = vz.preprocesar(audio)
            if len(limpio) < int(vz.FRECUENCIA * 0.6):
                return None
            emb = np.asarray(encoder_voz.embed_utterance(limpio),
                             dtype=np.float32)
        except Exception:
            return None
        n = np.linalg.norm(emb)
        return emb / n if n else emb

    def _label_actual(ids):
        """Etiqueta mayoritaria ACTUAL de esas filas en la base (puede haber
        cambiado desde que el tramo se cerró)."""
        if not ids:
            return None
        marcas = ",".join("?" * len(ids))
        fila = con.execute(
            f"SELECT orador FROM participaciones WHERE id IN ({marcas}) "
            f"GROUP BY orador ORDER BY COUNT(*) DESC LIMIT 1", ids
        ).fetchone()
        return fila[0] if fila else None

    def _aplicar_correccion(t, nueva, actual, razon):
        marcas = ",".join("?" * len(t["ids"]))
        con.execute(
            f"UPDATE participaciones SET orador=?, fuente='corregido' "
            f"WHERE id IN ({marcas})", [nueva] + t["ids"])
        con.commit()
        dur = t["segundos"]
        sa = estado["stats"].get(actual)
        if sa:
            sa[0] = max(0.0, sa[0] - dur)
        sn = estado["stats"].setdefault(nueva, [0.0, 0])
        sn[0] += dur
        aviso = (f"[corr] {hms(t['ini'])}–{hms(t['fin'])}: \"{actual}\" -> "
                 f"{nueva} ({razon}).")
        print("\n" + aviso)
        archivo_txt.write(aviso + "\n")
        archivo_txt.flush()

    def corregir_tramo(i):
        """2ª pasada sobre el tramo i. Solo actúa en la zona gris; el
        protocolo y una voz fuerte mandan y no se tocan."""
        hist = estado["tramos_hist"]
        t = hist[i]
        if not t["ids"] or t["resuelto"]:
            return   # ya identificado con confianza al cierre
        actual = _label_actual(t["ids"]) or t["etiqueta"]
        if actual not in GRISES:
            return   # ya es un orador nombrado: intocable
        emb = _huella_consolidada(t["ini"], t["fin"])
        if emb is None:
            return   # sin audio suficiente para una huella fiable
        r = identificar_huella(emb, nombres_voz, matriz_voz, args.umbral_voz)
        if r is None:
            return
        et_voz = etiqueta_orador(r["candidato"])
        corto = t["segundos"] < SEG_MIN_CAMBIO
        # ¿La voz apunta con contundencia a un tercero? (interjección real
        # desde la curul): se respeta la lógica de intruso y NO se funde
        ri = identificar_huella(emb, nombres_voz, matriz_voz,
                                args.umbral_intruso, margen_min=MARGEN_INTRUSO)
        intruso_claro = bool(ri and ri["fuerte"])
        izq = hist[i - 1] if i - 1 >= 0 else None
        der = hist[i + 1] if i + 1 < len(hist) else None
        et_izq = _label_actual(izq["ids"]) if izq else None
        et_der = _label_actual(der["ids"]) if der else None

        nueva = razon = None
        # 1) La huella limpia identifica con fuerza a un orador concreto
        if r["fuerte"] and et_voz != actual:
            nueva = et_voz
            razon = f"voz consolidada {r['similitud']:.2f}"
        # 2) Suavizado / sándwich: mismo orador a ambos lados y sin intruso
        #    claro -> fue una pausa o alternancia natural, no otra persona.
        #    Un micro-corte se funde siempre; un tramo más largo, solo si su
        #    propia voz no contradice al vecino.
        elif (et_izq and et_izq == et_der and et_izq not in GRISES
              and not intruso_claro):
            idx_v = perfil_por_etiqueta.get(et_izq)
            sim_vecino = (float(matriz_voz[idx_v] @ emb)
                          if idx_v is not None else 0.0)
            if corto or sim_vecino >= SIM_SANDWICH:
                nueva = et_izq
                razon = ("micro-corte reabsorbido" if corto
                         else f"turno continuo, voz {sim_vecino:.2f}")

        if nueva and nueva != actual:
            _aplicar_correccion(t, nueva, actual, razon)

    def corrector_avanzar(frontera, forzar_final=False):
        """Corrige los tramos ya asentados (fin < frontera) que además
        tengan vecino derecho conocido. Cada tramo se procesa UNA sola vez.
        Con forzar_final procesa también el último tramo (solo tiene
        contexto izquierdo)."""
        hist = estado["tramos_hist"]
        while estado["corr_idx"] < len(hist):
            i = estado["corr_idx"]
            hay_der = (i + 1) < len(hist)
            if not forzar_final:
                if not hay_der:
                    break   # aún no hay contexto derecho: esperar
                fin = hist[i]["fin"]
                if fin is not None and fin >= frontera:
                    break   # todavía dentro de la ventana: no asentado
            try:
                corregir_tramo(i)
            except Exception as e:
                print(f"   [corr] aviso: no se pudo corregir el tramo "
                      f"{i} ({type(e).__name__}: {e})")
            estado["corr_idx"] += 1

    def liberar_audio(frontera):
        """Suelta de la RAM y del disco el audio que ya nadie necesita.
        Clave: un tramo pendiente puede ser largo y arrancar mucho antes de
        la frontera, así que el límite lo marca el audio MÁS ANTIGUO que
        cualquier tramo aún sin corregir podría necesitar, nunca la frontera
        a secas."""
        pend = [t for t in estado["tramos_hist"][estado["corr_idx"]:]
                if t["ini"] is not None]
        if pend:
            limite_idx = int(min(t["ini"] for t in pend) // args.bloque)
        else:
            limite_idx = int(frontera // args.bloque)
        for idx in [k for k in _wav_cache if k < limite_idx]:
            _wav_cache.pop(idx, None)
        if args.conservar_audio:
            return
        for ruta in list(estado["bloques_retenidos"]):
            if indice_de_bloque(ruta) < limite_idx:
                try:
                    os.remove(ruta)
                except OSError:
                    pass
                estado["bloques_retenidos"].discard(ruta)

    def procesar_bloque(ruta):
        idx = indice_de_bloque(ruta)
        offset = idx * args.bloque
        prompt = (prompt_sesion + estado["contexto"])[-800:]
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
            # Corregir errores de oído de Whisper (términos del protocolo
            # y nombres del catálogo) antes de cualquier detección
            texto = corregir_transcripcion(texto)
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
            # diputado. Abro la discusión..."): aplica en ESTE segmento.
            # La fórmula ES respaldo del protocolo: la etiqueta queda
            # protegida y no se degrada a Desconocido si la voz no alcanza
            # confianza plena (transiciones cortas y con ruido de sala)
            if estado["orador"] != ORADOR_MESA and retoma_presidencia(texto):
                estado["orador"] = ORADOR_MESA
                estado["protegido"] = True
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
                        and es_respuesta_lista(texto):
                    estado["orador"] = estado["llamado"]
                    estado["protegido"] = True   # respaldado por el llamado
                    estado["llamado"] = None
                elif estado["secretario_lista"] \
                        and (llamado_aqui
                             or not es_respuesta_lista(texto)):
                    # No es una respuesta de lista: es el secretario que
                    # sigue leyendo (aunque el texto sea corto, como la
                    # mitad de un apellido cortado por Whisper)
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
                estado["protegido"] = True      # anunciar es acto de la Mesa
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
                # Un llamado REAL de pase de lista es una frase corta
                # ("Diputado Fulano de Tal."). Un nombre al final de una
                # frase larga es la lectura de un listado (integraciones de
                # comisiones, orden del día): mencionar no es llamar.
                if llamado_aqui and len(texto) <= MAX_LARGO_LLAMADO:
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
                            and ini_abs <= estado["llamado_hasta"] \
                            and es_respuesta_lista(texto):
                        # cambio de voz + llamado vigente + el texto tiene
                        # forma de respuesta: es el diputado llamado
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
                # 3) Intervención BREVE de otra persona (interjección,
                #    moción desde la curul): un segmento corto no puede
                #    declarar cambio de voz (huella ruidosa), pero si su
                #    huella (a) se despega de la voz promedio del tramo Y
                #    (b) apunta con similitud muy alta y margen amplio a un
                #    diputado distinto, se reatribuye SOLO ese segmento sin
                #    cortar el tramo: el orador principal continúa y la
                #    huella del intruso no contamina su promedio
                intruso = False
                if emb is not None and not estado["modo_lista"] \
                        and (fin_abs - ini_abs) < SEG_MIN_CAMBIO \
                        and tramo["n"] >= 1:
                    prom = huella_promedio(tramo)
                    if prom is not None and float(np.dot(prom, emb)) \
                            < args.umbral_cambio_voz:
                        ri = identificar_huella(
                            emb, nombres_voz, matriz_voz,
                            args.umbral_intruso,
                            margen_min=MARGEN_INTRUSO)
                        if ri and ri["fuerte"]:
                            et_i = etiqueta_orador(ri["candidato"])
                            if et_i != estado["orador"] \
                                    and et_i != (tramo["etiqueta"] or ""):
                                intruso = True
                                con.execute(
                                    "UPDATE participaciones SET orador=?, "
                                    "voz_orador=?, voz_similitud=? "
                                    "WHERE id=?",
                                    (et_i, et_i, round(ri["similitud"], 3),
                                     row_id))
                                dur_seg = fin_abs - ini_abs
                                sa = estado["stats"].get(estado["orador"])
                                if sa:
                                    sa[0] = max(0.0, sa[0] - dur_seg)
                                    sa[1] = max(0, sa[1] - 1)
                                si_ = estado["stats"].setdefault(
                                    et_i, [0.0, 0])
                                si_[0] += dur_seg
                                si_[1] += 1
                                estado["ultimo_impreso"] = None
                                aviso_i = (
                                    f"   [voz] {hms(ini_abs)}: intervención "
                                    f"breve atribuida a {et_i} (similitud "
                                    f"{ri['similitud']:.2f}); "
                                    f"{estado['orador']} continúa.")
                                print(aviso_i)
                                archivo_txt.write(aviso_i + "\n")
                                archivo_txt.flush()
                if tramo["etiqueta"] is None:
                    tramo["etiqueta"] = estado["orador"]
                    tramo["protegido"] = estado["protegido"]
                if not intruso:
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
        if args.corrector:
            # El audio no se borra todavía: el corrector lo necesita para
            # sacar la huella limpia del turno cuando el tramo se asiente.
            estado["bloques_retenidos"].add(ruta)
            frontera = (offset + args.bloque) - args.retraso
            corrector_avanzar(frontera)
            liberar_audio(frontera)
        elif not args.conservar_audio:
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
        if args.corrector:
            # Última pasada: procesa todo lo pendiente, incluido el tramo
            # final (que solo tiene contexto izquierdo)
            corrector_avanzar(float("inf"), forzar_final=True)
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
