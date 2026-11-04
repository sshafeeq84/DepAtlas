import pytest

pytest.importorskip("botocore")  # optional extra: depatlas[aws]

from botocore.exceptions import ClientError  # noqa: E402

from depatlas.connectors.codecommit import (
    PROFILE_ENV_VAR,
    CodeCommitConnectorError,
    fetch_repo,
    get_client,
)


class _FakeCodeCommitClient:
    """A minimal stand-in for boto3's CodeCommit client. `folders` maps a
    folder path to the get_folder response it should return; `files` maps
    an absolutePath to the file content bytes it should return.
    """

    def __init__(self, folders: dict, files: dict, raise_code: str | None = None):
        self.folders = folders
        self.files = files
        self.raise_code = raise_code

    def get_folder(self, repositoryName, folderPath, commitSpecifier=None):
        if self.raise_code:
            raise ClientError(
                {"Error": {"Code": self.raise_code, "Message": "fake error"}}, "GetFolder"
            )
        return self.folders[folderPath]

    def get_file(self, repositoryName, filePath, commitSpecifier=None):
        return {"fileContent": self.files[filePath]}


def test_fetch_repo_reconstructs_nested_tree(tmp_path):
    folders = {
        "/": {
            "files": [{"absolutePath": "README.md"}],
            "subFolders": [{"absolutePath": "src"}],
        },
        "src": {
            "files": [{"absolutePath": "src/charge.py"}],
            "subFolders": [],
        },
    }
    files = {
        "README.md": b"# payments\n",
        "src/charge.py": b"import auth_client\n",
    }
    client = _FakeCodeCommitClient(folders, files)

    result = fetch_repo("payments", client, tmp_path)

    assert result == tmp_path / "payments"
    assert (result / "README.md").read_bytes() == b"# payments\n"
    assert (result / "src" / "charge.py").read_bytes() == b"import auth_client\n"


def test_fetch_repo_overwrites_stale_cache(tmp_path):
    stale_dir = tmp_path / "payments"
    stale_dir.mkdir()
    (stale_dir / "old_stale_file.py").write_text("# should be gone after rescan")

    folders = {"/": {"files": [], "subFolders": []}}
    client = _FakeCodeCommitClient(folders, {})

    result = fetch_repo("payments", client, tmp_path)

    assert not (result / "old_stale_file.py").exists()


def test_fetch_repo_raises_on_repository_not_found(tmp_path):
    client = _FakeCodeCommitClient({}, {}, raise_code="RepositoryDoesNotExistException")

    with pytest.raises(CodeCommitConnectorError, match="not found"):
        fetch_repo("nonexistent", client, tmp_path)


def test_fetch_repo_treats_missing_folder_as_empty_not_an_error(tmp_path):
    client = _FakeCodeCommitClient({}, {}, raise_code="FolderDoesNotExistException")

    result = fetch_repo("empty-repo", client, tmp_path)

    assert result == tmp_path / "empty-repo"
    assert list(result.iterdir()) == []


def test_get_client_uses_profile_env_when_set(monkeypatch):
    calls = {}

    class FakeSession:
        def __init__(self, profile_name=None):
            calls["profile_name"] = profile_name

        def client(self, service, region_name=None):
            calls["service"] = service
            calls["region_name"] = region_name
            return "fake-client"

    import depatlas.connectors.codecommit as codecommit_module

    monkeypatch.setenv(PROFILE_ENV_VAR, "depatlas-profile")
    monkeypatch.setattr(codecommit_module.boto3, "Session", FakeSession)

    result = get_client("us-east-1")

    assert calls["profile_name"] == "depatlas-profile"
    assert calls["service"] == "codecommit"
    assert calls["region_name"] == "us-east-1"
    assert result == "fake-client"


def test_get_client_falls_back_to_default_session_without_profile(monkeypatch):
    calls = {}

    class FakeSession:
        def __init__(self, profile_name=None):
            calls["profile_name"] = profile_name

        def client(self, service, region_name=None):
            return "fake-client"

    import depatlas.connectors.codecommit as codecommit_module

    monkeypatch.delenv(PROFILE_ENV_VAR, raising=False)
    monkeypatch.setattr(codecommit_module.boto3, "Session", FakeSession)

    get_client("us-east-1")

    assert calls["profile_name"] is None


def test_fetch_repo_records_commit_id(tmp_path):
    from depatlas.gitinfo import get_sha

    folders = {"/": {"files": [], "subFolders": [], "commitId": "deadbeef1234"}}
    result = fetch_repo("payments", _FakeCodeCommitClient(folders=folders, files={}), tmp_path)

    assert get_sha(result) == "deadbeef1234"


def test_get_client_explains_missing_boto3(monkeypatch):
    import depatlas.connectors.codecommit as codecommit_module

    monkeypatch.setattr(codecommit_module, "boto3", None)

    with pytest.raises(CodeCommitConnectorError, match=r"depatlas\[aws\]"):
        get_client("us-east-1")
