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
  3. Pestaña "Comisiones esperando audio": cola compartida (de cualquier
     usuario) de trabajos que ya están corriendo y solo les falta que
     alguien les mande el audio — elige el dispositivo (USB o canal
     Dante) y presiona "Iniciar". Puede tener varias transmitiendo a la
     vez.
  4. Pestaña "Mis trabajos": los que él mismo inició, con su estado. Si
     alguno se cayó (detenido/con error), lo puede reanudar sin pedirle
     a nadie más que entre al panel; también puede ver el texto que
     lleva transcrito (sin oradores, solo para seguirlo).

No requiere Python ni ffmpeg instalados aparte: se distribuye como un
único .exe con todo incluido (ver .github/workflows/build-agente-captura.yml).
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
from urllib.parse import urlparse

import requests

CONFIG_NOMBRE = "agente_captura_config.json"
REFRESCO_EVENTOS_MS = 20_000
REFRESCO_ESTADO_MS = 2_000
REFRESCO_TRABAJOS_MS = 10_000
REFRESCO_TEXTO_MS = 3_000

MODELOS = ["tiny", "base", "small", "medium", "large-v3"]
MODELO_DEFECTO = "small"

# Mismos estados que api/jobs.py: solo desde estos tiene sentido reanudar
# (un trabajo "ejecutando" ya está corriendo, no hay nada que reanudar).
ESTADOS_REANUDABLES = {"detenido", "error", "finalizado"}

ESTADO_ETIQUETAS = {
    "ejecutando": "🟢 ejecutando",
    "deteniendo": "🟡 deteniendo…",
    "detenido": "⚪ detenido",
    "error": "🔴 error",
    "finalizado": "✅ finalizado",
}


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

    @property
    def host(self):
        return urlparse(self.base_url).hostname


