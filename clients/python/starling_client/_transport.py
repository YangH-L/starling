"""HTTP transport. Errors are mapped to starling_client.errors; messages omit url and token."""
from __future__ import annotations

import httpx

from . import errors

PROTOCOL = 1


class Transport:
    def __init__(self, url: str, token: str, timeout_s: float = 30.0, *, _httpx_transport=None):
        self.timeout_s = float(timeout_s)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            self._client = httpx.Client(
                base_url=url, headers=headers, timeout=timeout_s, transport=_httpx_transport
            )
        except (httpx.InvalidURL, UnicodeError, ValueError):
            raise errors.ServiceError("invalid client configuration", code="invalid_config") from None

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
        try:
            resp = self._client.request(method, path, **kw)
        except httpx.TimeoutException:
            raise errors.ServiceTimeout("service request timed out") from None
        except httpx.TransportError:
            raise errors.ServiceUnavailable("service unreachable") from None
        except (httpx.HTTPError, httpx.InvalidURL, UnicodeError):
            # DecodingError, TooManyRedirects, bad URL/header text: fixed text, no cause chain.
            raise errors.ServiceError("service request failed", code="bad_request") from None
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = None
            raise errors.from_body(resp.status_code, body)
        try:
            data = resp.json()
        except ValueError:
            raise errors.ServiceError("invalid service response", code="bad_response") from None
        return data if isinstance(data, dict) else {"result": data}
