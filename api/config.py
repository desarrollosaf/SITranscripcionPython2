import os


class Settings:
    mysql_host = os.environ.get("DB_HOST", "host.docker.internal")
    mysql_port = int(os.environ.get("DB_PORT", "3306"))
    mysql_user = os.environ.get("DB_USER", "root")
    mysql_password = os.environ.get("DB_PASS", "")
    mysql_db = os.environ.get("DB_NAME", "transcripcion_api")

    jwt_secret = os.environ.get("JWT_SECRET", "cambia-esta-clave")
    jwt_algoritmo = "HS256"
    jwt_expira_minutos = int(os.environ.get("JWT_EXPIRA_MINUTOS", "480"))

    perfiles_path = os.environ.get("PERFILES_PATH", "voces_perfiles.json")
    db_path = os.environ.get("DB_PATH", "sesiones.db")
    jobs_dir = os.environ.get("JOBS_DIR", "jobs_data")

    modelos_permitidos = {"tiny", "base", "small", "medium", "large-v3"}

    # Rango de puertos UDP reservados para audio SRT entrante (consola de
    # audio / Dante empujando en vivo), uno por transmisión simultánea.
    # Debe coincidir con el rango publicado en docker-compose.yml.
    srt_puerto_base = int(os.environ.get("SRT_PUERTO_BASE", "9000"))
    srt_puerto_fin = int(os.environ.get("SRT_PUERTO_FIN", "9009"))


settings = Settings()
