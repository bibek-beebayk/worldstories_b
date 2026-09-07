#!/bin/bash
set -e

if [ -z "$PORT" ]; then
    export PORT=8000
fi

# Any arguments passed to the container are run instead of the web server.
# This is what lets the Celery worker and beat run from this same image as
# separate Railway services, e.g.
#
#   celery -A core worker -l info --concurrency=2
#   celery -A core beat   -l info --schedule=/tmp/celerybeat-schedule
#
# With no arguments the behaviour is exactly what it was before: migrate,
# collect static, serve with gunicorn.
if [ "$#" -gt 0 ]; then
    exec "$@"
fi

python manage.py migrate -v 0
python manage.py collectstatic --noinput
exec gunicorn core.wsgi:application --bind 0.0.0.0:$PORT --workers 3
