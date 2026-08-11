#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
revisar_v2_modificado.py
==========
Interfaz web LOCAL para revisar y corregir los oradores de las sesiones
guardadas en la base de datos (sesiones.db).

Uso:
    python revisar_v2_modificado.py                  (usa sesiones.db en la carpeta actual)
    python revisar_v2_modificado.py --db otra.db     (otra base de datos)

Se abre solo en tu navegador. Todo queda en tu computadora: no sube nada
a internet. Ctrl+C en la terminal para cerrarlo.

Si existe un archivo diputados.txt (un nombre por línea), esos nombres
aparecen como sugerencias al corregir.
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from docx import Document
from docx.shared import Pt
from io import BytesIO

# ---------------------------------------------------------------------------
# Autenticación opcional (--requiere-login): reutiliza los mismos usuarios
# de MySQL y tokens JWT que la API (api/security.py, api/db_mysql.py). Solo
# se activa con la bandera --requiere-login (la usa el servicio Docker
# "revisor"), así que el uso local de siempre (python revisar.py, sin login,
# sin depender de MySQL) sigue funcionando igual.
# ---------------------------------------------------------------------------

COOKIE_SESION = "sesion_revisor"

# Se rellenan en main() solo si --requiere-login está activo.
_verificar_password = None
_obtener_usuario = None
_crear_token = None
_jwt = None
_jwt_secret = None
_jwt_algoritmo = None

