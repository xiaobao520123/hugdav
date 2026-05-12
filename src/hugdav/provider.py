"""WsgiDAV provider that maps a Hugging Face repo to a WebDAV tree."""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from typing import List, Optional

from huggingface_hub import (
    CommitOperationAdd,
    CommitOperationCopy,
    CommitOperationDelete,
    HfApi,
    hf_hub_download,
)
from huggingface_hub.errors import (
    EntryNotFoundError,
    HfHubHTTPError,
    RepositoryNotFoundError,
)
from wsgidav import util
from wsgidav.dav_error import (
    HTTP_FORBIDDEN,
    HTTP_INTERNAL_ERROR,
    HTTP_NOT_FOUND,
    DAVError,
)
from wsgidav.dav_provider import DAVCollection, DAVNonCollection, DAVProvider

from .cache import Entry, HfTreeCache, canon, split_parent
from .config import PLACEHOLDER_NAME, Config

log = logging.getLogger("hugdav.provider")


# --- helpers ---------------------------------------------------------------


def _map_hf_error(exc: Exception) -> DAVError:
    if isinstance(exc, RepositoryNotFoundError):
        return DAVError(HTTP_NOT_FOUND, "repo not found")
    if isinstance(exc, EntryNotFoundError):
        return DAVError(HTTP_NOT_FOUND, "entry not found")
    if isinstance(exc, HfHubHTTPError):
        status = getattr(exc.response, "status_code", 500) if exc.response else 500
        if status in (401, 403):
            return DAVError(HTTP_FORBIDDEN, str(exc))
        if status == 404:
            return DAVError(HTTP_NOT_FOUND, str(exc))
        return DAVError(status, str(exc))
    return DAVError(HTTP_INTERNAL_ERROR, str(exc))


# --- resources -------------------------------------------------------------


class _BaseResource:
    """Mixin for shared bookkeeping between collection / non-collection."""

    provider: "HfProvider"
    path: str  # WebDAV path (with leading /)
    environ: dict

    @property
    def cfg(self) -> Config:
        return self.provider.cfg

    @property
    def cache(self) -> HfTreeCache:
        return self.provider.cache

    @property
    def hf_path(self) -> str:
        return canon(self.path)

    def _token(self) -> Optional[str]:
        # BASIC-auth password (per-request) overrides the static token.
        tok = self.environ.get("hugdav.token")
        return tok or self.cfg.token


class HfFileResource(_BaseResource, DAVNonCollection):
    def __init__(self, path: str, environ: dict, entry: Entry,
                 provider: "HfProvider"):
        DAVNonCollection.__init__(self, path, environ)
        self.provider = provider
        self.entry = entry
        self._tmp_upload: Optional[tempfile.SpooledTemporaryFile] = None

    # -- read-only properties ------------------------------------------------

    def get_content_length(self) -> int:
        return int(self.entry.size or 0)

    def get_content_type(self) -> str:
        return util.guess_mime_type(self.path)

    def get_creation_date(self) -> float:
        return self.entry.last_modified or 0.0

    def get_last_modified(self) -> float:
        return self.entry.last_modified or 0.0

    def get_etag(self) -> str:
        return self.entry.etag or f"hugdav-{abs(hash(self.path)):x}"

    def support_etag(self) -> bool:
        return True

    def support_modified(self) -> bool:
        return True

    def support_content_length(self) -> bool:
        return True

    def support_ranges(self) -> bool:
        return True

    # -- read content --------------------------------------------------------

    def get_content(self):
        try:
            local_path = hf_hub_download(
                repo_id=self.cfg.repo_id,
                filename=self.hf_path,
                repo_type=self.cfg.repo_type,
                revision=self.cfg.revision,
                token=self._token(),
            )
        except Exception as exc:  # noqa: BLE001
            raise _map_hf_error(exc) from exc
        return open(local_path, "rb")

    # -- write content -------------------------------------------------------

    def begin_write(self, *, content_type: Optional[str] = None):
        # WsgiDAV closes the file object after writing the request body, so we
        # cannot rely on a SpooledTemporaryFile that lives across calls.
        # Use a NamedTemporaryFile (delete=False) and re-open by path in
        # end_write to upload its contents.
        f = tempfile.NamedTemporaryFile(prefix="hugdav-up-", delete=False)
        self._tmp_upload_path = f.name
        return f

    def end_write(self, *, with_errors: bool):
        path = getattr(self, "_tmp_upload_path", None)
        self._tmp_upload_path = None
        if path is None:
            return
        try:
            if with_errors:
                return
            size = os.path.getsize(path)
            try:
                self.provider.api.upload_file(
                    path_or_fileobj=path,
                    path_in_repo=self.hf_path,
                    repo_id=self.cfg.repo_id,
                    repo_type=self.cfg.repo_type,
                    revision=self.cfg.revision,
                    token=self._token(),
                    commit_message=f"hugdav: upload {self.hf_path}",
                )
            except Exception as exc:  # noqa: BLE001
                raise _map_hf_error(exc) from exc
            self.cache.upsert_file(self.hf_path, size=size)
            self.provider.try_clear_placeholder(
                split_parent(self.hf_path)[0], token=self._token(),
            )
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    # -- delete --------------------------------------------------------------

    def delete(self):
        try:
            self.provider.api.delete_file(
                path_in_repo=self.hf_path,
                repo_id=self.cfg.repo_id,
                repo_type=self.cfg.repo_type,
                revision=self.cfg.revision,
                token=self._token(),
                commit_message=f"hugdav: delete {self.hf_path}",
            )
        except Exception as exc:  # noqa: BLE001
            raise _map_hf_error(exc) from exc
        self.cache.remove(self.hf_path)
        self.remove_all_locks(recursive=False)
        self.remove_all_properties(recursive=False)

    # -- copy / move ---------------------------------------------------------

    def support_recursive_move(self, dest_path):  # noqa: ARG002
        return False

    def handle_copy(self, dest_path, *, depth_infinity):  # noqa: ARG002
        self._do_copy_or_move(dest_path, is_move=False)
        return True

    def handle_move(self, dest_path):
        self._do_copy_or_move(dest_path, is_move=True)
        return True

    def _do_copy_or_move(self, dest_path: str, *, is_move: bool):
        dst = canon(dest_path)
        ops = [CommitOperationCopy(
            src_path_in_repo=self.hf_path,
            path_in_repo=dst,
            src_revision=self.cfg.revision,
        )]
        if is_move:
            ops.append(CommitOperationDelete(path_in_repo=self.hf_path))
        msg = f"hugdav: {'move' if is_move else 'copy'} {self.hf_path} -> {dst}"
        try:
            self.provider.api.create_commit(
                repo_id=self.cfg.repo_id,
                repo_type=self.cfg.repo_type,
                revision=self.cfg.revision,
                token=self._token(),
                operations=ops,
                commit_message=msg,
            )
        except Exception as exc:  # noqa: BLE001
            raise _map_hf_error(exc) from exc
        if is_move:
            self.cache.move(self.hf_path, dst)
        else:
            self.cache.upsert_file(dst, size=self.entry.size, etag=self.entry.etag)

    def copy_move_single(self, dest_path, *, is_move):
        # Fallback path used by wsgidav for the legacy file-by-file flow.
        self._do_copy_or_move(dest_path, is_move=is_move)


