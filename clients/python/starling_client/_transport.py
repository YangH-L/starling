"""HTTP transport. Errors are mapped to starling_client.errors; messages omit url and token."""
from __future__ import annotations

import httpx

from . import errors

PROTOCOL = 1

_NOT_JSON = object()


def _json_or(resp: httpx.Response, error_value):
    """resp 的 JSON；不是 JSON 时 2xx 回 _NOT_JSON、4xx/5xx 回 error_value（None：from_body 按状态码定码）。在 except 块
    之外返回，不留异常链。"""
    try:
        return resp.json()
    except ValueError:
        pass
    return error_value if resp.status_code >= 400 else _NOT_JSON


class Transport:
    def __init__(self, url: str, token: str, timeout_s: float = 30.0, *, _httpx_transport=None):
        self.timeout_s = float(timeout_s)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        # 需求 5.5（修复轮 1 F5）：httpx 的异常文本带 url 或 token。在 except 块里只记下「出错了」，到块外再抛：这样新异常的
        # __context__ 是 None（`raise … from None` 只是不打印它，调用方照样读得到）。
        client = None
        try:
            client = httpx.Client(
                base_url=url, headers=headers, timeout=timeout_s, transport=_httpx_transport
            )
        except (httpx.InvalidURL, UnicodeError, ValueError):
            pass
        if client is None:
            raise errors.ServiceError("invalid client configuration", code="invalid_config")
        self._client = client

    def __repr__(self) -> str:
        return "Transport(...)"

    def close(self) -> None:
        self._client.close()

    def post(self, path: str, json: dict | None = None, *, timeout_s: float | None = None) -> dict:
        """timeout_s overrides the client timeout for this one request (e.g. a handle close that
        waits on the service side for in-flight calls)."""
        if timeout_s is None:
            return self._request("POST", path, json=json)
        return self._request("POST", path, json=json, timeout=timeout_s)

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    def _request(self, method: str, path: str, **kw) -> dict:
        # 需求 5.5（修复轮 1 F5）：except 块里只记下失败的类别，块外再抛，新异常的 __context__ 与 __cause__ 都是 None，
        # httpx 那个带 url / token 的异常不挂在链上。
        resp = None
        failure: errors.StarlingClientError | None = None
        try:
            resp = self._client.request(method, path, **kw)
        except httpx.TimeoutException:
            failure = errors.ServiceTimeout("service request timed out")
        except httpx.TransportError:
            failure = errors.ServiceUnavailable("service unreachable")
        except (httpx.HTTPError, httpx.InvalidURL, UnicodeError):
            # DecodingError, TooManyRedirects, bad URL/header text: fixed text, no chain.
            failure = errors.ServiceError("service request failed", code="bad_request")
        if failure is not None:
            raise failure
        assert resp is not None
        body = _json_or(resp, None)
        if resp.status_code >= 400:
            raise errors.from_body(resp.status_code, body)
        if body is _NOT_JSON:
            raise errors.ServiceError("invalid service response", code="bad_response")
        return body if isinstance(body, dict) else {"result": body}
