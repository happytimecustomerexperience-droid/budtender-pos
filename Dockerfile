FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY . .

# Deps come as manylinux wheels (cryptography, curl_cffi, Pillow) — no compiler needed.
RUN pip install ".[prod]"

# Bake static into the image (WhiteNoise serves them). Build-time defaults are fine.
RUN python manage.py collectstatic --noinput

EXPOSE 8000

# migrate -> (optional admin from DJANGO_SUPERUSER_* env) -> gunicorn (0.0.0.0:8000).
CMD ["sh", "-c", "python manage.py migrate --noinput && (python manage.py createsuperuser --noinput 2>/dev/null || true) && exec gunicorn -c gunicorn.conf.py budtender_pos.wsgi"]
