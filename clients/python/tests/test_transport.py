import subprocess
import sys

import httpx
import pytest

from starling_client import errors as E
from starling_client._transport import Transport


def make(handler, **kw):
    return Transport("http://svc.invalid", "tok", 1.0, _httpx_transport=httpx.MockTransport(handler), **kw)


def test_post_get_roundtrip_and_auth_header():
    seen = {}

    def h(req):
        seen["auth"] = req.headers.get("authorization")
        seen["q"] = dict(req.url.params)
        return httpx.Response(200, json={"ok": True, "m": req.method})

    t = make(h)
    assert t.post("/api/x", {"a": 1}) == {"ok": True, "m": "POST"}
    assert t.get("/api/y", {"k": "v"})["m"] == "GET"
    assert seen["auth"] == "Bearer tok" and seen["q"] == {"k": "v"}


def test_connect_refused_is_service_unavailable_transient():
    def h(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(E.ServiceUnavailable) as ei:
        make(h).post("/p", {})
    assert ei.value.transient is True


def test_timeout_is_service_timeout():
    def h(req):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(E.ServiceTimeout) as ei:
        make(h).get("/p", None)
    assert ei.value.transient is True


@pytest.mark.parametrize("transient", [True, False])
def test_5xx_is_service_error_with_transient_from_body(transient):
    def h(req):
        return httpx.Response(500, json={"code": "internal_error", "transient": transient, "detail": "internal_error"})

    with pytest.raises(E.ServiceError) as ei:
        make(h).post("/p", {})
    assert ei.value.transient is transient


def test_4xx_body_maps_via_from_body():
    def h(req):
        return httpx.Response(409, json={"code": "event_conflict", "transient": False, "detail": "event_conflict"})

    with pytest.raises(E.EventConflict):
        make(h).post("/p", {})


def test_non_json_5xx_is_transient_service_error():
    with pytest.raises(E.ServiceError) as ei:
        make(lambda r: httpx.Response(502, text="bad gateway")).get("/p", None)
    assert ei.value.transient is True


def test_errors_do_not_leak_url_or_token():
    t = Transport(url="http://127.0.0.1:1/x", token="secret-tok", timeout_s=1.0)
    with pytest.raises(E.StarlingClientError) as ei:
        t.post("/p", {})
    for s in (str(ei.value), repr(ei.value)):
        assert "127.0.0.1:1" not in s and "secret-tok" not in s


def test_client_has_no_starling_core_import():
    code = (
        "import sys, starling_client, starling_client._transport, starling_client.types;"
        "bad=[m for m in sys.modules if m=='starling' or m.startswith('starling.') or m=='starling._core'];"
        "sys.exit(1 if bad else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