class HfFolderResource(_BaseResource, DAVCollection):
    def __init__(self, path: str, environ: dict, entry: Entry,
                 provider: "HfProvider"):
        DAVCollection.__init__(self, path, environ)
        self.provider = provider
        self.entry = entry

    # -- read-only properties ------------------------------------------------

    def get_creation_date(self) -> float:
        return self.entry.last_modified or 0.0

    def get_last_modified(self) -> float:
        return self.entry.last_modified or 0.0

    def get_display_info(self) -> dict:
        return {"type": "Directory"}

    def support_etag(self) -> bool:
        return False

    def support_modified(self) -> bool:
        return True

    def support_recursive_delete(self) -> bool:
        return True

    def support_recursive_move(self, dest_path) -> bool:  # noqa: ARG002
        return False

    # -- listing -------------------------------------------------------------

    def get_member_names(self) -> List[str]:
        return [c.path.rsplit("/", 1)[-1] if "/" in c.path else c.path
                for c in self.cache.list_visible_children(self.hf_path)]

    def get_member(self, name):
        child_path = f"{self.path.rstrip('/')}/{name}"
        return self.provider.get_resource_inst(child_path, self.environ)

    # -- create children -----------------------------------------------------

    def create_collection(self, name):
        new_dir = canon(f"{self.hf_path}/{name}" if self.hf_path else name)
        # Materialise via a placeholder file.
        placeholder_path = f"{new_dir}/{PLACEHOLDER_NAME}"
        try:
            self.provider.api.upload_file(
                path_or_fileobj=b"hugdav placeholder\n",
                path_in_repo=placeholder_path,
                repo_id=self.cfg.repo_id,
                repo_type=self.cfg.repo_type,
                revision=self.cfg.revision,
                token=self._token(),
                commit_message=f"hugdav: mkdir {new_dir}",
            )
        except Exception as exc:  # noqa: BLE001
            raise _map_hf_error(exc) from exc
        self.cache.upsert_dir(new_dir)
        self.cache.upsert_file(placeholder_path, size=20)

    def create_empty_resource(self, name):
        # WsgiDAV calls this for PUT to a not-yet-existing file.
        new_path = canon(f"{self.hf_path}/{name}" if self.hf_path else name)
        # Synthesise a placeholder Entry; the file is materialised on end_write.
        entry = Entry(path=new_path, is_dir=False, size=0)
        full = f"{self.path.rstrip('/')}/{name}"
        return HfFileResource(full, self.environ, entry, self.provider)

    # -- delete --------------------------------------------------------------

    def delete(self):
        # Recursive delete: collect every concrete file in this subtree, then
        # commit a single create_commit with N CommitOperationDelete entries.
        files = self._all_descendant_files(self.entry)
        if not files:
            return
        ops = [CommitOperationDelete(path_in_repo=p) for p in files]
        try:
            self.provider.api.create_commit(
                repo_id=self.cfg.repo_id,
                repo_type=self.cfg.repo_type,
                revision=self.cfg.revision,
                token=self._token(),
                operations=ops,
                commit_message=f"hugdav: rmdir {self.hf_path}",
            )
        except Exception as exc:  # noqa: BLE001
            raise _map_hf_error(exc) from exc
        self.cache.remove(self.hf_path)
        self.remove_all_locks(recursive=True)
        self.remove_all_properties(recursive=True)

    @classmethod
    def _all_descendant_files(cls, entry: Entry) -> List[str]:
        out: List[str] = []
        if not entry.is_dir:
            return [entry.path]
        for child in entry.children.values():
            if child.is_dir:
                out.extend(cls._all_descendant_files(child))
            else:
                out.append(child.path)
        return out

    # -- copy / move ---------------------------------------------------------

    def handle_copy(self, dest_path, *, depth_infinity):  # noqa: ARG002
        self._do_copy_or_move(dest_path, is_move=False)
        return True

    def handle_move(self, dest_path):
        self._do_copy_or_move(dest_path, is_move=True)
        return True

    def copy_move_single(self, dest_path, *, is_move):
        self._do_copy_or_move(dest_path, is_move=is_move)

    def _do_copy_or_move(self, dest_path: str, *, is_move: bool):
        dst = canon(dest_path)
        files = self._all_descendant_files(self.entry)
        ops = []
        for f in files:
            rel = f[len(self.hf_path) + 1:] if self.hf_path else f
            new_path = f"{dst}/{rel}" if rel else dst
            ops.append(CommitOperationCopy(
                src_path_in_repo=f,
                path_in_repo=new_path,
                src_revision=self.cfg.revision,
            ))
        if is_move:
            for f in files:
                ops.append(CommitOperationDelete(path_in_repo=f))
        if not ops:
            ops.append(CommitOperationAdd(
                path_in_repo=f"{dst}/{PLACEHOLDER_NAME}",
                path_or_fileobj=b"hugdav placeholder\n",
            ))
        msg = f"hugdav: {'move' if is_move else 'copy'} {self.hf_path} -> {dst}"
        try:
            self.provider.api.create_commit(
                repo_id=self.cfg.repo_id,
                repo_type=self.cfg.repo_type,
                revision=self.cfg.revision,
                token=self._token(),
                operations=ops,
                commit_message=msg,
            )
        except Exception as exc:  # noqa: BLE001
            raise _map_hf_error(exc) from exc
        if is_move:
            self.cache.move(self.hf_path, dst)
        else:
            self.cache.invalidate()


