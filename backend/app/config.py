"""Application configuration loaded from environment variables and optional .env."""

import ipaddress
import os
import warnings
from urllib.parse import urlparse

from dotenv import load_dotenv

project_root_env = os.path.join(os.path.dirname(__file__), "../../.env")
if os.path.exists(project_root_env):
    load_dotenv(project_root_env, override=False)
else:
    load_dotenv(override=False)


def _neo4j_uri_validation_error(uri: str | None) -> str | None:
    if not uri:
        return None

    try:
        parsed = urlparse(uri)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return "NEO4J_URI must be a valid Neo4j URI"

    allowed_schemes = {
        "bolt", "neo4j", "bolt+s", "neo4j+s", "bolt+ssc", "neo4j+ssc"
    }
    if parsed.scheme not in allowed_schemes or not hostname or port == 0:
        return "NEO4J_URI must be a valid Neo4j URI"
    if parsed.scheme in {"bolt+ssc", "neo4j+ssc"}:
        return "NEO4J_URI must use certificate-verified TLS for remote hosts"

    host = hostname.lower()
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host == "localhost"

    if is_loopback:
        if parsed.scheme == "neo4j":
            return "NEO4J_URI must use direct bolt:// for unencrypted loopback connections"
        return None
    if parsed.scheme not in {"bolt+s", "neo4j+s"}:
        return "NEO4J_URI must use certificate-verified TLS for remote hosts"
    return None


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY")
    DEBUG = os.environ.get("FLASK_DEBUG", "False").strip().lower() in {"1", "true", "yes"}
    JSON_AS_ASCII = False

    LLM_API_KEY = os.environ.get("LLM_API_KEY")
    LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
    LLM_MODEL_NAME = os.environ.get("LLM_MODEL_NAME", "gpt-4o-mini")

    EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY")
    EMBEDDING_BASE_URL = os.environ.get("EMBEDDING_BASE_URL")
    EMBEDDING_MODEL_NAME = os.environ.get("EMBEDDING_MODEL_NAME")
    EMBEDDING_DIM = os.environ.get("EMBEDDING_DIM")

    GRAPHITI_STRUCTURED_OUTPUT_MODE = os.environ.get(
        "GRAPHITI_STRUCTURED_OUTPUT_MODE", "json_object"
    ).strip().lower()

    NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687")
    NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
    NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD")

    MAX_CONTENT_LENGTH = 50 * 1024 * 1024
    UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "../uploads")
    ALLOWED_EXTENSIONS = {"pdf", "md", "txt", "markdown"}

    DEFAULT_CHUNK_SIZE = int(os.environ.get("GRAPH_CHUNK_SIZE", "1000"))
    DEFAULT_CHUNK_OVERLAP = int(os.environ.get("GRAPH_CHUNK_OVERLAP", "100"))
    OASIS_DEFAULT_MAX_ROUNDS = int(os.environ.get("OASIS_DEFAULT_MAX_ROUNDS", "10"))
    OASIS_SIMULATION_DATA_DIR = os.path.join(
        os.path.dirname(__file__), "../uploads/simulations"
    )

    OASIS_TWITTER_ACTIONS = [
        "CREATE_POST", "LIKE_POST", "REPOST", "FOLLOW", "DO_NOTHING", "QUOTE_POST"
    ]
    OASIS_REDDIT_ACTIONS = [
        "LIKE_POST", "DISLIKE_POST", "CREATE_POST", "CREATE_COMMENT",
        "LIKE_COMMENT", "DISLIKE_COMMENT", "SEARCH_POSTS", "SEARCH_USER",
        "TREND", "REFRESH", "DO_NOTHING", "FOLLOW", "MUTE"
    ]

    REPORT_AGENT_MAX_TOOL_CALLS = int(os.environ.get("REPORT_AGENT_MAX_TOOL_CALLS", "5"))
    REPORT_AGENT_MAX_REFLECTION_ROUNDS = int(
        os.environ.get("REPORT_AGENT_MAX_REFLECTION_ROUNDS", "2")
    )
    REPORT_AGENT_TEMPERATURE = float(os.environ.get("REPORT_AGENT_TEMPERATURE", "0.5"))

    @classmethod
    def validate(cls) -> list[str]:
        errors: list[str] = []
        required = {
            "SECRET_KEY": cls.SECRET_KEY,
            "LLM_API_KEY": cls.LLM_API_KEY,
            "LLM_BASE_URL": cls.LLM_BASE_URL,
            "LLM_MODEL_NAME": cls.LLM_MODEL_NAME,
            "EMBEDDING_API_KEY": cls.EMBEDDING_API_KEY,
            "EMBEDDING_BASE_URL": cls.EMBEDDING_BASE_URL,
            "EMBEDDING_MODEL_NAME": cls.EMBEDDING_MODEL_NAME,
            "NEO4J_PASSWORD": cls.NEO4J_PASSWORD,
        }
        errors.extend(f"{name} is not configured" for name, value in required.items() if not value)
        neo4j_uri_error = _neo4j_uri_validation_error(cls.NEO4J_URI)
        if neo4j_uri_error:
            errors.append(neo4j_uri_error)
        try:
            embedding_dim = int(cls.EMBEDDING_DIM or "")
            if embedding_dim < 1:
                raise ValueError
        except ValueError:
            errors.append("EMBEDDING_DIM must be a positive integer matching the embedding model output")
        if cls.GRAPHITI_STRUCTURED_OUTPUT_MODE not in {"json_object", "json_schema"}:
            errors.append("GRAPHITI_STRUCTURED_OUTPUT_MODE must be json_object or json_schema")
        if cls.DEFAULT_CHUNK_SIZE < 1 or not (
            0 <= cls.DEFAULT_CHUNK_OVERLAP < cls.DEFAULT_CHUNK_SIZE
        ):
            errors.append("GRAPH_CHUNK_SIZE must be positive and 0 <= GRAPH_CHUNK_OVERLAP < GRAPH_CHUNK_SIZE")
        if cls.DEBUG:
            warnings.warn("Flask DEBUG mode is enabled. Do not use in production.", RuntimeWarning)
        return errors
