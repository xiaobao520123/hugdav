"""Extra tests covering range, large uploads, deep dirs, and listing depth."""

import os

import requests


def dav(method, url, **kw):
    kw.setdefault("proxies", {"http": None, "https": None})
    return requests.request(method, url, timeout=15, **kw)


def test_propfind_depth_0(live_server):
    base, _ = live_server
    r = dav("PROPFIND", base + "/", headers={"Depth": "0"})
    assert r.status_code == 207
    assert "hello.txt" not in r.text  # only root reported


def test_propfind_depth_infinity(live_server):
    base, _ = live_server
    r = dav("PROPFIND", base + "/", headers={"Depth": "infinity"})
    assert r.status_code == 207
    assert "hello.txt" in r.text
    assert "readme.md" in r.text


def test_put_overwrite_changes_size(live_server):
    base, api = live_server
    dav("PUT", base + "/sized.txt", data=b"a" * 100)
    assert api._files["sized.txt"] == b"a" * 100
    dav("PUT", base + "/sized.txt", data=b"a" * 5)
    assert api._files["sized.txt"] == b"a" * 5
    r = dav("GET", base + "/sized.txt")
    assert int(r.headers.get("Content-Length", "0")) == 5


def test_large_upload(live_server):
    base, api = live_server
    big = os.urandom(2_500_000)  # 2.5 MB
    r = dav("PUT", base + "/big.bin", data=big)
    assert r.status_code in (201, 204)
    assert api._files["big.bin"] == big
    r = dav("GET", base + "/big.bin")
    assert r.content == big


def test_mkcol_nested(live_server):
    base, api = live_server
    r = dav("MKCOL", base + "/a/")
    assert r.status_code == 201
    r = dav("MKCOL", base + "/a/b/")
    assert r.status_code == 201
    # deeper PUT should now succeed
    r = dav("PUT", base + "/a/b/c.txt", data=b"deep")
    assert r.status_code in (201, 204)
    assert api._files["a/b/c.txt"] == b"deep"


def test_mkcol_when_parent_missing_returns_409(live_server):
    base, _ = live_server
    r = dav("MKCOL", base + "/no/such/parent/")
    assert r.status_code == 409


def test_path_traversal_rejected(live_server):
    base, _ = live_server
    # The HTTP layer normalises ../ — this test mostly ensures we don't 500.
    r = dav("GET", base + "/../etc/passwd")
    assert r.status_code in (400, 403, 404)


def test_copy_directory(live_server):
    base, api = live_server
    r = dav("COPY", base + "/docs/",
            headers={"Destination": base + "/docs-copy/", "Overwrite": "T"})
    assert r.status_code in (201, 204)
    assert api._files.get("docs-copy/readme.md") == b"# readme\n"
    # original still present
    assert api._files.get("docs/readme.md") == b"# readme\n"


def test_move_directory(live_server):
    base, api = live_server
    r = dav("MOVE", base + "/docs/",
            headers={"Destination": base + "/archive/", "Overwrite": "T"})
    assert r.status_code in (201, 204)
    assert api._files.get("archive/readme.md") == b"# readme\n"
    assert "docs/readme.md" not in api._files
