# hugdav — Mount a Hugging Face repo as a WebDAV drive

`hugdav` runs a small WebDAV server in front of a Hugging Face repository
(dataset / model / space) so you can mount that repo as a network drive
from **Finder**, **Windows Explorer**, **Cyberduck**, **Nextcloud** sync
clients, **rclone** and any other WebDAV-aware tool.

> Use HF as a free-ish, versioned blob storage backend.  Every change is a
> commit, and large files are transparently stored in LFS.

## Features
- Read / write / delete / list (`GET`, `PUT`, `DELETE`, `MKCOL`, `PROPFIND`).
- HTTP `Range` requests (`206 Partial Content` with `Content-Range`).
- Server-side `MOVE` and `COPY` (a single HF commit, no re-upload).
- Recursive directory delete in one commit.
- BASIC auth — pass your **HF token as the password**.
- Per-request token override (multi-tenant friendly).
- In-memory tree cache with TTL + write-through updates.
- Empty directories preserved via a hidden placeholder file
  (`.hf-webdav-keep`) that is **never** exposed to clients.
- Real WsgiDAV lock manager (good enough for Finder / Office save flows).
- Healthcheck endpoints `/healthz` & `/readyz`, SIGTERM-safe shutdown,
  X-Forwarded-* support for TLS-terminating reverse proxies.

## Install
```bash
git clone https://github.com/xiaobao520123/hugdav.git
cd hugdav
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e .
```

## Quick start
1. Create (or pick) a Hugging Face repo to act as your drive — a
   **private dataset** is the usual choice:
   <https://huggingface.co/new-dataset>.
2. Generate a write-enabled token:
   <https://huggingface.co/settings/tokens>.
3. Start the server:
   ```bash
   export HUGDAV_HF_REPO=your-username/my-drive
   export HUGDAV_HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxx
   hugdav --port 8080
   ```
4. Mount it from any client at `http://localhost:8080/`,
   user = anything, password = your HF token (unless you ran with
   `--auth anonymous`).

### macOS Finder
`Go → Connect to Server… → http://localhost:8080`

### rclone
```bash
rclone config create myhf webdav \
    url=http://localhost:8080 vendor=other \
    user=me pass=$(rclone obscure $HUGDAV_HF_TOKEN)
rclone ls myhf:
rclone copy ./photos myhf:photos
```

### Nextcloud (external storage)
- *Storage type*: WebDAV
- *URL*: `http://your-server:8080`
- *Username*: any
- *Password*: HF token

## Configuration

| Env var               | CLI flag         | Default     | Description                                  |
| --------------------- | ---------------- | ----------- | -------------------------------------------- |
| `HUGDAV_HF_REPO`      | `--repo`         | *required*  | `user/repo` on Hugging Face                  |
| `HUGDAV_HF_REPO_TYPE` | `--repo-type`    | `dataset`   | `dataset`, `model`, or `space`               |
| `HUGDAV_HF_REVISION`  | `--revision`     | `main`      | Branch to write to                           |
| `HUGDAV_HF_TOKEN`     | `--token`        | unset       | Default HF token (BASIC auth still preferred)|
| `HUGDAV_HOST`         | `--host`         | `0.0.0.0`   | Bind host                                    |
| `HUGDAV_PORT`         | `--port`         | `8080`      | Bind port                                    |
| `HUGDAV_CACHE_TTL`    | `--cache-ttl`    | `30`        | Tree cache TTL (seconds)                     |
| `HUGDAV_AUTH`         | `--auth`         | `token`     | `token` (BASIC) or `anonymous`               |

## Architecture
```
WebDAV client ──HTTP──▶ cheroot ──WSGI──▶ WsgiDAV ──▶ HfProvider ──▶ huggingface_hub
                                                          │
                                                          ▼
                                                  HfTreeCache (TTL + write-through)
```

- **`HfTreeCache`** loads the entire repo file tree once via
  `HfApi.list_repo_tree(recursive=True)` and refreshes after the TTL
  (default 30s).  Every successful write updates the cache in place so the
  client sees its own write immediately.
- **`HfProvider`** maps each WebDAV path to either an `HfFileResource`
  (`GET`/`PUT`/`DELETE`) or `HfFolderResource` (`PROPFIND`/`MKCOL` etc.).
- **`MOVE` / `COPY`** are implemented as a single
  `HfApi.create_commit([CommitOperationCopy, CommitOperationDelete?])`,
  i.e. zero re-uploads.
- **Recursive `DELETE`** issues one commit with N
  `CommitOperationDelete` operations.
- **Empty directories** materialise on disk as a `.hf-webdav-keep`
  placeholder — filtered out of every WebDAV view and removed lazily
  when a real file appears.

