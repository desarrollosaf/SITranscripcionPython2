from fastapi import FastAPI

from .db_mysql import inicializar_esquema
from .routers import auth, participantes, transcripciones, usuarios

app = FastAPI(title="API de transcripción en vivo",
              description="Lanza transcribir_en_vivo_c3.py acotado a los "
                          "integrantes de un evento y consulta el resultado.")


@app.on_event("startup")
def _startup():
    inicializar_esquema()


app.include_router(auth.router)
app.include_router(usuarios.router)
app.include_router(participantes.router)
app.include_router(transcripciones.router)


@app.get("/salud")
def salud():
    return {"ok": True}