PAGINA_LOGIN = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Iniciar sesión — revisión de oradores</title>
<style>
  :root{--papel:#FBFAF6;--tinta:#23241F;--tenue:#6E6C63;--verde:#1E5A38;
        --verde-suave:#E9F0EA;--linea:#E3E1D7;--ambar:#A9691F}
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;background:var(--papel);color:var(--tinta);
    font:15px/1.5 -apple-system,"Segoe UI",sans-serif;
    display:flex;align-items:center;justify-content:center}
  form{background:#fff;border:1px solid var(--linea);border-radius:10px;
    padding:32px;width:320px;box-shadow:0 2px 10px rgba(0,0,0,.04)}
  h1{font-size:18px;margin:0 0 20px}
  label{display:block;font-size:13px;color:var(--tenue);margin:14px 0 4px}
  input{width:100%;padding:9px 10px;border:1px solid var(--linea);
    border-radius:6px;font-size:14px}
  button{margin-top:20px;width:100%;padding:10px;border:0;border-radius:6px;
    background:var(--verde);color:#fff;font-size:14px;cursor:pointer}
  button:hover{opacity:.92}
  .err{color:var(--ambar);font-size:13px;margin-top:12px;min-height:16px}
</style></head>
<body>
  <form id="f">
    <h1>Revisión de oradores</h1>
    <label>Email</label>
    <input type="email" name="email" required autofocus>
    <label>Contraseña</label>
    <input type="password" name="password" required>
    <div class="err" id="err"></div>
    <button type="submit">Entrar</button>
  </form>
<script>
document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const r = await fetch('/login', {method:'POST', body: JSON.stringify({
    email: fd.get('email'), password: fd.get('password')})});
  if (r.ok) { location.href = '/'; }
  else {
    const d = await r.json().catch(() => ({}));
    document.getElementById('err').textContent =
      d.error || 'Email o contraseña incorrectos';
  }
});
</script>
</body></html>"""


# ---------------------------------------------------------------------------
# Catálogo opcional de diputados
# ---------------------------------------------------------------------------

def cargar_catalogo():
    base = os.path.dirname(os.path.abspath(__file__))
    for ruta in (os.path.join(base, "diputados.txt"), "diputados.txt"):
        if os.path.isfile(ruta):
            with open(ruta, encoding="utf-8") as f:
                return [ln.strip() for ln in f
                        if ln.strip() and not ln.strip().startswith("#")]
    return []


# ---------------------------------------------------------------------------
# Página (HTML + CSS + JS en un solo bloque)
# ---------------------------------------------------------------------------

PAGINA = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Versión estenográfica — revisión de oradores</title>
<style>
:root{
  --papel:#FAF8F2; --panel:#FFFFFF; --tinta:#20221C; --tinta-suave:#494A41;
  --tenue:#63625A; --verde:#1E5A38; --verde-hondo:#154029;
  --verde-suave:#E7EFE9; --linea:#E4E1D6; --linea-fuerte:#D2CFC2;
  --ambar:#9C5E18; --ambar-suave:#FAF0DE; --blanco:#FFFFFF;
  --sombra:0 1px 2px rgba(30,40,25,.05), 0 6px 20px rgba(30,40,25,.05);
  --sombra-barra:0 6px 14px -8px rgba(30,40,25,.28);
  --r:8px; --r-sm:6px;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--papel);color:var(--tinta);
  font:15.5px/1.55 -apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
:focus-visible{outline:2px solid var(--ambar);outline-offset:2px}
@media (prefers-reduced-motion: reduce){*{transition:none!important}}
.oculto{display:none!important}

header{border-bottom:1px solid var(--linea-fuerte);background:var(--panel);
  padding:14px 24px;display:flex;flex-wrap:wrap;gap:8px 18px;align-items:center}
header .marca{display:flex;flex-direction:column;gap:2px;line-height:1}
header h1{font:700 22px/1.05 Georgia,"Times New Roman",serif;margin:0;
  color:var(--verde);letter-spacing:.01em}
header .sub{color:var(--tenue);font-size:11.5px;text-transform:uppercase;
  letter-spacing:.16em;font-weight:600}
header .controles{margin-left:auto;display:flex;gap:10px;flex-wrap:wrap;
  align-items:center}
select,input[type=search]{font:inherit;font-size:14.5px;padding:8px 11px;
  border:1px solid var(--linea-fuerte);border-radius:var(--r-sm);
  background:var(--panel);color:var(--tinta);max-width:340px}
select:hover,input[type=search]:hover{border-color:var(--verde)}
#selSesion{font-weight:600;min-width:170px}
#buscar{min-width:200px}
.ver{display:inline-block;margin-left:8px;padding:1px 7px;border-radius:999px;
  background:var(--verde-suave);color:var(--verde);font-size:10px;
  letter-spacing:.06em;vertical-align:middle}

#meta{padding:11px 24px;color:var(--tenue);font-size:13.5px;
  border-bottom:1px solid var(--linea);background:var(--papel)}
#meta a{color:var(--verde);font-weight:600;text-decoration:none}
#meta a:hover{text-decoration:underline}
#meta strong{color:var(--tinta);font-weight:700}

.cuerpo{display:grid;grid-template-columns:250px minmax(0,1fr);gap:0;
  max-width:1160px;margin:0 auto}
aside{padding:20px 18px;border-right:1px solid var(--linea)}
aside h2{font-size:11px;text-transform:uppercase;letter-spacing:.14em;
  color:var(--tenue);margin:0 0 12px;font-weight:700}
#listaOradores{list-style:none;margin:0;padding:0}
#listaOradores li{margin:2px 0}
#listaOradores button{width:100%;text-align:left;font:inherit;font-size:13.5px;
  border:0;background:none;padding:7px 10px;border-radius:var(--r-sm);
  cursor:pointer;color:var(--tinta);display:flex;justify-content:space-between;
  gap:8px;transition:background .12s}
#listaOradores button:hover{background:var(--verde-suave)}
#listaOradores button.activo{background:var(--verde);color:#fff}
#listaOradores .min{color:var(--tenue);font-variant-numeric:tabular-nums}
#listaOradores button.activo .min{color:#D7E4DA}
aside .nota{font-size:12.5px;line-height:1.55;color:var(--tenue);
  margin-top:18px;padding-top:14px;border-top:1px solid var(--linea)}

main{padding:26px 30px 90px;max-width:820px}
.turno{display:grid;grid-template-columns:78px minmax(0,1fr);gap:16px;
  margin:0 0 22px;padding-bottom:2px}
.cuerpo-turno{min-width:0}
.cabecera-turno{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;
  margin-bottom:6px}
.tiempo{font-size:12px;color:var(--tenue);text-decoration:none;
  font-variant-numeric:tabular-nums;padding-top:5px;white-space:nowrap}
.tiempo:hover{color:var(--ambar)}
.orador{font:700 13px/1.3 -apple-system,"Segoe UI",Roboto,sans-serif;
  text-transform:uppercase;letter-spacing:.05em;color:var(--verde);
  background:none;border:0;border-bottom:1px dashed var(--verde);
  padding:0 0 1px;cursor:pointer;text-align:left}
.orador:hover{color:var(--ambar);border-color:var(--ambar)}
.voz-pista{font:600 12px/1.4 -apple-system,"Segoe UI",Roboto,sans-serif;
  padding:2px 9px;border-radius:999px;cursor:pointer;white-space:nowrap;
  border:1px solid var(--ambar);background:#FBF3E4;color:var(--ambar)}
.voz-pista:hover{background:var(--ambar);color:#fff}
.insignia-ia{font:700 11px/1.4 -apple-system,"Segoe UI",Roboto,sans-serif;
  padding:2px 9px;border-radius:999px;white-space:nowrap;letter-spacing:.02em}
.insignia-ia.validado{background:var(--verde-suave);color:var(--verde);
  border:1px solid var(--verde)}
.insignia-ia.descartado{background:#F1EFE8;color:var(--tenue);
  border:1px solid var(--linea)}
.insignia-ia.media{background:#FBF3E4;color:var(--ambar);
  border:1px solid var(--ambar)}
.motivo-ia{font:italic 12px/1.4 Georgia,"Times New Roman",serif;
  color:var(--tenue);max-width:360px;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;cursor:help}
/* Barra de acciones: fija, agrupada por etapa del flujo de trabajo */
.barra{position:sticky;top:0;z-index:20;display:flex;flex-wrap:wrap;
  align-items:center;gap:8px 14px;padding:9px 24px;background:var(--panel);
  border-bottom:1px solid var(--linea-fuerte);box-shadow:var(--sombra-barra)}
.grupo{display:flex;align-items:center;gap:8px;position:relative;
  padding-right:14px}
.grupo:not(:last-of-type)::after{content:"";position:absolute;right:0;top:50%;
  transform:translateY(-50%);width:1px;height:22px;background:var(--linea)}
.grupo-tit{font:700 10.5px/1 -apple-system,"Segoe UI",Roboto,sans-serif;
  text-transform:uppercase;letter-spacing:.12em;color:var(--tenue);
  padding-right:2px}
.grupo-tit .paso{display:inline-flex;align-items:center;justify-content:center;
  width:16px;height:16px;border-radius:999px;background:var(--verde-suave);
  color:var(--verde);font-size:10px;margin-right:5px}

.accion{font:600 14px/1.2 -apple-system,"Segoe UI",Roboto,sans-serif;
  padding:8px 13px;border-radius:var(--r-sm);cursor:pointer;
  border:1px solid var(--linea-fuerte);background:var(--panel);
  color:var(--tinta);transition:background .12s,border-color .12s,color .12s;
  white-space:nowrap}
.accion:hover{border-color:var(--verde);background:var(--verde-suave);
  color:var(--verde-hondo)}
.accion.ia{border-color:#E1D2B4;background:var(--ambar-suave);color:var(--ambar)}
.accion.ia:hover{background:var(--ambar);border-color:var(--ambar);color:#fff}
.accion.primario{border-color:var(--verde);background:var(--verde);color:#fff}
.accion.primario:hover{background:var(--verde-hondo);border-color:var(--verde-hondo)}
.accion:disabled{opacity:.55;cursor:default;filter:saturate(.6)}
#listaOradores button.pendiente{color:var(--ambar);font-weight:700}
#listaOradores button.pendiente.activo{background:var(--ambar);color:#fff}
.mini{font:600 12px/1.4 -apple-system,"Segoe UI",Roboto,sans-serif;
  padding:2px 9px;border-radius:999px;cursor:pointer;white-space:nowrap;
  border:1px solid var(--linea);background:#fff;color:var(--tinta-suave)}
.mini:hover{border-color:var(--verde);color:var(--verde)}
.mini.guardado{border-color:var(--verde);background:var(--verde-suave);
  color:var(--verde);font-weight:700}
.vivo{margin-left:auto;display:flex;align-items:center;gap:7px;font-size:14px;
  font-weight:600;color:var(--tinta-suave);cursor:pointer;white-space:nowrap;
  padding:6px 10px;border:1px solid var(--linea);border-radius:999px}
.vivo:hover{border-color:var(--verde);color:var(--verde)}
.vivo input{accent-color:var(--verde);width:15px;height:15px}
.editor-texto{grid-column:1 / -1;margin-top:10px;padding:12px;
  border:1px solid var(--linea);border-radius:8px;background:#FCFAF5}
.editor-texto label{display:flex;gap:10px;margin-bottom:8px;
  align-items:flex-start}
.seg-tiempo{font:600 12px/2 -apple-system,"Segoe UI",Roboto,sans-serif;
  color:var(--verde);white-space:nowrap;min-width:64px}
.editor-texto textarea{flex:1;font:15px/1.55 Georgia,serif;
  color:var(--tinta);padding:7px 10px;border:1px solid var(--linea);
  border-radius:6px;resize:vertical;background:#fff}
.editor-texto textarea:focus{outline:2px solid var(--verde-suave);
  border-color:var(--verde)}
.divisor{grid-column:1 / -1;margin-top:10px;padding:12px;
  border:1px solid var(--linea);border-radius:8px;background:#FCFAF5}
.divisor .ayuda{margin:0 0 8px;color:var(--tenue);font-size:13px}
.divisor .frases{font:15px/1.8 Georgia,"Times New Roman",serif}
.divisor .frase{cursor:pointer;padding:1px 3px;border-radius:4px}
.divisor .frase:hover{background:var(--verde-suave)}
.divisor .frase.p2{background:#F4E7CE}
.split-oradores{display:flex;flex-wrap:wrap;gap:10px;align-items:center;
  margin-top:12px}
.split-oradores label{display:flex;gap:6px;align-items:center;
  font-size:13px;color:var(--tenue)}
.split-oradores input{font:inherit;padding:6px 9px;
  border:1px solid var(--linea);border-radius:6px}
.split-oradores .b-div-ok{font:inherit;font-size:13.5px;padding:7px 12px;
  border-radius:6px;border:1px solid var(--verde);background:var(--verde);
  color:#fff;cursor:pointer}
.fila-botones{display:flex;gap:8px;align-items:center;margin-top:4px}
.fila-botones strong{margin-right:auto;color:var(--verde)}
.panel-resumen{grid-column:1 / -1;margin-top:12px;padding:18px 22px;
  border-radius:10px;border:1px solid var(--verde);
  background:var(--verde-suave);max-width:100%}
.panel-resumen .cabecera{display:flex;gap:8px;align-items:center;
  margin-bottom:10px}
.panel-resumen .cabecera strong{margin-right:auto;color:var(--verde);
  font:700 15px/1.3 -apple-system,"Segoe UI",Roboto,sans-serif}
.panel-resumen .md{font:15.5px/1.65 Georgia,"Times New Roman",serif;
  color:var(--tinta);overflow-wrap:break-word}
.panel-resumen .md h1,.panel-resumen .md h2,.panel-resumen .md h3{
  font:700 16px/1.35 -apple-system,"Segoe UI",Roboto,sans-serif;
  color:var(--verde);margin:14px 0 6px}
.panel-resumen .md h1{font-size:18px}
.panel-resumen .md p{margin:8px 0}
.panel-resumen .md strong{color:var(--tinta);font-weight:700}
.panel-resumen .md ul{margin:8px 0;padding-left:22px}
.panel-resumen .md li{margin:3px 0}
.panel-resumen .md hr{border:0;border-top:1px solid var(--verde);
  margin:12px 0;opacity:.4}
.texto{font:16.5px/1.7 Georgia,"Times New Roman",serif;margin:6px 0 0;
  text-align:justify;hyphens:auto;color:var(--tinta)}
.seccion-od{font:700 15.5px/1.45 Georgia,"Times New Roman",serif;
  color:var(--verde);text-align:justify;
  margin:26px 0 10px;padding:8px 0 6px;border-top:2px solid var(--verde);
  border-bottom:1px solid var(--linea)}
.resaltado{background:#F4E7CE}

.editor{grid-column:1 / -1;background:var(--blanco);border:1px solid var(--linea);
  border-left:3px solid var(--verde);border-radius:6px;padding:12px;
  margin-top:10px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.editor input{flex:1 1 240px;font:inherit;padding:7px 10px;
  border:1px solid var(--linea);border-radius:6px}
.editor button{font:inherit;font-size:13.5px;padding:7px 12px;border-radius:6px;
  border:1px solid var(--verde);background:var(--verde);color:#fff;cursor:pointer}
.editor button:hover{filter:brightness(1.1)}
.editor .secundario{background:var(--blanco);color:var(--verde)}
.editor .cancelar{border-color:var(--linea);background:var(--blanco);
  color:var(--tenue)}

.vacio{color:var(--tenue);padding:40px 0;text-align:center}
#aviso{position:fixed;left:50%;bottom:24px;transform:translateX(-50%) translateY(80px);
  background:var(--tinta);color:#fff;padding:10px 18px;border-radius:8px;
  font-size:14px;transition:transform .25s;pointer-events:none}
#aviso.visible{transform:translateX(-50%) translateY(0)}


.lanzador{border:1px solid var(--linea);border-radius:var(--r);
  background:var(--panel);margin:16px 24px 0;padding:0;box-shadow:var(--sombra)}
.lanzador>summary{cursor:pointer;padding:13px 18px;
  font:700 14px/1.2 -apple-system,"Segoe UI",Roboto,sans-serif;
  color:var(--verde);list-style:none;display:flex;align-items:center;gap:8px}
.lanzador>summary::-webkit-details-marker{display:none}
.lanzador>summary::before{content:"\\002B";display:inline-flex;
  align-items:center;justify-content:center;width:20px;height:20px;
  border-radius:999px;background:var(--verde-suave);color:var(--verde);
  font-weight:700}
.lanzador[open]>summary::before{content:"\\2212"}
.lanzador .cuerpo-lan{padding:6px 18px 18px;display:flex;flex-wrap:wrap;
  gap:14px;align-items:flex-end;border-top:1px solid var(--linea)}
.lanzador .campo{display:flex;flex-direction:column;gap:5px;font-size:12.5px;
  font-weight:600;color:var(--tenue)}
.lanzador .campo input,.lanzador .campo select{max-width:none;font-weight:400}
.lanzador #lanUrl{min-width:320px}
.lanzador #lanComisiones{min-width:260px;min-height:96px}
.lanzador .nota-lan{flex-basis:100%;font-size:12.5px;color:var(--tenue);margin:0}
.lanzador pre.log-lan{flex-basis:100%;margin:8px 0 0;max-height:220px;
  overflow:auto;background:#1d1f1b;color:#e7e9e3;padding:10px 12px;
  border-radius:var(--r-sm);font:12px/1.5 ui-monospace,Consolas,monospace;
  white-space:pre-wrap;word-break:break-word}
.lanzador .estado-lan{flex-basis:100%;font-size:13px;font-weight:600}
.lanzador .estado-lan.corriendo{color:var(--ambar)}
.lanzador .estado-lan.fin{color:var(--verde)}

@media (max-width:860px){
  header{padding:12px 16px}
  header .controles{width:100%;margin-left:0}
  #selSesion,#buscar{flex:1 1 45%;min-width:0;max-width:none}
  .barra{padding:9px 16px;gap:8px 10px;top:0}
  .grupo{padding-right:10px}
  .grupo-tit{display:none}
  .vivo{margin-left:0}
  .cuerpo{grid-template-columns:1fr}
  aside{border-right:0;border-bottom:1px solid var(--linea)}
  #listaOradores{display:flex;flex-wrap:wrap;gap:4px}
  #listaOradores li{flex:0 0 auto}
  #listaOradores button{border:1px solid var(--linea);border-radius:999px;
    padding:5px 13px}
  .turno{grid-template-columns:1fr}
  .editor{grid-column:1}
  main{padding:20px 16px 90px}
  .lanzador{margin:14px 16px 0}
  .lanzador #lanUrl{min-width:0;width:100%}
}
@media (max-width:560px){
  .grupo{padding-right:0}
  .grupo:not(:last-of-type)::after{display:none}
  .accion{flex:1 1 auto}
}
</style>
</head>
<body>
<header>
  <div class="marca">
    <h1>Versión estenográfica</h1>
    <span class="sub">Revisión de oradores <span class="ver">diseño v9</span></span>
  </div>
  <div class="controles">
    <select id="selSesion" aria-label="Sesión"></select>
    <input id="buscar" type="search" placeholder="Buscar en el texto…"
           aria-label="Buscar en el texto">
    <a id="btnSalir" href="/logout" class="accion" style="display:none;text-decoration:none">Cerrar sesión</a>
  </div>
</header>
<nav class="barra" aria-label="Acciones de la sesión">
  <div class="grupo">
    <span class="grupo-tit"><span class="paso">1</span>Preparar</span>
    <button id="btnCompactar" class="accion"
            title="Une en la base de datos los registros consecutivos del mismo orador">Unir iguales</button>
  </div>
  <div class="grupo">
    <span class="grupo-tit"><span class="paso">2</span>Revisar con IA</span>
    <button id="btnCorregirEstilo" class="accion ia"
            title="Pasa todo el texto por IA para corregir ortografía, gramática y separar párrafos">✨ Corregir estilo</button>
    <button id="btnEstructurar" class="accion ia"
            title="La IA detecta dónde empieza cada punto del orden del día y los numera (1., 2., 3.)">🗂 Numerar orden del día</button>
  </div>
  <div class="grupo">
    <span class="grupo-tit"><span class="paso">3</span>Exportar</span>
    <button id="btnExportarWord" class="accion primario"
            title="Descargar el documento Word formateado">📄 Descargar Word</button>
  </div>
  <label class="vivo" title="Sigue la transcripción casi en tiempo real (se actualiza cada ~2.5 s y se pausa mientras editas). Desmárcala para pausar.">
    <input type="checkbox" id="chkVivo" checked> Auto-actualizar</label>
</nav>
<details class="lanzador" id="lanzador">
  <summary>Nueva transcripción</summary>
  <div class="cuerpo-lan">
    <label class="campo">URL del video o transmisión
      <input id="lanUrl" type="url" placeholder="https://www.youtube.com/watch?v=…">
    </label>
    <label class="campo">Tipo de sesión
      <select id="lanTipo">
        <option value="pleno">Pleno (sesión)</option>
        <option value="comision">Comisión</option>
      </select>
    </label>
    <label class="campo oculto" id="campoComisiones">Comisión(es) — Ctrl/Cmd para varias unidas
      <select id="lanComisiones" multiple></select>
    </label>
    <label class="campo">Modelo
      <select id="lanModelo">
        <option value="tiny">tiny (rápido)</option>
        <option value="base">base</option>
        <option value="small" selected>small (recomendado)</option>
        <option value="medium">medium</option>
        <option value="large-v3">large-v3 (lento, preciso)</option>
      </select>
    </label>
    <label class="campo oculto" id="campoFecha">Fecha (opcional)
      <input id="lanFecha" type="text" placeholder="AAAA-MM-DD" size="12">
    </label>
    <button id="btnTranscribir" class="accion primario">Iniciar transcripción</button>
    <button id="btnDetener" class="accion oculto">Detener</button>
    <p class="nota-lan" id="notaLan"></p>
    <div class="estado-lan oculto" id="estadoLan"></div>
    <pre class="log-lan oculto" id="logLan"></pre>
  </div>
</details>
<div id="meta"></div>
<div class="cuerpo">
  <aside>
    <h2>Oradores de la sesión</h2>
    <ul id="listaOradores"></ul>
    <p class="nota">Haz clic en un <strong>nombre</strong> dentro del texto
    para corregirlo. La <strong>hora</strong> abre YouTube en ese minuto,
    para verificar quién habla.</p>
  </aside>
  <main id="transcripcion"></main>
</div>
<datalist id="dlOradores"></datalist>
<div id="aviso" role="status" aria-live="polite"></div>

<script>
if (__REQUIERE_LOGIN__) document.getElementById('btnSalir').style.display = '';
const $ = s => document.querySelector(s);
const estado = {sesiones:[], sesion:null, filas:[], turnos:[],
                catalogo:[], filtroOrador:null, q:''};

const esc = s => s.replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

// Convierte el Markdown del resumen (títulos #, negritas **, listas -/•,
// separadores ---) en HTML legible. Escapa todo primero para que sea seguro.
function mdAhtml(texto){
  const enlinea = s => esc(s)
    .replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>')
    .replace(/\\*([^*]+)\\*/g, '<em>$1</em>');
  const lineas = (texto||'').replace(/\\r/g,'').split('\\n');
  let html = '', enLista = false;
  const cerrarLista = () => { if(enLista){ html += '</ul>'; enLista = false; } };
  for(let cruda of lineas){
    const l = cruda.trim();
    if(!l){ cerrarLista(); continue; }
    if(/^(-{3,}|_{3,}|\\*{3,})$/.test(l)){ cerrarLista(); html += '<hr>'; continue; }
    let m;
    if((m = l.match(/^(#{1,6})\\s+(.*)$/))){
      cerrarLista();
      const n = Math.min(m[1].length, 3);
      html += '<h'+n+'>'+enlinea(m[2])+'</h'+n+'>';
    } else if((m = l.match(/^[-*•]\\s+(.*)$/))){
      if(!enLista){ html += '<ul>'; enLista = true; }
      html += '<li>'+enlinea(m[1])+'</li>';
    } else {
      cerrarLista();
      html += '<p>'+enlinea(l)+'</p>';
    }
  }
  cerrarLista();
  return html;
}

const norm = s => s.normalize('NFD').replace(/[\\u0300-\\u036f]/g,'').toLowerCase();
const hmsDe = seg => { seg=Math.floor(seg);
  const h=String(Math.floor(seg/3600)).padStart(2,'0'),
        m=String(Math.floor(seg%3600/60)).padStart(2,'0'),
        s=String(seg%60).padStart(2,'0');
  return h+':'+m+':'+s; };

async function api(ruta, datos){
  const r = datos
    ? await fetch(ruta,{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify(datos)})
    : await fetch(ruta);
  return r.json();
}

function avisar(msg){
  const a = $('#aviso'); a.textContent = msg; a.classList.add('visible');
  clearTimeout(a._t); a._t = setTimeout(()=>a.classList.remove('visible'), 2600);
}

function enlaceYT(seg){
  const url = estado.sesion && estado.sesion.url;
  if(!url || !url.startsWith('http')) return null;
  return url + (url.includes('?') ? '&' : '?') + 't=' + Math.floor(seg) + 's';
}

function agrupar(filas){
  const turnos = []; let t = null;
  for(const f of filas){
    if(!t || t.orador !== f.orador){
      t = {orador:f.orador, inicio:f.inicio_seg, ids:[], textos:[],
           voz:null, vozSim:0, revIa:null, motivoIa:''};
      turnos.push(t);
    }
    t.ids.push(f.id); t.textos.push(f.texto); t.fin = f.fin_seg;
    if(f.voz_orador && (f.voz_similitud||0) > t.vozSim){
      t.voz = f.voz_orador; t.vozSim = f.voz_similitud||0;
    }
    // Veredicto del corrector: prioridad validado > media > descartado
    const _rango = {validado:3, media:2, descartado:1};
    if(f.revisado_ia && (_rango[f.revisado_ia]||0) > (_rango[t.revIa]||0)){
      t.revIa = f.revisado_ia; t.motivoIa = f.motivo_ia || '';
    }
  }
  return turnos;
}

function pintarLateral(){
  const acc = {};
  for(const f of estado.filas){
    const a = acc[f.orador] || (acc[f.orador] = {seg:0});
    a.seg += Math.max(0, f.fin_seg - f.inicio_seg);
  }
  const orden = Object.entries(acc).sort((x,y)=>{
    const dx = x[0]==='Desconocido' ? 1 : 0, dy = y[0]==='Desconocido' ? 1 : 0;
    if(dx !== dy) return dy - dx;          // Desconocido siempre primero
    return y[1].seg - x[1].seg;
  });
  $('#listaOradores').innerHTML = orden.map(([nom,a]) =>
    '<li><button data-orador="'+esc(nom)+'"'
    + ' class="'+(estado.filtroOrador===nom ? 'activo ' : '')
    + (nom==='Desconocido' ? 'pendiente' : '')+'">'
    + '<span>'+(nom==='Desconocido' ? '⚠ ' : '')+esc(nom)+'</span>'
    + '<span class="min">'+Math.round(a.seg/60)+' min</span></button></li>'
  ).join('');
  document.querySelectorAll('#listaOradores button').forEach(b =>
    b.onclick = () => {
      const n = b.dataset.orador;
      estado.filtroOrador = (estado.filtroOrador===n) ? null : n;
      pintarLateral(); pintarTranscripcion();
    });
}

function pintarTranscripcion(){
  const q = norm(estado.q||'');
  const visibles = estado.turnos.filter(t =>
    (!estado.filtroOrador || t.orador===estado.filtroOrador) &&
    (!q || norm(t.textos.join(' ')).includes(q) || norm(t.orador).includes(q)));
  if(!visibles.length){
    $('#transcripcion').innerHTML =
      '<p class="vacio">' + (estado.turnos.length
        ? 'Sin resultados con este filtro.'
        : 'Esta sesión no tiene participaciones todavía.') + '</p>';
    return;
  }
  let htmlTurnos = visibles.map((t,i) => {
    const yt = enlaceYT(t.inicio);
    const tiempo = yt
      ? '<a class="tiempo" target="_blank" rel="noopener" href="'+yt
        +'" title="Ver este momento en YouTube">'+hmsDe(t.inicio)+' ▸</a>'
      : '<span class="tiempo">'+hmsDe(t.inicio)+'</span>';
    let texto = esc(t.textos.join(' '));
    if(q){
      const rex = new RegExp('('+estado.q.replace(/[.*+?^${}()|[\\]\\\\]/g,'\\\\$&')+')','gi');
      texto = texto.replace(rex,'<mark class="resaltado">$1</mark>');
    }
    const encabezado = (estado.seccionesPorAncla
                        && estado.seccionesPorAncla[t.ids[0]])
      ? '<h3 class="seccion-od">'
        + esc(estado.seccionesPorAncla[t.ids[0]])+'</h3>'
      : '';
    return encabezado
      + '<article class="turno" data-i="'+estado.turnos.indexOf(t)+'">'
      + tiempo
      + '<div class="cuerpo-turno"><div class="cabecera-turno">'
      + '<button class="orador" title="Corregir orador">'
      + esc(t.orador)+':</button>'
      + (t.revIa
         ? '<span class="insignia-ia '+t.revIa+'" title="'
           + esc(t.motivoIa || '')+'">'
           + (t.revIa==='validado' ? '✓ validado por IA'
              : t.revIa==='media' ? '◐ validado (media) por IA'
              : '✗ descartado por IA')+'</span>'
           + (t.motivoIa
              ? '<span class="motivo-ia" title="'+esc(t.motivoIa)+'">'
                + esc(t.motivoIa)+'</span>'
              : '')
         : '')
      + ((t.voz && t.voz !== t.orador)
         ? '<button class="voz-pista" title="Aplicar esta sugerencia de voz">'
           + '🎙 voz: '+esc(t.voz)+' ('+t.vozSim.toFixed(2)+') · aplicar</button>'
         : '')
      + '<button class="mini b-editar" title="Corregir el texto de este bloque">✏ texto</button>'
      + '<button class="mini b-dividir" title="Separar dos oradores dentro de este bloque">✂ dividir</button>'
      + '<button class="mini b-resumen'
        + ((estado.resumenes && estado.resumenes[t.ids[0]]) ? ' guardado' : '')
        + '" title="Resumen ejecutivo de esta intervención">📋 resumen'
        + ((estado.resumenes && estado.resumenes[t.ids[0]]) ? ' ✓' : '')
        + '</button>'
      + '</div>'
      + '<p class="texto">'+texto+'</p></div></article>';
  }).join('');
  $('#transcripcion').innerHTML = htmlTurnos;
  document.querySelectorAll('.orador').forEach(b =>
    b.onclick = e => abrirEditor(e.target.closest('.turno')));
  document.querySelectorAll('.voz-pista').forEach(b =>
    b.onclick = async e => {
      const t = estado.turnos[+e.target.closest('.turno').dataset.i];
      await asignar(t, t.voz, 'Sugerencia de voz aplicada');
    });
  document.querySelectorAll('.b-editar').forEach(b =>
    b.onclick = e => editarTexto(e.target.closest('.turno')));
  document.querySelectorAll('.b-dividir').forEach(b =>
    b.onclick = e => dividirBloque(e.target.closest('.turno')));
  document.querySelectorAll('.b-resumen').forEach(b =>
    b.onclick = e => pedirResumen(e.target.closest('.turno'), e.target));
  // Mostrar automáticamente los resúmenes ya guardados
  if(estado.resumenes){
    document.querySelectorAll('.turno').forEach(art => {
      const t = estado.turnos[+art.dataset.i];
      if(t && estado.resumenes[t.ids[0]])
        mostrarResumen(art, t, estado.resumenes[t.ids[0]]);
    });
  }
}

function editarTexto(art){
  document.querySelectorAll('.editor, .editor-texto, .panel-resumen')
    .forEach(e => e.remove());
  const t = estado.turnos[+art.dataset.i];
  const cont = document.createElement('div');
  cont.className = 'editor-texto';
  cont.innerHTML = t.ids.map((id, k) => {
    const f = estado.porId[id] || {};
    return '<label><span class="seg-tiempo">'+hmsDe(f.inicio_seg||0)+'</span>'
      + '<textarea data-id="'+id+'" rows="2">'+esc(t.textos[k]||'')
      + '</textarea></label>';
  }).join('')
  + '<div class="fila-botones">'
  + '<button class="b-guardar">Guardar texto</button>'
  + '<button class="b-cancelar cancelar">Cancelar</button></div>';
  art.appendChild(cont);
  cont.querySelector('textarea').focus();
  cont.querySelector('.b-cancelar').onclick = () => cont.remove();
  cont.querySelector('.b-guardar').onclick = async () => {
    const cambios = [];
    cont.querySelectorAll('textarea').forEach((ta, k) => {
      if(ta.value !== (t.textos[k]||''))
        cambios.push({id:+ta.dataset.id, texto:ta.value});
    });
    if(!cambios.length){ cont.remove(); return; }
    const r = await api('/api/textos', {cambios});
    avisar('Texto corregido: '+(r.cambios||0)+' segmento(s).');
    cargarSesion(estado.sesion.id);
  };
}

function dividirBloque(art){
  document.querySelectorAll('.editor, .editor-texto, .panel-resumen, .divisor')
    .forEach(e => e.remove());
  const t = estado.turnos[+art.dataset.i];
  const full = t.textos.join(' ');
  // Partir el texto del bloque en frases, conservando el offset de cada una
  const frases = []; let m; const rex = /[^.?!]+[.?!]*\\s*/g;
  while((m = rex.exec(full)) !== null){
    if(m[0].trim()) frases.push({txt:m[0].trim(), off:m.index});
    if(rex.lastIndex === m.index) rex.lastIndex++;
  }
  if(frases.length < 2){
    avisar('Bloque muy corto para dividir por frases; usa "✏ texto".');
    return;
  }
  const cont = document.createElement('div');
  cont.className = 'divisor';
  cont.innerHTML =
    '<p class="ayuda">Haz clic en la frase donde empieza el '
    + '<strong>segundo orador</strong>:</p>'
    + '<div class="frases">'
    + frases.map((f,k) => '<span class="frase" data-k="'+k+'" data-off="'
        + f.off+'">'+esc(f.txt)+'</span>').join(' ')
    + '</div>'
    + '<div class="split-oradores" hidden>'
    + '<label>1ª parte <input class="o1" list="dlOradores"></label>'
    + '<label>2ª parte <input class="o2" list="dlOradores"></label>'
    + '<button class="b-div-ok">Dividir aquí</button>'
    + '<button class="b-cancelar cancelar">Cancelar</button></div>';
  art.appendChild(cont);
  let corte = null;
  const panelO = cont.querySelector('.split-oradores');
  cont.querySelectorAll('.frase').forEach(sp => sp.onclick = () => {
    const k = +sp.dataset.k;
    if(k === 0){ avisar('Elige una frase posterior a la primera.'); return; }
    corte = +sp.dataset.off;
    cont.querySelectorAll('.frase').forEach((s,j) =>
      s.classList.toggle('p2', j >= k));
    panelO.hidden = false;
    cont.querySelector('.o1').value = t.orador;
    cont.querySelector('.o2').focus();
  });
  cont.querySelector('.b-cancelar').onclick = () => cont.remove();
  cont.querySelector('.b-div-ok').onclick = async () => {
    const o1 = cont.querySelector('.o1').value.trim();
    const o2 = cont.querySelector('.o2').value.trim();
    if(corte === null || !o1 || !o2){
      avisar('Marca la frase y escribe los dos oradores.'); return;
    }
    const r = await api('/api/dividir', {sesion_id:estado.sesion.id,
      ids:t.ids, corte:corte, orador1:o1, orador2:o2});
    if(r && r.error){ avisar('No se pudo dividir: '+r.error); return; }
    avisar('Bloque dividido en dos oradores.');
    cargarSesion(estado.sesion.id);
  };
}

function mostrarResumen(art, t, resumen){
  art.querySelectorAll('.panel-resumen').forEach(e => e.remove());
  const p = document.createElement('div');
  p.className = 'panel-resumen';
  p.innerHTML =
    '<div class="cabecera"><strong>Resumen ejecutivo</strong>'
    + '<button class="mini b-copiar">Copiar</button>'
    + '<button class="mini b-borrar">Borrar</button>'
    + '<button class="mini b-cerrar">Cerrar</button></div>'
    + '<div class="md">'+mdAhtml(resumen)+'</div>';
  art.appendChild(p);
  p.querySelector('.b-cerrar').onclick = () => p.remove();
  p.querySelector('.b-copiar').onclick = async () => {
    await navigator.clipboard.writeText(resumen);
    avisar('Resumen copiado al portapapeles.');
  };
  p.querySelector('.b-borrar').onclick = async () => {
    await api('/api/borrar_resumen', {ancla_id:t.ids[0]});
    delete estado.resumenes[t.ids[0]];
    p.remove();
    avisar('Resumen borrado.');
  };
}

async function pedirResumen(art, btn){
  const t = estado.turnos[+art.dataset.i];
  // Si ya hay uno guardado, mostrarlo sin volver a gastar API
  if(estado.resumenes && estado.resumenes[t.ids[0]]){
    mostrarResumen(art, t, estado.resumenes[t.ids[0]]);
    return;
  }
  const original = btn.textContent;
  btn.disabled = true; btn.textContent = 'Generando…';
  try{
    const r = await api('/api/resumen',
      {orador:t.orador, texto:t.textos.join(' '),
       sesion_id:estado.sesion.id, ancla_id:t.ids[0]});
    if(r.modo === 'auto' && r.resumen){
      estado.resumenes[t.ids[0]] = r.resumen;   // recordar en memoria
      mostrarResumen(art, t, r.resumen);
    } else if(r.prompt){
      await navigator.clipboard.writeText(r.prompt);
      avisar(r.error ? r.error
        : 'Prompt copiado: pégalo en Claude (claude.ai) para generar el '
          +'resumen. Para hacerlo automático configura tu clave API '
          +'(ver README).');
    }
  } finally {
    btn.disabled = false; btn.textContent = original;
  }
}

async function asignar(t, orador, mensaje){
  const r = await api('/api/actualizar',
    {ids:t.ids, orador:orador, sesion_id:estado.sesion.id});
  avisar(mensaje+': '+(r.cambios||0)+' segmento(s)'
    + (r.unidos ? ' · '+r.unidos+' unidos en la base' : '')+'.');
  cargarSesion(estado.sesion.id);
}

function opcionesOradores(){
  const set = new Set(['Presidencia / Mesa Directiva']);
  estado.catalogo.forEach(n => set.add('Dip. ' + n));
  estado.filas.forEach(f => set.add(f.orador));
  $('#dlOradores').innerHTML = [...set].sort().map(n =>
    '<option value="'+esc(n)+'">').join('');
}

function abrirEditor(art){
  document.querySelectorAll('.editor').forEach(e => e.remove());
  const i = +art.dataset.i;
  const t = estado.turnos[i];
  const prev = estado.turnos[i-1], next = estado.turnos[i+1];
  const ed = document.createElement('div');
  ed.className = 'editor';
  ed.innerHTML =
    '<input list="dlOradores" value="'+esc(t.orador)+'" aria-label="Nuevo orador">'
    +'<button class="b-bloque">Cambiar solo este bloque</button>'
    +(prev ? '<button class="b-prev secundario" title="Asignar: '
        +esc(prev.orador)+'">⬆ Unir con el anterior</button>' : '')
    +(next ? '<button class="b-next secundario" title="Asignar: '
        +esc(next.orador)+'">⬇ Unir con el siguiente</button>' : '')
    +'<button class="b-sesion secundario">Cambiar en toda la sesión</button>'
    +'<button class="b-cancelar cancelar">Cancelar</button>';
  art.appendChild(ed);
  const input = ed.querySelector('input');
  input.focus(); input.select();
  ed.querySelector('.b-cancelar').onclick = () => ed.remove();
  ed.querySelector('.b-bloque').onclick = async () => {
    const v = input.value.trim(); if(!v) return;
    await asignar(t, v, 'Listo');
  };
  if(prev) ed.querySelector('.b-prev').onclick = () =>
    asignar(t, prev.orador, 'Unido con '+prev.orador);
  if(next) ed.querySelector('.b-next').onclick = () =>
    asignar(t, next.orador, 'Unido con '+next.orador);
  ed.querySelector('.b-sesion').onclick = async () => {
    const v = input.value.trim(); if(!v) return;
    const r = await api('/api/renombrar',
      {sesion_id:estado.sesion.id, de:t.orador, a:v});
    avisar('Listo: '+(r.cambios||0)+' renombrados'
      + (r.unidos ? ' · '+r.unidos+' unidos en la base' : '')+'.');
    cargarSesion(estado.sesion.id);
  };
}

async function cargarSesion(id, opts){
  opts = opts || {};
  const d = await api('/api/participaciones?sesion='+id);
  const filas = d.filas || [];
  // Firma barata para detectar bloques nuevos (nº de filas + último id +
  // largo del último texto). Sirve para no redibujar si nada cambió.
  const ult = filas.length ? filas[filas.length-1] : null;
  const firma = filas.length + ':' + (ult ? ult.id+':'+((ult.texto||'').length) : '0');
  const mismaSesion = estado.sesion && estado.sesion.id === (d.sesion && d.sesion.id);
  if(opts.soloSiCambia && mismaSesion && firma === estado.firma){
    return false;   // sin novedades: no se redibuja
  }
  // Seguro extra: si abriste algo para editar mientras este refresco ya iba
  // en curso, NO redibujamos (perderías lo escrito). Dejamos la firma vieja
  // para que el próximo tic —cuando cierres el editor— muestre lo nuevo.
  if(opts.soloSiCambia &&
     document.querySelector('.editor, .editor-texto, .panel-resumen, .divisor')){
    return false;
  }
  estado.firma = firma;
  estado.sesion = d.sesion; estado.filas = filas;
  estado.resumenes = d.resumenes || {};
  estado.estructura = d.estructura || null;
  estado.seccionesPorAncla = {};
  if(estado.estructura && estado.estructura.secciones){
    estado.estructura.secciones.forEach(s => {
      estado.seccionesPorAncla[s.ancla_id] = s.titulo;
    });
  }
  estado.porId = {};
  filas.forEach(f => estado.porId[f.id] = f);
  estado.turnos = agrupar(filas);
  const s = d.sesion || {};
  $('#meta').innerHTML = s.id
    ? '<strong>'+esc(s.titulo||'Sesión '+s.id)+'</strong> · inicio '
      + esc(s.inicio||'?')
      + (s.url && s.url.startsWith('http')
         ? ' · <a href="'+esc(s.url)+'" target="_blank" rel="noopener">ver video</a>'
         : '')
    : '';
  opcionesOradores(); pintarLateral(); pintarTranscripcion();
  return true;    // se redibujó
}

// Deja la vista vacía (sin sesión cargada): al abrir la app o al elegir
// la opción "— Elige una sesión —" del selector.
function limpiarVista(){
  estado.sesion = null; estado.filas = []; estado.turnos = [];
  estado.filtroOrador = null; estado.estructura = null;
  estado.resumenes = {}; estado.seccionesPorAncla = {};
  $('#meta').innerHTML = '';
  $('#listaOradores').innerHTML = '';
  const dl = $('#dlOradores'); if(dl) dl.innerHTML = '';
  $('#transcripcion').innerHTML =
    '<p class="vacio">Elige una sesión en el menú de arriba para empezar.</p>';
}

async function iniciar(){
  estado.catalogo = await api('/api/catalogo');
  estado.sesiones = await api('/api/sesiones');
  if(!estado.sesiones.length){
    $('#transcripcion').innerHTML =
      '<p class="vacio">Aún no hay sesiones en la base de datos.<br>'
      +'Corre primero <code>transcribir_en_vivo.py</code> y vuelve aquí.</p>';
    return;
  }
  $('#selSesion').innerHTML =
    '<option value="" selected>— Elige una sesión —</option>'
    + estado.sesiones.map(s =>
      '<option value="'+s.id+'">#'+s.id+' — '
      +esc((s.titulo||'').slice(0,60))+' ('+s.segmentos+' seg.)</option>').join('');
  $('#selSesion').onchange = e => {
    const id = +e.target.value;
    if(id) cargarSesion(id); else limpiarVista();
  };
  $('#buscar').oninput = e => {estado.q = e.target.value; pintarTranscripcion();};
  
  $('#btnCompactar').onclick = async () => {
    if(!estado.sesion) return;
    const r = await api('/api/compactar', {sesion_id:estado.sesion.id});
    avisar(r.unidos ? r.unidos+' registro(s) unidos en la base.'
                    : 'No había registros consecutivos por unir.');
    cargarSesion(estado.sesion.id);
  };

  $('#btnExportarWord').onclick = () => {
    if(!estado.sesion) return avisar('No hay sesión activa.');
    window.open('/api/exportar_word?sesion=' + estado.sesion.id, '_blank');
  };

  $('#btnCorregirEstilo').onclick = async () => {
    if(!estado.sesion) return avisar('No hay sesión activa.');
    if(!confirm('¿Estás seguro? Esto enviará todos los segmentos a la IA para corrección ortográfica y gramatical. Tomará unos momentos.')) return;
    
    const btn = $('#btnCorregirEstilo');
    const txtOriginal = btn.textContent;
    btn.textContent = 'Procesando... ⏳';
    btn.disabled = true;
    
    try {
      const r = await api('/api/corregir_estilo', {sesion_id: estado.sesion.id});
      if(r.error) {
        avisar('Error: ' + r.error);
      } else {
        avisar('Revisión completada. ' + r.cambios + ' segmentos corregidos.'
               + (r.aviso ? ' ⚠ ' + r.aviso : ''));
        cargarSesion(estado.sesion.id);
      }
    } finally {
      btn.textContent = txtOriginal;
      btn.disabled = false;
    }
  };

  $('#btnEstructurar').onclick = async () => {
    if(!estado.sesion) return avisar('No hay sesión activa.');
    if(!confirm('La IA leerá toda la sesión para detectar dónde empieza '
      + 'cada punto del orden del día y numerarlos (1., 2., 3., …). '
      + 'No toca el texto ni identifica acuerdos. ¿Continuar?')) return;
    const btn = $('#btnEstructurar');
    const txt = btn.textContent;
    btn.textContent = 'Analizando... ⏳'; btn.disabled = true;
    try {
      const r = await api('/api/estructurar', {sesion_id: estado.sesion.id});
      if(r.error){ avisar('Error: ' + r.error); }
      else {
        avisar('Orden del día detectado: ' + r.secciones
          + ' punto(s) numerado(s). Ya aparece en la transcripción y en el Word.');
        cargarSesion(estado.sesion.id);
      }
    } finally { btn.textContent = txt; btn.disabled = false; }
  };

  async function autoRefrescoTick(){
    if(document.hidden) return;                 // pestaña en segundo plano
    // no interrumpir si hay algo abierto para editar
    if(document.querySelector('.editor, .editor-texto, .panel-resumen, .divisor'))
      return;
    if(!estado.sesion) return;
    const doc = document.documentElement;
    // ¿el usuario ya está pegado al final? (para "seguir" la transcripción)
    const cercaFinal = (window.innerHeight + window.scrollY) >= (doc.scrollHeight - 140);
    const y = window.scrollY;
    const cambio = await cargarSesion(estado.sesion.id, {soloSiCambia:true});
    if(cambio){
      // Si venías al final, salta a los bloques nuevos; si estabas leyendo
      // más arriba, respeta tu posición.
      window.scrollTo(0, cercaFinal ? document.documentElement.scrollHeight : y);
    }
  }
  function iniciarAutoRefresco(){
    clearInterval(estado.timerVivo);
    estado.timerVivo = setInterval(autoRefrescoTick, 2500);
  }

  $('#chkVivo').onchange = e => {
    if(e.target.checked){
      iniciarAutoRefresco();
      avisar('Auto-actualización activada: sigue la transcripción casi en '
        +'tiempo real (se pausa mientras editas).');
    } else {
      clearInterval(estado.timerVivo);
      avisar('Auto-actualización en pausa.');
    }
  };
  // Arranca sola: el interruptor viene marcado por defecto.
  if($('#chkVivo').checked) iniciarAutoRefresco();
  limpiarVista();   // arrancamos en blanco; el usuario elige la sesión
}

async function refrescarSelectorSesiones(){
  estado.sesiones = await api('/api/sesiones');
  if(!estado.sesiones.length) return;
  const sel = $('#selSesion');
  const previa = sel.value;
  sel.innerHTML = '<option value="">— Elige una sesión —</option>'
    + estado.sesiones.map(s =>
      '<option value="'+s.id+'">#'+s.id+' — '
      +esc((s.titulo||'').slice(0,60))+' ('+s.segmentos+' seg.)</option>').join('');
  sel.onchange = e => {
    const id = +e.target.value;
    if(id) cargarSesion(id); else limpiarVista();
  };
  sel.value = previa;   // conserva lo elegido (o la opción vacía)
}

function pintarEstadoLan(est){
  const elLog = $('#logLan'), elEst = $('#estadoLan');
  if(est.lineas){
    elLog.classList.remove('oculto');
    const abajo = elLog.scrollTop + elLog.clientHeight >= elLog.scrollHeight - 20;
    elLog.textContent = est.lineas;
    if(abajo) elLog.scrollTop = elLog.scrollHeight;
  }
  elEst.classList.remove('oculto');
  if(est.corriendo){
    elEst.textContent = 'Transcribiendo… (PID '+est.pid+'). Puedes revisar '
      +'mientras tanto; la página se actualiza sola y los bloques irán '
      +'apareciendo.';
    elEst.className = 'estado-lan corriendo';
    $('#btnDetener').classList.remove('oculto');
    $('#btnTranscribir').classList.add('oculto');
  } else {
    elEst.textContent = (est.codigo === 0 || est.codigo === null)
      ? 'Transcripción finalizada.'
      : 'La transcripción terminó (código '+est.codigo+'). Revisa el detalle abajo.';
    elEst.className = 'estado-lan fin';
    $('#btnDetener').classList.add('oculto');
    $('#btnTranscribir').classList.remove('oculto');
  }
}

async function vigilarTranscripcion(){
  clearInterval(estado.timerLan);
  const tic = async () => {
    const est = await api('/api/transcripcion_estado');
    pintarEstadoLan(est);
    if(est.corriendo){
      // Mantén la lista de sesiones al día (la sesión en curso puede ser
      // nueva) y, si aún no hay ninguna elegida, engánchate a la más
      // reciente para seguir la transcripción en vivo en el panel.
      await refrescarSelectorSesiones();
      if(!estado.sesion && estado.sesiones.length){
        const viva = estado.sesiones[0].id;   // la más reciente (id DESC)
        $('#selSesion').value = String(viva);
        await cargarSesion(viva);
      }
    } else {
      clearInterval(estado.timerLan);
      await refrescarSelectorSesiones();
    }
  };
  await tic();
  estado.timerLan = setInterval(tic, 2500);
}

async function cargarLanzador(){
  let info;
  try { info = await api('/api/contextos'); }
  catch(e){ info = {disponible:false, comisiones:[]}; }
  const selCom = $('#lanComisiones'), nota = $('#notaLan');
  selCom.innerHTML = (info.comisiones||[]).map(c =>
    '<option value="'+esc(c)+'">'+esc(c)+'</option>').join('');

  const avisos = [];
  if(!info.tiene_transcriptor)
    avisos.push('No encuentro el script de transcripción en la carpeta; '
      +'el botón no podrá arrancar hasta que esté junto a revisar.py.');
  if(!info.disponible)
    avisos.push('No hay contextos.json: puedes transcribir el Pleno, pero el '
      +'tipo Comisión no tendrá lista hasta que crees ese archivo.');
  else if(!(info.comisiones||[]).length)
    avisos.push('contextos.json no tiene comisiones todavía.');
  nota.textContent = avisos.join(' ');

  const sincronizarTipo = () => {
    const esCom = $('#lanTipo').value === 'comision';
    $('#campoComisiones').classList.toggle('oculto', !esCom);
    $('#campoFecha').classList.toggle('oculto', esCom);
  };
  $('#lanTipo').onchange = sincronizarTipo;
  sincronizarTipo();

  $('#btnTranscribir').onclick = async () => {
    const tipo = $('#lanTipo').value;
    const cuerpo = {
      url: $('#lanUrl').value.trim(),
      tipo: tipo,
      modelo: $('#lanModelo').value,
      fecha: (tipo === 'pleno' ? $('#lanFecha').value.trim() : ''),
      comisiones: (tipo === 'comision'
        ? Array.from($('#lanComisiones').selectedOptions).map(o => o.value)
        : [])
    };
    if(!cuerpo.url.startsWith('http'))
      return avisar('Pega una URL de video válida.');
    if(tipo === 'comision' && !cuerpo.comisiones.length)
      return avisar('Elige al menos una comisión.');
    $('#btnTranscribir').disabled = true;
    const r = await api('/api/transcribir', cuerpo);
    $('#btnTranscribir').disabled = false;
    if(r.error) return avisar(r.error);
    avisar('Transcripción iniciada.');
    vigilarTranscripcion();
  };

  $('#btnDetener').onclick = async () => {
    $('#btnDetener').disabled = true;
    const r = await api('/api/detener', {});
    $('#btnDetener').disabled = false;
    if(r.error) return avisar(r.error);
    avisar(r.detenida ? 'Transcripción detenida.' : 'No había nada corriendo.');
    vigilarTranscripcion();
  };

  // Si al abrir la página ya había una transcripción en curso, engancharse.
  const est = await api('/api/transcripcion_estado');
  if(est.corriendo){ $('#lanzador').open = true; vigilarTranscripcion(); }
}

iniciar();
cargarLanzador();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Compactación: une físicamente los registros consecutivos del mismo orador
# ---------------------------------------------------------------------------

def hms(segundos):
    segundos = int(segundos or 0)
    h, r = divmod(segundos, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _ruta_membrete():
    """Devuelve la ruta de la imagen del membrete o None si no se encuentra.
    Prioridad: variable de entorno MEMBRETE_IMG; luego varios nombres
    habituales junto al script y en la carpeta actual."""
    env = os.environ.get("MEMBRETE_IMG", "").strip()
    if env and os.path.isfile(env):
        return env
    base = os.path.dirname(os.path.abspath(__file__))
    candidatos = [
        "membrete.jpeg", "membrete.jpg", "membrete.png",
        "membrete sap_Mesa de trabajo 1.jpg.jpeg",
    ]
    for nombre in candidatos:
        for ruta in (os.path.join(base, nombre), nombre):
            if os.path.isfile(ruta):
                return ruta
    return None


MESES_ES = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio",
            "julio", "agosto", "septiembre", "octubre", "noviembre",
            "diciembre"]


def _fecha_legible(valor):
    """Convierte un timestamp ISO (u otro texto) en una fecha en español
    del estilo '15 de marzo de 2026'. Si no puede, devuelve el texto tal cual."""
    if not valor:
        return "N/A"
    texto = str(valor).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            d = datetime.strptime(texto[:len(fmt) + 2], fmt)
            return f"{d.day} de {MESES_ES[d.month]} de {d.year}"
        except ValueError:
            continue
    return texto


def _campo(fila, nombre, defecto=None):
    return fila[nombre] if nombre in fila.keys() else defecto


def compactar(con, sesion_id, solo_ids=None):
    """Une en un solo registro las filas consecutivas del mismo orador
    (texto concatenado, tiempos extendidos, mejor evidencia de voz).
    Con solo_ids, solo compacta los grupos que tocan esos ids; sin él,
    compacta la sesión completa. Devuelve cuántas filas se fusionaron."""
    filas = list(con.execute(
        "SELECT * FROM participaciones WHERE sesion_id=? "
        "ORDER BY inicio_seg, id", (sesion_id,)))
    grupos, actual = [], []
    for f in filas:
        if actual and actual[-1]["orador"] == f["orador"]:
            actual.append(f)
        else:
            actual = [f]
            grupos.append(actual)
    solo = set(solo_ids) if solo_ids else None
    unidos = 0
    for g in grupos:
        if len(g) < 2:
            continue
        if solo is not None and not any(f["id"] in solo for f in g):
            continue
        base = g[0]
        texto = " ".join((f["texto"] or "").strip() for f in g).strip()
        fin = g[-1]["fin_seg"]
        sets = ["texto=?", "fin_seg=?", "fin_hms=?"]
        vals = [texto, fin, hms(fin)]
        if "voz_orador" in base.keys():
            mejor = max(g, key=lambda f: (_campo(f, "voz_similitud") or 0))
            sets += ["voz_orador=?", "voz_similitud=?"]
            vals += [_campo(mejor, "voz_orador"),
                     _campo(mejor, "voz_similitud")]
        if "revisado_ia" in base.keys():
            # se conserva el veredicto de mayor rango del grupo
            orden = {"validado": 3, "media": 2, "descartado": 1}
            rev = mot = None
            mejor = 0
            for f in g:
                rr = _campo(f, "revisado_ia")
                if orden.get(rr, 0) > mejor:
                    mejor, rev, mot = orden.get(rr, 0), rr, _campo(f, "motivo_ia")
            sets += ["revisado_ia=?", "motivo_ia=?"]
            vals += [rev, mot]
        con.execute("UPDATE participaciones SET " + ", ".join(sets)
                    + " WHERE id=?", vals + [base["id"]])
        marcas = ",".join("?" * (len(g) - 1))
        con.execute(f"DELETE FROM participaciones WHERE id IN ({marcas})",
                    [f["id"] for f in g[1:]])
        unidos += len(g) - 1
    con.commit()
    return unidos


# ---------------------------------------------------------------------------
# Resumen ejecutivo de intervenciones (prompt de analista parlamentario)
# ---------------------------------------------------------------------------

PROMPT_RESUMEN = """Actúa como analista parlamentario especializado en actividad legislativa.

