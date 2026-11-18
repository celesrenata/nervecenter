"""Minimal async Kubernetes REST client using the in-cluster ServiceAccount.

Only the verbs the IDE manager needs: pods get/list/create/delete,
secrets get/create/delete, events list.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
DEFAULT_NAMESPACE = "workbench"


class KubeError(Exception):
    """Raised for any failed Kubernetes API call."""

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(f"kube api error {status}: {message}")
        self.status = status
        self.message = message


class KubeClient:
    def __init__(
        self,
        namespace: str = DEFAULT_NAMESPACE,
        *,
        base_url: str | None = None,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        ca_path: str | Path | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.namespace = namespace
        if base_url is None:
            host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
            port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            base_url = f"https://{host}:{port}"
        if token is None:
            token_file = SA_DIR / "token"
            token = token_file.read_text().strip() if token_file.exists() else ""
        verify: Any = True
        if transport is None:
            ca = Path(ca_path) if ca_path else SA_DIR / "ca.crt"
            if ca.exists():
                verify = str(ca)
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        kwargs: dict[str, Any] = {
            "base_url": base_url,
            "headers": headers,
            "timeout": timeout,
        }
        if transport is not None:
            kwargs["transport"] = transport
        else:
            kwargs["verify"] = verify
        self._http = httpx.AsyncClient(**kwargs)

    async def aclose(self) -> None:
        await self._http.aclose()

    def _path(self, kind: str, name: str | None = None) -> str:
        base = f"/api/v1/namespaces/{self.namespace}/{kind}"
        return f"{base}/{name}" if name else base

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: Any = None,
    ) -> dict[str, Any]:
        try:
            resp = await self._http.request(method, path, params=params, json=json)
        except httpx.HTTPError as exc:
            raise KubeError(0, f"{type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400:
            msg = resp.text
            try:
                body = resp.json()
                if isinstance(body, dict):
                    msg = str(body.get("message") or msg)
            except ValueError:
                pass
            raise KubeError(resp.status_code, msg)
        try:
            data = resp.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    # pods
    async def get_pod(self, name: str) -> dict[str, Any]:
        return await self._request("GET", self._path("pods", name))

    async def list_pods(self, label_selector: str | None = None) -> list[dict[str, Any]]:
        params = {"labelSelector": label_selector} if label_selector else None
        data = await self._request("GET", self._path("pods"), params=params)
        return list(data.get("items") or [])

    async def create_pod(self, spec: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", self._path("pods"), json=spec)

    async def delete_pod(self, name: str) -> dict[str, Any]:
        return await self._request("DELETE", self._path("pods", name))

    # secrets
    async def get_secret(self, name: str) -> dict[str, Any]:
        return await self._request("GET", self._path("secrets", name))

    async def create_secret(self, spec: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", self._path("secrets"), json=spec)

    async def delete_secret(self, name: str) -> dict[str, Any]:
        return await self._request("DELETE", self._path("secrets", name))

    # events
    async def list_events(self, field_selector: str | None = None) -> list[dict[str, Any]]:
        params = {"fieldSelector": field_selector} if field_selector else None
        data = await self._request("GET", self._path("events"), params=params)
        return list(data.get("items") or [])
