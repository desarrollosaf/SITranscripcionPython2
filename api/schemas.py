from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator


class UsuarioCrear(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    es_admin: bool = False


class UsuarioOut(BaseModel):
    id: int
    email: str
    es_admin: bool
    nombre: Optional[str] = None


class UsuarioGenerar(BaseModel):
    # str y no EmailStr: email-validator rechaza dominios internos como
    # "operador1@congreso.local", que es justo lo que se usa para estas cuentas.
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=255)
    es_admin: bool = False
    nombre: Optional[str] = Field(None, max_length=150)

    @field_validator("email")
    @classmethod
    def _minusculas(cls, v):
        return v.strip().lower()


class UsuarioConPassword(UsuarioOut):
    password: str


class UsuarioEditar(BaseModel):
    # El email no se edita: el módulo estenográfico guarda quién tomó y
    # terminó cada bloque por email, y cambiarlo rompería ese historial.
    nombre: Optional[str] = Field(None, max_length=150)
    es_admin: bool


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TrabajoCrear(BaseModel):
    url: Optional[str] = None
    participantes: list[str] = Field(min_length=1)
    modelo: str = "small"
    bloque: int = 30
    umbral_voz: float = 0.75
    umbral_cambio_voz: float = 0.50
    fuente: str = "youtube"  # "youtube" | "srt"


class TrabajoOut(BaseModel):
    id: str
    url: str
    estado: str
    fuente: str
    puerto: Optional[int] = None
    passphrase: Optional[str] = None
    participantes_pedidos: list[str]
    participantes_encontrados: list[str]
    participantes_no_encontrados: list[str]
    sesion_id: Optional[int] = None
    error: Optional[str] = None
    creado_en: str


class TrabajoDesdeEvento(BaseModel):
    evento_id: str
    tipo: int = Field(ge=0, le=1)  # 0 = Comisión, 1 = Sesión/Diputación permanente
    modelo: str = "small"
    bloque: int = 30


class ParticipacionOut(BaseModel):
    orador: str
    inicio_hms: str
    fin_hms: str
    texto: str