class FilaEvento:
    """Una comisión/evento en la lista, con su selector de dispositivo,
    botón de iniciar/detener y estado. Puede haber varias transmitiendo
    a la vez, cada una con su propio proceso ffmpeg."""

    def __init__(self, parent, app, evento, dispositivos, nuevo=False):
        self.app = app
        self.evento = evento
        self.proc = None
        self.ruta_log = None
        self.mapa_dispositivos = {}   # nombre visible -> id alterno (o None)

        self.marco = ttk.Frame(parent, padding=6, relief="groove", borderwidth=1)
        self.marco.pack(fill="x", padx=4, pady=3)

        if nuevo:
            # Aviso temporal para que no se pase por alto un trabajo que
            # acaba de reaparecer (p. ej. lo acaban de reanudar): además
            # de la campana y de traer la ventana al frente (ver
            # VentanaPrincipal._aplicar_eventos), esta etiqueta marca CUÁL
            # fila es la nueva. Se quita sola a los 20 s.
            self.aviso_nuevo = ttk.Label(
                self.marco, text="● Nuevo — manda audio",
                foreground="#c0392b", font=("", 9, "bold"))
            self.aviso_nuevo.pack(anchor="w")
            self.marco.after(20_000, self._quitar_aviso_nuevo)
        else:
            self.aviso_nuevo = None

        nombre = evento.get("url") or f"Trabajo {evento['id'][:8]}"
        ttk.Label(self.marco, text=nombre, font=("", 10, "bold")).pack(anchor="w")
        ttk.Label(self.marco, text=f"puerto {evento.get('puerto')}",
                  foreground="#777").pack(anchor="w")

        fila = ttk.Frame(self.marco)
        fila.pack(fill="x", pady=(4, 0))

        self.combo = ttk.Combobox(fila, values=[], state="readonly", width=40)
        self.combo.pack(side="left", padx=(0, 8))
        self.actualizar_dispositivos(dispositivos)

        self.boton = ttk.Button(fila, text="Iniciar", command=self._alternar)
        self.boton.pack(side="left")

        self.estado_var = tk.StringVar(value="⚪ Detenido")
        ttk.Label(fila, textvariable=self.estado_var).pack(side="left", padx=8)

        self.boton_log = ttk.Button(fila, text="Ver registro",
                                    command=self._ver_registro, state="disabled")
        self.boton_log.pack(side="left", padx=8)

    def _quitar_aviso_nuevo(self):
        if self.aviso_nuevo is not None:
            self.aviso_nuevo.destroy()
            self.aviso_nuevo = None

    def actualizar_dispositivos(self, dispositivos):
        """dispositivos: lista de (nombre, id_alterno_o_None)."""
        self.mapa_dispositivos = {nombre: id_alt for nombre, id_alt in dispositivos}
        nombres = list(self.mapa_dispositivos.keys())
        actual = self.combo.get()
        self.combo["values"] = nombres
        if actual in nombres:
            self.combo.set(actual)
        elif nombres and not self.combo.get():
            self.combo.current(0)

    def _alternar(self):
        if self.proc and self.proc.poll() is None:
            self._detener()
        else:
            self._iniciar()

    def _iniciar(self):
        dispositivo = self.combo.get()
        if not dispositivo:
            messagebox.showwarning("Falta dispositivo",
                                   "Elige un dispositivo de audio primero.")
            return
        # Preferir el id alterno (@device_cm_{...}) sobre el nombre visible:
        # en Windows, ffmpeg suele fallar al abrir por nombre si tiene
        # acentos/símbolos, aunque lo liste bien.
        referencia = self.mapa_dispositivos.get(dispositivo) or dispositivo
        host = self.app.cliente.host
        puerto = self.evento["puerto"]
        passphrase = self.evento["passphrase"]
        destino = f"srt://{host}:{puerto}?mode=caller&passphrase={passphrase}"
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
        fd, self.ruta_log = tempfile.mkstemp(prefix="agente_captura_", suffix=".log")
        os.close(fd)
        archivo_log = open(self.ruta_log, "w", encoding="utf-8", errors="replace")
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=archivo_log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            messagebox.showerror("No pude iniciar", str(e))
            return
        finally:
            archivo_log.close()
        self.boton.config(text="Detener")
        self.boton_log.config(state="normal")
        self.estado_var.set("🟡 Conectando…")

    def _detener(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.boton.config(text="Iniciar")
        self.estado_var.set("⚪ Detenido")

    def _ver_registro(self):
        contenido = "(sin registro todavía)"
        if self.ruta_log and os.path.isfile(self.ruta_log):
            try:
                with open(self.ruta_log, encoding="utf-8", errors="replace") as f:
                    contenido = f.read() or "(el registro está vacío — sin "\
                        "errores hasta ahora, o ffmpeg no ha impreso nada)"
            except OSError as e:
                contenido = f"No pude leer el registro: {e}"

        ventana = tk.Toplevel(self.marco)
        ventana.title("Registro de ffmpeg")
        ventana.geometry("640x360")
        texto = scrolledtext.ScrolledText(ventana, wrap="word")
        texto.pack(fill="both", expand=True, padx=8, pady=8)
        texto.insert("1.0", contenido)
        texto.config(state="disabled")

    def refrescar_estado(self):
        if self.proc is None:
            return
        codigo = self.proc.poll()
        if codigo is None:
            self.estado_var.set("🟢 Transmitiendo")
        elif codigo == 0:
            self.estado_var.set("⚪ Detenido")
            self.boton.config(text="Iniciar")
        else:
            self.estado_var.set(f"🔴 Error (código {codigo}) — ver registro")
            self.boton.config(text="Iniciar")


def _lista_desplazable(parent):
    """Frame con scroll vertical para apilar filas dentro de una pestaña
    (igual patrón para "esperando audio" y "mis trabajos"). Devuelve el
    frame donde hay que pack() cada fila."""
    contenedor = ttk.Frame(parent)
    contenedor.pack(fill="both", expand=True, pady=8)
    lienzo = tk.Canvas(contenedor, highlightthickness=0)
    scroll = ttk.Scrollbar(contenedor, orient="vertical", command=lienzo.yview)
    lista = ttk.Frame(lienzo)
    lista.bind("<Configure>",
              lambda e: lienzo.configure(scrollregion=lienzo.bbox("all")))
    lienzo.create_window((0, 0), window=lista, anchor="nw")
    lienzo.configure(yscrollcommand=scroll.set)
    lienzo.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    return lista


class VentanaTranscripcion(tk.Toplevel):
    """Ventana de solo lectura con el texto que lleva transcrito un
    trabajo — sin oradores ni edición, nomás para que el operador pueda
    seguirlo sin entrar al panel web. Se refresca sola cada pocos
    segundos mientras siga abierta."""

    def __init__(self, parent, app, trabajo):
        super().__init__(parent)
        self.app = app
        self.job_id = trabajo["id"]
        self.title("Transcripción — " + (trabajo.get("url")
                                         or f"Trabajo {self.job_id[:8]}"))
        self.geometry("560x420")
        self._cerrado = False
        self.protocol("WM_DELETE_WINDOW", self._cerrar)

        self.estado_var = tk.StringVar(value="Cargando…")
        ttk.Label(self, textvariable=self.estado_var,
                  foreground="#777").pack(anchor="w", padx=8, pady=(8, 0))

        self.texto = scrolledtext.ScrolledText(self, wrap="word", state="disabled")
        self.texto.pack(fill="both", expand=True, padx=8, pady=8)

        self._refrescar()

    def _refrescar(self):
        if self._cerrado:
            return

        def trabajo_hilo():
            try:
                participaciones = self.app.cliente.participaciones(self.job_id)
            except Exception as e:
                if not self._cerrado:
                    self.after(0, lambda: self.estado_var.set(
                        f"No pude actualizar: {e}"))
                return
            if not self._cerrado:
                self.after(0, lambda: self._aplicar(participaciones))
        threading.Thread(target=trabajo_hilo, daemon=True).start()
        self.after(REFRESCO_TEXTO_MS, self._refrescar)

    def _aplicar(self, participaciones):
        self.estado_var.set(
            f"{len(participaciones)} intervención(es) transcritas — se "
            "actualiza solo")
        texto_completo = "\n".join(
            f"[{p['inicio_hms']}] {p['texto']}" for p in participaciones)
        al_fondo = self.texto.yview()[1] >= 0.999
        self.texto.config(state="normal")
        self.texto.delete("1.0", "end")
        self.texto.insert("1.0", texto_completo or "(todavía no hay texto)")
        if al_fondo:
            self.texto.see("end")
        self.texto.config(state="disabled")

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

        self.marco = ttk.Frame(parent, padding=6, relief="groove", borderwidth=1)
        self.marco.pack(fill="x", padx=4, pady=3)

        nombre = trabajo.get("url") or f"Trabajo {trabajo['id'][:8]}"
        ttk.Label(self.marco, text=nombre, font=("", 10, "bold"),
                  wraplength=560, justify="left").pack(anchor="w")

        fila = ttk.Frame(self.marco)
        fila.pack(fill="x", pady=(4, 0))

        self.estado_var = tk.StringVar()
        ttk.Label(fila, textvariable=self.estado_var).pack(side="left")

        self.boton_reanudar = ttk.Button(
            fila, text="Reanudar", command=self._reanudar)
        self.boton_reanudar.pack(side="left", padx=8)

        self.boton_texto = ttk.Button(
            fila, text="Ver transcripción", command=self._ver_texto)
        self.boton_texto.pack(side="left")

        self.error_var = tk.StringVar()
        ttk.Label(self.marco, textvariable=self.error_var, foreground="#b00",
                 wraplength=560, justify="left").pack(anchor="w", pady=(2, 0))

        self.actualizar(trabajo)

    def actualizar(self, trabajo):
        self.trabajo = trabajo
        estado = trabajo.get("estado", "")
        self.estado_var.set(ESTADO_ETIQUETAS.get(estado, estado))
        self.error_var.set(f"Error: {trabajo['error']}" if trabajo.get("error") else "")
        puede_reanudar = estado in ESTADOS_REANUDABLES and bool(trabajo.get("sesion_id"))
        self.boton_reanudar.config(state="normal" if puede_reanudar else "disabled")
        self.boton_texto.config(
            state="normal" if trabajo.get("sesion_id") else "disabled")

    def _reanudar(self):
        self.boton_reanudar.config(state="disabled")

        def trabajo_hilo():
            try:
                self.app.cliente.reanudar(self.trabajo["id"])
            except Exception as e:
                self.app.root.after(0, lambda: messagebox.showerror(
                    "No pude reanudar", str(e)))
                self.app.root.after(0, lambda: self.boton_reanudar.config(
                    state="normal"))
                return
            self.app.root.after(0, self.app.refrescar_mis_trabajos)
            self.app.root.after(0, self.app.refrescar_eventos)
        threading.Thread(target=trabajo_hilo, daemon=True).start()

    def _ver_texto(self):
        if self.ventana_texto is not None and self.ventana_texto.winfo_exists():
            self.ventana_texto.lift()
            return
        self.ventana_texto = VentanaTranscripcion(self.marco, self.app, self.trabajo)


class PanelNuevaTranscripcion(ttk.Frame):
    """Elegir un evento de hoy (Sesión o Comisión) e iniciarlo, sin
    depender de que alguien con permisos de administrador lo haga primero
    desde el panel web."""

    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app = app
        self.mapa_eventos = {}   # etiqueta mostrada -> evento completo

        ttk.Label(self, text="Elige el tipo y el evento de hoy; los "
                 "participantes salen automáticos de ahí, no hace falta "
                 "escribir nada más.", foreground="#777", wraplength=560,
                 justify="left").pack(anchor="w", pady=(0, 10))

        fila1 = ttk.Frame(self)
        fila1.pack(fill="x")
        ttk.Label(fila1, text="Tipo", width=10).pack(side="left")
        self.combo_tipo = ttk.Combobox(
            fila1, state="readonly", width=32,
            values=["Sesión / Diputación permanente", "Comisión"])
        self.combo_tipo.current(0)
        self.combo_tipo.pack(side="left", padx=(8, 0))
        self.combo_tipo.bind("<<ComboboxSelected>>",
                             lambda e: self._cargar_eventos())

        fila2 = ttk.Frame(self)
        fila2.pack(fill="x", pady=(10, 0))
        ttk.Label(fila2, text="Evento", width=10).pack(side="left")
        self.combo_evento = ttk.Combobox(fila2, state="readonly", width=66)
        self.combo_evento.pack(side="left", padx=(8, 0))
        ttk.Button(fila2, text="🔄", width=3,
                   command=self._cargar_eventos).pack(side="left", padx=4)

        fila3 = ttk.Frame(self)
        fila3.pack(fill="x", pady=(10, 0))
        ttk.Label(fila3, text="Modelo", width=10).pack(side="left")
        self.combo_modelo = ttk.Combobox(
            fila3, state="readonly", width=15, values=MODELOS)
        self.combo_modelo.set(MODELO_DEFECTO)
        self.combo_modelo.pack(side="left", padx=(8, 0))

        self.boton_iniciar = ttk.Button(
            self, text="Iniciar transcripción", command=self._iniciar)
        self.boton_iniciar.pack(anchor="w", pady=(14, 0))

        self.aviso_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.aviso_var, foreground="#777",
                 wraplength=560, justify="left").pack(anchor="w", pady=(10, 0))

        self._cargar_eventos()

    def _tipo_actual(self):
        # 0 = Comisión, 1 = Sesión/Diputación permanente (mismo índice que
        # el combo arriba: "Sesión..." es la opción 0 del combo, pero tipo
        # 1 en el Congreso).
        return 0 if self.combo_tipo.current() == 1 else 1

    def _cargar_eventos(self):
        self.combo_evento.set("Cargando…")
        self.combo_evento["values"] = []
        tipo = self._tipo_actual()

        def trabajo_hilo():
            try:
                eventos = self.app.cliente.eventos(tipo)
            except Exception as e:
                self.after(0, lambda: self.aviso_var.set(
                    f"No pude cargar eventos: {e}"))
                return
            self.after(0, lambda: self._aplicar_eventos(eventos))
        threading.Thread(target=trabajo_hilo, daemon=True).start()

    def _aplicar_eventos(self, eventos):
        self.mapa_eventos = {}
        etiquetas = []
        for e in eventos:
            comisiones = ", ".join(e.get("comisiones") or []) or "(sin comisión)"
            marca = " 🔗" if e.get("liga") else ""
            etiqueta = (f"{e.get('fecha') or ''} — "
                       f"{(e.get('descripcion') or '')[:60]} "
                       f"[{comisiones}]{marca}")
            self.mapa_eventos[etiqueta] = e
            etiquetas.append(etiqueta)
        self.combo_evento["values"] = etiquetas
        if etiquetas:
            self.combo_evento.current(0)
        else:
            self.combo_evento.set("(sin eventos recientes)")
        self.aviso_var.set(f"{len(eventos)} evento(s) encontrados." if eventos
                           else "No hay eventos recientes de este tipo.")

    def _iniciar(self):
        etiqueta = self.combo_evento.get()
        evento = self.mapa_eventos.get(etiqueta)
        if not evento:
            messagebox.showwarning("Falta evento", "Elige un evento primero.")
            return
        self.boton_iniciar.config(state="disabled")
        tipo = self._tipo_actual()
        modelo = self.combo_modelo.get() or MODELO_DEFECTO

        def trabajo_hilo():
            try:
                self.app.cliente.crear_desde_evento(evento["id"], tipo, modelo)
            except Exception as e:
                self.after(0, lambda: self._fallo(str(e)))
                return
            self.after(0, self._exito)
        threading.Thread(target=trabajo_hilo, daemon=True).start()

    def _fallo(self, mensaje):
        self.boton_iniciar.config(state="normal")
        messagebox.showerror("No pude iniciar", mensaje)

    def _exito(self):
        self.boton_iniciar.config(state="normal")
        self.aviso_var.set(
            "Transcripción iniciada — revisa la pestaña 'Comisiones "
            "esperando audio' si necesita que le mandes audio, o "
            "'Mis trabajos' para seguirla.")
        self.app.refrescar_eventos()
        self.app.refrescar_mis_trabajos()


