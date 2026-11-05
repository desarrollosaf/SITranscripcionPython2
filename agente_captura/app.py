#!/usr/bin/env python3
"""
agente_captura
==============
App de escritorio (Windows) para el operador de audio: empuja el audio de
una interfaz USB o Dante Virtual Soundcard hacia el servidor de
transcripción, por SRT, sin necesitar saber programar.

Flujo:
  1. Inicia sesión con su usuario (el mismo de la API/revisar.py).
  2. Pestaña "Nueva transcripción": elige Tipo (Sesión/Comisión) y el
     evento de hoy, y lo inicia él mismo, sin depender de que alguien con
     permisos de administrador lo haga primero desde el panel web.
  3. Pestaña "Mandar audio": cola compartida (de cualquier
     usuario) de trabajos que ya están corriendo y solo les falta que
     alguien les mande el audio — elige el dispositivo (USB o canal
     Dante) y presiona "Iniciar". Puede tener varias transmitiendo a la
     vez. Ahí mismo las detiene, las reanuda si se cortaron y las finaliza.
  4. Pestaña "Mis trabajos": los que él mismo inició, con su estado. Si
     alguno se cayó (detenido/con error), lo puede reanudar sin pedirle
     a nadie más que entre al panel; también puede ver el texto que
     lleva transcrito (sin oradores, solo para seguirlo).

No requiere Python ni ffmpeg instalados aparte: se distribuye como un
único .exe con todo incluido (ver .github/workflows/build-agente-captura.yml).

Para ver la interfaz sin servidor ni dispositivos de audio (también en Mac):
    python agente_captura/app.py --demo
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from tkinter import messagebox
from urllib.parse import urlparse

import customtkinter as ctk
import requests

CONFIG_NOMBRE = "agente_captura_config.json"
REFRESCO_EVENTOS_MS = 20_000
REFRESCO_ESTADO_MS = 2_000
REFRESCO_TRABAJOS_MS = 10_000
REFRESCO_TEXTO_MS = 3_000

# Al iniciar o reanudar, el servidor tarda en abrir el puerto SRT porque
# primero carga Whisper: mientras tanto ffmpeg falla y se reintenta solo.
ESPERA_SERVIDOR_SEG = 120
# ffmpeg sigue vivo unos segundos aunque no logre conectar: antes de esto
# no se puede afirmar que ya esté transmitiendo.
SEG_PARA_DAR_POR_CONECTADO = 6

MODELOS = ["tiny", "base", "small", "medium", "large-v3"]
MODELO_DEFECTO = "small"

# Mismos estados que api/jobs.py: solo desde estos tiene sentido reanudar
# (un trabajo "ejecutando" ya está corriendo, no hay nada que reanudar).
ESTADOS_REANUDABLES = {"detenido", "error", "finalizado"}

def ruta_ffmpeg():
    """Ruta al ffmpeg empacado (PyInstaller onefile) o, en desarrollo, el
    ffmpeg del PATH del sistema."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    candidato = os.path.join(base, "ffmpeg.exe")
    return candidato if os.path.isfile(candidato) else "ffmpeg"


def ruta_config():
    base = (os.path.dirname(os.path.abspath(sys.executable))
            if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, CONFIG_NOMBRE)