Analiza la siguiente intervención de un diputado o diputada y genera un resumen ejecutivo objetivo, claro y conciso.

Instrucciones:

1. Identifica el nombre del legislador/a.
2. Resume la participación en un máximo de 500 palabras.
3. Conserva únicamente las ideas principales, propuestas, posicionamientos, críticas o argumentos relevantes.
4. Elimina saludos, agradecimientos, fórmulas protocolarias, repeticiones y expresiones retóricas.
5. Mantén un lenguaje neutral y sin opiniones.
6. Identifica el tema principal de la intervención.
7. Señala, cuando exista:
- Propuesta presentada.
- Problema señalado.
- Solicitud realizada.
- Postura a favor o en contra de algún asunto.
8. Si la intervención no contiene propuestas o posicionamientos claros, indícalo expresamente.

Formato de salida:

Diputado(a): [Nombre]

Tema:
[Tema principal]

Resumen:
[Resumen ejecutivo]

Puntos clave:
• [Punto 1]
• [Punto 2]
• [Punto 3]

Propuesta o solicitud:
[Texto o "No se identifica"]

Posicionamiento:
[A favor / En contra / Informativo / No identificado]

Intervención a analizar:
Orador registrado: {orador}

{texto}"""


def generar_resumen(orador, texto):
    """Con clave API (variable de entorno ANTHROPIC_API_KEY) genera el
    resumen automáticamente llamando a la API de Anthropic; sin clave,
    devuelve el prompt completo listo para copiarse y pegarse en Claude."""
    prompt = PROMPT_RESUMEN.format(orador=orador or "No registrado",
                                   texto=texto)
    clave = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not clave:
        return {"modo": "copiar", "prompt": prompt}
    import urllib.request
    cuerpo = json.dumps({
        "model": os.environ.get("RESUMEN_MODELO", "claude-sonnet-5"),
        "max_tokens": 1500,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    solicitud = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=cuerpo,
        headers={"Content-Type": "application/json", "x-api-key": clave,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(solicitud, timeout=90) as r:
            data = json.loads(r.read().decode("utf-8"))
        partes = [b.get("text", "") for b in data.get("content", [])
                  if b.get("type") == "text"]
        resumen = "\n".join(p for p in partes if p).strip()
        if not resumen:
            raise ValueError("respuesta vacía")
        return {"modo": "auto", "resumen": resumen}
    except Exception as e:
        return {"modo": "copiar", "prompt": prompt,
                "error": "No se pudo generar automáticamente "
                         f"({type(e).__name__}); se copió el prompt para "
                         "usarlo manualmente en Claude."}


PROMPT_CORRECCION = """Actúa como corrector de estilo y editor profesional para versiones estenográficas legislativas.
Tu tarea es pulir el siguiente fragmento aplicando rigurosamente estas reglas:

