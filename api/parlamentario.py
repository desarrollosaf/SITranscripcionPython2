"""Cliente del sistema de registro parlamentario (congresoedomex): trae
eventos (sesiones/comisiones) con sus integrantes, para crear trabajos de
transcripción sin escribir la lista de participantes a mano."""
import json
import os
import urllib.request

BASE_URL = os.environ.get(
    "PARLAMENTARIO_API_URL",
    "https://parlamentario.congresoedomex.gob.mx/backend/api/eventos/ultimoseventos")


def obtener_eventos(tipo):
    """tipo: 0 = Comisión, 1 = Sesión/Diputación permanente. Devuelve los
    últimos ~10 eventos de ese tipo tal cual los manda el Congreso."""
    with urllib.request.urlopen(f"{BASE_URL}/{tipo}", timeout=15) as r:
        datos = json.loads(r.read().decode("utf-8"))
    return datos.get("data", [])


def obtener_evento(evento_id, tipo):
    """No hay endpoint de 'un solo evento por id': se busca en la lista de
    últimos eventos de ese tipo. None si ya no está entre los últimos ~10."""
    for evento in obtener_eventos(tipo):
        if evento.get("id") == evento_id:
            return evento
    return None


def _raiz_eventos():
    """BASE_URL apunta a '.../api/eventos/ultimoseventos'; el endpoint de
    asistencia vive un nivel arriba, en '.../api/eventos'."""
    base = BASE_URL.rstrip("/")
    return base[:-len("/ultimoseventos")] if base.endswith("/ultimoseventos") else base


def obtener_asistencia(evento_id):
    """Modalidad (Presencial/Remota (zoom)/Justificada/Pendiente) de cada
    integrante de un evento. Igual que participantes_de_evento(), puede
    venir plana (Sesión/Diputación permanente) o anidada por comisión
    (Comisión). Nunca lanza: si el endpoint falla o el evento no trae el
    dato, se devuelve {} y la creación del trabajo sigue igual, solo sin
    ese detalle extra."""
    url = f"{_raiz_eventos()}/asistenciaevento/{evento_id}"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            datos = json.loads(r.read().decode("utf-8"))
        crudos = (datos.get("data") or {}).get("integrantes") or []
        mapa = {}
        for item in crudos:
            if "diputado" in item:
                mapa[item["diputado"]] = (item.get("asistencia") or "").strip()
            elif "integrantes" in item:
                for sub in item["integrantes"]:
                    if "diputado" in sub:
                        mapa[sub["diputado"]] = (sub.get("asistencia") or "").strip()
        return mapa
    except Exception:
        return {}


def participantes_de_evento(evento):
    """Aplana 'integrantes': en Sesión/Diputación permanente ya es una
    lista plana de {"diputado": nombre}; en Comisión viene anidada por
    comisión ({"integrantes": [{"diputado": ...}, ...]})."""
    nombres = []
    for item in evento.get("integrantes") or []:
        if "diputado" in item:
            nombres.append(item["diputado"])
        elif "integrantes" in item:
            for sub in item["integrantes"]:
                if "diputado" in sub:
                    nombres.append(sub["diputado"])
    vistos, resultado = set(), []
    for nombre in nombres:
        if nombre not in vistos:
            vistos.add(nombre)
            resultado.append(nombre)
    return resultado
