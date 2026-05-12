"""End-to-end WebDAV protocol tests against a live cheroot server."""

import requests


def dav_request(method, url, **kw):
    kw.setdefault("proxies", {"http": None, "https": None})
    return requests.request(method, url, timeout=10, **kw)


def test_options_advertises_dav(live_server):
    base, _ = live_server
    r = dav_request("OPTIONS", base + "/")
    assert r.status_code in (200, 207)
    assert "DAV" in {k.upper() for k in r.headers.keys()}
    assert "1" in r.headers.get("DAV", "")


def test_propfind_root_lists_seed_files(live_server):
    base, _ = live_server
    r = dav_request("PROPFIND", base + "/", headers={"Depth": "1"})
    assert r.status_code == 207
    body = r.text
    assert "hello.txt" in body
    assert "docs" in body
    assert ".hf-webdav-keep" not in body  # placeholder must be hidden


def test_get_file(live_server):
    base, _ = live_server
    r = dav_request("GET", base + "/hello.txt")
    assert r.status_code == 200
    assert r.content == b"hi there\n"


def test_put_creates_file_and_propfind_sees_it(live_server):
    base, api = live_server
    r = dav_request("PUT", base + "/new.txt", data=b"brand new\n")
    assert r.status_code in (201, 204)
    assert api._files["new.txt"] == b"brand new\n"

    r = dav_request("PROPFIND", base + "/", headers={"Depth": "1"})
    assert "new.txt" in r.text

    r = dav_request("GET", base + "/new.txt")
    assert r.content == b"brand new\n"


def test_put_overwrite(live_server):
    base, api = live_server
    dav_request("PUT", base + "/hello.txt", data=b"replaced\n")
    assert api._files["hello.txt"] == b"replaced\n"
    r = dav_request("GET", base + "/hello.txt")
    assert r.content == b"replaced\n"


def test_delete_file(live_server):
    base, api = live_server
    r = dav_request("DELETE", base + "/hello.txt")
    assert r.status_code in (200, 204)
    assert "hello.txt" not in api._files
    r = dav_request("GET", base + "/hello.txt")
    assert r.status_code == 404


def test_mkcol_then_put_inside(live_server):
    base, api = live_server
    r = dav_request("MKCOL", base + "/newdir/")
    assert r.status_code == 201
    # placeholder created on backend
    assert any(p.startswith("newdir/.hf-webdav-keep") for p in api._files)

    r = dav_request("PUT", base + "/newdir/a.txt", data=b"A")
    assert r.status_code in (201, 204)
    assert api._files["newdir/a.txt"] == b"A"
    # placeholder should be cleared once a real file exists
    assert "newdir/.hf-webdav-keep" not in api._files

    # PROPFIND on newdir lists a.txt and not the placeholder
    r = dav_request("PROPFIND", base + "/newdir/", headers={"Depth": "1"})
    assert "a.txt" in r.text
    assert ".hf-webdav-keep" not in r.text


def test_move_file(live_server):
    base, api = live_server
    r = dav_request("MOVE", base + "/hello.txt",
                    headers={"Destination": base + "/renamed.txt",
                             "Overwrite": "T"})
    assert r.status_code in (201, 204)
    assert "hello.txt" not in api._files
    assert api._files["renamed.txt"] == b"hi there\n"


def test_copy_file(live_server):
    base, api = live_server
    r = dav_request("COPY", base + "/hello.txt",
                    headers={"Destination": base + "/copy.txt",
                             "Overwrite": "T"})
    assert r.status_code in (201, 204)
    assert api._files["hello.txt"] == b"hi there\n"
    assert api._files["copy.txt"] == b"hi there\n"


def test_recursive_delete_directory(live_server):
    base, api = live_server
    # Standard WebDAV: parents must be MKCOL'd before PUT can create a child.
    r = dav_request("MKCOL", base + "/docs/sub/")
    assert r.status_code == 201
    r = dav_request("PUT", base + "/docs/sub/inner.txt", data=b"deep")
    assert r.status_code in (201, 204)
    assert "docs/sub/inner.txt" in api._files
    r = dav_request("DELETE", base + "/docs/")
    assert r.status_code in (200, 204)
    assert not any(p.startswith("docs/") for p in api._files)


def test_propfind_404_on_missing(live_server):
    base, _ = live_server
    r = dav_request("PROPFIND", base + "/no-such-file", headers={"Depth": "0"})
    assert r.status_code == 404


def test_lock_unlock(live_server):
    base, _ = live_server
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<D:lockinfo xmlns:D="DAV:">'
        '<D:lockscope><D:exclusive/></D:lockscope>'
        '<D:locktype><D:write/></D:locktype>'
        '<D:owner><D:href>tester</D:href></D:owner>'
        '</D:lockinfo>'
    )
    r = dav_request("LOCK", base + "/hello.txt",
                    data=body, headers={"Content-Type": "application/xml"})
    assert r.status_code == 200
    token = r.headers.get("Lock-Token", "").strip("<>")
    assert token
    r = dav_request("UNLOCK", base + "/hello.txt",
                    headers={"Lock-Token": f"<{token}>"})
    assert r.status_code in (204, 200)
