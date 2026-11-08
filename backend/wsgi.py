"""Gunicorn entry point with fail-fast production configuration validation."""

from app import create_app
from app.config import Config

errors = Config.validate()
if Config.DEBUG:
    errors.append("FLASK_DEBUG must be disabled when using Gunicorn")
if errors:
    raise RuntimeError("Invalid MiroFish configuration: " + "; ".join(errors))

application = create_app()
