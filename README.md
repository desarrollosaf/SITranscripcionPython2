# Transcriptor en vivo de sesiones legislativas

Transcribe casi en tiempo real las sesiones del Congreso transmitidas por YouTube, identifica a los oradores y guarda todo en una base de datos SQLite.

Mientras la sesión transcurre, verás el texto aparecer en pantalla con un retraso de 1 a 2 minutos, con este formato:

```
[00:14:03] >>> Presidencia / Mesa Directiva
   Tiene el uso de la palabra la diputada María Pérez, hasta por cinco minutos.

[00:14:21] >>> Dip. María Pérez
   Con su permiso, presidenta. Compañeras y compañeros diputados...
```

Al mismo tiempo, cada intervención se va guardando en la base de datos `sesiones.db` y en un archivo de texto de respaldo.

## Instalación (una sola vez)

**1. Python 3.9 o superior.** Descárgalo de https://www.python.org/downloads/ — en Windows, al instalarlo marca la casilla **"Add Python to PATH"**.

**2. ffmpeg** (procesa el audio):

- **Windows:** abre PowerShell y ejecuta `winget install Gyan.FFmpeg`, luego cierra y vuelve a abrir la terminal.
- **macOS:** `brew install ffmpeg`
- **Linux (Ubuntu/Debian):** `sudo apt install ffmpeg`

**3. Las librerías de Python.** En la terminal, dentro de la carpeta de este proyecto:

```
pip install -r requirements.txt
```

La primera vez que corras el script se descargará automáticamente el modelo de Whisper (unos 500 MB para el modelo `small`). Solo pasa una vez.

## Uso

Copia la URL de la transmisión en vivo de YouTube y ejecuta:

```
python transcribir_en_vivo_c3.py "https://www.youtube.com/watch?v=XXXXXXX"
```

Eso es todo. Notas importantes:

- Si la transmisión está programada pero aún no empieza, el script espera y reintenta cada 30 segundos.
- Para **detener**, presiona `Ctrl+C`: el script deja de capturar, termina de transcribir lo que quedaba pendiente, guarda todo y te muestra un resumen por orador.
- El mismo comando funciona también con **videos ya terminados** (los transcribe conforme los descarga).

### Opciones

```
--modelo tiny|base|small|medium|large-v3   Calidad vs. velocidad (default: small)
--bloque 30                                 Segundos de audio por bloque
--db sesiones.db                            Archivo de base de datos
--dispositivo auto|cpu|cuda                 Usa "cuda" si tienes tarjeta NVIDIA
--conservar-audio                           No borrar los bloques de audio WAV
--voz                                       Identificar por voz los bloques sin orador anunciado
--perfiles voces_perfiles.json              Archivo de huellas (con --voz)
--umbral-voz 0.75                           Exigencia de la coincidencia por voz
--umbral-cambio-voz 0.50                    Sensibilidad al cambio de voz
--pausa-voz 2.0                             Pausa (s) que cierra un tramo de voz
```

Ejemplo con más calidad (requiere computadora potente o GPU):

```
python transcribir_en_vivo_c3.py "URL" --modelo medium --dispositivo cuda
```

Ejemplo con identificación de voz en vivo (requiere haber corrido antes
`voz.py` para tener `voces_perfiles.json`):

```
python transcribir_en_vivo_c3.py "URL" --voz
```

Con `--voz`, el sistema le saca huella de voz a **cada segmento** transcrito
y agrupa los que suenan a la misma persona en "tramos". Cuando la voz cambia
(aunque no haya habido anuncio formal) o cuando el protocolo anuncia a otro
orador, el tramo se cierra y se identifica contra tus huellas. Si hay
coincidencia fuerte, corrige la base de datos y avisa en pantalla:

```
[voz] 01:23:10–01:23:45: la voz corresponde a Dip. Ruth Salinas Reyes (similitud 0.81), no a "Presidencia / Mesa Directiva"; corregido en la base de datos.
```

