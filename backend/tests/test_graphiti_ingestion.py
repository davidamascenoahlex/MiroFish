from types import SimpleNamespace

import pytest

from app.services.graph_builder import GraphBuilderService


def test_add_text_batches_tracks_graphiti_generated_episode_uuids():
    calls = []

    def add(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(uuid_=f"graphiti-episode-{len(calls)}")

    builder = object.__new__(GraphBuilderService)
    builder.client = SimpleNamespace(graph=SimpleNamespace(add=add))

    submission = builder.add_text_batches(
        "group-a", ["first chunk", "second chunk"], batch_size=1
    )

    assert submission.episode_uuids == ["graphiti-episode-1", "graphiti-episode-2"]
    assert submission.item_count == 2
    assert len(submission.operation_id) == 64
    assert [call["name"] for call in calls] == ["chunk-1", "chunk-2"]
    assert all("uuid" not in call for call in calls)


def test_add_text_batches_requires_graphiti_to_return_episode_uuid():
    builder = object.__new__(GraphBuilderService)
    builder.client = SimpleNamespace(
        graph=SimpleNamespace(add=lambda **_kwargs: SimpleNamespace())
    )

    with pytest.raises(RuntimeError, match="no episode UUID"):
        builder.add_text_batches("group-a", ["chunk"])


def _resumable_builder(stored, add):
    builder = object.__new__(GraphBuilderService)
    builder.client = SimpleNamespace(
        graph=SimpleNamespace(add=add, list_episodes=lambda **_kwargs: list(stored))
    )
    return builder


def test_resume_reuses_stored_chunks_and_adds_only_missing_ones():
    calls = []

    def add(**kwargs):
        calls.append(kwargs["name"])
        return SimpleNamespace(uuid_="new-episode")

    builder = _resumable_builder(
        [{"uuid": "stored-episode", "name": "chunk-1", "content": "first chunk"}], add
    )

    submission = builder.add_text_batches(
        "group-a", ["first chunk", "second chunk"], resume=True
    )

    assert submission.episode_uuids == ["stored-episode", "new-episode"]
    assert calls == ["chunk-2"]


def test_resume_rejects_a_stored_chunk_with_different_text():
    def add(**_kwargs):
        raise AssertionError("must not add over a mismatched episode")

    builder = _resumable_builder(
        [{"uuid": "stored-episode", "name": "chunk-1", "content": "old text"}], add
    )

    with pytest.raises(RuntimeError, match="force=true"):
        builder.add_text_batches("group-a", ["new text"], resume=True)


def test_failed_chunk_is_retried(monkeypatch):
    monkeypatch.setattr(GraphBuilderService, "EPISODE_RETRY_DELAY_SECONDS", 0)
    attempts = []

    def add(**kwargs):
        attempts.append(kwargs["name"])
        if len(attempts) == 1:
            raise RuntimeError("transient graph-store failure")
        return SimpleNamespace(uuid_="retried-episode")

    builder = _resumable_builder([], add)

    submission = builder.add_text_batches("group-a", ["chunk"])

    assert submission.episode_uuids == ["retried-episode"]
    assert attempts == ["chunk-1", "chunk-1"]


def test_retry_reuses_an_episode_committed_before_the_error(monkeypatch):
    monkeypatch.setattr(GraphBuilderService, "EPISODE_RETRY_DELAY_SECONDS", 0)
    stored = []

    def add(**kwargs):
        stored.append(
            {"uuid": "committed-episode", "name": kwargs["name"], "content": kwargs["data"]}
        )
        raise RuntimeError("connection lost after commit")

    builder = _resumable_builder(stored, add)

    submission = builder.add_text_batches("group-a", ["chunk"])

    assert submission.episode_uuids == ["committed-episode"]
    assert len(stored) == 1


def test_chunk_failure_is_raised_after_the_last_attempt(monkeypatch):
    monkeypatch.setattr(GraphBuilderService, "EPISODE_RETRY_DELAY_SECONDS", 0)

    def add(**_kwargs):
        raise RuntimeError("persistent failure")

    builder = _resumable_builder([], add)

    with pytest.raises(RuntimeError, match="persistent failure"):
        builder.add_text_batches("group-a", ["chunk"])