1. ORTOGRAFÍA Y MAYÚSCULAS: Corrige nombres propios, lugares e instituciones. Capitaliza SIEMPRE los cargos públicos, honoríficos y legislativos (ej. Diputado, Diputada, Presidente, Presidenta, Secretario, Secretaria, Gobernadora, Alcalde, Pleno, Legislatura, Congreso).
2. PUNTUACIÓN Y PÁRRAFOS: Mejora la legibilidad rompiendo oraciones interminables (usa puntos y seguido en lugar de un exceso de comas). Divide los bloques de texto muy largos en párrafos más cortos y digeribles, estructurando las ideas de forma clara y ejecutiva (máximo 4-5 líneas por párrafo si es posible). Separa cada párrafo con un salto de línea (\\n).
3. NOMBRES DE DIPUTADOS: Si en el texto aparece el nombre de un legislador o legisladora escrito de forma incorrecta o incompleta, y se corresponde claramente con uno de la lista de referencia, corrígelo a la forma EXACTA de esa lista. No inventes ni fuerces coincidencias dudosas.
4. REGLA ESTRICTA: NO inventes información, no resumas y NO modifiques el sentido ni las ideas del orador. Respeta la naturalidad del discurso, solo dale un formato profesional y ortográficamente impecable.

Devuelve ÚNICAMENTE el texto corregido, sin introducciones, saludos ni comentarios.{lista_diputados}

