import io
import zipfile

import pytest
import requests

from depgraph.connectors.github import (
    GitHubConnectorError,
    TOKEN_ENV_VAR,
    fetch_repo,
    get_token,
)


def _make_fake_zip_bytes() -> bytes:
    """Build a small in-memory zip mimicking GitHub's zipball shape:
    everything wrapped in one top-level "owner-repo-<sha>" folder.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("payments-abc1234/charge.py", "import auth_client\n")
        zf.writestr("payments-abc1234/README.md", "# payments\n")
    return buffer.getvalue()


class _FakeResponse:
    def __init__(self, status_code: int, content: bytes = b"", text: str = ""):
        self.status_code = status_code
        self.content = content
        self.text = text or content.decode(errors="ignore")


def test_get_token_raises_when_env_var_missing(monkeypatch):
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)
    with pytest.raises(GitHubConnectorError):
        get_token()


def test_get_token_returns_value_when_set(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV_VAR, "fake-token-123")
    assert get_token() == "fake-token-123"


def test_fetch_repo_extracts_zip_and_resolves_inner_dir(tmp_path, monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        assert "payments" in url
        assert headers["Authorization"] == "Bearer fake-token"
        return _FakeResponse(200, content=_make_fake_zip_bytes())

    monkeypatch.setattr(requests, "get", fake_get)

    result = fetch_repo("org", "payments", "fake-token", tmp_path)

    assert result.name == "payments-abc1234"
    assert (result / "charge.py").exists()
    assert (result / "charge.py").read_text() == "import auth_client\n"


def test_fetch_repo_raises_on_404(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(404))

    with pytest.raises(GitHubConnectorError, match="not found"):
        fetch_repo("org", "nonexistent", "fake-token", tmp_path)


def test_fetch_repo_raises_on_401(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _FakeResponse(401))

    with pytest.raises(GitHubConnectorError, match="invalid or expired"):
        fetch_repo("org", "payments", "bad-token", tmp_path)


def test_fetch_repo_raises_on_network_error(tmp_path, monkeypatch):
    def raise_connection_error(*args, **kwargs):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(requests, "get", raise_connection_error)

    with pytest.raises(GitHubConnectorError, match="Failed to reach"):
        fetch_repo("org", "payments", "fake-token", tmp_path)
