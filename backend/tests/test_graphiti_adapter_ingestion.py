import asyncio
from types import SimpleNamespace
from typing import Optional

from pydantic import BaseModel, Field

from app.services import graphiti_adapter
from app.services.graphiti_adapter import (
    EdgeModel,
    EntityModel,
    normalize_ontology_attribute_response,
)


class Organization(EntityModel):
    location: Optional[str] = Field(default=None)
    org_name: Optional[str] = Field(default=None)
    organization_role: Optional[str] = Field(default=None)


class WorksFor(EdgeModel):
    role: Optional[str] = Field(default=None)


class Unrelated(BaseModel):
    role: Optional[str] = None


def test_wrapped_entity_attributes_become_flat_storable_values():
    response = {
        "Organization": {
            "location": None,
            "org_name": "Organização das Nações Unidas",
            "organization_role": None,
        }
    }

    normalized = normalize_ontology_attribute_response(response, Organization)

    assert normalized == {"org_name": "Organização das Nações Unidas"}
    Organization(**normalized)


def test_attribute_values_are_limited_to_declared_text_fields():
    response = {
        "org_name": {"sigla": "ONU", "nome": "Nações Unidas"},
        "location": ["Nova York", "Genebra"],
        "organization_role": 3,
        "unexpected": "dropped",
    }

    normalized = normalize_ontology_attribute_response(response, Organization)

    assert normalized == {
        "org_name": "sigla: ONU; nome: Nações Unidas",
        "location": "Nova York, Genebra",
        "organization_role": "3",
    }
    Organization(**normalized)


def test_top_level_attributes_override_a_wrapped_response():
    response = {
        "Organization": {"org_name": "ONU", "location": "Genebra"},
        "location": "Nova York",
        "organization_role": None,
    }

    assert normalize_ontology_attribute_response(response, Organization) == {
        "org_name": "ONU",
        "location": "Nova York",
    }


def test_llm_client_sanitizes_only_ontology_attribute_extraction(monkeypatch):
    client_class = graphiti_adapter._ontology_safe_llm_client_class()
    raw = {"WorksFor": {"role": ["analista", "consultor"]}, "extra": {"nested": True}}

    async def generate_response(self, messages, response_model=None, *args, **kwargs):
        return dict(raw)

    monkeypatch.setattr(client_class.__mro__[1], "generate_response", generate_response)
    client = object.__new__(client_class)

    assert asyncio.run(
        client.generate_response([], WorksFor, attribute_extraction=True)
    ) == {"role": "analista, consultor"}
    assert asyncio.run(client.generate_response([], WorksFor)) == raw
    assert asyncio.run(
        client.generate_response([], Unrelated, attribute_extraction=True)
    ) == raw


def test_list_episodes_reads_only_the_requested_group():
    queries = []

    class Driver:
        async def execute_query(self, query, **params):
            queries.append((query, params))
            return SimpleNamespace(
                records=[{"uuid": "episode-1", "name": "chunk-1", "content": "text"}]
            )

    class Backend:
        graphiti = SimpleNamespace(driver=Driver())

        def run(self, coroutine):
            return asyncio.run(coroutine)

    graph = graphiti_adapter._GraphNamespace(Backend())

    assert graph.list_episodes(graph_id="mirofish_group") == [
        {"uuid": "episode-1", "name": "chunk-1", "content": "text"}
    ]
    assert queries[0][1] == {"group_id": "mirofish_group"}
    assert "Episodic {group_id: $group_id}" in queries[0][0]
