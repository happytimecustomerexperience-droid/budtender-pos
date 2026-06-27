"""Gunicorn config (Linux prod). Windows: use waitress or runserver for dev.

    gunicorn -c gunicorn.conf.py budtender_pos.wsgi
"""

import multiprocessing
import os

bind = os.environ.get("BUDTENDER_BIND", "0.0.0.0:8000")
workers = int(os.environ.get("WEB_CONCURRENCY", multiprocessing.cpu_count() * 2 + 1))
worker_class = "gthread"
threads = int(os.environ.get("WEB_THREADS", 4))
timeout = 60  # Dutchie calls can be slow behind Cloudflare
graceful_timeout = 30
keepalive = 5
max_requests = 1000
max_requests_jitter = 100