Texto original:
{texto}"""


def corregir_texto_ia(texto, catalogo=None):
    """Corrige ortografía, gramática, estilo y nombres de un fragmento usando
    la API de Anthropic. `catalogo` es una lista opcional de nombres de
    diputados que se usa como referencia para uniformar los nombres."""
    clave = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not clave:
        return {"error": "No hay clave API configurada (variable "
                         "ANTHROPIC_API_KEY)."}

    lista = ""
    if catalogo:
        # Limitamos para no inflar el prompt en catálogos enormes.
        nombres = "\n".join("- " + n for n in catalogo[:400])
        lista = ("\n\nLista de referencia de nombres correctos de "
                 "diputados y diputadas:\n" + nombres)
    prompt = PROMPT_CORRECCION.format(texto=texto, lista_diputados=lista)

    # Escalamos el límite de salida al tamaño del texto para no truncar
    # intervenciones largas (aprox. 1 token ~ 4 caracteres, con holgura).
    max_tokens = max(1024, min(8192, int(len(texto) / 2) + 512))

    import urllib.request
    cuerpo = json.dumps({
        "model": os.environ.get("CORRECCION_MODELO", "claude-haiku-4-5"),
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    solicitud = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=cuerpo,
        headers={"Content-Type": "application/json", "x-api-key": clave,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(solicitud, timeout=120) as r:
            data = json.loads(r.read().decode("utf-8"))
        partes = [b.get("text", "") for b in data.get("content", [])
                  if b.get("type") == "text"]
        texto_corregido = "\n".join(p for p in partes if p).strip()
        if not texto_corregido:
            return {"error": "La IA devolvió una respuesta vacía."}
        return {"texto_corregido": texto_corregido}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Edición de contenido: numeración de los puntos del orden del día
# ---------------------------------------------------------------------------

PROMPT_ESTRUCTURA = """Eres editor parlamentario. Recibes las intervenciones de una sesión legislativa, cada una con un identificador [ID], el nombre del orador y su texto, en orden cronológico.

Al inicio de la reunión se LEE el ORDEN DEL DÍA: la lista de puntos a tratar. Tu tarea es:
1. Encontrar esa lectura y COPIAR el texto COMPLETO de cada punto, tal cual se enuncia.
2. Indicar en qué intervención EMPIEZA el desahogo (la discusión) de cada punto.

