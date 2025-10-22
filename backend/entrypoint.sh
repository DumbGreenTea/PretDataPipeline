#!/usr/bin/env sh
# entrypoint.sh: espera a Postgres (con login real), ejecuta migraciones y luego el comando.
set -eu

# Tiempo máximo de espera (segundos). Cambia con WAIT_FOR_DB_TIMEOUT=120 si quieres.
: "${WAIT_FOR_DB_TIMEOUT:=60}"

wait_for_postgres() {
  echo "Esperando a Postgres en ${POSTGRES_HOST:-db}:${POSTGRES_PORT:-5432} (timeout ${WAIT_FOR_DB_TIMEOUT}s)..."
  python - <<'PY'
import os, time, sys
import psycopg2

host = os.getenv('POSTGRES_HOST', 'db')
port = int(os.getenv('POSTGRES_PORT', '5432'))
db   = os.getenv('POSTGRES_DB', '')
usr  = os.getenv('POSTGRES_USER', '')
pwd  = os.getenv('POSTGRES_PASSWORD', '')
timeout = int(os.getenv('WAIT_FOR_DB_TIMEOUT', '60'))
deadline = time.time() + timeout

while True:
    try:
        conn = psycopg2.connect(dbname=db, user=usr, password=pwd, host=host, port=port)
        conn.close()
        print("Postgres OK: conexión y autenticación verificadas.")
        sys.exit(0)
    except Exception as e:
        if time.time() > deadline:
            print(f"ERROR: timeout esperando a Postgres: {e}")
            sys.exit(1)
        time.sleep(1)
PY
}

# Permite saltarse la espera (útil en tests rápidos)
if [ -z "${SKIP_WAIT_FOR_DB:-}" ]; then
  wait_for_postgres
else
  echo "SKIP_WAIT_FOR_DB=1 -> saltando espera de Postgres."
fi

echo "Aplicando migraciones..."
python manage.py migrate --noinput

echo "Arrancando aplicación: $*"
exec "$@"
