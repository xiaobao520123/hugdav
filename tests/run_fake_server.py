"""Run a hugdav server backed by the in-memory FakeHfApi.

Usage:
    python tests/run_fake_server.py [--port 8080]

Convenience entry-point used for manual / rclone smoke tests.
"""

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from fake_hf import FakeHfApi, patched_hf_hub_download_factory  # noqa: E402

from hugdav import provider as provider_mod  # noqa: E402
from hugdav import server as server_mod  # noqa: E402
from hugdav.config import Config  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--auth", default="anonymous", choices=["token", "anonymous"])
    ap.add_argument("--cache-ttl", type=float, default=2.0)
    args = ap.parse_args()

    api = FakeHfApi(repo_id="test/repo")
    api._files["hello.txt"] = b"hello fake hugdav\n"
    api._files["docs/readme.md"] = b"# readme\n"
    api._files["docs/notes/todo.md"] = b"- [ ] iterate\n"

    provider_mod.hf_hub_download = patched_hf_hub_download_factory(api)
    cfg = Config(
        repo_id="test/repo", repo_type="dataset", revision="main",
        host=args.host, port=args.port, token="fake-token",
        auth_mode=args.auth, cache_ttl=args.cache_ttl,
    )
    prov = provider_mod.HfProvider(cfg, api=api)
    srv = server_mod.serve(cfg, provider=prov)

    print(f"Fake hugdav listening on http://{args.host}:{args.port}", flush=True)
    print(f"Seeded files: {sorted(api._files)}", flush=True)

    try:
        srv.start()
    except KeyboardInterrupt:
        srv.stop()


if __name__ == "__main__":
    main()
