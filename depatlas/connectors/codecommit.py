"""AWS CodeCommit connector (SPEC.pdf section 4.1, "Code store connectors":
AWS CodeCommit).

Unlike the GitHub connector (a single zip download), CodeCommit's API has
no bulk-download endpoint -- a repo's tree is walked one folder at a time
via `get_folder` (which lists files and subfolders at a given path) and
each file's content is fetched individually via `get_file`. This
connector reconstructs that tree locally so the existing language
parsers can run against it unchanged, same as the GitHub connector's job.

The boto3 client is passed in rather than constructed deep inside
`fetch_repo`, so tests can supply a fake client with canned responses
instead of mocking botocore internals -- the same "inject the thing you
want to fake" approach used for `requests.get` in the GitHub connector's
tests, just via a parameter instead of monkeypatching a module.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from depatlas.gitinfo import write_sha

try:  # boto3 is an optional extra: pip install "depatlas[aws]"
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:  # pragma: no cover - exercised via get_client's error
    boto3 = None

    class BotoCoreError(Exception):  # type: ignore[no-redef]
        pass

    class ClientError(Exception):  # type: ignore[no-redef]
        pass

PROFILE_ENV_VAR = "DEPATLAS_AWS_PROFILE"


class CodeCommitConnectorError(Exception):
    """Raised when CodeCommit can't be reached or returns an error."""


def get_client(region: str, profile_env: str = PROFILE_ENV_VAR):
    """Build a boto3 CodeCommit client using an AWS profile named by the
    environment variable `profile_env` (SPEC.pdf section 9's config
    convention: profile_env: DEPATLAS_AWS_PROFILE). Falls back to boto3's
    normal default credential chain if the env var isn't set, so this
    also works in environments using instance roles, SSO, etc.

    Never accepts an access key or secret directly -- credentials always
    come from the AWS credentials file / environment / role, matching
    SPEC.pdf section 12's "Auth credential storage: Environment variables
    only -- never written to disk" (the profile *name* is not a secret;
    the actual keys live in the AWS credentials file, managed by the AWS
    CLI, not by DepAtlas).
    """
    profile_name = os.environ.get(profile_env)
    if boto3 is None:
        raise CodeCommitConnectorError(
            "AWS CodeCommit support needs boto3. Install it with: "
            'pip install "depatlas[aws]"'
        )
    session = boto3.Session(profile_name=profile_name) if profile_name else boto3.Session()
    return session.client("codecommit", region_name=region)


def fetch_repo(repo_name: str, client, cache_dir: Path, commit_specifier: str = "") -> Path:
    """Download a CodeCommit repo's tree via get_folder/get_file and
    reconstruct it locally under cache_dir/repo_name. Returns the local
    path to the reconstructed repo root.

    Re-fetches and overwrites the cached copy every call, same "always
    current, never a stale partial cache" behavior as the GitHub
    connector.
    """
    extract_root = Path(cache_dir) / repo_name
    if extract_root.exists():
        shutil.rmtree(extract_root)
    extract_root.mkdir(parents=True)

    commit_ids: list[str] = []
    # Every call after the first reads the same commit. Without pinning, a
    # push during the walk would produce a tree made of two commits.
    pinned = {"id": commit_specifier}

    def _walk(folder_path: str) -> None:
        kwargs = {"repositoryName": repo_name, "folderPath": folder_path}
        if pinned["id"]:
            kwargs["commitSpecifier"] = pinned["id"]

        try:
            response = client.get_folder(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "RepositoryDoesNotExistException":
                raise CodeCommitConnectorError(
                    f"Repo '{repo_name}' not found (or credentials lack access)."
                ) from exc
            if code in ("FolderDoesNotExistException", "PathDoesNotExistException"):
                return  # empty folder at this path; nothing more to walk
            raise CodeCommitConnectorError(f"CodeCommit API error ({code}): {exc}") from exc

        if response.get("commitId"):
            if not commit_ids:
                # Pin to the commit id from the first response -- even if
                # the caller passed a branch name, which would otherwise be
                # re-resolved on every call (and move if someone pushes).
                pinned["id"] = response["commitId"]
            commit_ids.append(response["commitId"])

        for file_info in response.get("files", []):
            file_kwargs = {"repositoryName": repo_name, "filePath": file_info["absolutePath"]}
            if pinned["id"]:
                file_kwargs["commitSpecifier"] = pinned["id"]
            file_response = client.get_file(**file_kwargs)

            local_path = extract_root / file_info["absolutePath"].lstrip("/")
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(file_response["fileContent"])

        for subfolder in response.get("subFolders", []):
            _walk(subfolder["absolutePath"])

    try:
        _walk("/")
    except (BotoCoreError,) as exc:
        raise CodeCommitConnectorError(f"Failed to reach AWS CodeCommit: {exc}") from exc

    write_sha(extract_root, commit_ids[0] if commit_ids else None)
    return extract_root