def cargar_config():
    try:
        with open(ruta_config(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def guardar_config(datos):
    try:
        with open(ruta_config(), "w", encoding="utf-8") as f:
            json.dump(datos, f)
    except OSError:
        pass


def listar_dispositivos_audio():
    """Corre ffmpeg -list_devices y devuelve una lista de (nombre, id) por
    cada dispositivo de audio DirectShow (interfaces USB, canales Dante,
    etc.). 'id' es el "Alternative name" (@device_cm_{...}) que ffmpeg
    imprime junto a cada dispositivo, o None si no lo trae — se usa para
    ABRIR el dispositivo en vez del nombre visible: en Windows, ffmpeg
    frecuentemente no puede abrir por nombre si tiene acentos/símbolos
    ("Could not find audio only device..."), aunque sí lo liste bien."""
    try:
        r = subprocess.run(
            [ruta_ffmpeg(), "-hide_banner", "-list_devices", "true",
             "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"No pude ejecutar ffmpeg: {e}")

    texto = r.stderr or ""
    dispositivos = []   # [(nombre, id_alternativo_o_None), ...]
    pendiente = None
    en_audio_legacy = False

    def cerrar_pendiente():
        nonlocal pendiente
        if pendiente:
            dispositivos.append((pendiente, None))
            pendiente = None

    for linea in texto.splitlines():
        # Formato viejo de ffmpeg: encabezados de sección.
        if "DirectShow audio devices" in linea:
            en_audio_legacy = True
            continue
        if "DirectShow video devices" in linea:
            en_audio_legacy = False
            cerrar_pendiente()
            continue

        m_alt = re.search(r'Alternative name\s+"([^"]+)"', linea)
        if m_alt:
            if pendiente:
                dispositivos.append((pendiente, m_alt.group(1)))
                pendiente = None
            continue

        # Formato nuevo: "(audio)"/"(video)" pegado a la línea.
        m_nuevo = re.search(r'"([^"]+)"\s*\(audio\)', linea)
        if m_nuevo:
            cerrar_pendiente()
            pendiente = m_nuevo.group(1)
            continue
        if re.search(r'"[^"]+"\s*\(video\)', linea):
            cerrar_pendiente()
            continue

        if en_audio_legacy:
            m_legacy = re.search(r'"([^"]+)"', linea)
            if m_legacy:
                cerrar_pendiente()
                pendiente = m_legacy.group(1)
                continue
    cerrar_pendiente()

    if not dispositivos:
        # No calzó el parseo (versión/formato distinto de ffmpeg) o de
        # verdad no hay dispositivos — mostramos la salida cruda para
        # poder diagnosticarlo en vez de dejar el desplegable vacío
        # y en silencio.
        raise RuntimeError(
            "No detecté ningún dispositivo de audio. Salida de ffmpeg:\n\n"
            + (texto[-1500:] if texto else "(sin salida)"))
    return dispositivos


class ClienteAPI:
    es_demo = False

    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")
        self.token = None

    def login(self, email, password):
        r = requests.post(f"{self.base_url}/auth/login",
                          data={"username": email, "password": password},
                          timeout=15)
        if r.status_code != 200:
            detalle = r.json().get("detail", "credenciales inválidas") \
                if r.headers.get("content-type", "").startswith("application/json") \
                else "credenciales inválidas"
            raise RuntimeError(detalle)
        self.token = r.json()["access_token"]

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def _detalle_error(self, r):
        if r.headers.get("content-type", "").startswith("application/json"):
            try:
                return r.json().get("detail", r.text)
            except ValueError:
                pass
        return r.text

    def eventos_esperando_audio(self):
        r = requests.get(f"{self.base_url}/transcripciones/esperando-audio",
                         headers=self._headers(), timeout=15)
        r.raise_for_status()
        return r.json()

    def eventos(self, tipo):
        """Últimos eventos del Congreso de ese tipo (0 = Comisión,
        1 = Sesión/Diputación permanente), ya con fecha/hora local y
        comisiones resueltas — para elegir uno e iniciar la transcripción
        sin tener que escribir nada a mano."""
        r = requests.get(f"{self.base_url}/transcripciones/eventos",
                         params={"tipo": tipo}, headers=self._headers(),
                         timeout=20)
        if r.status_code >= 400:
            raise RuntimeError(self._detalle_error(r))
        return r.json()

    def crear_desde_evento(self, evento_id, tipo, modelo):
        r = requests.post(
            f"{self.base_url}/transcripciones/desde-evento",
            json={"evento_id": evento_id, "tipo": tipo, "modelo": modelo},
            headers=self._headers(), timeout=20)
        if r.status_code >= 400:
            raise RuntimeError(self._detalle_error(r))
        return r.json()

    def mis_trabajos(self):
        """Los trabajos del usuario que inició sesión (el servidor ya los
        filtra así para cuentas que no son de administrador)."""
        r = requests.get(f"{self.base_url}/transcripciones",
                         headers=self._headers(), timeout=15)
        r.raise_for_status()
        return r.json()

    def detener(self, job_id):
        r = requests.post(
            f"{self.base_url}/transcripciones/{job_id}/detener",
            headers=self._headers(), timeout=15)
        if r.status_code >= 400:
            raise RuntimeError(self._detalle_error(r))
        return r.json()

    def reanudar(self, job_id):
        r = requests.post(
            f"{self.base_url}/transcripciones/{job_id}/reanudar",
            headers=self._headers(), timeout=15)
        if r.status_code >= 400:
            raise RuntimeError(self._detalle_error(r))
        return r.json()

    def participaciones(self, job_id):
        r = requests.get(
            f"{self.base_url}/transcripciones/{job_id}/participaciones",
            headers=self._headers(), timeout=15)
        if r.status_code >= 400:
            raise RuntimeError(self._detalle_error(r))
        return r.json()

    def listar_dispositivos(self):
        return listar_dispositivos_audio()

    def lanzar_ffmpeg(self, cmd, archivo_log):
        return subprocess.Popen(
            cmd, stdout=archivo_log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    @property
    def host(self):
        return urlparse(self.base_url).hostname


# ---- Estilo visual ---------------------------------------------------

TITULO_APP = "Agente de captura de audio"

FONDO = "#F1F1F3"
BLANCO = "#FFFFFF"
BORDE = "#D5D6DA"
TEXTO = "#1F2024"
TEXTO_TENUE = "#62656B"
ACENTO = "#7B1E45"
ACENTO_OSCURO = "#621737"
ACENTO_TENUE = "#C798AB"   # botón principal deshabilitado
GRIS = "#6E7177"
GRIS_OSCURO = "#585B60"
HOVER_CLARO = "#ECECEF"
EXITO = "#1E7A34"
AVISO = "#A76A00"
PELIGRO = "#B3261E"
PELIGRO_OSCURO = "#8F1E18"

# tono -> (color de texto, color de fondo) de las pastillas de estado.
TONOS = {
    "neutro": ("#4A4D52", "#E6E7EA"),
    "exito": (EXITO, "#DFF2E3"),
    "aviso": (AVISO, "#FBEFD5"),
    "peligro": (PELIGRO, "#FADFDD"),
}

# Mismos estados que api/jobs.py -> (texto visible, tono).
ESTADO_ETIQUETAS = {
    "ejecutando": ("En curso", "exito"),
    "deteniendo": ("Deteniendo…", "aviso"),
    "detenido": ("Detenido", "neutro"),
    "error": ("Con error", "peligro"),
    "finalizado": ("Finalizado", "neutro"),
}

TIPO_SESION = "Sesión / Diputación permanente"
TIPO_COMISION = "Comisión"
TAB_NUEVA = "Nueva transcripción"
TAB_AUDIO = "Mandar audio"
TAB_MIOS = "Mis trabajos"
SIN_DISPOSITIVOS = "(sin dispositivos)"

FAMILIA_FUENTE = {"win32": "Segoe UI", "darwin": "Helvetica Neue"}.get(sys.platform)


def fuente(tamano, peso="normal"):
    if FAMILIA_FUENTE:
        return ctk.CTkFont(family=FAMILIA_FUENTE, size=tamano, weight=peso)
    return ctk.CTkFont(size=tamano, weight=peso)


def tarjeta(parent):
    return ctk.CTkFrame(parent, fg_color=BLANCO, corner_radius=10,
                        border_width=1, border_color=BORDE)


def tarjeta_paso(parent, numero, titulo):
    """Tarjeta con su número de paso en círculo; devuelve el frame donde
    van los controles de ese paso."""
    caja = tarjeta(parent)
    caja.pack(fill="x", padx=2, pady=5)
    encabezado = ctk.CTkFrame(caja, fg_color="transparent")
    encabezado.pack(fill="x", padx=14, pady=(10, 6))
    # En un marco de tamaño fijo: CTkLabel se ensancha según su radio y
    # el número quedaría en un óvalo en vez de un círculo.
    circulo = ctk.CTkFrame(encabezado, width=26, height=26, corner_radius=13,
                           fg_color=ACENTO)
    circulo.pack(side="left", padx=(0, 8))
    circulo.pack_propagate(False)
    ctk.CTkLabel(circulo, text=str(numero), width=20, height=20,
                 fg_color="transparent", text_color="white",
                 font=fuente(13, "bold")).place(relx=0.5, rely=0.5, anchor="center")
    ctk.CTkLabel(encabezado, text=titulo, font=fuente(14, "bold"),
                 text_color=TEXTO).pack(side="left")
    cuerpo = ctk.CTkFrame(caja, fg_color="transparent")
    cuerpo.pack(fill="x", padx=14, pady=(0, 12))
    return cuerpo


def boton_primario(parent, texto, comando, width=140):
    return ctk.CTkButton(parent, text=texto, command=comando, width=width,
                         height=36, corner_radius=8, font=fuente(13, "bold"),
                         fg_color=ACENTO, hover_color=ACENTO_OSCURO,
                         text_color="white", text_color_disabled="#F7EEF1")


def habilitar_primario(boton, habilitado):
    """CTkButton no cambia de color al deshabilitarse: sin esto, un botón
    inactivo se ve igual que uno que sí se puede presionar."""
    boton.configure(state="normal" if habilitado else "disabled",
                    fg_color=ACENTO if habilitado else ACENTO_TENUE)


def boton_secundario(parent, texto, comando, width=120):
    return ctk.CTkButton(parent, text=texto, command=comando, width=width,
                         height=36, corner_radius=8, font=fuente(13),
                         fg_color=BLANCO, hover_color=HOVER_CLARO,
                         border_width=1, border_color=BORDE, text_color=TEXTO,
                         text_color_disabled="#A9ABB0")


def menu_opciones(parent, width=200, values=None, command=None):
    return ctk.CTkOptionMenu(
        parent, width=width, height=36, corner_radius=8, values=values or [],
        command=command, dynamic_resizing=False, font=fuente(13),
        dropdown_font=fuente(13), fg_color="#E9EAED", button_color=GRIS,
        button_hover_color=GRIS_OSCURO, text_color=TEXTO,
        dropdown_fg_color=BLANCO, dropdown_text_color=TEXTO,
        dropdown_hover_color=HOVER_CLARO)


def pastilla(parent, texto, tono):
    etiqueta = ctk.CTkLabel(parent, text="", height=26, corner_radius=13,
                            font=fuente(12, "bold"))
    poner_pastilla(etiqueta, texto, tono)
    return etiqueta


def poner_pastilla(etiqueta, texto, tono):
    color_texto, color_fondo = TONOS[tono]
    # Espacios a los lados: CTkLabel recorta el texto al borde del fondo.
    etiqueta.configure(text=f"  {texto}  ", text_color=color_texto,
                       fg_color=color_fondo)


def lista_desplazable(parent):
    lista = ctk.CTkScrollableFrame(parent, fg_color="transparent")
    lista.pack(fill="both", expand=True)
    return lista


def caja_de_texto(parent):
    return ctk.CTkTextbox(parent, wrap="word", font=fuente(13), fg_color=BLANCO,
                          text_color=TEXTO, border_width=1, border_color=BORDE,
                          corner_radius=8, state="disabled")


def traer_al_frente(ventana):
    # En Windows, CTkToplevel se abre detrás de la principal si no se le
    # da un momento para terminar de dibujarse.
    ventana.after(200, ventana.lift)
    ventana.after(250, ventana.focus_force)


def ventana_de_texto(parent, titulo, contenido):
    ventana = ctk.CTkToplevel(parent, fg_color=FONDO)
    ventana.title(titulo)
    ventana.geometry("640x380")
    caja = caja_de_texto(ventana)
    caja.pack(fill="both", expand=True, padx=14, pady=14)
    caja.configure(state="normal")
    caja.insert("1.0", contenido)
    caja.configure(state="disabled")
    traer_al_frente(ventana)


class FilaEvento:
    """Una transcripción en la lista "Mandar audio", con su selector de
    dispositivo, botones y estado. Puede haber varias transmitiendo a la
    vez, cada una con su propio proceso ffmpeg.

    Detener solo apaga el envío; el servidor, al perder el audio, termina
    lo pendiente y cierra el trabajo por su cuenta. Por eso la fila se
    queda en la lista hasta que el operador presiona Finalizar: desde aquí
    mismo puede reanudar sin ir a "Mis trabajos"."""

    def __init__(self, parent, app, evento, dispositivos, nuevo=False):
        self.app = app
        self.evento = evento
        self.proc = None
        self.ruta_log = None
        self.detenido_a_mano = False
        self.ya_inicio = False
        self.buscando = False        # hay un hilo averiguando a dónde conectar
        self.reintentar_hasta = 0.0  # time.monotonic(): ver ESPERA_SERVIDOR_SEG
        self.lanzado_en = 0.0
        self.mapa_dispositivos = {}   # nombre visible -> id alterno (o None)

        self.marco = tarjeta(parent)
        self.marco.pack(fill="x", padx=2, pady=5)

        cabecera = ctk.CTkFrame(self.marco, fg_color="transparent")
        cabecera.pack(fill="x", padx=14, pady=(12, 0))

        nombre = evento.get("url") or f"Trabajo {evento['id'][:8]}"
        ctk.CTkLabel(cabecera, text=nombre, font=fuente(14, "bold"),
                     text_color=TEXTO, anchor="w", justify="left",
                     wraplength=560).pack(side="left", fill="x", expand=True)

        self.estado = pastilla(cabecera, "Sin audio", "neutro")
        self.estado.pack(side="right", padx=(8, 0))

        self.aviso_nuevo = None
        if nuevo:
            # Aviso temporal para que no se pase por alto un trabajo que
            # acaba de aparecer: además de la campana y de traer la ventana
            # al frente (ver VentanaPrincipal.avisar_nuevo), marca CUÁL
            # fila es la nueva. Se quita sola a los 20 s.
            self.aviso_nuevo = pastilla(cabecera, "Nuevo: manda el audio", "peligro")
            self.aviso_nuevo.pack(side="right", padx=(8, 0))
            self.marco.after(20_000, self._quitar_aviso_nuevo)

        self.etiqueta_puerto = ctk.CTkLabel(
            self.marco, text=f"Puerto {evento.get('puerto')}", font=fuente(12),
            text_color=TEXTO_TENUE, anchor="w")
        self.etiqueta_puerto.pack(fill="x", padx=14)

        fila = ctk.CTkFrame(self.marco, fg_color="transparent")
        fila.pack(fill="x", padx=14, pady=(8, 12))

        self.combo = menu_opciones(fila, width=320)
        self.combo.pack(side="left", padx=(0, 10))
        self.actualizar_dispositivos(dispositivos)

        self.boton = boton_primario(fila, "▶  Iniciar", self._alternar, width=120)
        self.boton.pack(side="left")

        self.boton_finalizar = boton_secundario(fila, "Finalizar", self._finalizar)

        self.boton_log = boton_secundario(fila, "Ver registro", self._ver_registro)
        self.boton_log.pack(side="left", padx=(10, 0))
        self.boton_log.configure(state="disabled")

        self._mostrar_botones()

    @property
    def transmitiendo(self):
        return self.proc is not None and self.proc.poll() is None

    def _activa(self):
        """¿Está mandando audio o intentando conectarse?"""
        if self.detenido_a_mano:
            return False
        return (self.transmitiendo or self.buscando
                or time.monotonic() < self.reintentar_hasta)

    def _quitar_aviso_nuevo(self):
        if self.aviso_nuevo is not None and self.aviso_nuevo.winfo_exists():
            self.aviso_nuevo.destroy()
        self.aviso_nuevo = None

    def actualizar_trabajo(self, evento):
        # Al reanudarse el trabajo puede cambiar de puerto.
        self.evento = evento
        self.etiqueta_puerto.configure(text=f"Puerto {evento.get('puerto')}")

    def actualizar_dispositivos(self, dispositivos):
        """dispositivos: lista de (nombre, id_alterno_o_None)."""
        self.mapa_dispositivos = {nombre: id_alt for nombre, id_alt in dispositivos}
        nombres = list(self.mapa_dispositivos.keys())
        actual = self.combo.get()
        self.combo.configure(values=nombres or [SIN_DISPOSITIVOS])
        if actual in nombres:
            self.combo.set(actual)
        else:
            self.combo.set(nombres[0] if nombres else SIN_DISPOSITIVOS)

    def _alternar(self):
        if self._activa():
            self._detener()
        else:
            self._iniciar()

    def _iniciar(self):
        if self.combo.get() not in self.mapa_dispositivos:
            messagebox.showwarning("Falta dispositivo",
                                   "Elige un dispositivo de audio primero.")
            return
        self.detenido_a_mano = False
        self.reintentar_hasta = time.monotonic() + ESPERA_SERVIDOR_SEG
        poner_pastilla(self.estado, "Conectando…", "aviso")
        self._buscar_y_lanzar(manual=True)
        self._mostrar_botones()

    def _buscar_y_lanzar(self, manual):
        """Pregunta al servidor a dónde conectar: si el trabajo sigue en la
        cola, a su puerto actual; si ya se cerró (se cortó el audio o se
        cayó el proceso) y esta fila ya había transmitido, lo reanuda."""
        self.buscando = True
        job_id = self.evento["id"]
        ya_inicio = self.ya_inicio
        cliente = self.app.cliente
        despachar = self.app.root.after

        def trabajo_hilo():
            try:
                activos = cliente.eventos_esperando_audio()
                actual = next((e for e in activos if e["id"] == job_id), None)
                if actual is None:
                    if not ya_inicio:
                        raise RuntimeError(
                            "Esta transcripción ya no está esperando audio.")
                    actual = cliente.reanudar(job_id)
            except Exception as e:
                mensaje = str(e)
                despachar(0, lambda: self._fallo_conexion(mensaje, manual))
                return
            despachar(0, lambda: self._lanzar(actual))
        threading.Thread(target=trabajo_hilo, daemon=True).start()

    def _fallo_conexion(self, mensaje, manual):
        self.buscando = False
        if self.detenido_a_mano or not self.marco.winfo_exists():
            return
        if not manual and time.monotonic() < self.reintentar_hasta:
            return  # refrescar_estado() lo vuelve a intentar
        self.reintentar_hasta = 0.0
        poner_pastilla(self.estado, "No se pudo conectar", "peligro")
        self._mostrar_botones()
        if manual:
            messagebox.showerror("No pude conectar", mensaje)

    def _lanzar(self, evento):
        self.buscando = False
        if self.detenido_a_mano or not self.marco.winfo_exists():
            return
        self.actualizar_trabajo(evento)
        dispositivo = self.combo.get()
        # Preferir el id alterno (@device_cm_{...}) sobre el nombre visible:
        # en Windows, ffmpeg suele fallar al abrir por nombre si tiene
        # acentos/símbolos, aunque lo liste bien.
        referencia = self.mapa_dispositivos.get(dispositivo) or dispositivo
        host = self.app.cliente.host
        destino = (f"srt://{host}:{evento['puerto']}"
                   f"?mode=caller&passphrase={evento['passphrase']}")
        cmd = [
            ruta_ffmpeg(), "-hide_banner", "-loglevel", "warning",
            "-f", "dshow", "-i", f"audio={referencia}",
            # AAC, no PCM crudo: MPEG-TS no tiene un tipo de stream estándar
            # para PCM, así que el lado que recibe no lo reconoce al leer.
            "-ac", "1", "-ar", "16000", "-acodec", "aac", "-b:a", "128k",
            "-f", "mpegts", destino,
        ]
        # El registro de ffmpeg se manda a un archivo (no a un PIPE sin
        # leer: si nadie lo lee, el buffer se llena y el proceso se cuelga).
        # Un solo archivo por fila: los reintentos se le van agregando.
        if self.ruta_log is None:
            fd, self.ruta_log = tempfile.mkstemp(prefix="agente_captura_", suffix=".log")
            os.close(fd)
        archivo_log = open(self.ruta_log, "a", encoding="utf-8", errors="replace")
        try:
            self.proc = self.app.cliente.lanzar_ffmpeg(cmd, archivo_log)
        except OSError as e:
            self.reintentar_hasta = 0.0
            poner_pastilla(self.estado, "No se pudo iniciar", "peligro")
            self._mostrar_botones()
            messagebox.showerror("No pude iniciar", str(e))
            return
        finally:
            archivo_log.close()
        self.ya_inicio = True
        self.lanzado_en = time.monotonic()
        self.boton_log.configure(state="normal")
        self._mostrar_botones()

    def _detener(self):
        self.detenido_a_mano = True
        self.reintentar_hasta = 0.0
        if self.transmitiendo:
            self.proc.terminate()
        poner_pastilla(self.estado, "Detenido", "neutro")
        self._mostrar_botones()

    def _mostrar_botones(self):
        if self._activa():
            self.boton.configure(text="■  Detener", fg_color=PELIGRO,
                                 hover_color=PELIGRO_OSCURO)
            self.boton_finalizar.pack_forget()
            return
        texto = "▶  Reanudar" if self.ya_inicio else "▶  Iniciar"
        self.boton.configure(text=texto, fg_color=ACENTO, hover_color=ACENTO_OSCURO)
        self.boton_finalizar.pack(side="left", padx=(10, 0), after=self.boton)

    def _finalizar(self):
        if not messagebox.askyesno(
                "Finalizar transcripción",
                "Ya no se mandará más audio a esta transcripción y el servidor "
                "terminará de procesar lo pendiente.\n\n¿Finalizarla?"):
            return
        self.boton_finalizar.configure(state="disabled")
        job_id = self.evento["id"]
        cliente = self.app.cliente
        despachar = self.app.root.after

        def trabajo_hilo():
            try:
                # Si ya no está en la cola, el servidor ya la cerró sola y
                # solo falta quitar la fila.
                if any(e["id"] == job_id for e in cliente.eventos_esperando_audio()):
                    cliente.detener(job_id)
            except Exception as e:
                mensaje = str(e)
                despachar(0, lambda: self._fallo_finalizar(mensaje))
                return
            despachar(0, lambda: self.app.finalizada(job_id))
        threading.Thread(target=trabajo_hilo, daemon=True).start()

    def _fallo_finalizar(self, mensaje):
        self.boton_finalizar.configure(state="normal")
        messagebox.showerror("No pude finalizar", mensaje)

    def _ver_registro(self):
        contenido = "(sin registro todavía)"
        if self.ruta_log and os.path.isfile(self.ruta_log):
            try:
                with open(self.ruta_log, encoding="utf-8", errors="replace") as f:
                    contenido = f.read() or "(el registro está vacío: sin "\
                        "errores hasta ahora, o ffmpeg no ha impreso nada)"
            except OSError as e:
                contenido = f"No pude leer el registro: {e}"
        ventana_de_texto(self.app.root, "Registro de ffmpeg", contenido)

    def refrescar_estado(self):
        if self.buscando or self.proc is None or self.detenido_a_mano:
            return
        codigo = self.proc.poll()
        ahora = time.monotonic()
        if codigo is None:
            if ahora - self.lanzado_en >= SEG_PARA_DAR_POR_CONECTADO:
                self.reintentar_hasta = 0.0
                poner_pastilla(self.estado, "Transmitiendo", "exito")
            return
        if ahora < self.reintentar_hasta:
            # El servidor abre el puerto hasta terminar de cargar Whisper.
            poner_pastilla(self.estado, "Preparando servidor…", "aviso")
            self._buscar_y_lanzar(manual=False)
            return
        if codigo == 0:
            poner_pastilla(self.estado, "Se cortó el audio", "aviso")
        else:
            poner_pastilla(self.estado, f"Error (código {codigo}): ver registro",
                           "peligro")
        self._mostrar_botones()


class VentanaTranscripcion(ctk.CTkToplevel):
    """Ventana de solo lectura con el texto que lleva transcrito un
    trabajo: sin oradores ni edición, nomás para que el operador pueda
    seguirlo sin entrar al panel web. Se refresca sola cada pocos
    segundos mientras siga abierta."""

    def __init__(self, parent, app, trabajo):
        super().__init__(parent, fg_color=FONDO)
        self.app = app
        self.job_id = trabajo["id"]
        self.title("Transcripción: " + (trabajo.get("url")
                                        or f"Trabajo {self.job_id[:8]}"))
        self.geometry("620x460")
        self._cerrado = False
        self.protocol("WM_DELETE_WINDOW", self._cerrar)

        self.estado = ctk.CTkLabel(self, text="Cargando…", font=fuente(12),
                                   text_color=TEXTO_TENUE, anchor="w")
        self.estado.pack(fill="x", padx=14, pady=(12, 0))

        self.texto = caja_de_texto(self)
        self.texto.pack(fill="both", expand=True, padx=14, pady=(6, 14))

        traer_al_frente(self)
        self._refrescar()

    def _refrescar(self):
        if self._cerrado:
            return

        def trabajo_hilo():
            try:
                participaciones = self.app.cliente.participaciones(self.job_id)
            except Exception as e:
                mensaje = f"No pude actualizar: {e}"
                if not self._cerrado:
                    self.after(0, lambda: self.estado.configure(text=mensaje))
                return
            if not self._cerrado:
                self.after(0, lambda: self._aplicar(participaciones))
        threading.Thread(target=trabajo_hilo, daemon=True).start()
        self.after(REFRESCO_TEXTO_MS, self._refrescar)

    def _aplicar(self, participaciones):
        if self._cerrado:
            return
        self.estado.configure(
            text=f"{len(participaciones)} intervención(es) transcritas. "
                 "Se actualiza solo.")
        texto_completo = "\n\n".join(
            f"[{p['inicio_hms']}] {p['texto']}" for p in participaciones)
        al_fondo = self.texto.yview()[1] >= 0.999
        self.texto.configure(state="normal")
        self.texto.delete("1.0", "end")
        self.texto.insert("1.0", texto_completo or "(todavía no hay texto)")
        if al_fondo:
            self.texto.see("end")
        self.texto.configure(state="disabled")

    def _cerrar(self):
        self._cerrado = True
        self.destroy()


class FilaTrabajo:
    """Una fila en 'Mis trabajos': lo que el propio operador inició desde
    aquí mismo. Si quedó detenido/con error/finalizado, lo deja reanudar
    sin pedirle a un administrador que entre al panel web; si ya tiene
    sesión, deja ver el texto que lleva transcrito."""

    def __init__(self, parent, app, trabajo):
        self.app = app
        self.trabajo = trabajo
        self.ventana_texto = None

        self.marco = tarjeta(parent)
        self.marco.pack(fill="x", padx=2, pady=5)

        cabecera = ctk.CTkFrame(self.marco, fg_color="transparent")
        cabecera.pack(fill="x", padx=14, pady=(12, 0))

        nombre = trabajo.get("url") or f"Trabajo {trabajo['id'][:8]}"
        ctk.CTkLabel(cabecera, text=nombre, font=fuente(14, "bold"),
                     text_color=TEXTO, anchor="w", justify="left",
                     wraplength=560).pack(side="left", fill="x", expand=True)

        self.estado = pastilla(cabecera, "", "neutro")
        self.estado.pack(side="right", padx=(8, 0))

        fila = ctk.CTkFrame(self.marco, fg_color="transparent")
        fila.pack(fill="x", padx=14, pady=(8, 12))

        self.boton_reanudar = boton_primario(fila, "Reanudar", self._reanudar,
                                             width=120)
        self.boton_reanudar.pack(side="left", padx=(0, 10))

        self.boton_texto = boton_secundario(fila, "Ver transcripción", self._ver_texto)
        self.boton_texto.pack(side="left")

        self.boton_error = boton_secundario(fila, "Ver error", self._ver_error)

        self.actualizar(trabajo)

    def actualizar(self, trabajo):
        self.trabajo = trabajo
        estado = trabajo.get("estado", "")
        etiqueta, tono = ESTADO_ETIQUETAS.get(estado, (estado, "neutro"))
        poner_pastilla(self.estado, etiqueta, tono)
        puede_reanudar = estado in ESTADOS_REANUDABLES and bool(trabajo.get("sesion_id"))
        habilitar_primario(self.boton_reanudar, puede_reanudar)
        self.boton_texto.configure(
            state="normal" if trabajo.get("sesion_id") else "disabled")
        # El error puede traer hasta 4000 caracteres de log: va en su propia
        # ventana para que no se coma toda la lista.
        if trabajo.get("error"):
            self.boton_error.pack(side="left", padx=(10, 0))
        else:
            self.boton_error.pack_forget()

    def _reanudar(self):
        habilitar_primario(self.boton_reanudar, False)

        def trabajo_hilo():
            try:
                self.app.cliente.reanudar(self.trabajo["id"])
            except Exception as e:
                mensaje = str(e)
                self.app.root.after(0, lambda: messagebox.showerror(
                    "No pude reanudar", mensaje))
                self.app.root.after(0, lambda: habilitar_primario(
                    self.boton_reanudar, True))
                return
            self.app.root.after(0, self.app.refrescar_mis_trabajos)
            self.app.root.after(0, self.app.refrescar_eventos)
        threading.Thread(target=trabajo_hilo, daemon=True).start()

    def _ver_texto(self):
        if self.ventana_texto is not None and self.ventana_texto.winfo_exists():
            self.ventana_texto.lift()
            return
        self.ventana_texto = VentanaTranscripcion(self.app.root, self.app, self.trabajo)

    def _ver_error(self):
        ventana_de_texto(self.app.root, "Error del trabajo",
                         self.trabajo.get("error") or "")


class PanelNuevaTranscripcion(ctk.CTkFrame):
    """Elegir un evento de hoy (Sesión o Comisión) e iniciarlo, sin
    depender de que alguien con permisos de administrador lo haga primero
    desde el panel web."""

    def __init__(self, parent, app):
        super().__init__(parent, fg_color="transparent")
        self.app = app
        self.mapa_eventos = {}   # etiqueta mostrada -> evento completo

        paso1 = tarjeta_paso(self, 1, "Elige el tipo de evento")
        self.selector_tipo = ctk.CTkSegmentedButton(
            paso1, values=[TIPO_SESION, TIPO_COMISION], font=fuente(13),
            selected_color=ACENTO, selected_hover_color=ACENTO_OSCURO,
            unselected_color=GRIS, unselected_hover_color=GRIS_OSCURO,
            text_color="white", command=lambda _: self._cargar_eventos())
        self.selector_tipo.set(TIPO_SESION)
        self.selector_tipo.pack(anchor="w")

        paso2 = tarjeta_paso(self, 2, "Elige el evento")
        fila_evento = ctk.CTkFrame(paso2, fg_color="transparent")
        fila_evento.pack(fill="x")
        self.menu_evento = menu_opciones(
            fila_evento, width=560, command=lambda _: self._mostrar_detalle())
        self.menu_evento.pack(side="left", padx=(0, 10))
        boton_secundario(fila_evento, "Actualizar",
                         self._cargar_eventos).pack(side="left")
        self.detalle = ctk.CTkLabel(paso2, text="", font=fuente(12),
                                    text_color=TEXTO_TENUE, anchor="w",
                                    justify="left", wraplength=680)
        self.detalle.pack(fill="x", pady=(6, 0))

        paso3 = tarjeta_paso(self, 3, "Revisa la calidad e inicia")
        fila_inicio = ctk.CTkFrame(paso3, fg_color="transparent")
        fila_inicio.pack(fill="x")
        self.menu_modelo = menu_opciones(fila_inicio, width=130, values=MODELOS)
        self.menu_modelo.set(MODELO_DEFECTO)
        self.menu_modelo.pack(side="left", padx=(0, 10))
        ctk.CTkLabel(fila_inicio, text=f"Déjalo en “{MODELO_DEFECTO}” salvo que "
                     "te indiquen otro.", font=fuente(12),
                     text_color=TEXTO_TENUE).pack(side="left")
        self.boton_iniciar = boton_primario(
            fila_inicio, "▶  Iniciar transcripción", self._iniciar, width=210)
        self.boton_iniciar.pack(side="right")

        self.aviso = ctk.CTkLabel(self, text="", font=fuente(13), text_color=TEXTO,
                                  anchor="w", justify="left", wraplength=760)
        self.aviso.pack(fill="x", padx=4, pady=(8, 0))

        self._cargar_eventos()

    def _tipo_actual(self):
        # Tipo en el Congreso: 0 = Comisión, 1 = Sesión/Diputación permanente.
        return 0 if self.selector_tipo.get() == TIPO_COMISION else 1

    def _cargar_eventos(self):
        self.mapa_eventos = {}
        self.menu_evento.configure(values=[])
        self.menu_evento.set("Cargando…")
        self.detalle.configure(text="")
        tipo = self._tipo_actual()

        def trabajo_hilo():
            try:
                eventos = self.app.cliente.eventos(tipo)
            except Exception as e:
                mensaje = f"No pude cargar eventos: {e}"
                self.after(0, lambda: self._fallo_eventos(tipo, mensaje))
                return
            self.after(0, lambda: self._aplicar_eventos(tipo, eventos))
        threading.Thread(target=trabajo_hilo, daemon=True).start()

    def _fallo_eventos(self, tipo, mensaje):
        if tipo != self._tipo_actual():
            return
        self.menu_evento.set("(no se pudieron cargar)")
        self.aviso.configure(text=mensaje, text_color=PELIGRO)

    def _aplicar_eventos(self, tipo, eventos):
        # Si cambió de tipo mientras cargaba, esta respuesta ya no aplica.
        if tipo != self._tipo_actual():
            return
        self.mapa_eventos = {}
        etiquetas = []
        for e in eventos:
            etiqueta = (f"{e.get('fecha') or ''}  ·  "
                        f"{(e.get('descripcion') or '')[:60]}")
            self.mapa_eventos[etiqueta] = e
            etiquetas.append(etiqueta)
        self.menu_evento.configure(values=etiquetas)
        self.menu_evento.set(etiquetas[0] if etiquetas else "(sin eventos recientes)")
        self._mostrar_detalle()
        self.aviso.configure(
            text="" if eventos else "No hay eventos recientes de este tipo.",
            text_color=TEXTO)

    def _mostrar_detalle(self):
        evento = self.mapa_eventos.get(self.menu_evento.get())
        if not evento:
            self.detalle.configure(text="")
            return
        comisiones = ", ".join(evento.get("comisiones") or []) or "Sin comisión"
        participantes = evento.get("participantes")
        self.detalle.configure(
            text=f"{comisiones}  ·  {participantes} participantes "
                 "(salen automáticos del evento)")

    def _iniciar(self):
        evento = self.mapa_eventos.get(self.menu_evento.get())
        if not evento:
            messagebox.showwarning("Falta evento", "Elige un evento primero.")
            return
        habilitar_primario(self.boton_iniciar, False)
        tipo = self._tipo_actual()
        modelo = self.menu_modelo.get() or MODELO_DEFECTO

        def trabajo_hilo():
            try:
                trabajo = self.app.cliente.crear_desde_evento(
                    evento["id"], tipo, modelo)
            except Exception as e:
                mensaje = str(e)
                self.after(0, lambda: self._fallo(mensaje))
                return
            self.after(0, lambda: self._exito(trabajo))
        threading.Thread(target=trabajo_hilo, daemon=True).start()

    def _fallo(self, mensaje):
        habilitar_primario(self.boton_iniciar, True)
        messagebox.showerror("No pude iniciar", mensaje)

    def _exito(self, trabajo):
        habilitar_primario(self.boton_iniciar, True)
        self.app.refrescar_eventos()
        self.app.refrescar_mis_trabajos()
        if trabajo.get("fuente") == "srt":
            self.aviso.configure(
                text="Transcripción iniciada. Ahora elige el dispositivo y "
                     "manda el audio.", text_color=EXITO)
            self.app.ir_a_mandar_audio()
        else:
            self.aviso.configure(
                text="Transcripción iniciada desde la liga del evento; no "
                     "necesita audio. Síguela en “Mis trabajos”.",
                text_color=EXITO)


class PanelEsperandoAudio(ctk.CTkFrame):
    """Cola compartida de trabajos SRT ya corriendo que solo les falta
    que alguien les mande el audio — de cualquier usuario, no solo los
    propios: el operador necesita ver todas las que están esperando,
    las haya iniciado él mismo o un administrador desde el panel web."""

    def __init__(self, parent, app):
        super().__init__(parent, fg_color="transparent")
        self.app = app
        self.filas = {}
        self.dispositivos = []
        self._ya_cargo = False

        barra = ctk.CTkFrame(self, fg_color="transparent")
        barra.pack(fill="x", pady=(0, 4))
        self.aviso = ctk.CTkLabel(barra, text="Cargando…", font=fuente(13),
                                  text_color=TEXTO, anchor="w")
        self.aviso.pack(side="left", padx=4)
        boton_secundario(barra, "Buscar dispositivos",
                         self._refrescar_dispositivos).pack(side="right")
        boton_secundario(barra, "Actualizar",
                         self.refrescar).pack(side="right", padx=8)

        self.lista = lista_desplazable(self)

        self._refrescar_dispositivos()
        self.refrescar()

    def _refrescar_dispositivos(self):
        def trabajo_hilo():
            try:
                disp = self.app.cliente.listar_dispositivos()
            except RuntimeError as e:
                mensaje = str(e)
                self.after(0, lambda: messagebox.showerror("Dispositivos", mensaje))
                return
            self.after(0, lambda: self._aplicar_dispositivos(disp))
        threading.Thread(target=trabajo_hilo, daemon=True).start()

    def _aplicar_dispositivos(self, disp):
        self.dispositivos = disp
        for fila in self.filas.values():
            fila.actualizar_dispositivos(disp)

    def refrescar(self):
        def trabajo_hilo():
            try:
                eventos = self.app.cliente.eventos_esperando_audio()
            except Exception as e:
                mensaje = f"No pude actualizar la lista: {e}"
                self.after(0, lambda: self.aviso.configure(text=mensaje))
                return
            self.after(0, lambda: self._aplicar_eventos(eventos))
        threading.Thread(target=trabajo_hilo, daemon=True).start()

    def _aplicar_eventos(self, eventos):
        # self._ya_cargo distingue la primera carga (al abrir la app, donde
        # avisar de cada fila sería ruido) de las siguientes (donde una fila
        # nueva sí amerita avisar: es una transcripción recién creada o un
        # trabajo que acaban de reanudar y que había desaparecido de aquí
        # al detenerlo).
        avisar_nuevos = self._ya_cargo
        llegaron_nuevos = False
        vistos = set()
        for evento in eventos:
            vistos.add(evento["id"])
            if evento["id"] in self.filas:
                self.filas[evento["id"]].actualizar_trabajo(evento)
                continue
            self.filas[evento["id"]] = FilaEvento(
                self.lista, self.app, evento, self.dispositivos,
                nuevo=avisar_nuevos)
            llegaron_nuevos = True
        self._ya_cargo = True
        if avisar_nuevos and llegaron_nuevos:
            self.app.avisar_nuevo()
        # Quita las que ya no están en la cola y a las que nunca se les mandó
        # audio desde aquí. Las que sí transmitieron se quedan aunque el
        # servidor ya las haya cerrado, para poder reanudarlas o finalizarlas.
        for id_evento in list(self.filas):
            if id_evento not in vistos and not self.filas[id_evento].ya_inicio:
                self.quitar(id_evento)
        if not self.filas:
            self.aviso.configure(text="No hay transcripciones esperando audio.")
        else:
            self.aviso.configure(
                text="Elige el dispositivo y presiona Iniciar. Al terminar la "
                     "transcripción, presiona Detener y luego Finalizar.")

    def quitar(self, id_evento):
        fila = self.filas.pop(id_evento, None)
        if fila is not None:
            fila.marco.destroy()

    def ciclo_estado(self):
        for fila in self.filas.values():
            fila.refrescar_estado()


class PanelMisTrabajos(ctk.CTkFrame):
    """Los trabajos que el propio operador inició desde aquí, en
    cualquier estado — para reanudar los que se cayeron y ver el texto
    que llevan sin depender de nadie más."""

    def __init__(self, parent, app):
        super().__init__(parent, fg_color="transparent")
        self.app = app
        self.filas = {}

        barra = ctk.CTkFrame(self, fg_color="transparent")
        barra.pack(fill="x", pady=(0, 4))
        self.aviso = ctk.CTkLabel(barra, text="Cargando…", font=fuente(13),
                                  text_color=TEXTO, anchor="w")
        self.aviso.pack(side="left", padx=4)
        boton_secundario(barra, "Actualizar", self.refrescar).pack(side="right")

        self.lista = lista_desplazable(self)

        self.refrescar()

    def refrescar(self):
        def trabajo_hilo():
            try:
                trabajos = self.app.cliente.mis_trabajos()
            except Exception as e:
                mensaje = f"No pude actualizar la lista: {e}"
                self.after(0, lambda: self.aviso.configure(text=mensaje))
                return
            self.after(0, lambda: self._aplicar_trabajos(trabajos))
        threading.Thread(target=trabajo_hilo, daemon=True).start()

    def _aplicar_trabajos(self, trabajos):
        vistos = set()
        for trabajo in trabajos:
            vistos.add(trabajo["id"])
            if trabajo["id"] in self.filas:
                self.filas[trabajo["id"]].actualizar(trabajo)
            else:
                self.filas[trabajo["id"]] = FilaTrabajo(
                    self.lista, self.app, trabajo)
        for id_trabajo in list(self.filas):
            if id_trabajo not in vistos:
                self.filas[id_trabajo].marco.destroy()
                del self.filas[id_trabajo]
        self.aviso.configure(
            text=f"{len(trabajos)} trabajo(s) tuyo(s)." if trabajos
            else "Todavía no has iniciado ningún trabajo.")

    def contar(self, estado):
        return sum(1 for f in self.filas.values()
                   if f.trabajo.get("estado") == estado)


class VentanaPrincipal(ctk.CTkFrame):
    def __init__(self, root, cliente):
        super().__init__(root, fg_color="transparent")
        self.root = root
        self.cliente = cliente
        self.pack(fill="both", expand=True, padx=20, pady=(16, 12))

        cabecera = ctk.CTkFrame(self, fg_color="transparent")
        cabecera.pack(fill="x")
        titulos = ctk.CTkFrame(cabecera, fg_color="transparent")
        titulos.pack(side="left")
        ctk.CTkLabel(titulos, text=TITULO_APP, font=fuente(22, "bold"),
                     text_color=TEXTO, anchor="w").pack(anchor="w")
        ctk.CTkLabel(titulos, text="Inicia una transcripción y manda el audio "
                     "de la consola al servidor.", font=fuente(13),
                     text_color=TEXTO_TENUE, anchor="w").pack(anchor="w")
        if cliente.es_demo:
            pastilla(cabecera, "Modo demostración: datos de ejemplo",
                     "aviso").pack(side="right", anchor="n")

        resumen = ctk.CTkFrame(self, fg_color="transparent")
        resumen.pack(fill="x", pady=(12, 8))
        self.contadores = {}
        for columna, (clave, texto, color) in enumerate((
                ("esperando", "Esperando audio", AVISO),
                ("transmitiendo", "Transmitiendo", EXITO),
                ("ejecutando", "Mis trabajos en curso", ACENTO),
                ("error", "Con error", PELIGRO))):
            resumen.grid_columnconfigure(columna, weight=1, uniform="resumen")
            caja = tarjeta(resumen)
            caja.grid(row=0, column=columna, sticky="ew",
                      padx=(0 if columna == 0 else 8, 0))
            numero = ctk.CTkLabel(caja, text="0", font=fuente(22, "bold"),
                                  text_color=color)
            numero.pack(pady=(8, 0))
            ctk.CTkLabel(caja, text=texto, font=fuente(12),
                         text_color=TEXTO_TENUE).pack(pady=(0, 8))
            self.contadores[clave] = numero

        self.pestanas = ctk.CTkTabview(
            self, fg_color=FONDO, corner_radius=10,
            segmented_button_selected_color=ACENTO,
            segmented_button_selected_hover_color=ACENTO_OSCURO,
            segmented_button_unselected_color=GRIS,
            segmented_button_unselected_hover_color=GRIS_OSCURO,
            text_color="white")
        self.pestanas.pack(fill="both", expand=True)
        for nombre in (TAB_NUEVA, TAB_AUDIO, TAB_MIOS):
            self.pestanas.add(nombre)

        self.panel_nueva = PanelNuevaTranscripcion(self.pestanas.tab(TAB_NUEVA), self)
        self.panel_esperando = PanelEsperandoAudio(self.pestanas.tab(TAB_AUDIO), self)
        self.panel_mios = PanelMisTrabajos(self.pestanas.tab(TAB_MIOS), self)
        for panel in (self.panel_nueva, self.panel_esperando, self.panel_mios):
            panel.pack(fill="both", expand=True)

        self.root.after(500, self._ciclo_estado)
        self.root.after(REFRESCO_EVENTOS_MS, self._ciclo_eventos)
        self.root.after(REFRESCO_TRABAJOS_MS, self._ciclo_trabajos)

    def refrescar_eventos(self):
        self.panel_esperando.refrescar()

    def refrescar_mis_trabajos(self):
        self.panel_mios.refrescar()

    def finalizada(self, job_id):
        self.panel_esperando.quitar(job_id)
        self.refrescar_eventos()
        self.refrescar_mis_trabajos()

    def ir_a_mandar_audio(self):
        self.pestanas.set(TAB_AUDIO)

    def avisar_nuevo(self):
        """Suena y trae la ventana al frente para que la operadora note que
        llegó (o volvió) una transcripción esperando audio, aunque tenga la
        ventana minimizada o detrás de otras."""
        try:
            self.root.bell()
        except Exception:
            pass
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))

    def _actualizar_resumen(self):
        filas = self.panel_esperando.filas.values()
        transmitiendo = sum(1 for f in filas if f.transmitiendo)
        valores = {
            "esperando": len(filas) - transmitiendo,
            "transmitiendo": transmitiendo,
            "ejecutando": self.panel_mios.contar("ejecutando"),
            "error": self.panel_mios.contar("error"),
        }
        for clave, valor in valores.items():
            self.contadores[clave].configure(text=str(valor))

    def _ciclo_estado(self):
        self.panel_esperando.ciclo_estado()
        self._actualizar_resumen()
        self.root.after(REFRESCO_ESTADO_MS, self._ciclo_estado)

    def _ciclo_eventos(self):
        self.refrescar_eventos()
        self.root.after(REFRESCO_EVENTOS_MS, self._ciclo_eventos)

    def _ciclo_trabajos(self):
        self.refrescar_mis_trabajos()
        self.root.after(REFRESCO_TRABAJOS_MS, self._ciclo_trabajos)


class VentanaLogin(ctk.CTkFrame):
    def __init__(self, root, al_ingresar, crear_cliente=None):
        super().__init__(root, fg_color="transparent")
        self.al_ingresar = al_ingresar
        self.crear_cliente = crear_cliente or ClienteAPI
        self.es_demo = crear_cliente is not None
        self.pack(fill="both", expand=True)

        cfg = {"api_base": "https://demo.local", "email": "operador@demo.local"} \
            if self.es_demo else cargar_config()

        caja = tarjeta(self)
        caja.place(relx=0.5, rely=0.5, anchor="center")

        ctk.CTkLabel(caja, text=TITULO_APP, font=fuente(22, "bold"),
                     text_color=TEXTO).pack(padx=36, pady=(28, 2))
        ctk.CTkLabel(caja, text="Entra con el mismo usuario del sistema de "
                     "transcripción.", font=fuente(13),
                     text_color=TEXTO_TENUE).pack(padx=36, pady=(0, 16))

        self.var_url = ctk.StringVar(value=cfg.get("api_base", "https://"))
        self.var_email = ctk.StringVar(value=cfg.get("email", ""))
        self.var_password = ctk.StringVar(value="demo" if self.es_demo else "")

        self._campo(caja, "Servidor", self.var_url)
        self._campo(caja, "Email", self.var_email)
        entrada_pw = self._campo(caja, "Contraseña", self.var_password, show="•")
        entrada_pw.bind("<Return>", lambda e: self._entrar())

        self.estado = ctk.CTkLabel(caja, text="", font=fuente(12),
                                   text_color=PELIGRO, wraplength=340)
        self.estado.pack(padx=36, pady=(8, 0))

        self.boton = boton_primario(caja, "Entrar", self._entrar, width=340)
        self.boton.pack(fill="x", padx=36, pady=(6, 28))

    def _campo(self, parent, texto, variable, show=None):
        ctk.CTkLabel(parent, text=texto, font=fuente(13), text_color=TEXTO,
                     anchor="w").pack(fill="x", padx=36)
        entrada = ctk.CTkEntry(parent, textvariable=variable, width=340, height=36,
                               show=show, font=fuente(13), fg_color=BLANCO,
                               border_color=BORDE, text_color=TEXTO)
        entrada.pack(fill="x", padx=36, pady=(2, 10))
        return entrada

    def _entrar(self):
        url = self.var_url.get().strip()
        email = self.var_email.get().strip()
        password = self.var_password.get()
        if not url or not email or not password:
            self.estado.configure(text="Completa servidor, email y contraseña.")
            return
        habilitar_primario(self.boton, False)
        self.estado.configure(text="Entrando…", text_color=TEXTO_TENUE)

        def trabajo():
            cliente = self.crear_cliente(url)
            try:
                cliente.login(email, password)
            except Exception as e:
                mensaje = str(e)
                self.after(0, lambda: self._fallo(mensaje))
                return
            if not cliente.es_demo:
                guardar_config({"api_base": url, "email": email})
            self.after(0, lambda: self.al_ingresar(cliente))
        threading.Thread(target=trabajo, daemon=True).start()

    def _fallo(self, mensaje):
        habilitar_primario(self.boton, True)
        self.estado.configure(text=mensaje, text_color=PELIGRO)


def main():
    ctk.set_appearance_mode("light")
    root = ctk.CTk(fg_color=FONDO)
    root.title(TITULO_APP)
    root.geometry("640x520")

    def al_ingresar(cliente):
        for w in root.winfo_children():
            w.destroy()
        root.geometry("920x700")
        root.minsize(860, 620)
        VentanaPrincipal(root, cliente)

    crear_cliente = None
    if "--demo" in sys.argv:
        from demo import ClienteDemo
        crear_cliente = ClienteDemo
    VentanaLogin(root, al_ingresar, crear_cliente)
    root.mainloop()


if __name__ == "__main__":
    main()