# --- provider --------------------------------------------------------------


class HfProvider(DAVProvider):
    def __init__(self, cfg: Config, api: Optional[HfApi] = None):
        super().__init__()
        self.cfg = cfg
        self.api = api or HfApi(token=cfg.token)
        self.cache = HfTreeCache(
            api=self.api,
            repo_id=cfg.repo_id,
            repo_type=cfg.repo_type,
            revision=cfg.revision,
            token=cfg.token,
            ttl=cfg.cache_ttl,
        )
        self._placeholder_lock = threading.Lock()

    def is_readonly(self) -> bool:
        return False

    def get_resource_inst(self, path: str, environ: dict):
        try:
            hf_path = canon(path)
        except ValueError:
            return None
        # Hide the placeholder file completely.
        if hf_path.rsplit("/", 1)[-1] == PLACEHOLDER_NAME:
            return None
        try:
            entry = self.cache.get(hf_path)
        except FileNotFoundError:
            return None
        if entry is None:
            return None
        if entry.is_dir:
            return HfFolderResource(path, environ, entry, self)
        return HfFileResource(path, environ, entry, self)

    # called from HfFileResource.end_write to remove the placeholder once a
    # real file has been written into a previously-empty directory
    def try_clear_placeholder(self, dir_path: str, *, token: Optional[str]) -> None:
        with self._placeholder_lock:
            entry = self.cache.get(dir_path)
            if entry is None or not entry.is_dir:
                return
            if PLACEHOLDER_NAME not in entry.children:
                return
            placeholder_path = (
                f"{dir_path}/{PLACEHOLDER_NAME}" if dir_path else PLACEHOLDER_NAME
            )
            try:
                self.api.delete_file(
                    path_in_repo=placeholder_path,
                    repo_id=self.cfg.repo_id,
                    repo_type=self.cfg.repo_type,
                    revision=self.cfg.revision,
                    token=token,
                    commit_message=f"hugdav: drop placeholder in {dir_path}",
                )
                self.cache.remove(placeholder_path)
            except Exception:  # noqa: BLE001
                # Best-effort: the placeholder will simply remain hidden.
                log.debug("placeholder cleanup failed for %s", dir_path,
                          exc_info=True)
