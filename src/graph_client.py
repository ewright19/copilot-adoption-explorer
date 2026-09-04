"""Minimal, resilient Microsoft Graph client (app-only) for the Copilot Adoption Explorer."""
from __future__ import annotations

import threading
import time
from typing import Any, Iterable, Iterator

import requests

GRAPH = "https://graph.microsoft.com"
_RETRY_STATUS = {429, 500, 502, 503, 504}


class GraphError(RuntimeError):
    def __init__(self, status: int, url: str, body: str):
        self.status, self.url, self.body = status, url, body
        super().__init__(f"HTTP {status} for {url}: {body[:500]}")


class GraphClient:
    """Client-credentials Graph client with token caching, throttle-aware retry and $batch."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str, max_retries: int = 6):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.max_retries = max_retries
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = threading.Lock()
        self._local = threading.local()

    # ---------- auth ----------
    @property
    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            self._local.session = s
        return s

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expires_at - 120:
                return self._token
            resp = requests.post(
                f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": f"{GRAPH}/.default",
                    "grant_type": "client_credentials",
                },
                timeout=60,
            )
            if resp.status_code != 200:
                raise GraphError(resp.status_code, "token", resp.text)
            data = resp.json()
            self._token = data["access_token"]
            self._expires_at = time.time() + int(data.get("expires_in", 3600))
            return self._token

    # ---------- core request ----------
    def _request(self, method: str, url: str, *, json_body: Any = None,
                 headers: dict | None = None, tolerate: Iterable[int] = ()) -> requests.Response:
        if url.startswith("/"):
            url = GRAPH + url
        tolerate = set(tolerate)
        delay = 2.0
        last: requests.Response | None = None
        for attempt in range(self.max_retries):
            h = {"Authorization": f"Bearer {self._access_token()}",
                 "Accept": "application/json",
                 "ConsistencyLevel": "eventual"}
            if headers:
                h.update(headers)
            resp = self._session.request(method, url, json=json_body, headers=h, timeout=180)
            last = resp
            if resp.status_code < 400 or resp.status_code in tolerate:
                return resp
            if resp.status_code in _RETRY_STATUS and attempt < self.max_retries - 1:
                wait = float(resp.headers.get("Retry-After") or delay)
                time.sleep(min(wait, 90))
                delay = min(delay * 2, 60)
                continue
            if resp.status_code == 401 and attempt == 0:
                # force token refresh once
                with self._lock:
                    self._token = None
                continue
            raise GraphError(resp.status_code, url, resp.text)
        assert last is not None
        raise GraphError(last.status_code, url, last.text)

    def get_json(self, url: str, *, tolerate: Iterable[int] = ()) -> dict:
        resp = self._request("GET", url, tolerate=tolerate)
        if resp.status_code >= 400:
            return {"_error": resp.status_code, "_body": resp.text}
        if not resp.content:
            return {}
        return resp.json()

    def paged(self, url: str, *, tolerate: Iterable[int] = (), cap: int | None = None) -> Iterator[dict]:
        """Yield every item across @odata.nextLink pages."""
        seen = 0
        while url:
            data = self.get_json(url, tolerate=tolerate)
            if "_error" in data:
                return
            for item in data.get("value", []):
                yield item
                seen += 1
                if cap is not None and seen >= cap:
                    return
            url = data.get("@odata.nextLink") or ""

    # ---------- $batch ----------
    def batch(self, requests_list: list[dict], version: str = "v1.0") -> dict[str, dict]:
        """Run up to 20 GETs per batch. Returns {request id: response body-or-error}."""
        out: dict[str, dict] = {}
        for i in range(0, len(requests_list), 20):
            chunk = requests_list[i:i + 20]
            payload = {"requests": [{"id": r["id"], "method": "GET", "url": r["url"]} for r in chunk]}
            resp = self._request("POST", f"{GRAPH}/{version}/$batch", json_body=payload)
            for r in resp.json().get("responses", []):
                status = r.get("status", 500)
                body = r.get("body") or {}
                if status == 429:
                    # retry this single one inline
                    time.sleep(5)
                    orig = next((c for c in chunk if c["id"] == r["id"]), None)
                    if orig:
                        body = self.get_json(f"{GRAPH}/{version}{orig['url']}", tolerate=(400, 403, 404))
                        status = 200 if "_error" not in body else body["_error"]
                out[str(r["id"])] = {"status": status, "body": body}
        return out
