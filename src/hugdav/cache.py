"""Time-aware in-memory tree cache for a single HF repo revision.

The cache keeps a mapping ``path -> Entry`` where ``path`` is the canonical
posix path (no leading slash; root is ``""``).  Entries are either files or
directories.  Directories are *implicit* on the Hugging Face side; we model
them explicitly here and synthesise empty directories using a placeholder file
on commit time.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from huggingface_hub import HfApi
from huggingface_hub.errors import EntryNotFoundError, RepositoryNotFoundError
from huggingface_hub.hf_api import RepoFile, RepoFolder

from .config import PLACEHOLDER_NAME


@dataclass
class Entry:
    path: str  # canonical, no leading slash; "" == root
    is_dir: bool
    size: int = 0
    etag: str = ""
    last_modified: float = 0.0  # epoch seconds
    children: Dict[str, "Entry"] = field(default_factory=dict)


def canon(path: str) -> str:
    """Canonicalise a WebDAV/HF path into our internal form.

    - strip leading/trailing slashes
    - collapse repeated slashes
    - reject ``..``
    """
    if path is None:
        return ""
    parts: List[str] = []
    for seg in path.replace("\\", "/").split("/"):
        if not seg or seg == ".":
            continue
        if seg == "..":
            raise ValueError(f"illegal path component: {path!r}")
        parts.append(seg)
    return "/".join(parts)


def split_parent(path: str) -> tuple[str, str]:
    p = canon(path)
    if not p:
        return "", ""
    if "/" not in p:
        return "", p
    parent, _, name = p.rpartition("/")
    return parent, name


class HfTreeCache:
    """Snapshot of a HF repo file tree, with TTL refresh and write-through edits."""

    def __init__(self, api: HfApi, repo_id: str, repo_type: str, revision: str,
                 token: Optional[str], ttl: float = 30.0):
        self.api = api
        self.repo_id = repo_id
        self.repo_type = repo_type
        self.revision = revision
        self.token = token
        self.ttl = ttl
        self._lock = threading.RLock()
        self._root = Entry(path="", is_dir=True)
        self._loaded_at: float = 0.0

    # ----- loading ---------------------------------------------------------

    def _load(self) -> None:
        try:
            entries: Iterable = self.api.list_repo_tree(
                repo_id=self.repo_id,
                repo_type=self.repo_type,
                revision=self.revision,
                recursive=True,
                expand=True,
                token=self.token,
            )
        except RepositoryNotFoundError as exc:
            raise FileNotFoundError(f"repo not found: {self.repo_id}") from exc

        root = Entry(path="", is_dir=True)
        for it in entries:
            self._insert_repo_item(root, it)
        self._root = root
        self._loaded_at = time.time()

    @staticmethod
    def _insert_repo_item(root: Entry, it) -> None:
        path = canon(it.path)
        if not path:
            return
        parent, name = split_parent(path)
        node = HfTreeCache._ensure_dir(root, parent)
        if isinstance(it, RepoFolder):
            child = node.children.get(name)
            if child is None or not child.is_dir:
                node.children[name] = Entry(path=path, is_dir=True)
        elif isinstance(it, RepoFile):
            size = int(getattr(it, "size", 0) or 0)
            oid = str(getattr(it, "blob_id", None) or getattr(it, "oid", "") or "")
            last_mod = 0.0
            lc = getattr(it, "last_commit", None)
            if lc is not None:
                d = getattr(lc, "date", None)
                if d is not None:
                    try:
                        last_mod = d.timestamp()
                    except Exception:
                        last_mod = 0.0
            node.children[name] = Entry(
                path=path, is_dir=False, size=size, etag=oid,
                last_modified=last_mod,
            )

    @staticmethod
    def _ensure_dir(root: Entry, path: str) -> Entry:
        node = root
        if not path:
            return node
        cur = ""
        for seg in path.split("/"):
            cur = f"{cur}/{seg}" if cur else seg
            child = node.children.get(seg)
            if child is None:
                child = Entry(path=cur, is_dir=True)
                node.children[seg] = child
            elif not child.is_dir:
                # A file collides with a directory — should not happen on HF,
                # but be defensive.
                child = Entry(path=cur, is_dir=True)
                node.children[seg] = child
            node = child
        return node

    # ----- public API ------------------------------------------------------

    def refresh(self, *, force: bool = False) -> None:
        with self._lock:
            if force or (time.time() - self._loaded_at) > self.ttl:
                self._load()

    def invalidate(self) -> None:
        with self._lock:
            self._loaded_at = 0.0

    def get(self, path: str) -> Optional[Entry]:
        path = canon(path)
        self.refresh()
        with self._lock:
            if not path:
                return self._root
            node: Optional[Entry] = self._root
            for seg in path.split("/"):
                if node is None or not node.is_dir:
                    return None
                node = node.children.get(seg)
            return node

    def list_visible_children(self, path: str) -> List[Entry]:
        """Return children of a directory, hiding the placeholder file."""
        e = self.get(path)
        if e is None or not e.is_dir:
            return []
        with self._lock:
            return [c for n, c in e.children.items() if n != PLACEHOLDER_NAME]

    # ----- write-through mutations -----------------------------------------
    # These are pure cache mutations.  Callers are responsible for issuing the
    # corresponding HF commit *before* (deletes) or *after* (uploads) the
    # mutation, so the cached state matches the remote.

    def upsert_file(self, path: str, *, size: int, etag: str = "",
                     last_modified: Optional[float] = None) -> Entry:
        with self._lock:
            self.refresh()
            parent, name = split_parent(path)
            if not name:
                raise ValueError("cannot upsert empty path")
            parent_node = self._ensure_dir(self._root, parent)
            entry = Entry(
                path=canon(path), is_dir=False, size=size, etag=etag,
                last_modified=last_modified or time.time(),
            )
            parent_node.children[name] = entry
            return entry

    def upsert_dir(self, path: str) -> Entry:
        with self._lock:
            self.refresh()
            return self._ensure_dir(self._root, canon(path))

    def remove(self, path: str) -> None:
        with self._lock:
            self.refresh()
            parent, name = split_parent(path)
            if not name:
                return
            parent_node = self.get(parent)
            if parent_node is None or not parent_node.is_dir:
                return
            parent_node.children.pop(name, None)

    def move(self, src: str, dst: str) -> None:
        with self._lock:
            self.refresh()
            src_node = self.get(src)
            if src_node is None:
                return
            self.remove(src)
            parent, name = split_parent(dst)
            parent_node = self._ensure_dir(self._root, parent)
            new_node = Entry(
                path=canon(dst), is_dir=src_node.is_dir,
                size=src_node.size, etag=src_node.etag,
                last_modified=src_node.last_modified,
                children=src_node.children,
            )
            self._rewrite_paths(new_node, canon(dst))
            parent_node.children[name] = new_node

    def _rewrite_paths(self, node: Entry, base: str) -> None:
        node.path = base
        if node.is_dir:
            for name, child in node.children.items():
                self._rewrite_paths(child, f"{base}/{name}" if base else name)
