#!/usr/bin/env python3
"""Agrega una sesión de demostración a sesiones.db para probar la interfaz
web sin haber transcrito nada todavía. No toca las sesiones existentes.

Uso (dentro de Docker):
    docker compose -f docker-compose.yml -f docker-compose.local.yml exec api python crear_demo.py
"""
import argparse
from datetime import datetime

from api.config import settings
from api.revision import db
from revisar import hms

INTERVENCIONES = [
    ("Presidencia / Mesa Directiva",
     "Buenos días, diputadas y diputados. Se abre la sesión. Solicito a la "
     "Secretaría pasar lista de asistencia y verificar el quórum."),
    ("Secretaría",
     "Presidenta, se ha verificado la existencia del quórum. Hay cuarenta y "
     "dos diputados presentes."),
    ("Presidencia / Mesa Directiva",
     "Se declara la existencia del quórum y se abre la sesión. Solicito a la "
     "Secretaría dar lectura al orden del día. Punto uno, lectura y aprobación "
     "del acta de la sesión anterior. Punto dos, iniciativa con proyecto de "
     "decreto por el que se reforma la Ley de Movilidad del Estado de México. "
     "Punto tres, asuntos generales y clausura de la sesión."),
    ("Desconocido",
     "Con su permiso, Presidenta. Quiero hacer una precisión sobre el acta "
     "de la sesión anterior, porque no aparece mi intervención sobre el "
     "presupuesto de los municipios del oriente del estado."),
    ("Dip. María Fernanda Ruiz López",
     "Gracias, Presidenta. Compañeras y compañeros, la iniciativa que hoy "
     "presento busca garantizar el derecho a una movilidad segura, eficiente "
     "e incluyente para las personas que todos los días se trasladan entre "
     "municipios de la zona metropolitana. Proponemos reformar el artículo "
     "doce para obligar a la Secretaría de Movilidad a publicar cada año un "
     "diagnóstico de las rutas de transporte público. También proponemos "
     "crear un registro estatal de incidentes viales que sirva para planear "
     "la infraestructura. Es cuanto, Presidenta."),
    ("Dip. Jorge Alberto Mendoza Pérez",
     "Gracias. Coincidimos con el fondo de la iniciativa, pero pedimos que se "
     "turne también a la Comisión de Planeación y Gasto Público, porque el "
     "registro estatal tendrá un costo que debe preverse en el presupuesto."),
    ("Presidencia / Mesa Directiva",
     "Se turna la iniciativa a las comisiones legislativas de Comunicaciones "
     "y Transportes y de Planeación y Gasto Público para su estudio y "
     "dictamen. Pasamos al punto de asuntos generales."),
    ("Dip. María Fernanda Ruiz López",
     "Solo para agradecer a los vecinos que acompañaron la presentación de "
     "esta iniciativa desde la galería."),
    ("Presidencia / Mesa Directiva",
     "Agotados los asuntos del orden del día, se levanta la sesión y se cita "
     "para la próxima sesión ordinaria. Muchas gracias."),
]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--titulo", default="Sesión de demostración — Pleno")
    args = ap.parse_args()

    with db() as con:
        ahora = datetime.now().isoformat(timespec="seconds")
        sid = con.execute(
            "INSERT INTO sesiones (url, titulo, inicio, fin) VALUES (?,?,?,?)",
            ("https://www.youtube.com/watch?v=demo", args.titulo, ahora, ahora)
        ).lastrowid
        t = 0.0
        for orador, texto in INTERVENCIONES:
            # Cada intervención se guarda partida en frases, como lo hace el
            # transcriptor en vivo con sus bloques de audio.
            frases = [f.strip() + "." for f in texto.split(".") if f.strip()]
            for frase in frases:
                dur = max(4.0, len(frase) / 14)
                con.execute(
                    "INSERT INTO participaciones (sesion_id, orador, inicio_seg, "
                    "fin_seg, inicio_hms, fin_hms, texto) VALUES (?,?,?,?,?,?,?)",
                    (sid, orador, round(t, 2), round(t + dur, 2), hms(t),
                     hms(t + dur), frase))
                t += dur + 0.5
        con.commit()
    print(f"Sesión de demostración #{sid} creada en {settings.db_path} "
          f"({hms(t)} de duración).")


if __name__ == "__main__":
    main()