class PanelEsperandoAudio(ttk.Frame):
    """Cola compartida de trabajos SRT ya corriendo que solo les falta
    que alguien les mande el audio — de cualquier usuario, no solo los
    propios: el operador necesita ver todas las comisiones esperando,
    las haya iniciado él mismo o un administrador desde el panel web."""

    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app = app
        self.filas = {}
        self.dispositivos = []
        self._ya_cargo = False

        barra = ttk.Frame(self)
        barra.pack(fill="x")
        ttk.Button(barra, text="🔄 Dispositivos",
                   command=self._refrescar_dispositivos).pack(side="right", padx=4)
        ttk.Button(barra, text="🔄 Eventos",
                   command=self.refrescar).pack(side="right")

        self.lista = _lista_desplazable(self)

        self.aviso_var = tk.StringVar(value="Cargando…")
        ttk.Label(self, textvariable=self.aviso_var,
                  foreground="#777").pack(anchor="w")

        self._refrescar_dispositivos()
        self.refrescar()

    def _refrescar_dispositivos(self):
        def trabajo_hilo():
            try:
                disp = listar_dispositivos_audio()
            except RuntimeError as e:
                self.after(0, lambda: messagebox.showerror("Dispositivos", str(e)))
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
                self.after(0, lambda: self.aviso_var.set(
                    f"No pude actualizar la lista: {e}"))
                return
            self.after(0, lambda: self._aplicar_eventos(eventos))
        threading.Thread(target=trabajo_hilo, daemon=True).start()

    def _aplicar_eventos(self, eventos):
        # self._ya_cargo distingue la primera carga (al abrir la app, donde
        # avisar de cada fila sería ruido) de las siguientes (donde una fila
        # nueva sí amerita avisar: es una comisión recién creada o un
        # trabajo que acaban de reanudar y que había desaparecido de aquí
        # al detenerlo).
        avisar_nuevos = self._ya_cargo
        llegaron_nuevos = False
        vistos = set()
        for evento in eventos:
            vistos.add(evento["id"])
            if evento["id"] in self.filas:
                continue
            self.filas[evento["id"]] = FilaEvento(
                self.lista, self.app, evento, self.dispositivos,
                nuevo=avisar_nuevos)
            llegaron_nuevos = True
        self._ya_cargo = True
        if avisar_nuevos and llegaron_nuevos:
            self.app.avisar_nuevo()
        # Quita de la lista los que ya no están esperando audio y no
        # están transmitiendo (para no interrumpir una transmisión activa
        # solo porque tardó en refrescarse el estado en el servidor).
        for id_evento in list(self.filas):
            if id_evento not in vistos:
                fila = self.filas[id_evento]
                if fila.proc is None or fila.proc.poll() is not None:
                    fila.marco.destroy()
                    del self.filas[id_evento]
        if not eventos and not self.filas:
            self.aviso_var.set("No hay comisiones esperando audio ahorita.")
        else:
            self.aviso_var.set(f"{len(eventos)} comisión(es) esperando audio.")

    def ciclo_estado(self):
        for fila in self.filas.values():
            fila.refrescar_estado()