Devuelve ÚNICAMENTE un objeto JSON válido, sin texto adicional ni ```json, con esta forma exacta:
{{
  "secciones": [
    {{"ancla_id": 145, "titulo": "Análisis de la Iniciativa de Decreto por el que se expide la Ley ..., presentada por ..., en su caso, intervención de personas servidoras públicas del Gobierno del Estado."}},
    {{"ancla_id": 320, "titulo": "Clausura de la reunión."}}
  ]
}}

Reglas estrictas:
- El "titulo" debe ser el TEXTO ÍNTEGRO Y LITERAL del punto, completo, tal como aparece en la lectura del orden del día. NO lo acortes, NO lo resumas, NO lo parafrasees. Conserva la redacción formal completa, incluidos nombres propios, cargos y honoríficos.
- NO incluyas prefijos de numeración en el "titulo" ("1.", "Punto número uno", "Primer punto", etc.); la numeración se agrega aparte.
- "ancla_id" DEBE ser uno de los [ID] de abajo: la intervención donde EMPIEZA el desahogo de ese punto (no donde solo se lee la lista).
- Devuelve los puntos en el ORDEN del orden del día.
- Si un punto se lee pero no alcanzas a ver su texto completo, usa la versión más completa que aparezca en las intervenciones.
- NO inventes puntos. NO detectes acuerdos ni votaciones. NO modifiques el texto de las intervenciones.

Intervenciones:
{intervenciones}"""


def estructurar_ia(turnos):
    """Recibe una lista de turnos [{id, orador, texto}] y pide a la IA la
    numeración de los puntos del orden del día. Devuelve un dict con
    'secciones' (cada una con ancla_id, numero y titulo), o {'error': ...}."""
    clave = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not clave:
        return {"error": "No hay clave API configurada (variable "
                         "ANTHROPIC_API_KEY)."}
    if not turnos:
        return {"error": "La sesión no tiene intervenciones."}

    # La IA necesita VER el texto completo del punto para copiarlo íntegro.
    # El orden del día se lee al inicio de la reunión, así que damos un
    # extracto generoso a las primeras intervenciones (donde suele leerse) y
    # uno más corto al resto (basta para reconocer dónde empieza cada punto).
    lineas = []
    for i, t in enumerate(turnos):
        limite = 2500 if i < 40 else 400
        extracto = " ".join((t["texto"] or "").split())[:limite]
        lineas.append(f"[ID {t['id']}] {t['orador']}: {extracto}")
    cuerpo_prompt = "\n".join(lineas)
    # Guarda de tamaño: si la sesión es enorme, recomprimimos para no
    # desbordar el contexto (el orden del día ya quedó en las primeras).
    if len(cuerpo_prompt) > 180000:
        lineas = []
        for i, t in enumerate(turnos):
            limite = 2500 if i < 40 else 200
            extracto = " ".join((t["texto"] or "").split())[:limite]
            lineas.append(f"[ID {t['id']}] {t['orador']}: {extracto}")
        cuerpo_prompt = "\n".join(lineas)
    prompt = PROMPT_ESTRUCTURA.format(intervenciones=cuerpo_prompt)

    import urllib.request
    cuerpo = json.dumps({
        "model": os.environ.get("ESTRUCTURA_MODELO", "claude-sonnet-5"),
        "max_tokens": 8192,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    solicitud = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=cuerpo,
        headers={"Content-Type": "application/json", "x-api-key": clave,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(solicitud, timeout=180) as r:
            data = json.loads(r.read().decode("utf-8"))
        partes = [b.get("text", "") for b in data.get("content", [])
                  if b.get("type") == "text"]
        crudo = "\n".join(p for p in partes if p).strip()
        # Quitamos posibles vallas de código ```json ... ```
        if crudo.startswith("```"):
            crudo = crudo.split("\n", 1)[1] if "\n" in crudo else crudo
            crudo = crudo.rsplit("```", 1)[0]
        # Aislamos el objeto JSON por si viene con texto alrededor.
        ini, fin = crudo.find("{"), crudo.rfind("}")
        if ini != -1 and fin != -1:
            crudo = crudo[ini:fin + 1]
        obj = json.loads(crudo)
        # Orden cronológico de los turnos, para numerar los puntos en orden.
        posicion = {t["id"]: i for i, t in enumerate(turnos)}
        ids_validos = set(posicion)

        crudas = []
        vistas = set()
        for s in obj.get("secciones", []):
            try:
                aid = int(s.get("ancla_id"))
            except (TypeError, ValueError):
                continue
            if aid not in ids_validos or aid in vistas:
                continue
            titulo = (s.get("titulo") or "").strip()
            # Quitamos prefijos de numeración que la IA pudiera dejar; la
            # numeración la ponemos nosotros para que sea consistente.
            #  - "1. ", "2) ", "3 - " …
            titulo = re.sub(r"^\s*\d+\s*[.)-]\s*", "", titulo).strip()
            #  - "Punto número uno:", "Primer punto.", "Punto 1 -" …
            titulo = re.sub(
                r"^\s*(punto\s+(n[uú]mero\s+)?[\w]+|"
                r"(primer|segund|tercer|cuart|quint|sext|s[eé]ptim|octav|"
                r"noven|d[eé]cim)[oa]\s+punto)\s*[.:)-]?\s*",
                "", titulo, flags=re.IGNORECASE).strip()
            if not titulo:
                continue
            vistas.add(aid)
            crudas.append((posicion[aid], aid, titulo))

        # Ordenamos por aparición y numeramos 1., 2., 3., …
        crudas.sort(key=lambda x: x[0])
        secciones = []
        for numero, (_, aid, titulo) in enumerate(crudas, start=1):
            secciones.append({
                "ancla_id": aid,
                "numero": numero,
                "titulo": f"{numero}. {titulo}",
            })
        return {"secciones": secciones}
    except json.JSONDecodeError:
        return {"error": "La IA no devolvió un JSON válido; intenta de nuevo."}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def turnos_de_sesion(con, sesion_id):
    """Agrupa las participaciones en turnos consecutivos por orador y
    devuelve [{id, orador, texto}] usando el id de la primera fila del turno
    como ancla."""
    filas = con.execute(
        "SELECT id, orador, texto FROM participaciones "
        "WHERE sesion_id=? ORDER BY inicio_seg, id", (sesion_id,)).fetchall()
    turnos, actual = [], None
    for f in filas:
        if actual is None or actual["orador"] != f["orador"]:
            actual = {"id": f["id"], "orador": f["orador"],
                      "texto": (f["texto"] or "")}
            turnos.append(actual)
        else:
            actual["texto"] += " " + (f["texto"] or "")
    return turnos


# ---------------------------------------------------------------------------
# Servidor
# ---------------------------------------------------------------------------

