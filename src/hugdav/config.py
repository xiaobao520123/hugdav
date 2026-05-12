"""Configuration loaded from environment variables / kwargs."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


PLACEHOLDER_NAME = ".hf-webdav-keep"
"""Internal placeholder file used to materialise empty directories on HF.

Filtered from every WebDAV view so clients never see it.
"""


@dataclass
class Config:
    repo_id: str
    repo_type: str = "dataset"
    revision: str = "main"
    token: Optional[str] = None
    host: str = "0.0.0.0"
    port: int = 8080
    cache_ttl: float = 30.0
    auth_mode: str = "token"  # "token" | "anonymous"
    realm: str = "hugdav"
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "Config":
        e = env if env is not None else os.environ
        repo_id = e.get("HUGDAV_HF_REPO")
        if not repo_id:
            raise RuntimeError(
                "HUGDAV_HF_REPO is required (e.g. 'username/my-drive')"
            )
        return cls(
            repo_id=repo_id,
            repo_type=e.get("HUGDAV_HF_REPO_TYPE", "dataset"),
            revision=e.get("HUGDAV_HF_REVISION", "main"),
            token=e.get("HUGDAV_HF_TOKEN") or None,
            host=e.get("HUGDAV_HOST", "0.0.0.0"),
            port=int(e.get("HUGDAV_PORT", "8080")),
            cache_ttl=float(e.get("HUGDAV_CACHE_TTL", "30")),
            auth_mode=e.get("HUGDAV_AUTH", "token"),
            realm=e.get("HUGDAV_REALM", "hugdav"),
        )
