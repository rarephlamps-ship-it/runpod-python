"""Download status and resource-cleanup tests; no live network or fake dependency."""
import ast
import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests

from runpod.serverless.utils import rp_download


@pytest.fixture(autouse=True)
def no_network_and_isolated_storage(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    def deny(*args, **kwargs):
        raise AssertionError("UNIT_TEST_NETWORK_FORBIDDEN")

    monkeypatch.setattr(requests.Session, "send", deny)


def response(status=200):
    value = requests.Response()
    value.status_code = status
    value.url = "https://example.invalid/input.txt"
    value.headers["Content-Disposition"] = 'attachment; filename="input.txt"'
    value._content = b"offline response bytes"
    value._content_consumed = True
    value.close = Mock(wraps=value.close)
    return value


@pytest.mark.parametrize("status", [400, 401, 403, 404, 408, 429, 500, 503])
def test_http_failure_cannot_be_saved_as_successful_file(status, tmp_path):
    value = response(status)
    with patch.object(rp_download.SyncClientSession, "get", return_value=value):
        with pytest.raises(requests.HTTPError) as error:
            rp_download.file(value.url)
    assert error.value.response.status_code == status
    assert not list(tmp_path.rglob("*.txt"))
    value.close.assert_called_once_with()


def test_success_is_written_and_response_is_closed():
    value = response()
    with patch.object(rp_download.SyncClientSession, "get", return_value=value) as get:
        result = rp_download.file(value.url)
    assert Path(result["file_path"]).read_bytes() == value.content
    assert result["type"] == "txt"
    assert result["original_name"] == "input.txt"
    assert result["extracted_path"] is None
    get.assert_called_once_with(value.url, headers=rp_download.HEADERS, timeout=30)
    value.close.assert_called_once_with()


def test_response_is_closed_when_disk_write_fails():
    value = response()
    with patch.object(rp_download.SyncClientSession, "get", return_value=value), \
         patch("builtins.open", side_effect=PermissionError("offline fixture")):
        with pytest.raises(PermissionError, match="offline fixture"):
            rp_download.file(value.url)
    value.close.assert_called_once_with()


def test_response_is_closed_when_header_processing_fails():
    value = response()
    value.headers = None
    with patch.object(rp_download.SyncClientSession, "get", return_value=value):
        with pytest.raises(AttributeError):
            rp_download.file(value.url)
    value.close.assert_called_once_with()


def test_error_payload_named_zip_is_not_extracted(tmp_path):
    value = response(403)
    value.headers["Content-Disposition"] = 'attachment; filename="error.zip"'
    with patch.object(rp_download.SyncClientSession, "get", return_value=value), \
         patch.object(rp_download.zipfile, "ZipFile") as unzip:
        with pytest.raises(requests.HTTPError):
            rp_download.file(value.url)
    unzip.assert_not_called()
    assert not list(tmp_path.rglob("*.zip"))
    value.close.assert_called_once_with()


def test_test_methods_are_not_silently_overwritten():
    root = Path(os.environ.get("NEXT1000_SOURCE_ROOT", Path(__file__).resolve().parents[3]))
    target = root / "tests/test_serverless/test_utils/test_download.py"
    parsed = ast.parse(target.read_text(encoding="utf-8"))
    for cls in [node for node in parsed.body if isinstance(node, ast.ClassDef)]:
        names = [node.name for node in cls.body if isinstance(node, ast.FunctionDef)
                 and node.name.startswith("test_")]
        assert len(names) == len(set(names)), f"Duplicate methods in {cls.name}: {names}"


def test_http_session_is_closed_after_success():
    value = response()
    with patch.object(rp_download.SyncClientSession, "get", return_value=value), \
         patch.object(rp_download.SyncClientSession, "close", autospec=True) as close:
        rp_download.file(value.url)
    assert close.call_count == 1


def test_http_session_is_closed_when_request_itself_fails():
    with patch.object(rp_download.SyncClientSession, "get", side_effect=requests.ConnectionError("offline fixture")), \
         patch.object(rp_download.SyncClientSession, "close", autospec=True) as close:
        with pytest.raises(requests.ConnectionError, match="offline fixture"):
            rp_download.file("https://example.invalid/input.txt")
    assert close.call_count == 1