class Manejador(BaseHTTPRequestHandler):
    ruta_db = "sesiones.db"
    # Lanzador de transcripción (costura 2)
    ruta_transcriptor = "transcribir_en_vivo_c3.py"
    ruta_contextos = "contextos.json"
    proc_activo = None          # subprocess.Popen en curso (o None)
    log_transcripcion = None    # ruta del log de la transcripción en curso
    _lock_proc = threading.Lock()
    requiere_login = False

    def log_message(self, *args):        # silenciar el registro por consola
        pass

    def _db(self):
        # timeout: espera hasta 30 s si otro proceso (el transcriptor en
        # vivo) está escribiendo, en vez de fallar con "database is locked".
        con = sqlite3.connect(self.ruta_db, timeout=30)
        con.row_factory = sqlite3.Row
        # WAL permite leer mientras otro escribe; busy_timeout refuerza la
        # espera a nivel del motor SQLite.
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA busy_timeout=30000")
            con.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.OperationalError:
            pass
        # Tabla de resúmenes guardados (uno por bloque, identificado por el
        # primer id de sus segmentos). Se crea sola la primera vez.
        con.execute("""
            CREATE TABLE IF NOT EXISTS resumenes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                sesion_id  INTEGER,
                ancla_id   INTEGER UNIQUE,
                orador     TEXT,
                resumen    TEXT,
                creado     TEXT
            )""")
        # Estructura editorial de la sesión: encabezados de sección (orden
        # del día) numerados. Se guarda como JSON, uno por sesión.
        con.execute("""
            CREATE TABLE IF NOT EXISTS estructura (
                sesion_id  INTEGER PRIMARY KEY,
                datos      TEXT,
                creado     TEXT
            )""")
        con.commit()
        return con

    def _responder(self, cuerpo, tipo="application/json; charset=utf-8",
                   codigo=200):
        datos = (cuerpo if isinstance(cuerpo, bytes)
                 else json.dumps(cuerpo, ensure_ascii=False).encode("utf-8"))
        try:
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(datos)))
            # Evita que el navegador muestre una versión cacheada tras
            # actualizar el archivo (la causa típica de "lo veo igual").
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(datos)
        except OSError:
            # El cliente cerró la conexión antes de recibir toda la
            # respuesta (recarga, cambio de sesión, o el auto-refresh "En
            # vivo" que lanza una petición nueva y aborta la anterior). En
            # Windows esto llega como ConnectionAbortedError (WinError
            # 10053); en Linux/Mac como BrokenPipe/ConnectionReset. Todos
            # son inofensivos: se ignoran para no ensuciar la consola.
            pass

    # -- Autenticación opcional (--requiere-login) --------------------

    def _usuario_autenticado(self):
        if not self.requiere_login:
            return True
        m = re.search(rf"{COOKIE_SESION}=([^;]+)",
                      self.headers.get("Cookie", ""))
        if not m:
            return False
        try:
            _jwt.decode(m.group(1), _jwt_secret, algorithms=[_jwt_algoritmo])
            return True
        except Exception:
            return False

    def _redirigir_login(self):
        self.send_response(302)
        self.send_header("Location", "/login")
        self.end_headers()

    def _pagina_login(self):
        self._responder(PAGINA_LOGIN.encode("utf-8"),
                        "text/html; charset=utf-8")

    def _procesar_login(self, datos):
        email = (datos.get("email") or "").strip().lower()
        password = datos.get("password") or ""
        usuario = _obtener_usuario(email) if email else None
        if not usuario or not _verificar_password(password,
                                                   usuario["password_hash"]):
            return self._responder({"error": "Email o contraseña incorrectos"},
                                   codigo=401)
        token = _crear_token(usuario["email"])
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header(
            "Set-Cookie",
            f"{COOKIE_SESION}={token}; Path=/; HttpOnly; SameSite=Lax")
        cuerpo = json.dumps({"ok": True}).encode("utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _cerrar_sesion(self):
        self.send_response(302)
        self.send_header("Location", "/login")
        self.send_header(
            "Set-Cookie", f"{COOKIE_SESION}=; Path=/; Max-Age=0")
        self.end_headers()

    _args_transcriptor_cache = None
    _args_transcriptor_leido = False

    @classmethod
    def _args_transcriptor(cls):
        """Conjunto de opciones largas (--x) que acepta el transcriptor,
        leyendo su ayuda con --help. Se cachea una sola vez. Si no se pudo
        leer, devuelve None y el lanzador pasa todos los argumentos (como
        antes), para no romper transcriptores que no imprimen ayuda."""
        if cls._args_transcriptor_leido:
            return cls._args_transcriptor_cache
        soportadas = None
        try:
            out = subprocess.run(
                [sys.executable, cls.ruta_transcriptor, "--help"],
                capture_output=True, text=True, timeout=30)
            texto = (out.stdout or "") + (out.stderr or "")
            encontradas = set(re.findall(r"--[a-zA-Z][\w-]*", texto))
            # Solo confiamos en la detección si la ayuda se leyó de verdad.
            soportadas = encontradas or None
        except Exception:
            soportadas = None
        cls._args_transcriptor_cache = soportadas
        cls._args_transcriptor_leido = True
        return soportadas

    def _lanzar_transcripcion(self, datos):
        """Arranca transcribir_en_vivo_c3.py como proceso aparte, con el
        contexto elegido en los selects. Solo uno a la vez."""
        url = (datos.get("url") or "").strip()
        tipo = (datos.get("tipo") or "pleno").strip()
        comisiones = [c for c in (datos.get("comisiones") or []) if c]
        fecha = (datos.get("fecha") or "").strip()
        modelo = (datos.get("modelo") or "").strip()

        if not url.startswith("http"):
            return self._responder(
                {"error": "Pega una URL de video válida (http…)."}, codigo=400)
        if tipo == "comision" and not comisiones:
            return self._responder(
                {"error": "Elige al menos una comisión (o cambia a Pleno)."},
                codigo=400)
        if not os.path.isfile(self.ruta_transcriptor):
            return self._responder(
                {"error": f"No encuentro el transcriptor "
                          f"({self.ruta_transcriptor}). Ponlo en esta carpeta "
                          "o pásalo con --transcriptor."}, codigo=400)

        with Manejador._lock_proc:
            if Manejador.proc_activo and Manejador.proc_activo.poll() is None:
                return self._responder(
                    {"error": "Ya hay una transcripción en curso. Deténla "
                              "antes de iniciar otra."}, codigo=409)

            # Solo pasamos los argumentos que el transcriptor realmente acepta,
            # así funciona con versiones viejas que no conocen --tipo/--comision.
            soportadas = self._args_transcriptor()

            def acepta(flag):
                # Si no se pudo leer la ayuda (None), se asume que sí (previo).
                return soportadas is None or flag in soportadas

            cmd = [sys.executable, "-u", self.ruta_transcriptor, url]
            omitidas = []
            if acepta("--voz"):
                cmd += ["--voz"]
            if acepta("--db"):
                cmd += ["--db", self.ruta_db]
            if acepta("--contextos"):
                cmd += ["--contextos", self.ruta_contextos]
            else:
                omitidas.append("--contextos")
            if modelo and acepta("--modelo"):
                cmd += ["--modelo", modelo]
            if acepta("--tipo"):
                cmd += ["--tipo", tipo]
            else:
                omitidas.append("--tipo")
            if acepta("--comision"):
                for c in comisiones:
                    cmd += ["--comision", c]
            elif comisiones:
                omitidas.append("--comision")
            if fecha:
                if acepta("--fecha"):
                    cmd += ["--fecha", fecha]
                else:
                    omitidas.append("--fecha")

            carpeta = os.path.dirname(os.path.abspath(self.ruta_transcriptor))
            log = os.path.join(carpeta, "transcripcion_en_curso.log")
            entorno = dict(os.environ, PYTHONIOENCODING="utf-8",
                           PYTHONUNBUFFERED="1")
            try:
                flog = open(log, "wb")
                if omitidas:
                    flog.write((
                        "[aviso] Tu transcriptor no acepta estos argumentos y "
                        "se omitieron: " + ", ".join(omitidas) + ".\n"
                        "        La transcripción arrancará igual, pero la "
                        "sesión no quedará etiquetada con esos datos.\n"
                        "        Para usarlos, actualiza "
                        + os.path.basename(self.ruta_transcriptor)
                        + " para que los acepte.\n\n").encode("utf-8"))
                flog.write(("$ " + " ".join(
                    (f'"{a}"' if " " in a else a) for a in cmd)
                    + "\n\n").encode("utf-8"))
                flog.flush()
                proc = subprocess.Popen(
                    cmd, stdout=flog, stderr=subprocess.STDOUT,
                    cwd=carpeta or None, env=entorno)
            except Exception as e:
                return self._responder(
                    {"error": f"No pude arrancar la transcripción: {e}"},
                    codigo=500)
            Manejador.proc_activo = proc
            Manejador.log_transcripcion = log

        # Comando legible para mostrar en la interfaz (transparencia)
        legible = " ".join((f'"{a}"' if " " in a else a) for a in cmd)
        respuesta = {"ok": True, "pid": proc.pid, "comando": legible}
        if omitidas:
            respuesta["aviso"] = (
                "Tu transcriptor no acepta " + ", ".join(omitidas)
                + "; se omitieron y la sesión no quedará etiquetada con esos "
                "datos. Actualiza el transcriptor para usarlos.")
        return self._responder(respuesta)

    def _detener_transcripcion(self):
        """Pide al proceso en curso que termine."""
        proc = Manejador.proc_activo
        if not (proc and proc.poll() is None):
            return self._responder(
                {"ok": True, "nota": "No había ninguna transcripción en "
                                     "curso."})
        try:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception as e:
            return self._responder(
                {"error": f"No pude detenerla: {e}"}, codigo=500)
        return self._responder({"ok": True, "detenida": True})

    def do_GET(self):
        p = urlparse(self.path)
        if p.path == "/login":
            return self._pagina_login()
        if p.path == "/logout":
            return self._cerrar_sesion()
        if not self._usuario_autenticado():
            return self._redirigir_login()
        if p.path in ("/", "/index.html"):
            pagina = PAGINA.replace(
                "__REQUIERE_LOGIN__", "true" if self.requiere_login else "false")
            self._responder(pagina.encode("utf-8"),
                            "text/html; charset=utf-8")
        elif p.path == "/api/sesiones":
            con = self._db()
            filas = [dict(r) for r in con.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM participaciones p "
                " WHERE p.sesion_id = s.id) AS segmentos "
                "FROM sesiones s ORDER BY s.id DESC")]
            con.close()
            self._responder(filas)
        elif p.path == "/api/participaciones":
            q = parse_qs(p.query)
            sid = int(q.get("sesion", ["0"])[0] or 0)
            con = self._db()
            ses = con.execute("SELECT * FROM sesiones WHERE id=?",
                              (sid,)).fetchone()
            cols = {r[1] for r in con.execute(
                "PRAGMA table_info(participaciones)")}
            opcionales = [c for c in ("voz_orador", "voz_similitud",
                                      "revisado_ia", "motivo_ia")
                          if c in cols]
            extra = ("," + ",".join(opcionales)) if opcionales else ""
            filas = [dict(r) for r in con.execute(
                f"SELECT id, orador, inicio_seg, fin_seg, texto{extra} "
                "FROM participaciones WHERE sesion_id=? "
                "ORDER BY inicio_seg, id", (sid,))]
            resumenes = {r["ancla_id"]: r["resumen"] for r in con.execute(
                "SELECT ancla_id, resumen FROM resumenes WHERE sesion_id=?",
                (sid,))}
            estructura = None
            fila_est = con.execute(
                "SELECT datos FROM estructura WHERE sesion_id=?",
                (sid,)).fetchone()
            if fila_est and fila_est["datos"]:
                try:
                    estructura = json.loads(fila_est["datos"])
                except (ValueError, TypeError):
                    estructura = None
            con.close()
            self._responder({"sesion": dict(ses) if ses else None,
                             "filas": filas, "resumenes": resumenes,
                             "estructura": estructura})
        elif p.path == "/api/catalogo":
            self._responder(cargar_catalogo())
        elif p.path == "/api/contextos":
            # Lista de comisiones del JSON, para llenar el select. Si no hay
            # archivo o está mal, el lanzador sigue sirviendo para el pleno.
            info = {"disponible": False, "comisiones": [],
                    "tiene_transcriptor": os.path.isfile(
                        self.ruta_transcriptor),
                    "archivo": self.ruta_contextos}
            try:
                if os.path.isfile(self.ruta_contextos):
                    with open(self.ruta_contextos, encoding="utf-8") as f:
                        cfg = json.load(f)
                    info["disponible"] = True
                    info["comisiones"] = sorted(
                        (cfg.get("comisiones") or {}).keys())
                    info["tiene_mesa"] = bool(cfg.get("mesa_directiva"))
            except Exception as e:
                info["error"] = str(e)
            self._responder(info)
        elif p.path == "/api/exportar_word":
            q = parse_qs(p.query)
            try:
                sid = int(q.get("sesion", ["0"])[0] or 0)
            except (TypeError, ValueError):
                sid = 0
            if not sid:
                return self._responder({"error": "Falta el ID de la sesión"},
                                       codigo=400)
            con = self._db()
            try:
                ses = con.execute("SELECT * FROM sesiones WHERE id=?",
                                  (sid,)).fetchone()
                filas = con.execute(
                    "SELECT id, orador, texto FROM participaciones "
                    "WHERE sesion_id=? ORDER BY inicio_seg", (sid,)).fetchall()
                # Resúmenes ejecutivos guardados (si la tabla existe)
                try:
                    resumenes = con.execute(
                        "SELECT orador, resumen FROM resumenes "
                        "WHERE sesion_id=? ORDER BY ancla_id", (sid,)).fetchall()
                except sqlite3.Error:
                    resumenes = []
                # Estructura (encabezados del orden del día), si existe
                estructura = {"secciones": []}
                try:
                    fe = con.execute(
                        "SELECT datos FROM estructura WHERE sesion_id=?",
                        (sid,)).fetchone()
                    if fe and fe["datos"]:
                        estructura = json.loads(fe["datos"])
                except (sqlite3.Error, ValueError, TypeError):
                    pass
            finally:
                con.close()
            # Mapa ancla_id -> título de sección para insertar encabezados.
            titulos_seccion = {s["ancla_id"]: s["titulo"]
                               for s in estructura.get("secciones", [])
                               if s.get("ancla_id") is not None}

            from docx.shared import Inches, Pt, Cm
            from docx.enum.text import WD_ALIGN_PARAGRAPH
            doc = Document()

            # --- CONFIGURAR MÁRGENES ---
            for section in doc.sections:
                section.page_width = Inches(8.5)
                section.page_height = Inches(11)
                section.top_margin = Cm(3.5)
                section.bottom_margin = Cm(2.5)
                section.left_margin = Cm(3)
                section.right_margin = Cm(2.5)

                # --- INSERTAR IMAGEN (MEMBRETE) ---
                # Ruta configurable con la variable de entorno MEMBRETE_IMG;
                # si no, se buscan varios nombres junto al script.
                header = section.header
                header_para = (header.paragraphs[0] if header.paragraphs
                               else header.add_paragraph())
                header_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                ruta_membrete = _ruta_membrete()
                if ruta_membrete:
                    try:
                        header_para.add_run().add_picture(
                            ruta_membrete, width=Inches(7.5))
                    except Exception as e:
                        print("No se pudo insertar el membrete:", e)

            # --- CONFIGURAR FUENTE ARIAL 11, JUSTIFICADO Y ESPACIADO ---
            style = doc.styles['Normal']
            font = style.font
            font.name = 'Arial'
            font.size = Pt(11)
            pf = style.paragraph_format
            pf.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            pf.line_spacing = 1.15
            pf.space_after = Pt(12)

            # --- AGREGAR TÍTULOS AL DOCUMENTO ---
            titulo_texto = ("VERSIÓN ESTENOGRÁFICA: "
                            + (ses['titulo'] if ses and ses['titulo']
                               else 'SESIÓN LEGISLATIVA'))
            p_titulo = doc.add_paragraph()
            p_titulo.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run_titulo = p_titulo.add_run(titulo_texto.upper())
            run_titulo.bold = True
            run_titulo.font.size = Pt(12)

            p_fecha = doc.add_paragraph(
                "Fecha: " + _fecha_legible(ses['inicio'] if ses else None))
            p_fecha.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p_fecha.runs[0].font.size = Pt(10)
            p_fecha.runs[0].font.italic = True

            doc.add_paragraph("-" * 65).alignment = WD_ALIGN_PARAGRAPH.CENTER
            
            # Helper: inserta un encabezado de sección (punto del orden del día).
            def agregar_encabezado(titulo):
                pe = doc.add_paragraph()
                pe.paragraph_format.space_before = Pt(14)
                pe.paragraph_format.space_after = Pt(6)
                pe.paragraph_format.keep_with_next = True
                pe.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                run_e = pe.add_run(titulo)
                run_e.bold = True
                run_e.font.size = Pt(11)

            # Helper: vuelca un texto en uno o varios párrafos, respetando
            # los saltos de línea (\n) que introdujo la corrección de la IA.
            # El primer párrafo puede empezar con el nombre del orador en negrita.
            def volcar_intervencion(orador, texto):
                # Cada bloque separado por \n (o líneas en blanco) es un párrafo.
                bloques = [b.strip() for b in texto.replace("\r", "").split("\n")]
                bloques = [b for b in bloques if b]
                if not bloques:
                    return
                # Primer párrafo: NOMBRE en negrita + primer bloque.
                p0 = doc.add_paragraph()
                run_orador = p0.add_run(f"{(orador or 'DESCONOCIDO').upper()}: ")
                run_orador.bold = True
                p0.add_run(bloques[0])
                # Párrafos siguientes de la misma intervención (sangría de continuación).
                for b in bloques[1:]:
                    pc = doc.add_paragraph(b)
                    pc.paragraph_format.first_line_indent = Cm(1)

            # Agrupamos por orador consecutivo para que cada intervención
            # sea un bloque coherente (aunque en la BD estén partida en filas).
            orador_actual = None
            ancla_actual = None
            buffer_texto = []

            def vaciar_buffer():
                if orador_actual is not None and buffer_texto:
                    # Si esta intervención inicia un punto del orden del día,
                    # insertamos primero su encabezado de sección.
                    if ancla_actual in titulos_seccion:
                        agregar_encabezado(titulos_seccion[ancla_actual])
                    volcar_intervencion(orador_actual,
                                        "\n".join(buffer_texto).strip())

            for f in filas:
                texto_segmento = (f['texto'] or "").strip()
                if not texto_segmento:
                    continue
                if f['orador'] != orador_actual:
                    vaciar_buffer()
                    orador_actual = f['orador']
                    ancla_actual = f['id']
                    buffer_texto = [texto_segmento]
                else:
                    buffer_texto.append(texto_segmento)
            vaciar_buffer()

            # --- ANEXO: RESÚMENES EJECUTIVOS (si existen) ---
            resumenes_utiles = [r for r in resumenes
                                if (r["resumen"] or "").strip()]
            if resumenes_utiles:
                doc.add_page_break()
                p_anexo = doc.add_paragraph()
                p_anexo.alignment = WD_ALIGN_PARAGRAPH.CENTER
                r_anexo = p_anexo.add_run("ANEXO — RESÚMENES EJECUTIVOS")
                r_anexo.bold = True
                r_anexo.font.size = Pt(12)
                doc.add_paragraph("-" * 65).alignment = WD_ALIGN_PARAGRAPH.CENTER
                for r in resumenes_utiles:
                    if r["orador"]:
                        p_ro = doc.add_paragraph()
                        p_ro.add_run((r["orador"] or "").upper()).bold = True
                    for linea in (r["resumen"] or "").replace("\r", "").split("\n"):
                        linea = linea.strip()
                        if linea:
                            doc.add_paragraph(linea)

            archivo_memoria = BytesIO()
            doc.save(archivo_memoria)
            datos_docx = archivo_memoria.getvalue()

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document")
            self.send_header(
                "Content-Disposition",
                f"attachment; filename=Sesion_{sid}.docx")
            self.send_header("Content-Length", str(len(datos_docx)))
            self.end_headers()
            self.wfile.write(datos_docx)

        elif p.path == "/api/transcripcion_estado":
            proc = Manejador.proc_activo
            corriendo = bool(proc and proc.poll() is None)
            codigo = (None if corriendo
                      else (proc.returncode if proc else None))
            lineas = ""
            log = Manejador.log_transcripcion
            if log and os.path.isfile(log):
                try:
                    with open(log, "rb") as f:
                        # solo la cola del log, para no mandar todo el archivo
                        try:
                            f.seek(-8000, os.SEEK_END)
                        except OSError:
                            f.seek(0)
                        crudo = f.read()
                    lineas = crudo.decode("utf-8", errors="replace")
                    # descartar una posible primera línea partida por el seek
                    if len(lineas) >= 8000 and "\n" in lineas:
                        lineas = lineas.split("\n", 1)[1]
                except Exception:
                    lineas = ""
            self._responder({"corriendo": corriendo,
                             "pid": (proc.pid if proc else None),
                             "codigo": codigo, "lineas": lineas})
        else:
            self._responder({"error": "no encontrado"}, codigo=404)

    def do_POST(self):
        p = urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            datos = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._responder({"error": "JSON inválido"}, codigo=400)

        if p.path == "/login":
            return self._procesar_login(datos)
        if not self._usuario_autenticado():
            return self._responder({"error": "No autenticado"}, codigo=401)

        # --- Lanzador de transcripción (costura 2) ---------------------
        # Estas rutas no tocan la base de datos, así que se resuelven antes
        # de abrir la conexión.
        if p.path == "/api/transcribir":
            return self._lanzar_transcripcion(datos)
        if p.path == "/api/detener":
            return self._detener_transcripcion()

        con = self._db()
        try:
            if p.path == "/api/actualizar":
                ids = [int(i) for i in datos.get("ids", [])]
                orador = (datos.get("orador") or "").strip()
                sid = int(datos.get("sesion_id", 0) or 0)
                if not ids or not orador:
                    return self._responder({"error": "faltan datos"},
                                           codigo=400)
                marcas = ",".join("?" * len(ids))
                # Una corrección manual manda sobre el veredicto del
                # corrector: se limpia la insignia de IA de esas filas (si
                # la base tiene esas columnas).
                cols = {r[1] for r in con.execute(
                    "PRAGMA table_info(participaciones)")}
                limpiar = (", revisado_ia=NULL, motivo_ia=NULL"
                           if "revisado_ia" in cols else "")
                cur = con.execute(
                    f"UPDATE participaciones SET orador=?{limpiar} "
                    f"WHERE id IN ({marcas})", [orador] + ids)
                con.commit()
                # Unir en la base los registros que quedaron consecutivos
                # con el mismo orador (los corregidos y sus vecinos)
                unidos = compactar(con, sid, solo_ids=ids) if sid else 0
                self._responder({"ok": True, "cambios": cur.rowcount,
                                 "unidos": unidos})
            elif p.path == "/api/dividir":
                # Separar dos oradores dentro de un mismo bloque. 'corte' es
                # el desplazamiento en caracteres sobre el texto del bloque
                # unido con espacios (igual que lo arma el front).
                sid = int(datos.get("sesion_id", 0) or 0)
                ids = [int(i) for i in datos.get("ids", [])]
                corte = int(datos.get("corte", 0) or 0)
                orador1 = (datos.get("orador1") or "").strip()
                orador2 = (datos.get("orador2") or "").strip()
                if not (ids and orador1 and orador2):
                    return self._responder({"error": "faltan datos"},
                                           codigo=400)
                cols = {r[1] for r in con.execute(
                    "PRAGMA table_info(participaciones)")}
                limpiar = (", revisado_ia=NULL, motivo_ia=NULL"
                           if "revisado_ia" in cols else "")
                filas = {r["id"]: r for r in con.execute(
                    f"SELECT * FROM participaciones WHERE id IN "
                    f"({','.join('?' * len(ids))})", ids)}
                orden = [filas[i] for i in ids if i in filas]
                if not orden:
                    return self._responder({"error": "filas no encontradas"},
                                           codigo=400)
                # Localizar en qué fila (y en qué punto de ella) cae el corte
                pos, fila_k, off_local = 0, None, 0
                for k, f in enumerate(orden):
                    txt = f["texto"] or ""
                    if corte <= pos:
                        fila_k, off_local = k, 0
                        break
                    if corte < pos + len(txt):
                        fila_k, off_local = k, corte - pos
                        break
                    pos += len(txt) + 1          # +1 por el espacio de unión
                if fila_k is None:
                    return self._responder(
                        {"error": "el corte deja la segunda parte vacía"},
                        codigo=400)
                # 1ª parte: filas estrictamente anteriores al corte -> orador1
                antes = [orden[j]["id"] for j in range(fila_k)]
                if antes:
                    m = ",".join("?" * len(antes))
                    con.execute(
                        f"UPDATE participaciones SET orador=?{limpiar} "
                        f"WHERE id IN ({m})", [orador1] + antes)
                nuevo_id = None
                if off_local > 0:
                    # El corte cae DENTRO de una fila: se parte en dos
                    f = orden[fila_k]
                    txt = f["texto"] or ""
                    t1, t2 = txt[:off_local].strip(), txt[off_local:].strip()
                    ini_f = f["inicio_seg"] or 0
                    fin_f = f["fin_seg"] or ini_f
                    corte_seg = ini_f + (fin_f - ini_f) * (off_local / len(txt))
                    con.execute(
                        f"UPDATE participaciones SET orador=?, texto=?, "
                        f"fin_seg=?, fin_hms=?{limpiar} WHERE id=?",
                        (orador1, t1, corte_seg, hms(corte_seg), f["id"]))
                    campos = ("sesion_id, orador, inicio_seg, fin_seg, "
                              "inicio_hms, fin_hms, texto")
                    valores = [sid or f["sesion_id"], orador2, corte_seg,
                               fin_f, hms(corte_seg), hms(fin_f), t2]
                    if "fuente" in cols:
                        campos += ", fuente"
                        valores.append("manual")
                    cur = con.execute(
                        f"INSERT INTO participaciones ({campos}) VALUES "
                        f"({','.join('?' * len(valores))})", valores)
                    nuevo_id = cur.lastrowid
                    despues = [orden[j]["id"]
                               for j in range(fila_k + 1, len(orden))]
                else:
                    # Corte limpio en frontera de fila
                    despues = [orden[j]["id"]
                               for j in range(fila_k, len(orden))]
                if despues:
                    m = ",".join("?" * len(despues))
                    con.execute(
                        f"UPDATE participaciones SET orador=?{limpiar} "
                        f"WHERE id IN ({m})", [orador2] + despues)
                con.commit()
                self._responder({"ok": True, "nuevo_id": nuevo_id})
            elif p.path == "/api/renombrar":
                sid = int(datos.get("sesion_id", 0) or 0)
                de = (datos.get("de") or "").strip()
                a = (datos.get("a") or "").strip()
                if not (sid and de and a):
                    return self._responder({"error": "faltan datos"},
                                           codigo=400)
                cur = con.execute(
                    "UPDATE participaciones SET orador=? "
                    "WHERE sesion_id=? AND orador=?", (a, sid, de))
                con.commit()
                unidos = compactar(con, sid)
                self._responder({"ok": True, "cambios": cur.rowcount,
                                 "unidos": unidos})
            elif p.path == "/api/compactar":
                sid = int(datos.get("sesion_id", 0) or 0)
                if not sid:
                    return self._responder({"error": "faltan datos"},
                                           codigo=400)
                unidos = compactar(con, sid)
                self._responder({"ok": True, "unidos": unidos})
            elif p.path == "/api/textos":
                cambios = datos.get("cambios") or []
                n = 0
                for c in cambios:
                    try:
                        rid = int(c.get("id"))
                    except (TypeError, ValueError):
                        continue
                    cur = con.execute(
                        "UPDATE participaciones SET texto=? WHERE id=?",
                        (c.get("texto", ""), rid))
                    n += cur.rowcount
                con.commit()
                self._responder({"ok": True, "cambios": n})
            elif p.path == "/api/corregir_estilo":
                sid = int(datos.get("sesion_id", 0) or 0)
                if not sid:
                    return self._responder(
                        {"error": "Falta el ID de la sesión"}, codigo=400)
                if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
                    return self._responder(
                        {"error": "No hay clave API configurada "
                                  "(variable ANTHROPIC_API_KEY)."}, codigo=400)

                # 1) Unimos bloques consecutivos del mismo orador para corregir
                #    intervenciones completas (la IA reparte mejor los párrafos).
                compactar(con, sid)

                # 2) Nos aseguramos de tener columna de respaldo del original,
                #    para poder comparar o revertir la corrección.
                cols = {r[1] for r in con.execute(
                    "PRAGMA table_info(participaciones)")}
                if "texto_original" not in cols:
                    con.execute("ALTER TABLE participaciones "
                                "ADD COLUMN texto_original TEXT")
                    con.commit()

                catalogo = cargar_catalogo()
                filas = con.execute(
                    "SELECT id, texto, texto_original FROM participaciones "
                    "WHERE sesion_id=? AND texto IS NOT NULL", (sid,)).fetchall()
                modificados = 0
                errores = []
                for f in filas:
                    texto_actual = (f["texto"] or "").strip()
                    if len(texto_actual) < 10:
                        continue
                    res = corregir_texto_ia(texto_actual, catalogo)
                    if "texto_corregido" in res:
                        # Guardamos el original solo la primera vez.
                        if not (f["texto_original"] or "").strip():
                            con.execute(
                                "UPDATE participaciones SET texto_original=? "
                                "WHERE id=?", (texto_actual, f["id"]))
                        con.execute(
                            "UPDATE participaciones SET texto=? WHERE id=?",
                            (res["texto_corregido"], f["id"]))
                        modificados += 1
                    elif res.get("error"):
                        errores.append(res["error"])
                con.commit()
                resp = {"ok": True, "cambios": modificados}
                if errores:
                    # Reportamos solo el primer error para no saturar.
                    resp["aviso"] = (f"{len(errores)} segmento(s) no se "
                                     f"pudieron corregir. Ej.: {errores[0]}")
                self._responder(resp)
            elif p.path == "/api/estructurar":
                sid = int(datos.get("sesion_id", 0) or 0)
                if not sid:
                    return self._responder(
                        {"error": "Falta el ID de la sesión"}, codigo=400)
                if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
                    return self._responder(
                        {"error": "No hay clave API configurada "
                                  "(variable ANTHROPIC_API_KEY)."}, codigo=400)
                turnos = turnos_de_sesion(con, sid)
                res = estructurar_ia(turnos)
                if res.get("error"):
                    return self._responder({"error": res["error"]}, codigo=502)
                con.execute(
                    "INSERT INTO estructura (sesion_id, datos, creado) "
                    "VALUES (?,?,?) "
                    "ON CONFLICT(sesion_id) DO UPDATE SET "
                    "datos=excluded.datos, creado=excluded.creado",
                    (sid, json.dumps(res, ensure_ascii=False),
                     datetime.now().isoformat(timespec="seconds")))
                con.commit()
                self._responder({
                    "ok": True,
                    "secciones": len(res["secciones"]),
                    "estructura": res})
            elif p.path == "/api/borrar_estructura":
                sid = int(datos.get("sesion_id", 0) or 0)
                if sid:
                    con.execute("DELETE FROM estructura WHERE sesion_id=?",
                                (sid,))
                    con.commit()
                self._responder({"ok": True})
            elif p.path == "/api/resumen":
                orador = (datos.get("orador") or "").strip()
                texto = (datos.get("texto") or "").strip()
                sid = int(datos.get("sesion_id", 0) or 0)
                ancla = int(datos.get("ancla_id", 0) or 0)
                if not texto:
                    return self._responder({"error": "sin texto"},
                                           codigo=400)
                res = generar_resumen(orador, texto)
                # Si se generó automáticamente, se guarda pegado al bloque
                if res.get("modo") == "auto" and res.get("resumen") and ancla:
                    con.execute(
                        "INSERT INTO resumenes "
                        "(sesion_id, ancla_id, orador, resumen, creado) "
                        "VALUES (?,?,?,?,?) "
                        "ON CONFLICT(ancla_id) DO UPDATE SET "
                        "resumen=excluded.resumen, orador=excluded.orador, "
                        "creado=excluded.creado",
                        (sid, ancla, orador, res["resumen"],
                         datetime.now().isoformat(timespec="seconds")))
                    con.commit()
                self._responder(res)
            elif p.path == "/api/guardar_resumen":
                # Guardar un resumen editado o pegado a mano
                sid = int(datos.get("sesion_id", 0) or 0)
                ancla = int(datos.get("ancla_id", 0) or 0)
                orador = (datos.get("orador") or "").strip()
                resumen = (datos.get("resumen") or "").strip()
                if not ancla:
                    return self._responder({"error": "falta ancla"},
                                           codigo=400)
                if resumen:
                    con.execute(
                        "INSERT INTO resumenes "
                        "(sesion_id, ancla_id, orador, resumen, creado) "
                        "VALUES (?,?,?,?,?) "
                        "ON CONFLICT(ancla_id) DO UPDATE SET "
                        "resumen=excluded.resumen, orador=excluded.orador, "
                        "creado=excluded.creado",
                        (sid, ancla, orador, resumen,
                         datetime.now().isoformat(timespec="seconds")))
                else:
                    con.execute("DELETE FROM resumenes WHERE ancla_id=?",
                                (ancla,))
                con.commit()
                self._responder({"ok": True})
            elif p.path == "/api/borrar_resumen":
                ancla = int(datos.get("ancla_id", 0) or 0)
                con.execute("DELETE FROM resumenes WHERE ancla_id=?",
                            (ancla,))
                con.commit()
                self._responder({"ok": True})
            else:
                self._responder({"error": "no encontrado"}, codigo=404)
        finally:
            con.close()


