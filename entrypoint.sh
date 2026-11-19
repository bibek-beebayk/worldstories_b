#!/bin/bash
set -e

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

if [ -z "$PORT" ]; then
    export PORT=8000
fi

python manage.py migrate --noinput
python manage.py collectstatic --noinput
exec gunicorn core.wsgi:application --bind 0.0.0.0:$PORT --workers 3
