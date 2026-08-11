#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
revisar.py
==========
Interfaz web LOCAL para revisar y corregir los oradores de las sesiones
guardadas en la base de datos (sesiones.db).

Uso:
    python revisar.py                  (usa sesiones.db en la carpeta actual)
    python revisar.py --db otra.db     (otra base de datos)
"""

import argparse
import json
import os
import sqlite3
import sys
import threading
import webbrowser
import time
import urllib.request
import urllib.error
import logging
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


# ---------------------------------------------------------------------------
# Background Worker de Validación Semántica (MEMO) - Forzado Universal
# ---------------------------------------------------------------------------

class ValidadorSemantico(threading.Thread):
    def __init__(self, db_path="sesiones.db", intervalo=10):
        super().__init__()
        self.db_path = db_path
        self.intervalo = intervalo
        self.daemon = True
        self.detener_evento = threading.Event()
        self.ollama_url = "http://127.0.0.1:11434/api/generate"
        self.modelo_llm = "llama3.2:latest"

    def _conectar_db(self):
        con = sqlite3.connect(self.db_path, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA busy_timeout=30000")
        except sqlite3.OperationalError:
            pass
        return con

    def _llamar_ollama(self, prompt):
        payload = {
            "model": self.modelo_llm,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.0,
                "num_predict": 5
            }
        }
        req = urllib.request.Request(
            self.ollama_url, 
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode('utf-8'))
                return result.get("response", "").strip().upper()
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8')
            logging.error(f"Ollama devolvió error HTTP. Detalles: {error_body}")
            return "ERROR_HTTP"
        except Exception as e:
            logging.error(f"Error en la conexión a Ollama: {e}")
            return "ERROR"

    def procesar_pendientes(self):
        con = self._conectar_db()
        try:
            # Forzamos la revisión de cualquier registro Desconocido que no tenga las marcas de IA finales
            cursor = con.execute("""
                SELECT id, sesion_id, texto, voz_orador, voz_similitud, inicio_seg, fin_seg 
                FROM participaciones 
                WHERE orador = 'Desconocido' 
                  AND (voz_orador IS NULL OR (voz_orador NOT LIKE '%validado por IA%' AND voz_orador NOT LIKE '%descartado por IA%'))
                ORDER BY inicio_seg ASC
            """)
            
            candidatos = cursor.fetchall()
            
            for registro in candidatos:
                h_id, sesion_id, txt_huerfano, voz_cand, voz_sim, inicio, fin = registro
                
                if not txt_huerfano or len(txt_huerfano.strip()) < 10:
                    continue

                txt_lower = txt_huerfano.lower()

                frases_mesa = [
                    "pregunto a las", "tiene el uso de la palabra", "se abre", "someto a votación", 
                    "informo que", "damos cuenta", "damos la bienvenida", "presencia de la diputada", 
                    "presencia del diputado", "asistencia de", "registrarse", "se instruye"
                ]

                if any(f in txt_lower for f in frases_mesa):
                    con.execute("""
                        UPDATE participaciones 
                        SET orador = 'Presidencia / Mesa Directiva', voz_orador = '✓ validado por IA' 
                        WHERE id = ?
                    """, (h_id,))
                    con.commit()
                    print(f"\n[Filtro Institucional] ID {h_id} reasignado a 'Presidencia / Mesa Directiva'.")
                    continue

                cur_prev = con.execute("""
                    SELECT orador, texto FROM participaciones 
                    WHERE sesion_id = ? AND fin_seg <= ? AND id != ?
                    ORDER BY fin_seg DESC LIMIT 1
                """, (sesion_id, inicio, h_id))
                row_prev = cur_prev.fetchone()
                orador_prev = row_prev["orador"] if row_prev else "Ninguno"
                texto_prev = row_prev["texto"] if row_prev else "Inicio de la sesión"

                candidato_efectivo = voz_cand if voz_cand else orador_prev
                if candidato_efectivo == 'Desconocido' or not candidato_efectivo:
                    continue

                cur_next = con.execute("""
                    SELECT orador, texto FROM participaciones 
                    WHERE sesion_id = ? AND inicio_seg >= ? AND id != ?
                    ORDER BY inicio_seg ASC LIMIT 1
                """, (sesion_id, fin, h_id))
                row_next = cur_next.fetchone()
                orador_next = row_next["orador"] if row_next else "Ninguno"
                texto_next = row_next["texto"] if row_next else "Fin de la transcripción"

                print(f"\n[Doble Validación - LLM] Analizando ID {h_id}...")
                print(f"   ├─ Referencia Previa  ({orador_prev}): \"{texto_prev[:50]}...\"")
                print(f"   ├─ Fragmento Huérfano ({candidato_efectivo}): \"{txt_huerfano[:50]}...\"")
                print(f"   └─ Referencia Siguiente ({orador_next}): \"{texto_next[:50]}...\"")

                prompt = f"""Sistema: Eres un motor de validación semántica para el Módulo Electrónico de Monitoreo Optimizado (MEMO). Analizas la continuidad discursiva en debates legislativos.