class PanelMisTrabajos(ttk.Frame):
    """Los trabajos que el propio operador inició desde aquí, en
    cualquier estado — para reanudar los que se cayeron y ver el texto
    que llevan sin depender de nadie más."""

    def __init__(self, parent, app):
        super().__init__(parent, padding=10)
        self.app = app
        self.filas = {}

        barra = ttk.Frame(self)
        barra.pack(fill="x")
        ttk.Button(barra, text="🔄 Actualizar",
                   command=self.refrescar).pack(side="right")

        self.lista = _lista_desplazable(self)

        self.aviso_var = tk.StringVar(value="Cargando…")
        ttk.Label(self, textvariable=self.aviso_var,
                  foreground="#777").pack(anchor="w")

        self.refrescar()

    def refrescar(self):
        def trabajo_hilo():
            try:
                trabajos = self.app.cliente.mis_trabajos()
            except Exception as e:
                self.after(0, lambda: self.aviso_var.set(
                    f"No pude actualizar la lista: {e}"))
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
        self.aviso_var.set(f"{len(trabajos)} trabajo(s) tuyo(s)." if trabajos
                           else "Todavía no has iniciado ningún trabajo.")


class VentanaPrincipal(ttk.Frame):
    def __init__(self, root, cliente):
        super().__init__(root, padding=10)
        self.root = root
        self.cliente = cliente
        self.pack(fill="both", expand=True)

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)

        self.panel_nueva = PanelNuevaTranscripcion(notebook, self)
        self.panel_esperando = PanelEsperandoAudio(notebook, self)
        self.panel_mios = PanelMisTrabajos(notebook, self)

        notebook.add(self.panel_nueva, text="Nueva transcripción")
        notebook.add(self.panel_esperando, text="Comisiones esperando audio")
        notebook.add(self.panel_mios, text="Mis trabajos")

        self.root.after(REFRESCO_ESTADO_MS, self._ciclo_estado)
        self.root.after(REFRESCO_EVENTOS_MS, self._ciclo_eventos)
        self.root.after(REFRESCO_TRABAJOS_MS, self._ciclo_trabajos)

    def refrescar_eventos(self):
        self.panel_esperando.refrescar()

    def refrescar_mis_trabajos(self):
        self.panel_mios.refrescar()

    def avisar_nuevo(self):
        """Suena y trae la ventana al frente para que la operadora note que
        llegó (o volvió) una comisión esperando audio, aunque tenga la
        ventana minimizada o detrás de otras."""
        try:
            self.root.bell()
        except Exception:
            pass
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))

    def _ciclo_estado(self):
        self.panel_esperando.ciclo_estado()
        self.root.after(REFRESCO_ESTADO_MS, self._ciclo_estado)

    def _ciclo_eventos(self):
        self.refrescar_eventos()
        self.root.after(REFRESCO_EVENTOS_MS, self._ciclo_eventos)

    def _ciclo_trabajos(self):
        self.refrescar_mis_trabajos()
        self.root.after(REFRESCO_TRABAJOS_MS, self._ciclo_trabajos)


