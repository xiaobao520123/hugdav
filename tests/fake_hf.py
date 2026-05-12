"""In-memory fake of HfApi + hf_hub_download for tests."""

from __future__ import annotations

import io
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional

from huggingface_hub import (
    CommitOperationAdd,
    CommitOperationCopy,
    CommitOperationDelete,
)
from huggingface_hub.errors import EntryNotFoundError, RepositoryNotFoundError
from huggingface_hub.hf_api import RepoFile, RepoFolder


@dataclass
class _LastCommit:
    date: datetime


def _mk_file(path: str, data: bytes) -> RepoFile:
    return RepoFile(
        path=path,
        size=len(data),
        oid=f"sha-{hash(data) & 0xffffffff:x}",
        last_commit={"id": "fake", "title": "fake",
                     "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")},
    )


def _mk_folder(path: str) -> RepoFolder:
    return RepoFolder(
        path=path, oid="fake",
        last_commit={"id": "fake", "title": "fake",
                     "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")},
    )


class FakeHfApi:
    """Implements just the bits of HfApi that hugdav needs."""

    def __init__(self, *, repo_id: str = "test/repo", token: Optional[str] = None):
        self.repo_id = repo_id
        self.token = token
        # path -> bytes
        self._files: Dict[str, bytes] = {}
        self.commits: List[str] = []

    # ---- helpers --------------------------------------------------------

    def _check_repo(self, repo_id: str):
        if repo_id != self.repo_id:
            raise RepositoryNotFoundError(f"unknown repo {repo_id}")

    def _read_fileobj(self, obj) -> bytes:
        if isinstance(obj, (bytes, bytearray)):
            return bytes(obj)
        if isinstance(obj, str):
            with open(obj, "rb") as f:
                return f.read()
        if hasattr(obj, "read"):
            try:
                obj.seek(0)
            except Exception:
                pass
            return obj.read()
        raise TypeError(f"unsupported fileobj: {type(obj)!r}")

    # ---- API surface ----------------------------------------------------

    def list_repo_tree(self, repo_id: str, path_in_repo: Optional[str] = None,
                       *, recursive: bool = False, expand: bool = False,
                       revision: Optional[str] = None,
                       repo_type: Optional[str] = None,
                       token=None) -> Iterable:
        self._check_repo(repo_id)
        prefix = "" if not path_in_repo else path_in_repo.rstrip("/") + "/"
        seen_dirs: set = set()
        results = []
        for path in sorted(self._files.keys()):
            if not path.startswith(prefix):
                continue
            rel = path[len(prefix):]
            if not rel:
                continue
            if "/" in rel and not recursive:
                top = rel.split("/", 1)[0]
                full = prefix + top
                if full not in seen_dirs:
                    seen_dirs.add(full)
                    results.append(_mk_folder(full))
                continue
            # Yield interior folders for recursive listing too.
            if recursive and "/" in rel:
                cur = prefix
                for seg in rel.split("/")[:-1]:
                    cur = cur + seg + "/"
                    full = cur.rstrip("/")
                    if full not in seen_dirs:
                        seen_dirs.add(full)
                        results.append(_mk_folder(full))
            results.append(_mk_file(path, self._files[path]))
        return results

    def upload_file(self, *, path_or_fileobj, path_in_repo: str, repo_id: str,
                    repo_type=None, revision=None, token=None,
                    commit_message=None, **_):
        self._check_repo(repo_id)
        self._files[path_in_repo] = self._read_fileobj(path_or_fileobj)
        self.commits.append(commit_message or f"upload {path_in_repo}")

    def delete_file(self, path_in_repo: str, repo_id: str, *, repo_type=None,
                    revision=None, token=None, commit_message=None, **_):
        self._check_repo(repo_id)
        if path_in_repo not in self._files:
            raise EntryNotFoundError(f"missing {path_in_repo}")
        del self._files[path_in_repo]
        self.commits.append(commit_message or f"delete {path_in_repo}")

    def create_commit(self, repo_id: str, operations, *, commit_message: str,
                      repo_type=None, revision=None, token=None, **_):
        self._check_repo(repo_id)
        # Two-pass to handle copy-then-delete (move) atomically.
        snapshot = dict(self._files)
        for op in operations:
            if isinstance(op, CommitOperationAdd):
                self._files[op.path_in_repo] = self._read_fileobj(op.path_or_fileobj)
            elif isinstance(op, CommitOperationCopy):
                if op.src_path_in_repo not in snapshot:
                    raise EntryNotFoundError(
                        f"missing src {op.src_path_in_repo}",
                    )
                self._files[op.path_in_repo] = snapshot[op.src_path_in_repo]
            elif isinstance(op, CommitOperationDelete):
                # Folder deletes expand to all matching files; we only emit
                # file-level deletes from hugdav so just drop a single key.
                self._files.pop(op.path_in_repo, None)
            else:
                raise TypeError(f"unsupported op {type(op).__name__}")
        self.commits.append(commit_message)


def patched_hf_hub_download_factory(api: FakeHfApi):
    def fake_download(*, repo_id: str, filename: str, repo_type=None,
                      revision=None, token=None, **_):
        api._check_repo(repo_id)
        if filename not in api._files:
            raise EntryNotFoundError(f"missing {path_in_repo}")
        fd, tmp = tempfile.mkstemp(prefix="hugdav-fake-")
        with os.fdopen(fd, "wb") as f:
            f.write(api._files[filename])
        return tmp
    return fake_download
