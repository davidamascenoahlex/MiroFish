import httpx
import pytest

from app.utils import zep


def test_permanent_graph_errors_fail_without_retry():
    calls = []

    def operation():
        calls.append(True)
        raise ValueError("bad query")

    with pytest.raises(ValueError, match="bad query"):
        zep.call_graph_read_with_retry(operation, operation_name="permanent failure", sleep=lambda _: None)
    assert len(calls) == 1


def test_transient_transport_error_is_retried_with_bounded_delay():
    calls = []
    sleeps = []

    def operation():
        calls.append(True)
        if len(calls) == 1:
            raise httpx.ConnectError("connection lost", request=httpx.Request("GET", "http://graph"))
        return "ok"

    result = zep.call_graph_read_with_retry(
        operation, operation_name="transient read", initial_delay=0.25, sleep=sleeps.append
    )
    assert result == "ok"
    assert len(calls) == 2
    assert sleeps == [0.25]


def test_graphiti_client_rejects_cloud_credentials():
    with pytest.raises(ValueError, match="Zep Cloud credentials are unsupported"):
        zep.get_graphiti_client("legacy-key")


def test_graph_read_retry_requires_at_least_one_attempt():
    with pytest.raises(ValueError, match="at least 1"):
        zep.call_graph_read_with_retry(lambda: None, operation_name="invalid", max_attempts=0)