Detalles del comportamiento:

- **Verificación inmediata:** en cuanto se detecta un cambio de voz, el
  segmento que lo delató pasa a "Desconocido" y el sistema intenta
  identificar la voz nueva desde el primer segmento disponible,
  reintentando con cada segmento siguiente hasta lograrlo — sin esperar a
  acumular audio y sin depender de lo que diga el texto.
- Corrige **hacia atrás** (los segmentos ya guardados de ese tramo) y
  **hacia adelante** (si el tramo sigue vivo, lo que se transcriba después
  ya sale a nombre del orador identificado).
- **Retomas de la Presidencia sin anuncio:** frases inequívocas de la Mesa
  al inicio de un segmento ("Muchas gracias, señor diputado", "Abro la
  discusión", "Consulto a las...", "Pido a la Secretaría...") devuelven la
  palabra a la Presidencia en ese mismo segmento, sin depender de la voz.
- **Pausas prolongadas** (más de 2 s, ajustable con `--pausa-voz`): cierran
  el tramo de voz en curso; lo que siga —aunque arranque con frases
  cortas— acumula su propio audio y se identifica por su cuenta.
- **Pase de lista y votación nominal:** cuando el sistema detecta que
  inicia un pase de lista o una votación nominal, entiende la estructura
  llamado→respuesta: el nombre que el secretario dice al final de un
  segmento ("...diputado Carlos Antonio Martínez Zurita") se le atribuye a
  la respuesta corta que sigue ("Presente" / "A favor"), y los llamados
  regresan automáticamente a quien pasa la lista. El modo se apaga solo al
  declararse el quórum o el resultado.
- **Protección contra cortes falsos:** los fragmentos muy cortos que
  Whisper genera a media frase ("de renovar.", "una labor.") dan huellas
  ruidosas; por eso un segmento de menos de 2 s no puede declarar un cambio
  de voz por sí solo, las huellas cortas pesan menos en el promedio del
  tramo, y si aun así queda un micro-"Desconocido" atrapado entre dos
  tramos del mismo orador, se reabsorbe automáticamente (verás el aviso
  "micro-tramo reabsorbido").
- **Etiqueta "Desconocido":** un tramo sin respaldo del protocolo que la
  voz no logra identificar con confianza queda marcado como "Desconocido"
  (con su mejor candidato y similitud guardados como pista en las columnas
  `voz_*`), para que lo reclasifiques en `revisar.py`. Los tramos
  respaldados por un anuncio formal conservan su nombre salvo que la voz
  los contradiga con evidencia fuerte.
- Puede corregir incluso tramos **mal atribuidos**: si un diputado empezó a
  hablar sin anuncio y el sistema seguía acreditando al orador anterior, el
  cambio de voz lo delata.

Perillas de ajuste:

- `--umbral-voz 0.75`: exigencia para nombrar. Súbelo si ves correcciones
  equivocadas; bájalo (p.ej. 0.70) si muchos tramos quedan "Desconocido"
  con similitudes de 0.70-0.74 hacia el candidato correcto.
- `--umbral-cambio-voz 0.50`: sensibilidad al cambio de voz. Súbelo si no
  detecta cambios evidentes; bájalo si corta de más.
- `--pausa-voz 2.0`: segundos de silencio que cierran un tramo.

**Límite honesto:** los votos de una sola palabra ("a favor", "en contra")
siguen siendo difíciles — medio segundo de audio no da para una huella
confiable. Esos casos se resuelven en `revisar.py`.

**Nota de rendimiento:** correr Whisper y el modelo de voz al mismo tiempo
usa más memoria y CPU (ahora se calcula una huella por segmento). En una
computadora modesta, si notas que el texto se atrasa mucho, usa un modelo
Whisper más chico (`--modelo base`) junto con `--voz`.

**¿El texto se atrasa mucho respecto a la sesión?** Tu computadora no alcanza a transcribir al ritmo del audio. Usa un modelo más chico (`--modelo base`) o, ideal, una GPU NVIDIA con `--dispositivo cuda`.

## Cómo identifica a los oradores

El script aprovecha el protocolo parlamentario: cuando detecta frases como *"tiene el uso de la palabra la diputada..."*, *"se le concede el uso de la voz al diputado..."*, extrae el nombre y etiqueta las siguientes intervenciones con ese diputado. Cuando alguien dice *"es cuanto"*, regresa la etiqueta a la Presidencia. Todo lo que no está atribuido a un diputado queda como "Presidencia / Mesa Directiva".

Es una heurística: funciona bien la mayor parte del tiempo, pero conviene revisar los nombres después (Whisper puede escribir mal un apellido, o alguien puede tomar la palabra sin anuncio formal). Por eso todo queda en la base de datos, donde es fácil corregir.

## La base de datos

Se crean dos tablas:

- **sesiones**: id, url, titulo, inicio, fin
- **participaciones**: id, sesion_id, orador, inicio_seg, fin_seg, inicio_hms, fin_hms, texto

Para explorarla visualmente instala **DB Browser for SQLite** (gratuito, https://sqlitebrowser.org/) y abre el archivo `sesiones.db`. Desde ahí también puedes exportar a CSV o Excel.

Consultas útiles (pestaña "Ejecutar SQL"):

```sql
-- Todas las participaciones de la sesión 1, en orden
SELECT inicio_hms, orador, texto FROM participaciones
WHERE sesion_id = 1 ORDER BY inicio_seg;

-- Corregir un nombre mal transcrito en toda la sesión
UPDATE participaciones SET orador = 'Dip. Juan Hernández García'
WHERE orador = 'Dip. Juan Hernández' AND sesion_id = 1;

-- Tiempo total de participación por diputado
SELECT orador, COUNT(*) AS intervenciones,
       ROUND(SUM(fin_seg - inicio_seg)/60, 1) AS minutos
FROM participaciones WHERE sesion_id = 1
GROUP BY orador ORDER BY minutos DESC;
```

## Correr con Docker

Alternativa a instalar Python/ffmpeg/librerías a mano: todo queda dentro de un
contenedor (incluye `--voz`, ya trae torch/speechbrain).

**1. Construir la imagen** (una sola vez; tarda unos minutos):

```
docker compose build
```

**2. Ejecutar una transcripción:**

```
docker compose run --rm transcriptor "https://www.youtube.com/watch?v=XXXX"
```

Cualquier opción del script se agrega al final, igual que en local:

```
docker compose run --rm transcriptor "URL" --voz --modelo base
```

Detener con `Ctrl+C` (igual que en local: termina de procesar y cierra bien).

**Persistencia:** `docker-compose.yml` monta la carpeta completa del proyecto
dentro del contenedor, así que `sesiones.db`, `sesiones_en_vivo/`,
`voces_perfiles.json`, `diputados.txt` y `muestras_voz/` se leen y escriben
directo en tu disco (nada se pierde al parar el contenedor). Los modelos de
Whisper y de voz se cachean en volúmenes de Docker para no re-descargarlos
cada vez.

**Nota:** la imagen se construye para CPU. Si tienes GPU NVIDIA y quieres usar
`--dispositivo cuda`, hay que ajustar la imagen base a una con CUDA y agregar
`runtime: nvidia` en `docker-compose.yml` (no incluido por defecto).

### Desplegar en tu servidor (clonar y listo)

```
git clone <este repo> && cd SITranscripcionPython2
cp .env.example .env        # y llena DB_HOST/DB_USER/DB_PASS/JWT_SECRET reales
docker compose build
docker compose up -d api revisor      # NO uses "docker compose up -d" a secas:
                                       # "transcriptor" es para correr trabajos
                                       # sueltos (docker compose run), no un
                                       # servicio permanente — up -d intentaría
                                       # arrancarlo también y fallaría sin URL.
docker compose exec api python crear_admin.py --email tu@correo.com
```

`api` y `revisor` traen `restart: unless-stopped`: si el servidor se reinicia,
vuelven solos. Persistencia y modelos: ver arriba (`sesiones.db`,
`voces_perfiles.json`, cachés de Whisper/voz), todo vive en la carpeta del
proyecto o en volúmenes de Docker — nada se pierde entre despliegues.

### Puertos: qué sale y qué abrir en el firewall

| Puerto | Servicio | Protocolo | ¿Necesitas abrirlo? |
|---|---|---|---|
| `8000` (`API_PORT`) | `api` — login, crear trabajos, la app del operador de audio | TCP | Sí, si algo fuera del servidor le habla (el `.exe` del operador, tu otro sistema). Solo interno si todo corre en la misma red. |
| `8756` (`REVISOR_PORT`) | `revisor` — la interfaz web de revisión | TCP | Sí, para quien vaya a entrar desde su navegador. |
| `9000`-`9009` (`SRT_PUERTO_BASE`-`SRT_PUERTO_FIN`) | `api` — audio SRT entrante (consola/Dante) | **UDP** | Sí, si vas a usar la captura por SRT — el `.exe` de la consola de audio le habla directo a estos puertos desde otra máquina/red. |
| `3306` (`DB_PORT`) | Tu MySQL (fuera de este compose) | TCP | Solo si tu MySQL vive en **otro** servidor; si está en la misma máquina (`host.docker.internal`), no hace falta exponerlo a internet. |

Todos los puertos son configurables por variable de entorno en tu `.env` (ver
`.env.example`) — cámbialos ahí si ya usas esos puertos para otra cosa en el
servidor, no en `docker-compose.yml`.

## API para integrarlo con otro sistema (FastAPI + login + MySQL)

Si otro proyecto tuyo necesita disparar una transcripción mandando la URL de
YouTube y la lista de integrantes de un evento (para que la identificación
por voz busque solo entre esos, no entre los 75 perfiles del catálogo
completo), hay una API HTTP para eso en `api/`.

**Cómo reduce el error:** `voces_perfiles.json` es un diccionario
`{nombre: huella}`. Al crear un trabajo, la API filtra ese diccionario a
solo los nombres que mandaste (match sin distinguir mayúsculas/acentos) y
lanza `transcribir_en_vivo_c3.py --voz --perfiles <ese subconjunto>` — la
comparación de voz ocurre solo contra esos N perfiles, no contra el
catálogo completo.

**Login y usuarios:** viven en tu MySQL (el que ya tienes corriendo en
Docker) — email + contraseña, JWT para las peticiones, con rol admin para
crear más usuarios. Las transcripciones (texto, oradores, tiempos) se
siguen guardando en `sesiones.db` (SQLite), igual que en el uso manual.

### Levantarla

1. Copia `.env.example` a `.env` y llena `DB_HOST`/`DB_PORT`/`DB_NAME`/
   `DB_USER`/`DB_PASS` con los datos de tu MySQL, y un `JWT_SECRET` largo y
   aleatorio. En tu máquina normalmente `DB_HOST=host.docker.internal`
   apunta solo con eso al MySQL que ya corre en Docker; en el servidor
   cambias únicamente ese valor por el host real de tu MySQL ahí (así no
   dependes de que los contenedores de otro proyecto compartan red ni
   nombre).
2. `docker compose up -d api`
3. Crea el primer usuario administrador:
   ```
   docker compose exec api python crear_admin.py --email tu@correo.com
   ```

La API queda en `http://localhost:8000` (Swagger interactivo en
`http://localhost:8000/docs`).

### Endpoints principales

```
POST /auth/login              form-urlencoded username/password -> {access_token}
GET  /auth/me                 usuario autenticado actual

POST /usuarios                (solo admin) crea usuarios nuevos
GET  /usuarios                (solo admin) lista usuarios

GET  /participantes           nombres disponibles en voces_perfiles.json

POST /transcripciones         {url, participantes: [...], modelo?}
                               -> crea el trabajo y lo lanza en segundo plano
GET  /transcripciones         lista tus trabajos (admin ve todos)
GET  /transcripciones/{id}    estado: ejecutando | finalizado | error | detenido
POST /transcripciones/{id}/detener   equivalente a Ctrl+C, cierra bien la sesión
GET  /transcripciones/{id}/participaciones   texto por orador, una vez que hay sesion_id
```

Todos los endpoints (salvo `/auth/login`) requieren
`Authorization: Bearer <token>`. El otro sistema hace polling a
`GET /transcripciones/{id}` para saber cuándo terminó.

Ejemplo de creación de trabajo:

```json
POST /transcripciones
{
  "url": "https://www.youtube.com/watch?v=XXXX",
  "participantes": ["Juan Hernández García", "María Pérez López"]
}
```

Si algún nombre no tiene huella en `voces_perfiles.json`, la respuesta lo
indica en `participantes_no_encontrados` mientras sigue con los que sí
existen (si ninguno existe, devuelve error 400 antes de lanzar nada).

**Nota:** el estado de los trabajos vive en memoria del proceso de la API
además de en MySQL — corre la API con un solo worker/réplica (como está
configurado por default) para que el seguimiento de cada proceso funcione.

### Fuente de audio por SRT (consola de audio / Dante, sin YouTube)

Cuando el audio no viene de un link de YouTube sino de una consola de
audio (interfaz USB o Dante Virtual Soundcard en otra máquina), el
trabajo se crea con `fuente: "srt"` en vez de una URL:

```json
POST /transcripciones
{
  "fuente": "srt",
  "url": "Comisión de Desarrollo Social",
  "participantes": ["Juan Hernández García", "María Pérez López"]
}
```

La API asigna un puerto libre del rango `SRT_PUERTO_BASE`–`SRT_PUERTO_FIN`
(ver `.env.example`) y una contraseña, y deja `transcribir_en_vivo_c3.py`
escuchando ese puerto por SRT en vez de bajar de YouTube — todo lo demás
(Whisper, `--voz`, la base de datos) funciona exactamente igual.

`GET /transcripciones/esperando-audio` lista **todos** los trabajos SRT
activos (de cualquier usuario) con su puerto y contraseña — es lo que
consulta la app de escritorio del operador para saber a qué comisión
conectarse. Abre el rango `SRT_PUERTO_BASE`-`SRT_PUERTO_FIN` (UDP) en el
firewall/security group de tu servidor en la nube, además del puerto de
la API.

### App de escritorio para el operador de audio (`agente_captura/`)

Como quien opera la consola no necesariamente sabe programar, hay una app
de Windows (`agente_captura/app.py`, un solo `.exe` con todo incluido)
que hace el lado de la consola: inicia sesión con su usuario (el mismo de
la API), ve la lista de comisiones esperando audio, elige el dispositivo
detectado (interfaz USB o cualquier canal de Dante Virtual Soundcard —
soporta varios a la vez si hay varias comisiones simultáneas) y presiona
Iniciar/Detener por cada una.

**Generar el `.exe`:** cada vez que se sube algo en `agente_captura/`, el
workflow `.github/workflows/build-agente-captura.yml` lo compila solo en
un runner de Windows (empacando ffmpeg incluido) y lo deja como artifact
descargable en la pestaña "Actions" del repo en GitHub — no hace falta
una PC con Windows para generarlo, solo subir el código.

## Corregir oradores con la interfaz web (revisar.py)

Después de una sesión (o incluso mientras corre), abre la interfaz de revisión:

```
python revisar.py
```

Se abre sola en tu navegador (todo es local, no sube nada a internet).

**Con Docker:** en un contenedor no hay navegador que abrir solo, así que se
usa un servicio aparte que expone el puerto:

```
docker compose up -d revisor
```

Y abres tú mismo `http://localhost:8756` (o `http://IP_DE_TU_SERVIDOR:8756`
si corre en un servidor remoto). El puerto se puede cambiar con
`REVISOR_PORT` en tu `.env`. Si quieres los resúmenes automáticos con IA,
define `ANTHROPIC_API_KEY` también en ese `.env`. Usa la misma
`sesiones.db` que el servicio `transcriptor`/`api`, así que ves ahí
cualquier sesión que hayas transcrito con cualquiera de los dos.

**Con login:** el servicio `revisor` de Docker arranca con
`--requiere-login`, así que antes de ver nada pide iniciar sesión —
`http://localhost:8756` redirige solo a `/login`. Usa el **mismo usuario**
que ya tengas en MySQL (los que creas con `crear_admin.py` o
`POST /usuarios` desde la API, ver sección anterior): mismo email/contraseña
sirve para la API y para esta interfaz. Sin `.env`/MySQL configurado, el
login simplemente rechaza todo (no hay forma de entrar sin usuario válido).
Si corres `revisar.py` en tu máquina de siempre (sin Docker, sin
`--requiere-login`), sigue funcionando exactamente igual que antes, sin
pedir login ni depender de MySQL.

Ahí puedes:

- **Leer la sesión** como versión estenográfica, agrupada por orador.
- **Corregir un nombre:** haz clic en el nombre del orador dentro del texto, escribe o elige el correcto, y decide si cambias *solo ese bloque* o *todas las apariciones en la sesión*. Se guarda directo en la base de datos.
- **Corregir el texto:** el botón "✏ texto" de cada bloque abre el texto en
  cajas editables — una por segmento, con su marca de tiempo — para arreglar
  lo que Whisper oyó mal ("Berte" → "Verte"). "Guardar texto" escribe directo
  a la base de datos.
- **Modo "En vivo":** activa la casilla del encabezado y el portal se
  recarga solo cada 30 segundos. Así puedes ir corrigiendo nombres y textos
  **mientras la sesión todavía se está transcribiendo** (la recarga se pausa
  sola si tienes algo abierto a medio editar). El transcriptor y el portal
  comparten la base de datos sin estorbarse: si uno está escribiendo, el
  otro espera su turno automáticamente (hasta 30 s) en vez de fallar.
  Consejo: durante una sesión en vivo usa las correcciones libremente, pero
  deja el botón "Unir iguales" para cuando la transcripción haya terminado.
- **Resumen ejecutivo (📋):** cada bloque tiene un botón "📋 resumen" que
  analiza la intervención con formato de analista parlamentario (tema,
  resumen ≤500 palabras, puntos clave, propuesta y posicionamiento).
  Funciona en dos modos:
  - **Automático:** si configuraste tu clave API de Anthropic, el resumen
    se genera y aparece ahí mismo, con botón para copiarlo. **Se guarda solo
    en la base de datos**, pegado a esa intervención: al recargar la vista
    (incluso en modo "En vivo") reaparece sin volver a generarse — así no
    se pierde ni gastas API de más. El botón "📋 resumen" se marca con ✓ y
    en verde cuando el bloque ya tiene resumen guardado; para rehacerlo,
    ábrelo y usa "Borrar", luego genéralo de nuevo.
  - **Copiar el prompt (sin clave):** el botón copia al portapapeles el
    prompt completo con la intervención incluida; pégalo en Claude
    (claude.ai) y obtén el resumen. Gratis, sin configurar nada.

  Para activar el modo automático (una sola vez): consigue una clave en
  https://console.anthropic.com (el uso tiene un costo pequeño por resumen),
  y en Windows abre el Símbolo del sistema y ejecuta:

  ```
  setx ANTHROPIC_API_KEY "sk-ant-TU-CLAVE-AQUI"
  ```

  Cierra esa ventana, abre una nueva y vuelve a arrancar `revisar.py`. Si
  algún día la conexión falla, el botón degrada solo al modo de copiar.
- **Unir con un clic:** en el editor de cada bloque hay botones "⬆ Unir con el anterior" y "⬇ Unir con el siguiente" — le asignan el orador del bloque vecino sin escribir nada. Ideal para los "Desconocido" atrapados entre bloques.
- **Pista de voz:** cuando la voz sugirió un candidato distinto al orador actual, el bloque muestra una insignia 🎙 con el nombre y la similitud; un clic la aplica.
- **Unión física en la base:** al corregir un orador, los registros que quedan consecutivos con el mismo nombre se **fusionan en un solo registro** (texto concatenado, tiempos extendidos). El botón "Unir iguales" del encabezado hace esa fusión para toda la sesión de una vez.
- **Pendientes a la vista:** "Desconocido" aparece fijado al inicio de la barra lateral con ⚠; un clic lo filtra para revisarlos en orden.
- **Verificar quién habla:** cada marca de tiempo abre el video de YouTube en ese segundo exacto.
- **Filtrar** por orador (barra lateral) o buscar cualquier palabra en el texto.

Consejo: como la fusión física es permanente, haz de vez en cuando una copia de respaldo de `sesiones.db` (copiar y pegar el archivo basta).

Para cerrarla, `Ctrl+C` en la terminal; los cambios ya quedaron guardados.

## Catálogo de diputados (diputados.txt)

Crea un archivo llamado `diputados.txt` en la misma carpeta que los scripts, con **un nombre completo por línea** (las líneas que empiecen con `#` se ignoran):

```
# Diputados de la LXII Legislatura
María del Carmen Pérez López
Juan Hernández García
José Luis de la Rosa Ibarra
```

Con este archivo presente:

- `transcribir_en_vivo_c3.py` corrige automáticamente los nombres que detecta: si Whisper escuchó "María Pérez" o "Juan Hernandez", los ajusta al nombre oficial del catálogo.
- `revisar.py` te sugiere estos nombres al corregir manualmente.

## Problemas comunes

- **yt-dlp da error al conectar:** YouTube cambia seguido; actualiza con `pip install -U yt-dlp`.
- **"ffmpeg no se reconoce":** no está en el PATH; reinstala según la sección de instalación y abre una terminal nueva.
- **Acentos se ven mal en la consola de Windows:** usa Windows Terminal en lugar del CMD clásico.
- **"database is locked":** dos programas tocaron la base a la vez (el transcriptor y el portal). Las versiones actuales ya lo resuelven: comparten el archivo esperando su turno. Si lo ves con versiones viejas, actualiza `revisar.py` y `transcribir_en_vivo_c3.py`. Nota: aparecen dos archivos auxiliares junto a `sesiones.db` (`sesiones.db-wal` y `sesiones.db-shm`) — son normales; no los borres mientras algún programa esté abierto.

## Reconocimiento por huella de voz (voz.py)

Complementa la detección por anuncios: identifica las intervenciones que el
protocolo NO anunció ("para hechos" desde la curul, interrupciones), que
quedan etiquetadas como "Presidencia / Mesa Directiva".

La clave: **no necesitas grabar muestras de voz de nadie**. Tus sesiones ya
transcritas y corregidas en `revisar.py` son las muestras de las que el
sistema aprende.

Instalación (una vez, aparte de la base):

```
pip install -r requirements-voz.txt
```

**Paso 1 — Aprender voces** de una sesión ya transcrita y REVISADA (mientras
mejor corregidos estén los oradores, mejores huellas):

```
python voz.py perfiles --sesion 1
```

Re-descarga el audio del video, corta los segmentos de cada diputado según
tu base de datos y guarda su huella en `voces_perfiles.json`. Repítelo con
2 o 3 sesiones: cada una enriquece los perfiles y suma diputados nuevos.

**Paso 2 — Identificar** en una sesión nueva:

```
python voz.py identificar --sesion 5
```

Analiza los bloques de "Presidencia / Mesa Directiva" y reporta a quién se
parece cada voz, con su similitud (0 a 1). Los marcados con `***` son
coincidencias fuertes. Opciones:

```
--umbral 0.75    exigencia de la coincidencia fuerte (súbelo si hay errores)
--todos          auditar TODOS los bloques, incluso los ya identificados
--aplicar        guardar la evidencia en columnas voz_orador/voz_similitud
--reemplazar     además sustituir el orador en coincidencias fuertes
```

**También puedes darle muestras de voz directamente** (sin esperar a tener
sesiones revisadas), en tres formas:

```
# a) Un archivo que ya tengas (entrevista, video de redes del diputado):
python voz.py muestra --nombre "Zaira Cedillo Silva" --audio entrevista.mp3

# b) Un rango de un video de YouTube donde hable solo:
python voz.py muestra --nombre "Zaira Cedillo Silva" --url "https://youtube.com/watch?v=XXX" --desde 00:15:20 --hasta 00:16:20

# c) Una carpeta con subcarpeta por diputado (procesa todo de golpe):
#    muestras_voz/Zaira Cedillo Silva/entrevista.mp3
#    muestras_voz/Omar Ortega Álvarez/discurso.m4a
python voz.py muestras --carpeta muestras_voz
```

**Para juntar muestras en volumen** (ej. 3 clips por diputado) usa
`bajar_muestras.py`:

```
python bajar_muestras.py --plantilla     # crea muestras.csv con los 75+
                                         # nombres, 3 filas por diputado
# ...llenas url/desde/hasta en Excel y guardas...
python bajar_muestras.py --lista muestras.csv   # descarga y organiza todo
python voz.py muestras                   # crea las huellas
```

Los clips quedan en `muestras_voz/<Nombre>/` como mp3 que puedes abrir con
doble clic para verificar de oído. Las filas vacías se ignoran y las ya
descargadas se saltan, así que puedes llenar el CSV por partes y correr el
comando las veces que necesites.

Consejos para buenas muestras: 30 a 60 segundos por diputado, con el
diputado hablando **solo** (sin música ni otras voces), y usa el **nombre
oficial exacto** de `diputados.txt` para que las sugerencias empaten con el
catálogo. Todas las fuentes se combinan: muestras directas y sesiones
revisadas suman al mismo perfil.

**Administrar las huellas:**

```
python voz.py ver                                  # listar huellas (segundos y fuentes)
python voz.py olvidar --nombre "Nombre Completo"   # borrar UNA huella para rehacerla
python voz.py muestras --solo "Nombre Completo"    # reprocesar solo a ese diputado
```

Las huellas son acumulables e inteligentes: cada clip se registra una sola
vez, así que puedes correr `voz.py muestras` cuantas veces quieras (solo
procesa lo nuevo). Para **regenerar todo desde cero**: renombra
`voces_perfiles.json` (como respaldo) y vuelve a correr `voz.py muestras` —
tus clips siguen en `muestras_voz/`, así que se reconstruye solo.

**Expectativas honestas:** con audio de sala, voces parecidas y ~75
posibles hablantes, la voz no es infalible — por eso el modo por defecto
solo reporta y nada se reemplaza sin coincidencia fuerte. Úsala como
detector de "aquí habló alguien más" y confirma en `revisar.py`. Su
precisión mejora conforme alimentes más sesiones a los perfiles.

## Mejoras posibles a futuro

- Detección de votaciones nominales (cada diputado dice su nombre y el
  sentido de su voto: hoy no disparan la detección de orador).
- Interfaz web para ver la transcripción en vivo desde otra computadora.
- Resúmenes automáticos de cada sesión con IA.
