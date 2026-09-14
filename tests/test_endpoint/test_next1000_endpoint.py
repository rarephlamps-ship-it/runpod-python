"""Offline regression contracts for the NEXT1000 endpoint repair.

No provider calls, credentials, altered retry policy, or production signing.
"""
import copy
from unittest.mock import Mock, patch

import pytest
import requests

import runpod
from runpod.endpoint.helpers import FINAL_STATES, is_completed
from runpod.endpoint.runner import Endpoint, Job, RunPodClient


@pytest.fixture(autouse=True)
def no_outbound_network(monkeypatch):
    monkeypatch.setattr(runpod, "api_key", None)

    def deny(*args, **kwargs):
        raise AssertionError("UNIT_TEST_NETWORK_FORBIDDEN")

    monkeypatch.setattr(requests.Session, "send", deny)


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED"])
def test_all_terminal_states_have_one_shared_definition(status):
    assert status in FINAL_STATES
    assert is_completed(status) is True
    assert len(FINAL_STATES) == len(set(FINAL_STATES))


@pytest.mark.parametrize("status", ["IN_QUEUE", "IN_PROGRESS", "RUNNING", "UNKNOWN"])
def test_nonterminal_states_remain_nonterminal(status):
    assert status not in FINAL_STATES
    assert is_completed(status) is False


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED"])
@pytest.mark.parametrize("include_stream", [False, True])
def test_terminal_empty_stream_stops_without_another_request(status, include_stream):
    response = {"status": status}
    if include_stream:
        response["stream"] = []
    client = Mock()
    client.get.side_effect = [response, AssertionError("Terminal job polled again")]
    with patch("runpod.endpoint.runner.time.sleep"):
        assert list(Job("endpoint", "job", client).stream()) == []
    client.get.assert_called_once_with(endpoint="endpoint/stream/job")


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED"])
def test_final_chunks_are_drained_in_original_order(status):
    client = Mock()
    client.get.side_effect = [
        {"status": "IN_PROGRESS", "stream": [{"output": "a"}]},
        {"status": status, "stream": [{"output": "b"}, {"output": "c"}]},
        {"status": status, "stream": []},
        AssertionError("Drain never terminated"),
    ]
    with patch("runpod.endpoint.runner.time.sleep"):
        assert list(Job("endpoint", "job", client).stream()) == ["a", "b", "c"]
    assert client.get.call_count == 3


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "TIMED_OUT", "CANCELLED"])
def test_run_sync_terminal_response_is_not_polled_again(status):
    endpoint = Endpoint("endpoint", api_key="OFFLINE_TEST_KEY")
    with patch.object(endpoint.rp_client, "post", return_value={
        "id": "job", "status": status, "output": {"done": True}
    }) as post, patch.object(endpoint.rp_client, "get", side_effect=AssertionError("Unexpected poll")):
        assert endpoint.run_sync({"prompt": "local"}) == {"done": True}
    post.assert_called_once_with("endpoint/runsync", {"input": {"prompt": "local"}}, timeout=86400)


@pytest.mark.parametrize("method", ["run", "run_sync"])
@pytest.mark.parametrize("payload,expected", [
    ({}, {"input": {}}),
    ({"prompt": "local"}, {"input": {"prompt": "local"}}),
    ({"input": {}}, {"input": {}}),
    ({"input": {"prompt": "local"}}, {"input": {"prompt": "local"}}),
    ({"input": {}, "policy": {"ttl": 60}}, {"input": {}, "policy": {"ttl": 60}}),
])
def test_input_wrapping_preserves_empty_mapping_and_caller_data(method, payload, expected):
    before = copy.deepcopy(payload)
    endpoint = Endpoint("endpoint", api_key="OFFLINE_TEST_KEY")
    with patch.object(endpoint.rp_client, "post", return_value={
        "id": "job", "status": "COMPLETED", "output": "ok"
    }) as post:
        result = getattr(endpoint, method)(payload)
    assert post.call_args.args[1] == expected
    assert payload == before
    if method == "run":
        assert isinstance(result, Job)
    else:
        assert result == "ok"


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("status", [400, 401, 403, 404, 408, 429, 500, 503])
def test_http_error_contract_uses_real_response_without_network(method, status):
    client = RunPodClient(api_key="OFFLINE_TEST_KEY")
    response = requests.Response()
    response.status_code = status
    response.url = "https://example.invalid/test"
    response._content = b'{"error":"offline"}'
    with patch.object(client.rp_session, "request", return_value=response) as request:
        expected_exception = RuntimeError if status == 401 else requests.HTTPError
        with pytest.raises(expected_exception):
            if method == "get":
                client.get("endpoint/status/job", timeout=7)
            else:
                client.post("endpoint/run", {"input": {}}, timeout=7)
    assert request.call_count == 1
    assert request.call_args.args[0] == method.upper()
    assert request.call_args.kwargs["timeout"] == 7


def test_explicit_client_key_survives_global_key_changes(monkeypatch):
    monkeypatch.setattr(runpod, "api_key", "GLOBAL_OLD")
    first = Endpoint("endpoint", api_key="INSTANCE_KEY")
    second = Endpoint("endpoint")
    monkeypatch.setattr(runpod, "api_key", None)
    assert first.rp_client.headers["Authorization"] == "Bearer INSTANCE_KEY"
    assert second.rp_client.headers["Authorization"] == "Bearer GLOBAL_OLD"
    with pytest.raises(RuntimeError):
        Endpoint("endpoint")


def test_network_guard_rejects_any_unmocked_endpoint_call():
    client = RunPodClient(api_key="OFFLINE_TEST_KEY")
    with pytest.raises(AssertionError, match="UNIT_TEST_NETWORK_FORBIDDEN"):
        client.get("endpoint/status/job")
