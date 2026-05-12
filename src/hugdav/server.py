"""Server entry point: assembles WsgiDAVApp + cheroot."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from typing import Callable, Optional, Tuple

from cheroot import wsgi
from huggingface_hub.errors import RepositoryNotFoundError
from wsgidav.dc.base_dc import BaseDomainController
from wsgidav.wsgidav_app import WsgiDAVApp

from . import __version__
from .config import Config
from .provider import HfProvider


log = logging.getLogger("hugdav")


# ---------------------------------------------------------------------------
# Auth: BASIC where the password *is* the HF token.
# ---------------------------------------------------------------------------


class HfTokenDC(BaseDomainController):
    def __init__(self, wsgidav_app, config):
        super().__init__(wsgidav_app, config)
        self.cfg: Config = config["hugdav"]["config"]

    def get_domain_realm(self, path_info, environ):  # noqa: ARG002
        return self.cfg.realm

    def require_authentication(self, realm, environ):  # noqa: ARG002
        return self.cfg.auth_mode != "anonymous"

    def basic_auth_user(self, realm, user_name, password, environ):  # noqa: ARG002
        if self.cfg.auth_mode == "anonymous":
            environ["hugdav.token"] = self.cfg.token
            return True
        if not password:
            return False
        environ["hugdav.token"] = password
        environ["wsgidav.auth.user_name"] = user_name or "hf"
        return True

    def supports_http_digest_auth(self):
        return False


# ---------------------------------------------------------------------------
# WSGI middleware
# ---------------------------------------------------------------------------


def health_middleware(app: Callable, *, provider: HfProvider) -> Callable:
    """/healthz (always 200) + /readyz (200 once cache loaded)."""

    def _wrapped(environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path == "/healthz":
            body = json.dumps({"status": "ok",
                               "version": __version__}).encode()
            start_response("200 OK",
                           [("Content-Type", "application/json"),
                            ("Content-Length", str(len(body)))])
            return [body]
        if path == "/readyz":
            ready = provider.cache._loaded_at > 0
            status = "200 OK" if ready else "503 Service Unavailable"
            body = json.dumps({
                "ready": ready,
                "repo": provider.cfg.repo_id,
                "revision": provider.cfg.revision,
            }).encode()
            start_response(status,
                           [("Content-Type", "application/json"),
                            ("Content-Length", str(len(body)))])
            return [body]
        return app(environ, start_response)

    return _wrapped


def proxy_fix(app: Callable) -> Callable:
    """Honour X-Forwarded-Proto / X-Forwarded-Host / X-Forwarded-For.

    Required when running behind a TLS-terminating reverse proxy (Caddy,
    Nginx, Cloudflare Tunnel, …); otherwise WsgiDAV rejects MOVE/COPY
    because the Destination URL scheme/host don't match the WSGI environ.
    """

    def _wrapped(environ, start_response):
        proto = environ.get("HTTP_X_FORWARDED_PROTO")
        if proto:
            environ["wsgi.url_scheme"] = proto.split(",")[0].strip()
        host = environ.get("HTTP_X_FORWARDED_HOST")
        if host:
            environ["HTTP_HOST"] = host.split(",")[0].strip()
        for_ = environ.get("HTTP_X_FORWARDED_FOR")
        if for_:
            environ["REMOTE_ADDR"] = for_.split(",")[0].strip()
        return app(environ, start_response)

    return _wrapped


# ---------------------------------------------------------------------------
# App / server assembly
# ---------------------------------------------------------------------------


def make_app(cfg: Config, *, provider: Optional[HfProvider] = None
             ) -> Tuple[Callable, HfProvider]:
    """Build the full WSGI app stack and return (app, provider)."""
    provider = provider or HfProvider(cfg)
    dav_config = {
        "host": cfg.host,
        "port": cfg.port,
        "provider_mapping": {"/": provider},
        "http_authenticator": {
            "domain_controller": HfTokenDC,
            "accept_basic": True,
            "accept_digest": False,
            "default_to_digest": False,
            "trusted_auth_header": None,
        },
        "simple_dc": {"user_mapping": {"*": True}},
        "verbose": 1,
        "logging": {"enable": False, "enable_loggers": []},
        "lock_storage": True,
        "property_manager": True,
        "dir_browser": {
            "enable": True, "icon": False, "response_trailer": False,
            "show_user": True, "show_logout": False, "davmount": False,
            "ms_sharepoint_support": False, "libre_office_support": False,
        },
        "hugdav": {"config": cfg},
    }
    dav = WsgiDAVApp(dav_config)
    app = health_middleware(proxy_fix(dav), provider=provider)
    return app, provider


def serve(cfg: Config, *, provider: Optional[HfProvider] = None,
          warmup: bool = True) -> wsgi.Server:
    app, provider = make_app(cfg, provider=provider)
    if warmup:
        try:
            provider.cache.refresh(force=True)
            log.info("repo tree loaded: %d top-level entries",
                     len(provider.cache._root.children))
        except (FileNotFoundError, RepositoryNotFoundError) as exc:
            raise SystemExit(f"hugdav: HF repo not reachable: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.warning("initial repo load failed (%s); will retry on demand",
                        exc)

    server = wsgi.Server(
        bind_addr=(cfg.host, cfg.port),
        wsgi_app=app,
        server_name=f"hugdav/{__version__}",
        request_queue_size=64,
        timeout=60,
    )
    log.info("hugdav WebDAV serving on http://%s:%d  (repo=%s type=%s rev=%s)",
             cfg.host, cfg.port, cfg.repo_id, cfg.repo_type, cfg.revision)
    return server


def _install_signal_handlers(server: wsgi.Server) -> None:
    def _stop(signum, frame):  # noqa: ARG001
        log.info("received signal %d, shutting down…", signum)
        try:
            server.stop()
        except Exception:  # noqa: BLE001
            pass

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="hugdav")
    parser.add_argument("--repo", help="HF repo id (e.g. user/my-drive)")
    parser.add_argument("--repo-type", default=None,
                        choices=["dataset", "model", "space"])
    parser.add_argument("--revision", default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--token", default=None,
                        help="HF token; can also come from BASIC auth password")
    parser.add_argument("--auth", default=None, choices=["token", "anonymous"])
    parser.add_argument("--cache-ttl", type=float, default=None)
    parser.add_argument("--no-warmup", action="store_true",
                        help="skip eager repo-tree load at startup")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose >= 2 else
              logging.INFO if args.verbose == 1 else
              logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("hugdav").setLevel(
        logging.DEBUG if args.verbose >= 2 else logging.INFO,
    )

    cfg = Config.from_env() if args.repo is None else Config(repo_id=args.repo)
    if args.repo_type:  cfg.repo_type = args.repo_type
    if args.revision:   cfg.revision = args.revision
    if args.host:       cfg.host = args.host
    if args.port:       cfg.port = args.port
    if args.token:      cfg.token = args.token
    if args.auth:       cfg.auth_mode = args.auth
    if args.cache_ttl is not None:
        cfg.cache_ttl = args.cache_ttl

    server = serve(cfg, warmup=not args.no_warmup)
    _install_signal_handlers(server)
    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
