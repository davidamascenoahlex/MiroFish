import runpy
from pathlib import Path
from unittest.mock import Mock

import pytest

import app
from app.config import Config, _neo4j_uri_validation_error


@pytest.mark.parametrize(
    "uri",
    ["neo4j://localhost:7687", "neo4j://127.0.0.1:7687", "neo4j://[::1]:7687"],
)
def test_plaintext_routing_is_not_allowed_even_with_a_loopback_seed(uri):
    assert _neo4j_uri_validation_error(uri) == (
        "NEO4J_URI must use direct bolt:// for unencrypted loopback connections"
    )


@pytest.mark.parametrize(
    "uri", ["bolt://127.0.0.1:not-a-port", "bolt://127.0.0.1:65536", "bolt://127.0.0.1:0"]
)
def test_invalid_neo4j_port_is_rejected(uri):
    assert _neo4j_uri_validation_error(uri) == "NEO4J_URI must be a valid Neo4j URI"


def test_verified_routing_tls_remains_supported():
    assert _neo4j_uri_validation_error("neo4j+s://neo4j.example:7687") is None


def test_production_wsgi_rejects_debug_before_creating_the_app(monkeypatch):
    create_app = Mock()
    monkeypatch.setattr(app, "create_app", create_app)
    monkeypatch.setattr(Config, "validate", classmethod(lambda cls: []))
    monkeypatch.setattr(Config, "DEBUG", True)

    with pytest.raises(RuntimeError, match="FLASK_DEBUG must be disabled"):
        runpy.run_module("wsgi")
    create_app.assert_not_called()


def test_production_wsgi_creates_app_when_debug_is_disabled(monkeypatch):
    application = object()
    monkeypatch.setattr(app, "create_app", lambda: application)
    monkeypatch.setattr(Config, "validate", classmethod(lambda cls: []))
    monkeypatch.setattr(Config, "DEBUG", False)

    assert runpy.run_module("wsgi")["application"] is application


def test_backend_image_contains_shared_locales():
    dockerfile = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text()
    in_backend = False
    backend_lines = []
    for line in dockerfile.splitlines():
        if line.lower().startswith("from "):
            if in_backend:
                break
            in_backend = line.lower().endswith(" as backend")
        elif in_backend:
            backend_lines.append(line.strip())

    assert "COPY locales ./locales" in backend_lines