class VentanaLogin(ttk.Frame):
    def __init__(self, root, al_ingresar):
        super().__init__(root, padding=20)
        self.al_ingresar = al_ingresar
        self.pack(fill="both", expand=True)

        cfg = cargar_config()

        ttk.Label(self, text="Agente de captura de audio",
                  font=("", 13, "bold")).grid(row=0, column=0, columnspan=2,
                                              pady=(0, 14))

        ttk.Label(self, text="Servidor").grid(row=1, column=0, sticky="e", pady=4)
        self.var_url = tk.StringVar(value=cfg.get("api_base", "https://"))
        ttk.Entry(self, textvariable=self.var_url, width=32).grid(
            row=1, column=1, pady=4)

        ttk.Label(self, text="Email").grid(row=2, column=0, sticky="e", pady=4)
        self.var_email = tk.StringVar(value=cfg.get("email", ""))
        ttk.Entry(self, textvariable=self.var_email, width=32).grid(
            row=2, column=1, pady=4)

        ttk.Label(self, text="Contraseña").grid(row=3, column=0, sticky="e", pady=4)
        self.var_password = tk.StringVar()
        entrada_pw = ttk.Entry(self, textvariable=self.var_password,
                               width=32, show="•")
        entrada_pw.grid(row=3, column=1, pady=4)
        entrada_pw.bind("<Return>", lambda e: self._entrar())

        self.var_estado = tk.StringVar()
        ttk.Label(self, textvariable=self.var_estado,
                  foreground="#b00").grid(row=4, column=0, columnspan=2)

        self.boton = ttk.Button(self, text="Entrar", command=self._entrar)
        self.boton.grid(row=5, column=0, columnspan=2, pady=(10, 0))

    def _entrar(self):
        url = self.var_url.get().strip()
        email = self.var_email.get().strip()
        password = self.var_password.get()
        if not url or not email or not password:
            self.var_estado.set("Completa servidor, email y contraseña.")
            return
        self.boton.config(state="disabled")
        self.var_estado.set("Entrando…")

        def trabajo():
            cliente = ClienteAPI(url)
            try:
                cliente.login(email, password)
            except Exception as e:
                self.after(0, lambda: self._fallo(str(e)))
                return
            guardar_config({"api_base": url, "email": email})
            self.after(0, lambda: self.al_ingresar(cliente))
        threading.Thread(target=trabajo, daemon=True).start()

    def _fallo(self, mensaje):
        self.boton.config(state="normal")
        self.var_estado.set(mensaje)


def main():
    root = tk.Tk()
    root.title("Agente de captura de audio")
    root.geometry("640x480")

    def al_ingresar(cliente):
        for w in root.winfo_children():
            w.destroy()
        root.geometry("820x620")
        VentanaPrincipal(root, cliente)

    VentanaLogin(root, al_ingresar)
    root.mainloop()


if __name__ == "__main__":
    main()
