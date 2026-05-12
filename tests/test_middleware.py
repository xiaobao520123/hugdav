"""Tests for /healthz, /readyz and X-Forwarded-* middleware."""

import requests


def dav(method, url, **kw):
    kw.setdefault("proxies", {"http": None, "https": None})
    return requests.request(method, url, timeout=10, **kw)


def test_healthz_always_200(live_server):
    base, _ = live_server
    r = dav("GET", base + "/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_readyz_after_first_use(live_server):
    base, _ = live_server
    # The cache is loaded eagerly by HfTreeCache.refresh on first PROPFIND.
    dav("PROPFIND", base + "/", headers={"Depth": "0"})
    r = dav("GET", base + "/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["repo"] == "test/repo"


def test_proxy_fix_allows_https_destination_for_move(live_server):
    base, api = live_server
    # Simulate a TLS-terminating reverse proxy: client used https; we
    # forward to hugdav over http with X-Forwarded-Proto.
    https_dest = base.replace("http://", "https://") + "/renamed.txt"
    r = dav("MOVE", base + "/hello.txt",
            headers={"Destination": https_dest, "Overwrite": "T",
                     "X-Forwarded-Proto": "https"})
    assert r.status_code in (201, 204)
    assert "renamed.txt" in api._files
    assert "hello.txt" not in api._files
