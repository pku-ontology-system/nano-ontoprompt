"""Client for the local Auto Ontology runner (``tools/auto_ontology_runner``).

The runner keeps the source database credentials and the model keys; requests name a source and a model by id.
"""
from __future__ import annotations

import httpx

from app.config import settings


class RunnerUnavailable(Exception):
    """The runner is not configured or cannot be reached."""


class RunnerError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class RunnerClient:
    def __init__(self, base_url: str, token: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float | None = None):
        try:
            response = httpx.request(method, self.base_url + path, json=body, headers={"Authorization": f"Bearer {self.token}"},
                                     timeout=timeout or self.timeout)
        except httpx.HTTPError as exc:
            raise RunnerUnavailable(f"无法连接 Auto Ontology 运行器：{type(exc).__name__}") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code >= 400:
            error = payload.get("error") if isinstance(payload, dict) else None
            error = error if isinstance(error, dict) else {}
            if response.status_code in {401, 403} and error.get("code") == "unauthorized":
                raise RunnerUnavailable("运行器拒绝了平台的令牌，请检查 AUTO_ONTOLOGY_RUNNER_TOKEN")
            raise RunnerError(response.status_code, str(error.get("code") or "runner_error"), str(error.get("message") or "运行器返回错误"))
        return payload

    def health(self) -> dict:
        return self._call("GET", "/health")

    def models(self) -> list[dict]:
        return self._call("GET", "/models")["models"]

    def sources(self) -> list[dict]:
        return self._call("GET", "/sources", timeout=20.0)["sources"]

    def start(self, source_id: str, model_id: str, requested_by: str) -> dict:
        return self._call("POST", "/runs", {"source_id": source_id, "model_id": model_id, "requested_by": requested_by})

    def status(self, run_id: str) -> dict:
        return self._call("GET", f"/runs/{run_id}")

    def export(self, run_id: str) -> dict:
        return self._call("GET", f"/runs/{run_id}/export", timeout=30.0)

    def cancel(self, run_id: str) -> dict:
        return self._call("POST", f"/runs/{run_id}:cancel", {})


def get_runner_client() -> RunnerClient | None:
    """None when the platform has no runner configured (file import still works)."""
    if not settings.auto_ontology_runner_url or not settings.auto_ontology_runner_token:
        return None
    return RunnerClient(settings.auto_ontology_runner_url, settings.auto_ontology_runner_token)
