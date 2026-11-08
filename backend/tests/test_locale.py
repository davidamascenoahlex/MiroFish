import re
from collections import Counter

import pytest
from flask import Flask

from app.utils import locale


@pytest.fixture(autouse=True)
def isolated_locale(monkeypatch):
    monkeypatch.delattr(locale._thread_local, "locale", raising=False)
    yield
    if hasattr(locale._thread_local, "locale"):
        del locale._thread_local.locale


def test_portuguese_is_default_and_guides_model_output():
    assert locale.get_locale() == "pt"
    assert locale.t("common.cancel") == "Cancelar"
    assert locale.get_language_instruction() == "Por favor, responda em portugu\u00eas do Brasil."


@pytest.mark.parametrize("language", ["pt", "en", "zh"])
def test_explicit_thread_language_is_preserved(language):
    locale.set_locale(language)
    assert locale.get_locale() == language


@pytest.mark.parametrize("language", ["unknown", "es", ""])
def test_unsupported_thread_language_falls_back_to_portuguese(language):
    locale.set_locale(language)
    assert locale.get_locale() == "pt"
    assert locale.t("common.cancel") == "Cancelar"
    assert locale.get_language_instruction() == "Por favor, responda em portugu\u00eas do Brasil."


@pytest.mark.parametrize("header, expected", [(None, "pt"), ("pt", "pt"), ("en", "en"), ("zh", "zh"), ("unknown", "pt")])
def test_request_language_uses_portuguese_default(header, expected):
    app = Flask(__name__)
    headers = {} if header is None else {"Accept-Language": header}
    with app.test_request_context(headers=headers):
        assert locale.get_locale() == expected


def _messages(value, path=""):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _messages(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _messages(child, f"{path}[{index}]")
    else:
        yield path, value


def test_portuguese_messages_preserve_keys_types_and_placeholders():
    english = dict(_messages(locale._translations["en"]))
    portuguese = dict(_messages(locale._translations["pt"]))
    assert english.keys() == portuguese.keys()
    for key, value in english.items():
        translated = portuguese[key]
        assert isinstance(translated, str) and translated.strip(), key
        assert Counter(re.findall(r"\{[^{}]+\}", value)) == Counter(
            re.findall(r"\{[^{}]+\}", translated)
        ), key
