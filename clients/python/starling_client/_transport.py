"""HTTP transport. Errors are mapped to starling_client.errors; messages omit url and token."""
from __future__ import annotations

import httpx

from . import errors

PROTOCOL = 1


class Transport:
    def __init__(self, url: str, token: str, timeout_s: float = 30.0, *, _httpx_transport=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.Client(
            base_url=url, headers=headers, timeout=timeout_s, transport=_httpx_transport
        )

    def __repr__(self) -> str:
        return "Transport(...)"

    def close(self) -> None:
        self._client.close()

    def post(self, path: str, json: dict | None = None) -> dict:
        return self._request("POST", path, json=json)

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    def _request(self, method: str, path: str, **kw) -> dict:
        try:
            resp = self._client.request(method, path, **kw)
        except httpx.TimeoutException:
            raise errors.ServiceTimeout("service request timed out") from None
        except httpx.TransportError:
            raise errors.ServiceUnavailable("service unreachable") from None
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
