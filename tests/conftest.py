import os
import sys
import threading
import time
from base64 import b64encode

import pytest
import requests

# Make src/ importable
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from hugdav.config import Config  # noqa: E402
from hugdav import provider as provider_mod  # noqa: E402
from hugdav import server as server_mod  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_hf import FakeHfApi, patched_hf_hub_download_factory  # noqa: E402


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def fake_api(monkeypatch):
    api = FakeHfApi(repo_id="test/repo")
    # Seed repo with one file in root and one inside a subdir.
    api._files["hello.txt"] = b"hi there\n"
    api._files["docs/readme.md"] = b"# readme\n"
    monkeypatch.setattr(provider_mod, "hf_hub_download",
                        patched_hf_hub_download_factory(api))
    return api


@pytest.fixture
def cfg():
    return Config(
        repo_id="test/repo", repo_type="dataset", revision="main",
        token="t-default", host="127.0.0.1", port=_free_port(),
        cache_ttl=0.5, auth_mode="anonymous",
    )


@pytest.fixture
def live_server(fake_api, cfg):
    prov = provider_mod.HfProvider(cfg, api=fake_api)
    server = server_mod.serve(cfg, provider=prov)
    t = threading.Thread(target=server.start, daemon=True)
    t.start()
    # Wait for the server to accept connections.
    base = f"http://{cfg.host}:{cfg.port}"
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            requests.request("OPTIONS", base + "/", timeout=0.5,
                             proxies={"http": None, "https": None})
            break
        except Exception:
            time.sleep(0.1)
    yield base, fake_api
    server.stop()
