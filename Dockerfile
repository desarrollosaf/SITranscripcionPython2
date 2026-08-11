FROM python:3.11-slim

# ffmpeg: requerido por transcribir_en_vivo_c3.py para cortar el audio en bloques.
# git: algunas dependencias de speechbrain se instalan desde repos git.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencias base (faster-whisper, yt-dlp) + reconocimiento de voz (torch,
# torchaudio, speechbrain) en capas separadas para aprovechar la cache de Docker.
# torch/torchaudio se instalan desde el índice CPU-only de PyTorch: el índice
# público arrastra ~2 GB de paquetes nvidia-cuda-* aunque no haya GPU.
COPY requirements.txt requirements-voz.txt requirements-api.txt requirements-revisar.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch torchaudio \
    && pip install --no-cache-dir -r requirements-voz.txt \
    && pip install --no-cache-dir -r requirements-api.txt \
    && pip install --no-cache-dir -r requirements-revisar.txt

COPY transcribir_en_vivo_c3.py voz.py revisar.py crear_admin.py ./
COPY api ./api

# Carpetas de datos que normalmente se montan como volúmenes (ver docker-compose.yml)
RUN mkdir -p /app/sesiones_en_vivo /app/muestras_voz /app/modelos_voz /app/jobs_data

ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "transcribir_en_vivo_c3.py"]
