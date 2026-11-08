from app.config import Config


def test_graphiti_configuration_requires_independent_embedding_credentials(monkeypatch):
    values = {
        "SECRET_KEY": "test-secret",
        "LLM_API_KEY": "chat-key",
        "LLM_BASE_URL": "https://chat.example/v1",
        "LLM_MODEL_NAME": "chat-model",
        "EMBEDDING_API_KEY": "embedding-key",
        "EMBEDDING_BASE_URL": "https://embedding.example/v1",
        "EMBEDDING_MODEL_NAME": "embedding-model",
        "EMBEDDING_DIM": "768",
        "NEO4J_PASSWORD": "neo4j-secret",
        "GRAPHITI_STRUCTURED_OUTPUT_MODE": "json_object",
        "DEBUG": False,
    }
    for key, value in values.items():
        monkeypatch.setattr(Config, key, value)
    assert Config.validate() == []

    monkeypatch.setattr(Config, "EMBEDDING_API_KEY", None)
    errors = Config.validate()
    assert "EMBEDDING_API_KEY is not configured" in errors
    assert not any("LLM_API_KEY" in error for error in errors)


def test_graphiti_configuration_rejects_invalid_embedding_dimensions(monkeypatch):
    monkeypatch.setattr(Config, "EMBEDDING_DIM", "0")
    errors = Config.validate()
    assert any("EMBEDDING_DIM must be a positive integer" in error for error in errors)


def _set_valid_graphiti_config(monkeypatch, neo4j_uri):
    values = {
        "SECRET_KEY": "test-secret",
        "LLM_API_KEY": "chat-key",
        "LLM_BASE_URL": "https://chat.example/v1",
        "LLM_MODEL_NAME": "chat-model",
        "EMBEDDING_API_KEY": "embedding-key",
        "EMBEDDING_BASE_URL": "https://embedding.example/v1",
        "EMBEDDING_MODEL_NAME": "embedding-model",
        "EMBEDDING_DIM": "768",
        "NEO4J_URI": neo4j_uri,
        "NEO4J_PASSWORD": "neo4j-secret",
        "GRAPHITI_STRUCTURED_OUTPUT_MODE": "json_object",
        "DEBUG": False,
    }
    for key, value in values.items():
        monkeypatch.setattr(Config, key, value)


def test_neo4j_plain_bolt_is_allowed_for_loopback(monkeypatch):
    _set_valid_graphiti_config(monkeypatch, "bolt://127.0.0.1:7687")

    assert Config.validate() == []


def test_neo4j_remote_plain_bolt_is_rejected(monkeypatch):
    _set_valid_graphiti_config(monkeypatch, "bolt://neo4j:7687")

    assert "NEO4J_URI must use certificate-verified TLS for remote hosts" in Config.validate()


def test_neo4j_remote_verified_tls_is_allowed(monkeypatch):
    _set_valid_graphiti_config(monkeypatch, "bolt+s://neo4j.example:7687")

    assert Config.validate() == []


def test_neo4j_remote_self_signed_tls_is_rejected(monkeypatch):
    _set_valid_graphiti_config(monkeypatch, "bolt+ssc://neo4j.example:7687")

    assert "NEO4J_URI must use certificate-verified TLS for remote hosts" in Config.validate()


def test_malformed_neo4j_uri_returns_configuration_error(monkeypatch):
    _set_valid_graphiti_config(monkeypatch, "bolt://[::1")

    assert "NEO4J_URI must be a valid Neo4j URI" in Config.validate()
