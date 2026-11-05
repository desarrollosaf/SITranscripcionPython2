"""Datos de ejemplo para ver la interfaz sin servidor ni dispositivos de
audio: `python agente_captura/app.py --demo` (o `AgenteCapturaAudio.exe
--demo`). Nada de esto se conecta a ningún lado ni guarda configuración."""
import uuid
from datetime import date


class ProcesoDemo:
    """Se comporta como el Popen de ffmpeg, sin correr nada."""

    def __init__(self, al_terminar):
        self.codigo = None
        self.al_terminar = al_terminar

    def poll(self):
        return self.codigo

    def terminate(self):
        self.codigo = 1  # igual que TerminateProcess en Windows
        self.al_terminar()


def _trabajo(nombre, estado, fuente="srt", puerto=None, sesion_id=None,
             error=None, evento_id=None):
    id_trabajo = str(uuid.uuid4())
    # La contraseña es el id para que lanzar_ffmpeg() sepa de qué trabajo se trata.
    return {"id": id_trabajo, "url": nombre, "estado": estado,
            "fuente": fuente, "puerto": puerto, "passphrase": id_trabajo,
            "sesion_id": sesion_id, "error": error, "evento_id": evento_id}


class ClienteDemo:
    es_demo = True
    host = "demo.local"

    def __init__(self, base_url=""):
        hoy = date.today().isoformat()
        self._eventos = {
            1: [{"id": "s1", "fecha": f"{hoy} 11:00", "participantes": 75,
                 "descripcion": "Sesión deliberante de ejemplo", "comisiones": []},
                {"id": "s2", "fecha": f"{hoy} 17:00", "participantes": 9,
                 "descripcion": "Diputación permanente de ejemplo", "comisiones": []}],
            0: [{"id": "c1", "fecha": f"{hoy} 10:00", "participantes": 12,
                 "descripcion": "Reunión de trabajo de ejemplo",
                 "comisiones": ["Comisión de Salud"]},
                {"id": "c2", "fecha": f"{hoy} 13:00", "participantes": 18,
                 "descripcion": "Tablas de valores de ejemplo: una descripción larga "
                                "para ver cómo se acomoda debajo del selector cuando "
                                "ocupa más de un renglón en la pantalla.",
                 "comisiones": ["Comisión de Educación", "Comisión de Cultura"]}],
        }
        self._trabajos = [
            _trabajo(f"{hoy} 09:30 — [Comisión de Finanzas]", "ejecutando",
                     puerto=9000, sesion_id=101),
            _trabajo(f"{hoy} 08:00 — [Comisión de Movilidad]", "error",
                     sesion_id=100,
                     error="Ejemplo de error: se perdió la conexión con la "
                           "consola de audio."),
            _trabajo(f"{hoy} 07:00 — [Comisión de Turismo]", "finalizado",
                     sesion_id=99),
        ]

    def login(self, email, password):
        pass

    def listar_dispositivos(self):
        return [("Interfaz de audio USB (ejemplo)", None),
                ("Dante Virtual Soundcard — canal 1 (ejemplo)", None),
                ("Dante Virtual Soundcard — canal 2 (ejemplo)", None)]

    def lanzar_ffmpeg(self, cmd, archivo_log):
        # Igual que el servidor real: al perder el audio cierra el trabajo.
        trabajo = next(t for t in self._trabajos if t["passphrase"] in cmd[-1])
        return ProcesoDemo(lambda: trabajo.update(estado="finalizado"))

    def eventos(self, tipo):
        return self._eventos[tipo]

    def eventos_esperando_audio(self):
        return [t for t in self._trabajos
                if t["fuente"] == "srt" and t["estado"] == "ejecutando"]

    def mis_trabajos(self):
        return list(self._trabajos)

    def crear_desde_evento(self, evento_id, tipo, modelo):
        if any(t["evento_id"] == evento_id and t["estado"] == "ejecutando"
               for t in self._trabajos):
            raise RuntimeError("Ya hay un trabajo activo para este evento.")
        evento = next(e for e in self._eventos[tipo] if e["id"] == evento_id)
        nombre = f"{evento['fecha']} — {evento['descripcion']}"
        trabajo = _trabajo(nombre, "ejecutando", puerto=self._puerto_libre(),
                           sesion_id=102, evento_id=evento_id)
        self._trabajos.insert(0, trabajo)
        return trabajo

    def detener(self, job_id):
        trabajo = next(t for t in self._trabajos if t["id"] == job_id)
        trabajo["estado"] = "detenido"
        return trabajo

    def reanudar(self, job_id):
        trabajo = next(t for t in self._trabajos if t["id"] == job_id)
        trabajo.update(estado="ejecutando", error=None,
                       puerto=trabajo["puerto"] or self._puerto_libre())
        return trabajo

    def participaciones(self, job_id):
        return [{"inicio_hms": "00:00:12", "texto": "Texto de ejemplo de la "
                 "primera intervención transcrita."},
                {"inicio_hms": "00:01:05", "texto": "Texto de ejemplo de la "
                 "segunda intervención transcrita."}]

    def _puerto_libre(self):
        usados = {t["puerto"] for t in self._trabajos if t["estado"] == "ejecutando"}
        return next(p for p in range(9000, 9010) if p not in usados)