## Tests
```bash
pip install pytest
python -m pytest tests/ -q
```
The test suite spins up a live cheroot server against an in-memory
`FakeHfApi` and exercises the full WebDAV verb set
(`OPTIONS / PROPFIND / GET / PUT / DELETE / MKCOL / MOVE / COPY /
LOCK / UNLOCK`) plus large uploads, depth handling, nested MKCOL,
path-traversal rejection, healthcheck endpoints and the reverse-proxy
header middleware (24 tests, ~10s).

### Real-world client testing
Verified manually with **rclone v1.74.1** against both the in-memory fake
backend and the public HF dataset
[`hf-internal-testing/dataset_with_script`](https://huggingface.co/datasets/hf-internal-testing/dataset_with_script):

```text
rclone lsf -R hf:                    ✓ lists tree, including subdirs
rclone copy local hf:upload          ✓ uploads files & subdirs (incl. 1.5 MB binary)
rclone copy hf:upload local          ✓ round-trip byte-identical (diff -r empty)
rclone sync local hf:syncbox         ✓ deletes removed files, adds new ones
rclone moveto / copyto / purge       ✓ server-side single-commit
rclone cat hf:other_text.txt         ✓ streams real HF content
```

Additional `curl`-driven scenarios passed:

```text
0-byte PUT / GET                     ✓
Range requests (206 Partial)         ✓ Content-Range correctly set
If-None-Match → 304                  ✓ ETags from HF blob oid
PROPPATCH dead properties            ✓ persisted in-memory
Concurrent PUT (5x)                  ✓ last-write-wins, no 5xx
Percent-encoded paths                ✓ "with%20space.txt" round-trips
Unicode + emoji filenames            ✓ "你好.txt", "🚀.txt"
Deep MKCOL (a/b/c/d/e/)              ✓ nested directory creation
```

## Docker / Cloud deployment

### Quick: bare image
```bash
docker build -t hugdav:latest .
docker run -d --name hugdav -p 8080:8080 \
    -e HUGDAV_HF_REPO=user/repo \
    -e HUGDAV_HF_TOKEN=hf_xxx \
    -v hugdav-cache:/data/hf-cache \
    hugdav:latest
```

The image (Python 3.12-slim base, ~150 MB) ships with:
- `HEALTHCHECK` that calls `/healthz`
- non-root `hugdav` user (uid 10001)
- HF cache mounted as a volume so blob downloads survive restarts

### Stack: hugdav + Caddy auto-TLS
The repo includes `docker-compose.yml`, `deploy/Caddyfile` and an
`.env`-driven setup. After pointing your DNS at the host:

```bash
cp deploy/hugdav.env.example .env   # then edit
docker compose up -d
```

Caddy obtains a Let's Encrypt cert and reverse-proxies HTTPS →
`hugdav:8080`, forwarding `X-Forwarded-Proto/Host/For`. hugdav's
`proxy_fix` middleware honours those headers so WebDAV `MOVE` /
`COPY` requests with `https://…` Destination URLs work correctly
(otherwise WsgiDAV rejects them with 502 Bad Gateway).

### Bare-metal: systemd
`deploy/hugdav.service` ships a hardened unit (NoNewPrivileges,
ProtectSystem=strict, etc.). Together with `deploy/hugdav.env.example`:

```bash
sudo useradd -r -s /usr/sbin/nologin -d /opt/hugdav hugdav
sudo install -d -o hugdav /opt/hugdav /var/cache/hugdav
sudo -u hugdav python3 -m venv /opt/hugdav/.venv
sudo -u hugdav /opt/hugdav/.venv/bin/pip install /path/to/hugdav
sudo cp deploy/hugdav.env.example /etc/hugdav.env  # then edit
sudo cp deploy/hugdav.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hugdav
```

### Operational endpoints

| Path        | Auth | Purpose                                                       |
| ----------- | ---- | ------------------------------------------------------------- |
| `/healthz`  | none | Liveness — `200 {"status":"ok"}` if the process is alive      |
| `/readyz`   | none | Readiness — `200` once the HF tree has loaded at least once   |
| `/`         | DAV  | All WebDAV traffic                                            |

Healthcheck endpoints are intentionally unauthenticated so Kubernetes /
Docker / load-balancers can probe them without sharing the HF token.

## Limitations
- Atomic concurrency relies on HF Hub; running several `hugdav`
  instances against the same repo + revision may race.
- `LOCK` storage is in-memory; restarting the server forgets locks.
- Each write is a Hugging Face commit; chatty workloads
  (e.g. unattended Office autosave) may hit your push-rate quota.
- HF Hub does not support `Range` on its origin servers; we serve
  `Range` from the locally-cached blob, which means the first byte
  triggers a full download. Subsequent ranges are then cheap.

## License
See [LICENSE](./LICENSE).
