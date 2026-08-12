#!/usr/bin/env python3
"""
agente_captura
==============
App de escritorio (Windows) para el operador de audio: empuja el audio de
una interfaz USB o Dante Virtual Soundcard hacia el servidor de
transcripción, por SRT, sin necesitar saber programar.

Flujo:
  1. Inicia sesión con su usuario (el mismo de la API/revisar.py).
  2. Ve la lista de comisiones/eventos esperando audio.
  3. Por cada una, elige el dispositivo de audio (USB o canal Dante) y
     presiona "Iniciar". Puede tener varias transmitiendo a la vez.

No requiere Python ni ffmpeg instalados aparte: se distribuye como un
único .exe con todo incluido (ver .github/workflows/build-agente-captura.yml).
"""
import json
import os
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from urllib.parse import urlparse

import requests

CONFIG_NOMBRE = "agente_captura_config.json"
REFRESCO_EVENTOS_MS = 20_000
REFRESCO_ESTADO_MS = 2_000


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
    """Corre ffmpeg -list_devices y devuelve los nombres de los
    dispositivos de audio DirectShow (interfaces USB, canales Dante, etc.)."""
    try:
        r = subprocess.run(
            [ruta_ffmpeg(), "-hide_banner", "-list_devices", "true",
             "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"No pude ejecutar ffmpeg: {e}")

    texto = r.stderr or ""

    # Formato nuevo de ffmpeg: cada dispositivo trae "(audio)"/"(video)"
    # pegado a la misma línea, sin encabezado de sección — más confiable,
    # no depende de texto exacto de encabezado.
    dispositivos = [m.group(1) for m in
                    re.finditer(r'"([^"]+)"\s*\(audio\)', texto)]

    if not dispositivos:
        # Formato viejo de ffmpeg: encabezado "DirectShow audio devices"
        # seguido de líneas con solo el nombre entre comillas.
        en_audio = False
        for linea in texto.splitlines():
            if "DirectShow audio devices" in linea:
                en_audio = True
                continue
            if "DirectShow video devices" in linea:
                en_audio = False
                continue
            if en_audio:
                m = re.search(r'"([^"]+)"', linea)
                if m:
                    dispositivos.append(m.group(1))

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

    def eventos_esperando_audio(self):
        r = requests.get(f"{self.base_url}/transcripciones/esperando-audio",
                         headers=self._headers(), timeout=15)
        r.raise_for_status()
        return r.json()

    @property
    def host(self):
        return urlparse(self.base_url).hostname


class FilaEvento:
    """Una comisión/evento en la lista, con su selector de dispositivo,
    botón de iniciar/detener y estado. Puede haber varias transmitiendo
    a la vez, cada una con su propio proceso ffmpeg."""

    def __init__(self, parent, app, evento, dispositivos):
        self.app = app
        self.evento = evento
        self.proc = None

        self.marco = ttk.Frame(parent, padding=6, relief="groove", borderwidth=1)
        self.marco.pack(fill="x", padx=4, pady=3)

        nombre = evento.get("url") or f"Trabajo {evento['id'][:8]}"
        ttk.Label(self.marco, text=nombre, font=("", 10, "bold")).pack(anchor="w")
        ttk.Label(self.marco, text=f"puerto {evento.get('puerto')}",
                  foreground="#777").pack(anchor="w")

        fila = ttk.Frame(self.marco)
        fila.pack(fill="x", pady=(4, 0))

        self.combo = ttk.Combobox(fila, values=dispositivos, state="readonly",
                                  width=40)
        if dispositivos:
            self.combo.current(0)
        self.combo.pack(side="left", padx=(0, 8))

        self.boton = ttk.Button(fila, text="Iniciar", command=self._alternar)
        self.boton.pack(side="left")

        self.estado_var = tk.StringVar(value="⚪ Detenido")
        ttk.Label(fila, textvariable=self.estado_var).pack(side="left", padx=8)

    def actualizar_dispositivos(self, dispositivos):
        actual = self.combo.get()
        self.combo["values"] = dispositivos
        if actual in dispositivos:
            self.combo.set(actual)
        elif dispositivos and not self.combo.get():
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
        host = self.app.cliente.host
        puerto = self.evento["puerto"]
        passphrase = self.evento["passphrase"]
        destino = f"srt://{host}:{puerto}?mode=caller&passphrase={passphrase}"
        cmd = [
            ruta_ffmpeg(), "-hide_banner", "-loglevel", "warning",
            "-f", "dshow", "-i", f"audio={dispositivo}",
            # AAC, no PCM crudo: MPEG-TS no tiene un tipo de stream estándar
            # para PCM, así que el lado que recibe no lo reconoce al leer.
            "-ac", "1", "-ar", "16000", "-acodec", "aac", "-b:a", "128k",
            "-f", "mpegts", destino,
        ]
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            messagebox.showerror("No pude iniciar", str(e))
            return
        self.boton.config(text="Detener")
        self.estado_var.set("🟡 Conectando…")

    def _detener(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.boton.config(text="Iniciar")
        self.estado_var.set("⚪ Detenido")

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
            self.estado_var.set(f"🔴 Error (código {codigo})")
            self.boton.config(text="Iniciar")


class VentanaPrincipal(ttk.Frame):
    def __init__(self, root, cliente):
        super().__init__(root, padding=10)
        self.root = root
        self.cliente = cliente
        self.filas = {}
        self.dispositivos = []
        self.pack(fill="both", expand=True)

        barra = ttk.Frame(self)
        barra.pack(fill="x")
        ttk.Label(barra, text="Comisiones esperando audio",
                  font=("", 12, "bold")).pack(side="left")
        ttk.Button(barra, text="🔄 Dispositivos",
                   command=self._refrescar_dispositivos).pack(side="right", padx=4)
        ttk.Button(barra, text="🔄 Eventos",
                   command=self._refrescar_eventos).pack(side="right")

        contenedor = ttk.Frame(self)
        contenedor.pack(fill="both", expand=True, pady=8)
        lienzo = tk.Canvas(contenedor, highlightthickness=0)
        scroll = ttk.Scrollbar(contenedor, orient="vertical",
                               command=lienzo.yview)
        self.lista = ttk.Frame(lienzo)
        self.lista.bind("<Configure>",
                        lambda e: lienzo.configure(scrollregion=lienzo.bbox("all")))
        lienzo.create_window((0, 0), window=self.lista, anchor="nw")
        lienzo.configure(yscrollcommand=scroll.set)
        lienzo.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.aviso_var = tk.StringVar(value="Cargando…")
        ttk.Label(self, textvariable=self.aviso_var,
                  foreground="#777").pack(anchor="w")

        self._refrescar_dispositivos()
        self._refrescar_eventos()
        self.root.after(REFRESCO_ESTADO_MS, self._ciclo_estado)
        self.root.after(REFRESCO_EVENTOS_MS, self._ciclo_eventos)

    def _refrescar_dispositivos(self):
        def trabajo():
            try:
                disp = listar_dispositivos_audio()
            except RuntimeError as e:
                self.root.after(0, lambda: messagebox.showerror(
                    "Dispositivos", str(e)))
                return
            self.root.after(0, lambda: self._aplicar_dispositivos(disp))
        threading.Thread(target=trabajo, daemon=True).start()

    def _aplicar_dispositivos(self, disp):
        self.dispositivos = disp
        for fila in self.filas.values():
            fila.actualizar_dispositivos(disp)

    def _refrescar_eventos(self):
        def trabajo():
            try:
                eventos = self.cliente.eventos_esperando_audio()
            except Exception as e:
                self.root.after(0, lambda: self.aviso_var.set(
                    f"No pude actualizar la lista: {e}"))
                return
            self.root.after(0, lambda: self._aplicar_eventos(eventos))
        threading.Thread(target=trabajo, daemon=True).start()

    def _aplicar_eventos(self, eventos):
        vistos = set()
        for evento in eventos:
            vistos.add(evento["id"])
            if evento["id"] in self.filas:
                continue
            self.filas[evento["id"]] = FilaEvento(
                self.lista, self, evento, self.dispositivos)
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

    def _ciclo_estado(self):
        for fila in self.filas.values():
            fila.refrescar_estado()
        self.root.after(REFRESCO_ESTADO_MS, self._ciclo_estado)

    def _ciclo_eventos(self):
        self._refrescar_eventos()
        self.root.after(REFRESCO_EVENTOS_MS, self._ciclo_eventos)


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
        root.geometry("760x560")
        VentanaPrincipal(root, cliente)

    VentanaLogin(root, al_ingresar)
    root.mainloop()


if __name__ == "__main__":
    main()