def main():
    ap = argparse.ArgumentParser(
        description="Interfaz web local para revisar y corregir oradores.")
    ap.add_argument("--db", default="sesiones.db",
                    help="Base de datos SQLite (default: sesiones.db)")
    ap.add_argument("--puerto", type=int, default=8756,
                    help="Puerto local (default: 8756)")
    ap.add_argument("--transcriptor", default="transcribir_en_vivo_c3.py",
                    help="script de transcripción que arranca el botón "
                         "(default: transcribir_en_vivo_c3.py)")
    ap.add_argument("--contextos", default="contextos.json",
                    help="archivo de contextos para llenar el select de "
                         "comisiones (default: contextos.json)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="Dirección donde escuchar (default: 127.0.0.1; "
                        "usa 0.0.0.0 para exponerlo fuera de esta máquina, "
                        "p. ej. dentro de un contenedor Docker)")
    ap.add_argument("--requiere-login", action="store_true",
                    help="exige iniciar sesión con un usuario de la API "
                        "(mismo MySQL/usuarios de api/; ver README) antes "
                        "de usar la interfaz. Recomendado si --host no es "
                        "127.0.0.1")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not os.path.isfile(args.db):
        print(f"No encuentro la base de datos: {os.path.abspath(args.db)}")
        print("Corre primero transcribir_en_vivo.py, o indica la ruta "
              "con --db.")
        sys.exit(1)

    if args.requiere_login:
        global _verificar_password, _obtener_usuario, _crear_token
        global _jwt, _jwt_secret, _jwt_algoritmo
        from jose import jwt as _jwt_modulo
        from api.config import settings
        from api.security import (crear_token, obtener_usuario_por_email,
                                  verificar_password)
        _verificar_password = verificar_password
        _obtener_usuario = obtener_usuario_por_email
        _crear_token = crear_token
        _jwt = _jwt_modulo
        _jwt_secret = settings.jwt_secret
        _jwt_algoritmo = settings.jwt_algoritmo
        Manejador.requiere_login = True
        print("Login requerido: usuarios de MySQL (api/), ver /login")

    Manejador.ruta_db = args.db
    Manejador.ruta_transcriptor = args.transcriptor
    Manejador.ruta_contextos = args.contextos
    direccion = f"http://{args.host}:{args.puerto}"
    servidor = ThreadingHTTPServer((args.host, args.puerto), Manejador)

    catalogo = cargar_catalogo()
    print(f"Base de datos : {os.path.abspath(args.db)}")
    print(f"Catálogo      : {len(catalogo)} diputados"
          if catalogo else
          "Catálogo      : sin diputados.txt (opcional)")
    print(f"Interfaz      : {direccion}  (Ctrl+C para cerrar)")

    if args.host in ("127.0.0.1", "localhost"):
        # Solo tiene sentido abrir un navegador local si el servidor
        # también escucha en local (no dentro de un contenedor Docker).
        def _abrir():
            try:
                webbrowser.open(direccion)
            except Exception:
                pass
        threading.Timer(1.0, _abrir).start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nCerrado. Tus cambios ya quedaron guardados en la "
              "base de datos.")


if __name__ == "__main__":
    main()
