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
    _assert_clean(ei.value)                       # 修复轮 2 N4：JSON 解析失败不挂在链上


def test_non_json_2xx_is_bad_response_without_a_chain():
    with pytest.raises(E.ServiceError) as ei:
        make(lambda r: httpx.Response(200, text="<html>ok</html>")).post("/p", {})
    assert ei.value.code == "bad_response"
    _assert_clean(ei.value)


URL_SECRET = "10.9.8.7:4242/secret-path"
TOKEN_SECRET = "secret-tok"


def _chain(exc):
    """exc 与它 __cause__ / __context__ 两条链上的全部异常（去重，防环）。"""
    seen, todo = [], [exc]
    while todo:
        item = todo.pop()
        if item is None or any(item is other for other in seen):
            continue
        seen.append(item)
        todo += [item.__cause__, item.__context__]
    return seen


def _assert_clean(exc):
    """需求 5.5（修复轮 1 F5）：url 与 token 不出现在异常本身，也不出现在 __cause__ / __context__ 链上的任何一个异常里
    （`from None` 只是不打印 __context__，调用方仍能读到它）。"""
    import traceback

    assert exc.__cause__ is None and exc.__context__ is None
    for item in _chain(exc):
        text = "".join(traceback.format_exception(type(item), item, item.__traceback__))
        for s in (str(item), repr(item), text, repr(item.args)):
            assert URL_SECRET not in s and TOKEN_SECRET not in s, type(item).__name__


@pytest.mark.parametrize(
    "exc_factory,cls",
    [
        (lambda: httpx.ConnectError(f"refused {URL_SECRET} {TOKEN_SECRET}"), E.ServiceUnavailable),
        (lambda: httpx.ReadTimeout(f"slow {URL_SECRET} {TOKEN_SECRET}"), E.ServiceTimeout),
        (lambda: httpx.DecodingError(f"bad {URL_SECRET} {TOKEN_SECRET}"), E.ServiceError),
        (lambda: httpx.TooManyRedirects(f"loop {URL_SECRET} {TOKEN_SECRET}"), E.ServiceError),
        (lambda: httpx.InvalidURL(f"bad url {URL_SECRET} {TOKEN_SECRET}"), E.ServiceError),
        (lambda: UnicodeEncodeError("ascii", TOKEN_SECRET, 0, 1, URL_SECRET), E.ServiceError),
    ],
)
def test_errors_do_not_leak_url_or_token(exc_factory, cls):
    def h(req):
        raise exc_factory()

    with pytest.raises(cls) as ei:
        make(h).post("/p", {})
    _assert_clean(ei.value)


def test_constructor_invalid_url_is_wrapped(monkeypatch):
    def boom(*a, **k):
        raise httpx.InvalidURL(f"bad {URL_SECRET} {TOKEN_SECRET}")

    monkeypatch.setattr(httpx, "Client", boom)
    with pytest.raises(E.ServiceError) as ei:
        Transport("http://x", TOKEN_SECRET, 1.0)
    _assert_clean(ei.value)


def test_non_ascii_token_is_wrapped_without_leak():
    with pytest.raises(E.StarlingClientError) as ei:
        t = Transport("http://svc.invalid", "tok\u00e9n-" + TOKEN_SECRET, 1.0,
                      _httpx_transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
        t.get("/p", None)
    _assert_clean(ei.value)


def test_timeout_is_passed_to_httpx():
    t = make(lambda r: httpx.Response(200, json={}))
    assert t._client.timeout == httpx.Timeout(1.0)
    t2 = Transport("http://svc.invalid", "tok", 7.5, _httpx_transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    assert t2._client.timeout == httpx.Timeout(7.5)


def test_non_json_4xx_gets_http_status_code():
    with pytest.raises(E.StarlingClientError) as ei:
        make(lambda r: httpx.Response(404, text="nope")).get("/p", None)
    assert ei.value.code == "http_404" and ei.value.transient is False
    _assert_clean(ei.value)


def test_200_non_json_is_service_error():
    with pytest.raises(E.ServiceError) as ei:
        make(lambda r: httpx.Response(200, text="<html>")).get("/p", None)
    assert ei.value.code == "bad_response"


def test_200_list_is_wrapped():
    assert make(lambda r: httpx.Response(200, json=[1, 2])).get("/p", None) == {"result": [1, 2]}


def test_client_has_no_starling_core_import():
    code = (
        "import sys, starling_client, starling_client._transport, starling_client.types;"
        "bad=[m for m in sys.modules if m=='starling' or m.startswith('starling.') or m=='starling._core'];"
        "sys.exit(1 if bad else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0