Regla estricta: Responde ÚNICAMENTE con "SI" o "NO".

[Intervención Previa] {orador_prev}: "{texto_prev}"
[Fragmento a evaluar] {candidato_efectivo}: "{txt_huerfano}"
[Intervención Siguiente] {orador_next}: "{texto_next}"

Pregunta: ¿Es lógico y coherente en la conversación que el [Fragmento a evaluar] pertenezca a {candidato_efectivo}?
Respuesta (SI/NO):"""

                respuesta = self._llamar_ollama(prompt)
                if respuesta == "ERROR_HTTP":
                    logging.warning("Pausando el Worker 30 segundos por error en Ollama.")
                    time.sleep(30)
                    break 
                
                print(f"   -> Veredicto LLM para ID {h_id}: [{respuesta.strip()}]")

                respuesta_limpia = respuesta.replace('Í', 'I')
                if "SI" in respuesta_limpia or "YES" in respuesta_limpia:
                    con.execute("""
                        UPDATE participaciones 
                        SET orador = ?, voz_orador = '✓ validado por IA' 
                        WHERE id = ?
                    """, (candidato_efectivo, h_id))
                    con.commit()
                    logging.info(f"[Worker] ID {h_id} validado y asignado a {candidato_efectivo}.")
                else:
                    con.execute("""
                        UPDATE participaciones 
                        SET voz_orador = '✗ descartado por IA' 
                        WHERE id = ?
                    """, (h_id,))
                    con.commit()
                    print(f"   -> ID {h_id} DESCARTADO por el motor y marcado visualmente.")
                
        except sqlite3.OperationalError as e:
            logging.warning(f"[Worker] Bloqueo temporal en DB, reintentando: {e}")
        finally:
            con.close()

    def run(self):
        logging.info("Iniciando Background Worker de Validación Semántica (Forzado Universal)...")
        while not self.detener_evento.is_set():
            self.procesar_pendientes()
            self.detener_evento.wait(self.intervalo)

    def detener(self):
        self.detener_evento.set()


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
  --papel:#FBFAF6; --tinta:#23241F; --tenue:#6E6C63;
  --verde:#1E5A38; --verde-suave:#E9F0EA;
  --linea:#E3E1D7; --ambar:#A9691F; --blanco:#FFFFFF;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--papel);color:var(--tinta);
  font:15px/1.5 -apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
:focus-visible{outline:2px solid var(--ambar);outline-offset:2px}
@media (prefers-reduced-motion: reduce){*{transition:none!important}}

header{border-bottom:3px double var(--verde);background:var(--blanco);
  padding:14px 22px;display:flex;flex-wrap:wrap;gap:12px;align-items:baseline}
header h1{font:700 21px/1 Georgia,"Times New Roman",serif;margin:0;
  color:var(--verde);letter-spacing:.02em}
header .sub{color:var(--tenue);font-size:13px;text-transform:uppercase;
  letter-spacing:.14em}
header .controles{margin-left:auto;display:flex;gap:10px;flex-wrap:wrap}
select,input[type=search]{font:inherit;padding:7px 10px;border:1px solid var(--linea);
  border-radius:6px;background:var(--blanco);color:var(--tinta);max-width:340px}

#meta{padding:10px 22px;color:var(--tenue);font-size:13px;border-bottom:1px solid var(--linea)}
#meta a{color:var(--verde)}

.cuerpo{display:grid;grid-template-columns:250px minmax(0,1fr);gap:0;
  max-width:1120px;margin:0 auto}
aside{padding:18px 16px;border-right:1px solid var(--linea)}
aside h2{font-size:12px;text-transform:uppercase;letter-spacing:.14em;
  color:var(--tenue);margin:0 0 10px}
#listaOradores{list-style:none;margin:0;padding:0}
#listaOradores li{margin:2px 0}
#listaOradores button{width:100%;text-align:left;font:inherit;font-size:13.5px;
  border:0;background:none;padding:6px 8px;border-radius:6px;cursor:pointer;
  color:var(--tinta);display:flex;justify-content:space-between;gap:8px}
#listaOradores button:hover{background:var(--verde-suave)}
#listaOradores button.activo{background:var(--verde);color:#fff}
#listaOradores .min{color:var(--tenue);font-variant-numeric:tabular-nums}
#listaOradores button.activo .min{color:#D7E4DA}
aside .nota{font-size:12.5px;color:var(--tenue);margin-top:16px}

main{padding:22px 26px 80px;max-width:780px}
.turno{display:grid;grid-template-columns:86px minmax(0,1fr);gap:14px;
  margin:0 0 20px}
.cuerpo-turno{min-width:0}
.cabecera-turno{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;
  margin-bottom:5px}
.tiempo{font-size:12px;color:var(--tenue);text-decoration:none;
  font-variant-numeric:tabular-nums;padding-top:4px;white-space:nowrap}
.tiempo:hover{color:var(--ambar)}
.orador{font:700 13.5px/1.3 -apple-system,"Segoe UI",Roboto,sans-serif;
  text-transform:uppercase;letter-spacing:.06em;color:var(--verde);
  background:none;border:0;border-bottom:1px dashed var(--verde);
  padding:0 0 1px;cursor:pointer;text-align:left}
.orador:hover{color:var(--ambar);border-color:var(--ambar)}
.voz-pista{font:600 12px/1.4 -apple-system,"Segoe UI",Roboto,sans-serif;
  padding:2px 9px;border-radius:999px;cursor:pointer;white-space:nowrap;
  border:1px solid var(--ambar);background:#FBF3E4;color:var(--ambar)}
.voz-pista:hover{background:var(--ambar);color:#fff}
.btn-presidencia-rapido{font:600 12px/1.4 -apple-system,"Segoe UI",Roboto,sans-serif;
  padding:2px 9px;border-radius:999px;cursor:pointer;white-space:nowrap;
  border:1px solid var(--verde);background:var(--verde-suave);color:var(--verde)}
.btn-presidencia-rapido:hover{background:var(--verde);color:#fff}
.badge-ia{font:600 11px/1.4 -apple-system,"Segoe UI",Roboto,sans-serif;
  padding:2px 8px;border-radius:999px;background:#E2F0D9;color:#385723;border:1px solid #C5E0B4;margin-left:4px}
.badge-descartado{font:600 11px/1.4 -apple-system,"Segoe UI",Roboto,sans-serif;
  padding:2px 8px;border-radius:999px;background:#FCE4D6;color:#C65911;border:1px solid #F8CBAD;margin-left:4px}
.accion{font:inherit;padding:7px 12px;border-radius:6px;cursor:pointer;
  border:1px solid var(--verde);background:var(--verde-suave);
  color:var(--verde)}
.accion:hover{background:var(--verde);color:#fff}
#listaOradores button.pendiente{color:var(--ambar);font-weight:700}
#listaOradores button.pendiente.activo{background:var(--ambar);color:#fff}
.mini{font:600 12px/1.4 -apple-system,"Segoe UI",Roboto,sans-serif;
  padding:2px 9px;border-radius:999px;cursor:pointer;white-space:nowrap;
  border:1px solid var(--linea);background:#fff;color:var(--tinta-suave)}
.mini:hover{border-color:var(--verde);color:var(--verde)}
.mini.guardado{border-color:var(--verde);background:var(--verde-suave);
  color:var(--verde);font-weight:700}
.vivo{display:flex;align-items:center;gap:6px;font-size:14px;
  color:var(--tinta-suave);cursor:pointer;white-space:nowrap}
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
.texto{font:16px/1.65 Georgia,"Times New Roman",serif;margin:6px 0 0;
  text-align:justify;hyphens:auto}
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

@media (max-width:860px){
  .cuerpo{grid-template-columns:1fr}
  aside{border-right:0;border-bottom:1px solid var(--linea)}
  #listaOradores{display:flex;flex-wrap:wrap;gap:4px}
  #listaOradores li{flex:0 0 auto}
  #listaOradores button{border:1px solid var(--linea);border-radius:999px;
    padding:4px 12px}
  .turno{grid-template-columns:1fr}
  .editor{grid-column:1}
  main{padding:18px 16px 80px}
}
</style>
</head>
<body>
<header>
  <h1>Versión estenográfica</h1>
  <span class="sub">Revisión de oradores</span>
  <div class="controles">
    <select id="selSesion" aria-label="Sesión"></select>
    <input id="buscar" type="search" placeholder="Buscar en el texto…"
           aria-label="Buscar en el texto">
    <button id="btnCompactar" class="accion"
            title="Une en la base de datos los registros consecutivos del mismo orador">Unir iguales</button>
    <label class="vivo" title="Recargar automáticamente cada 4 segundos para corregir mientras se transcribe">
      <input type="checkbox" id="chkVivo" checked> En vivo</label>
  </div>
</header>
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
const $ = s => document.querySelector(s);
const estado = {sesiones:[], sesion:null, filas:[], turnos:[],
                catalogo:[], filtroOrador:null, q:''};

const esc = s => s.replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

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
           voz:null, vozSim:0, validadoIa:false, descartadoIa:false};
      turnos.push(t);
    }
    t.ids.push(f.id); t.textos.push(f.texto); t.fin = f.fin_seg;
    if(f.voz_orador === '✓ validado por IA'){
      t.validadoIa = true;
    }
    if(f.voz_orador === '✗ descartado por IA'){
      t.descartadoIa = true;
    }
    if(f.voz_orador && f.voz_orador !== '✓ validado por IA' && f.voz_orador !== '✗ descartado por IA' && (f.voz_similitud||0) > t.vozSim){
      t.voz = f.voz_orador; t.vozSim = f.voz_similitud||0;
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
    if(dx !== dy) return dy - dx;
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
  $('#transcripcion').innerHTML = visibles.map((t,i) => {
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
    return '<article class="turno" data-i="'+estado.turnos.indexOf(t)+'">'
      + tiempo
      + '<div class="cuerpo-turno"><div class="cabecera-turno">'
      + '<button class="orador" title="Corregir orador">'
      + esc(t.orador)+':</button>'
      + (t.validadoIa ? '<span class="badge-ia">✓ validado por IA</span>' : '')
      + (t.descartadoIa ? '<span class="badge-descartado">✗ descartado por IA</span>' : '')
      + ((t.voz && t.voz !== t.orador)
         ? '<button class="voz-pista" title="Aplicar esta sugerencia de voz">'
           + '🎙 voz: '+esc(t.voz)+' ('+t.vozSim.toFixed(2)+') · aplicar</button>'
         : '')
      + '<button class="btn-presidencia-rapido" title="Asignar directamente a Presidencia / Mesa Directiva">🏛 Presidencia</button>'
      + '<button class="mini b-editar" title="Corregir el texto de este bloque">✏ texto</button>'
      + '<button class="mini b-resumen'
        + ((estado.resumenes && estado.resumenes[t.ids[0]]) ? ' guardado' : '')
        + '" title="Resumen ejecutivo de esta intervención">📋 resumen'
        + ((estado.resumenes && estado.resumenes[t.ids[0]]) ? ' ✓' : '')
        + '</button>'
      + '</div>'
      + '<p class="texto">'+texto+'</p></div></article>';
  }).join('');
  document.querySelectorAll('.orador').forEach(b =>
    b.onclick = e => abrirEditor(e.target.closest('.turno')));
  document.querySelectorAll('.voz-pista').forEach(b =>
    b.onclick = async e => {
      const t = estado.turnos[+e.target.closest('.turno').dataset.i];
      await asignar(t, t.voz, 'Sugerencia de voz aplicada');
    });
  document.querySelectorAll('.btn-presidencia-rapido').forEach(b =>
    b.onclick = async e => {
      const t = estado.turnos[+e.target.closest('.turno').dataset.i];
      await asignar(t, 'Presidencia / Mesa Directiva', 'Asignado a Presidencia');
    });
  document.querySelectorAll('.b-editar').forEach(b =>
    b.onclick = e => editarTexto(e.target.closest('.turno')));
  document.querySelectorAll('.b-resumen').forEach(b =>
    b.onclick = e => pedirResumen(e.target.closest('.turno'), e.target));
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
      estado.resumenes[t.ids[0]] = r.resumen;
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
  ed.querySelector('.b-cancelar').onclick = () => cont.remove();
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

async function cargarSesion(id){
  const d = await api('/api/participaciones?sesion='+id);
  estado.sesion = d.sesion; estado.filas = d.filas;
  estado.resumenes = d.resumenes || {};
  estado.porId = {};
  d.filas.forEach(f => estado.porId[f.id] = f);
  estado.turnos = agrupar(d.filas);
  const s = d.sesion || {};
  $('#meta').innerHTML = s.id
    ? '<strong>'+esc(s.titulo||'Sesión '+s.id)+'</strong> · inicio '
      + esc(s.inicio||'?')
      + (s.url && s.url.startsWith('http')
         ? ' · <a href="'+esc(s.url)+'" target="_blank" rel="noopener">ver video</a>'
         : '')
    : '';
  opcionesOradores(); pintarLateral(); pintarTranscripcion();
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
  $('#selSesion').innerHTML = estado.sesiones.map(s =>
    '<option value="'+s.id+'">#'+s.id+' — '
    +esc((s.titulo||'').slice(0,60))+' ('+s.segmentos+' seg.)</option>').join('');
  $('#selSesion').onchange = e => cargarSesion(+e.target.value);
  $('#buscar').oninput = e => {estado.q = e.target.value; pintarTranscripcion();};
  $('#btnCompactar').onclick = async () => {
    if(!estado.sesion) return;
    const r = await api('/api/compactar', {sesion_id:estado.sesion.id});
    avisar(r.unidos ? r.unidos+' registro(s) unidos en la base.'
                    : 'No había registros consecutivos por unir.');
    cargarSesion(estado.sesion.id);
  };
  $('#chkVivo').onchange = e => {
    clearInterval(estado.timerVivo);
    if(e.target.checked){
      estado.timerVivo = setInterval(() => {
        if(document.querySelector('.editor, .editor-texto, .panel-resumen'))
          return;
        if(!estado.sesion) return;
        const y = window.scrollY;
        cargarSesion(estado.sesion.id).then(() => window.scrollTo(0, y));
      }, 4000);
      avisar('Actualización en vivo activada: la página se refresca cada 4 s.');
    } else {
      avisar('Actualización en vivo desactivada.');
    }
  };
  
  $('#chkVivo').checked = true;
  $('#chkVivo').dispatchEvent(new Event('change'));

  cargarSesion(estado.sesiones[0].id);
}
iniciar();
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


def _campo(fila, nombre, defecto=None):
    return fila[nombre] if nombre in fila.keys() else defecto


def compactar(con, sesion_id, solo_ids=None):
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
        mejor = max(g, key=lambda f: (_campo(f, "voz_similitud") or 0))
        voz_o = _campo(mejor, "voz_orador")
        voz_s = _campo(mejor, "voz_similitud")
        if "voz_orador" in base.keys():
            con.execute(
                "UPDATE participaciones SET texto=?, fin_seg=?, fin_hms=?, "
                "voz_orador=?, voz_similitud=? WHERE id=?",
                (texto, fin, hms(fin), voz_o, voz_s, base["id"]))
        else:
            con.execute(
                "UPDATE participaciones SET texto=?, fin_seg=?, fin_hms=? "
                "WHERE id=?",
                (texto, fin, hms(fin), base["id"]))
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
    prompt = PROMPT_RESUMEN.format(orador=orador or "No registrado",
                                   texto=texto)
    clave = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not clave:
        return {"modo": "copiar", "prompt": prompt}
    import urllib.request
    cuerpo = json.dumps({
        "model": os.environ.get("RESUMEN_MODELO", "claude-sonnet-4-6"),
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


# ---------------------------------------------------------------------------
# Servidor
# ---------------------------------------------------------------------------

class Manejador(BaseHTTPRequestHandler):
    ruta_db = "sesiones.db"

    def log_message(self, *args):
        pass

    def _db(self):
        con = sqlite3.connect(self.ruta_db, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA busy_timeout=30000")
            con.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.OperationalError:
            pass
        con.execute("""
            CREATE TABLE IF NOT EXISTS resumenes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                sesion_id  INTEGER,
                ancla_id   INTEGER UNIQUE,
                orador     TEXT,
                resumen    TEXT,
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
            self.end_headers()
            self.wfile.write(datos)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        p = urlparse(self.path)
        if p.path in ("/", "/index.html"):
            self._responder(PAGINA.encode("utf-8"),
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
            extra = (", voz_orador, voz_similitud"
                     if "voz_orador" in cols else "")
            filas = [dict(r) for r in con.execute(
                f"SELECT id, orador, inicio_seg, fin_seg, texto{extra} "
                "FROM participaciones WHERE sesion_id=? "
                "ORDER BY inicio_seg, id", (sid,))]
            resumenes = {r["ancla_id"]: r["resumen"] for r in con.execute(
                "SELECT ancla_id, resumen FROM resumenes WHERE sesion_id=?",
                (sid,))}
            con.close()
            self._responder({"sesion": dict(ses) if ses else None,
                             "filas": filas, "resumenes": resumenes})
        elif p.path == "/api/catalogo":
            self._responder(cargar_catalogo())
        else:
            self._responder({"error": "no encontrado"}, codigo=404)

    def do_POST(self):
        p = urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            datos = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._responder({"error": "JSON inválido"}, codigo=400)
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
                cur = con.execute(
                    f"UPDATE participaciones SET orador=? "
                    f"WHERE id IN ({marcas})", [orador] + ids)
                con.commit()
                unidos = compactar(con, sid, solo_ids=ids) if sid else 0
                self._responder({"ok": True, "cambios": cur.rowcount,
                                 "unidos": unidos})
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
            elif p.path == "/api/resumen":
                orador = (datos.get("orador") or "").strip()
                texto = (datos.get("texto") or "").strip()
                sid = int(datos.get("sesion_id", 0) or 0)
                ancla = int(datos.get("ancla_id", 0) or 0)
                if not texto:
                    return self._responder({"error": "sin texto"},
                                           codigo=400)
                res = generar_resumen(orador, texto)
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
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not os.path.isfile(args.db):
        print(f"No encuentro la base de datos: {os.path.abspath(args.db)}")
        print("Corre primero transcribir_en_vivo.py, o indica la ruta "
              "con --db.")
        sys.exit(1)

    Manejador.ruta_db = args.db
    direccion = f"http://127.0.0.1:{args.puerto}"
    servidor = ThreadingHTTPServer(("127.0.0.1", args.puerto), Manejador)
    
    worker_validacion = ValidadorSemantico(db_path=args.db, intervalo=10)
    worker_validacion.start()

    catalogo = cargar_catalogo()
    print(f"Base de datos : {os.path.abspath(args.db)}")
    print(f"Catálogo      : {len(catalogo)} diputados"
          if catalogo else
          "Catálogo      : sin diputados.txt (opcional)")
    print(f"Interfaz      : {direccion}  (Ctrl+C para cerrar)")

    threading.Timer(1.0, lambda: webbrowser.open(direccion)).start()
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nCerrado. Tus cambios ya quedaron guardados en la "
              "base de datos.")
        worker_validacion.detener()


if __name__ == "__main__":
    main()