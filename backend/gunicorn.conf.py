"""Gunicorn settings keep the API Pod-local by default."""

import os

bind = os.environ.get("GUNICORN_BIND", "127.0.0.1:5001")
workers = 1
worker_class = "gthread"
threads = 8
timeout = 600
accesslog = "-"
errorlog = "-"
