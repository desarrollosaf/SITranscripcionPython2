from fastapi import FastAPI

import db_sesiones

from . import jobs
from .db_mysql import inicializar_esquema
from .routers import (auth, esteno, lanzador, participantes, sesiones,
                      transcripciones, usuarios)

app = FastAPI(title="API de transcripción en vivo",
              description="Lanza transcribir_en_vivo_c3.py, revisa las "
                          "sesiones transcritas y coordina el módulo "
                          "estenográfico. La consume la interfaz web (web/).")


@app.on_event("startup")
def _startup():
    inicializar_esquema()
    if db_sesiones.usa_mysql():
        # Tablas de las sesiones transcritas (antes en sesiones.db).
        db_sesiones.crear_esquema_mysql()
    # _procesos vive en memoria: al arrancar siempre está vacío, así que
    # cualquier trabajo "ejecutando" en la base a esta altura es un
    # fantasma de un reinicio anterior. Se cierran solos.
    cerrados = jobs.limpiar_trabajos_huerfanos()
    if cerrados:
        print(f"[api] {cerrados} trabajo(s) huérfano(s) de un reinicio "
              "anterior, marcados como detenidos.")


app.include_router(auth.router)
app.include_router(usuarios.router)
app.include_router(participantes.router)
app.include_router(transcripciones.router)
app.include_router(sesiones.router)
app.include_router(esteno.router)
app.include_router(lanzador.router)


@app.get("/salud")
def salud():
    return {"ok": True}
