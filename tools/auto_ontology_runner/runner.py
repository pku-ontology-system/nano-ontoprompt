"""Local HTTP runner that lets the platform start NVIDIA Auto Ontology on a registered source database.

Start it with the Auto Ontology checkout's interpreter (it needs ``psycopg`` for the source catalog):

    <checkout>/.venv/bin/python -I runner.py --config config.json --env .env.local

It binds to 127.0.0.1 and requires a bearer token.  Requests name a *source id* and a *model id* from
the config; they can never carry a connection string or a key.  The platform therefore never sees the
credentials of the source database, and the runner enforces the privacy rule on its own: a private
source can only be compiled with a local model.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402

MAX_BODY = 16 * 1024
RUN_ID = re.compile(r"^[0-9a-f]{16}$")


class RunnerError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class Settings:
    def __init__(self, config_path: Path, env_path: Path):
        self.raw = json.loads(config_path.read_text(encoding="utf-8"))
        self.env = {**load_env_file(env_path), **os.environ}
        base = config_path.parent
        self.host = self.raw.get("listen_host", "127.0.0.1")
        self.port = int(self.raw.get("listen_port", 8765))
        self.ao_dir = (base / self.raw["auto_ontology_dir"]).resolve()
        self.work_dir = (base / self.raw.get("work_dir", "runs")).resolve()
        self.store = self.raw["store_admin"]
        self.sources: dict[str, dict] = self.raw["sources"]
        self.models: dict[str, dict] = self.raw["models"]
        self.timeout = int(self.raw.get("run_timeout_seconds", 1800))
        self.keep_runs = int(self.raw.get("keep_runs", 30))
        self.token = self.env.get("AUTO_ONTOLOGY_RUNNER_TOKEN", "")
        if len(self.token) < 16:
            raise SystemExit("AUTO_ONTOLOGY_RUNNER_TOKEN must be set (16+ characters) in the environment or the env file")
        self.python = self.ao_dir / ".venv" / "bin" / "python"
        if not self.python.exists():
            raise SystemExit(f"Auto Ontology interpreter not found: {self.python}")

    def secret(self, name: str | None, literal: str | None = None) -> str:
        if literal:
            return literal
        value = self.env.get(name or "", "")
        if not value:
            raise RunnerError(503, "secret_missing", f"The runner is missing the setting {name}")
        return value


def http_get_json(url: str, key: str | None, timeout: float = 3.0):
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"} if key else {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


class Manager:
    def __init__(self, settings: Settings):
        self.s = settings
        self.lock = threading.Lock()
        self.active: tuple[str, subprocess.Popen] | None = None
        self.cache: dict[str, tuple[float, object]] = {}
        settings.work_dir.mkdir(parents=True, exist_ok=True)
        self._recover()
        self.commit = self._git("rev-parse", "--short", "HEAD")
        match = re.search(r'^version\s*=\s*"([^"]+)"', (settings.ao_dir / "pyproject.toml").read_text(encoding="utf-8"), re.M)
        self.version = match.group(1) if match else "unknown"

    # ---- helpers -------------------------------------------------------------------------------------------
    def _git(self, *args: str) -> str:
        try:
            return subprocess.run(["git", "-C", str(self.s.ao_dir), *args], capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            return ""

    def _cached(self, key: str, ttl: float, produce):
        stamp, value = self.cache.get(key, (0.0, None))
        if time.monotonic() - stamp > ttl:
            value = produce()
            self.cache[key] = (time.monotonic(), value)
        return value

    def _recover(self) -> None:
        """A runner restart orphans unfinished runs; close them so nothing stays 'running' forever."""
        for path in self.s.work_dir.glob("*/status.json"):
            status = common.read_json(path, {})
            if status.get("status") in {"queued", "running"}:
                common.finish_stages(status, ok=False)
                status.update(status="failed", finished_at=common.now(), error="runner restarted while the run was in progress")
                common.write_json(path, status)

    def run_dir(self, run_id: str) -> Path:
        if not RUN_ID.match(run_id):
            raise RunnerError(404, "run_not_found", "Run not found")
        path = self.s.work_dir / run_id
        if not path.is_dir():
            raise RunnerError(404, "run_not_found", "Run not found")
        return path

    # ---- read side -----------------------------------------------------------------------------------------
    def models_view(self) -> list[dict]:
        rows = []
        for model_id, model in self.s.models.items():
            ids = self._cached(f"models:{model_id}", 10, lambda m=model: self._endpoint_models(m))
            rows.append({"id": model_id, "label": model["label"], "locality": model["locality"], "provider": model.get("provider", ""),
                         "chat_model": model["chat_model"], "embed_model": model["embed_model"],
                         "available": ids is not None and model["chat_model"] in ids,
                         "outbound": model.get("outbound", [])})
        return rows

    def _endpoint_models(self, model: dict):
        try:
            key = self.s.secret(model.get("chat_key_env"), model.get("chat_key"))
            return {row["id"] for row in http_get_json(model["chat_endpoint"].rstrip("/") + "/models", key).get("data", [])}
        except Exception:
            return None

    def sources_view(self) -> list[dict]:
        return [{"id": source_id, "label": source["label"], "privacy_level": source["privacy_level"],
                 "description": source.get("description", ""), "tables": self._cached(f"tables:{source_id}", 60, lambda s=source: self._tables(s))}
                for source_id, source in self.s.sources.items()]

    def _tables(self, source: dict) -> list[dict]:
        import psycopg
        try:
            with psycopg.connect(self.s.secret(source["connection_env"]), connect_timeout=3, options="-c default_transaction_read_only=on") as conn:
                rows = conn.execute("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public' "
                                    "ORDER BY table_name, ordinal_position").fetchall()
        except Exception:
            return []
        tables: dict[str, list[str]] = {}
        for table, column in rows:
            tables.setdefault(table, []).append(column)
        return [{"name": name, "columns": columns} for name, columns in tables.items()]

    def health(self) -> dict:
        def reachable(host: str, port: int) -> bool:
            try:
                with socket.create_connection((host, int(port)), timeout=1.5):
                    return True
            except OSError:
                return False
        embed = {m["embed_endpoint"] for m in self.s.models.values()}
        embeds_ok = all(self._probe(url) for url in embed)
        return {"ok": True, "auto_ontology": {"version": self.version, "commit": self.commit},
                "store": "ok" if reachable(self.s.store["host"], self.s.store["port"]) else "unreachable",
                "embedding": "ok" if embeds_ok else "unreachable", "active_run": self.active[0] if self.active else None}

    def _probe(self, url: str) -> bool:
        try:
            http_get_json(url.rstrip("/") + "/models", None, timeout=2.0)
            return True
        except Exception:
            return False

    def status(self, run_id: str) -> dict:
        directory = self.run_dir(run_id)
        status = common.read_json(directory / "status.json", {})
        log = directory / "run.log"
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[-40:] if log.exists() else []
        status["log_tail"] = [common.redact(line)[:300] for line in lines if "HTTP Request:" not in line][-25:]
        request = common.read_json(directory / "request.json", {})
        status["source_label"] = request.get("source", {}).get("label")
        status["model_label"] = request.get("model", {}).get("label")
        status["locality"] = request.get("model", {}).get("locality")
        return status

    def export(self, run_id: str) -> dict:
        directory = self.run_dir(run_id)
        status = common.read_json(directory / "status.json", {})
        if status.get("status") != "succeeded" or not (directory / "export.yaml").exists():
            raise RunnerError(409, "run_not_complete", "The run has not produced an export")
        return {"yaml": (directory / "export.yaml").read_text(encoding="utf-8"), "run": common.read_json(directory / "run.json", {}),
                "generator": {"tool": "NVIDIA Auto Ontology", "version": self.version, "commit": self.commit}}

    def list_runs(self) -> list[dict]:
        rows = []
        for path in sorted(self.s.work_dir.glob("*/status.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
            status = common.read_json(path, {})
            rows.append({key: status.get(key) for key in ("id", "source_id", "model_id", "status", "stage", "created_at", "finished_at")})
        return rows

    # ---- write side ----------------------------------------------------------------------------------------
    def start(self, source_id: str, model_id: str, requested_by: str | None) -> dict:
        source, model = self.s.sources.get(source_id), self.s.models.get(model_id)
        if source is None or model is None:
            raise RunnerError(400, "unknown_selection", "Unknown source or model")
        if source["privacy_level"] == "private" and model["locality"] != "local":
            raise RunnerError(403, "privacy_blocked", "A private source can only be compiled with a local model")
        env = self._child_env(source, model)
        with self.lock:
            if self.active and self.active[1].poll() is None:
                raise RunnerError(409, "run_active", f"Run {self.active[0]} is still in progress")
            self._prune()
            run_id = uuid.uuid4().hex[:16]
            directory = self.s.work_dir / run_id
            directory.mkdir()
            common.write_json(directory / "request.json", {
                "source": {"id": source_id, "label": source["label"], "privacy_level": source["privacy_level"]},
                "model": {"id": model_id, "label": model["label"], "locality": model["locality"], "chat_model": model["chat_model"],
                          "embed_model": model["embed_model"], "structured_output": model.get("structured_output", "default")},
                "requested_by": (requested_by or "")[:100], "created_at": common.now()})
            common.write_json(directory / "status.json", common.new_status(run_id, source_id, model_id))
            env["POSTGRES_DATABASE"] = f"ao_run_{run_id}"
            process = subprocess.Popen([str(self.s.python), "-I", str(HERE / "pipeline.py"), str(directory)], cwd=str(self.s.ao_dir),
                                       env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=(directory / "stderr.log").open("wb"), start_new_session=True)
            self.active = (run_id, process)
        threading.Thread(target=self._watch, args=(run_id, process), daemon=True).start()
        return self.status(run_id)

    def _child_env(self, source: dict, model: dict) -> dict:
        store = self.s.store
        env = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}}
        env.update({
            "POSTGRES_HOST": str(store["host"]), "POSTGRES_PORT": str(store["port"]), "POSTGRES_USER": store["user"],
            "POSTGRES_PASSWORD": self.s.secret(store.get("password_env"), store.get("password")),
            "CONNECTION_STRINGS": self.s.secret(source["connection_env"]),
            "DEFAULT_MODELS_API_KEY": self.s.secret(model.get("chat_key_env"), model.get("chat_key")),
            "DEFAULT_MODELS_ENDPOINT": model["chat_endpoint"], "DEFAULT_MODELS_MODEL": model["chat_model"],
            "EMBED_API_KEY": self.s.secret(model.get("embed_key_env"), model.get("embed_key")),
            "EMBED_ENDPOINT": model["embed_endpoint"], "EMBED_MODEL": model["embed_model"], "EMBED_DIM": str(model["embed_dim"]),
            "PYTHONDONTWRITEBYTECODE": "1"})
        return env

    def _watch(self, run_id: str, process: subprocess.Popen) -> None:
        directory = self.s.work_dir / run_id
        try:
            process.wait(timeout=self.s.timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill(process)
        status = common.read_json(directory / "status.json", {})
        if status.get("status") not in common.TERMINAL:
            tail = ""
            try:
                tail = (directory / "stderr.log").read_text(encoding="utf-8", errors="replace")[-300:]
            except OSError:
                pass
            common.finish_stages(status, ok=False)
            status.update(status="failed", finished_at=common.now(),
                          error="timeout" if timed_out else common.redact(f"The pipeline stopped unexpectedly: {tail}").strip()[:500])
            common.write_json(directory / "status.json", status)
        with self.lock:
            if self.active and self.active[0] == run_id:
                self.active = None

    @staticmethod
    def _kill(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
        except Exception:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except Exception:
                pass

    def cancel(self, run_id: str) -> dict:
        directory = self.run_dir(run_id)
        with self.lock:
            if not self.active or self.active[0] != run_id:
                raise RunnerError(409, "not_running", "The run is not in progress")
            process = self.active[1]
        self._kill(process)
        status = common.read_json(directory / "status.json", {})
        if status.get("status") not in common.TERMINAL:
            common.finish_stages(status, ok=False)
            status.update(status="cancelled", finished_at=common.now(), error="cancelled")
            common.write_json(directory / "status.json", status)
        return self.status(run_id)

    def _prune(self) -> None:
        runs = sorted((p for p in self.s.work_dir.iterdir() if p.is_dir() and RUN_ID.match(p.name)), key=lambda p: p.stat().st_mtime)
        for path in runs[:-self.s.keep_runs]:
            shutil.rmtree(path, ignore_errors=True)


def make_handler(manager: Manager):
    token = manager.s.token.encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "AutoOntologyRunner/1"

        def log_message(self, fmt, *args):  # no access log: paths carry run ids only, but keep the console quiet
            return

        def _send(self, status: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            return header.startswith("Bearer ") and hmac.compare_digest(header[7:].encode(), token)

        def _dispatch(self, method: str) -> None:
            try:
                if not self._authorized():
                    raise RunnerError(401, "unauthorized", "Invalid or missing token")
                path = self.path.split("?", 1)[0].rstrip("/")
                body = {}
                if method == "POST":
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > MAX_BODY:
                        raise RunnerError(413, "body_too_large", "Request body is too large")
                    raw = self.rfile.read(length) if length else b"{}"
                    try:
                        body = json.loads(raw or b"{}")
                    except ValueError as exc:
                        raise RunnerError(400, "invalid_json", "Request body is not valid JSON") from exc
                    if not isinstance(body, dict):
                        raise RunnerError(400, "invalid_json", "Request body must be an object")
                self._send(200 if method == "GET" or path != "/runs" else 202, self._route(method, path, body))
            except RunnerError as exc:
                self._send(exc.status, {"error": {"code": exc.code, "message": exc.message}})
            except Exception as exc:  # noqa: BLE001
                self._send(500, {"error": {"code": "internal_error", "message": common.redact(f"{type(exc).__name__}: {exc}")[:200]}})

        def _route(self, method: str, path: str, body: dict):
            if method == "GET":
                if path == "/health":
                    return manager.health()
                if path == "/models":
                    return {"models": manager.models_view()}
                if path == "/sources":
                    return {"sources": manager.sources_view()}
                if path == "/runs":
                    return {"runs": manager.list_runs()}
                match = re.fullmatch(r"/runs/([^/:]+)", path)
                if match:
                    return manager.status(match.group(1))
                match = re.fullmatch(r"/runs/([^/:]+)/export", path)
                if match:
                    return manager.export(match.group(1))
            else:
                if path == "/runs":
                    source_id, model_id = body.get("source_id"), body.get("model_id")
                    if not isinstance(source_id, str) or not isinstance(model_id, str):
                        raise RunnerError(400, "invalid_request", "source_id and model_id are required")
                    requested_by = body.get("requested_by")
                    return manager.start(source_id, model_id, requested_by if isinstance(requested_by, str) else None)
                match = re.fullmatch(r"/runs/([^/:]+):cancel", path)
                if match:
                    return manager.cancel(match.group(1))
            raise RunnerError(404, "not_found", "Unknown endpoint")

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--config", default=str(HERE / "config.json"))
    parser.add_argument("--env", default=str(HERE / ".env.local"))
    args = parser.parse_args()
    settings = Settings(Path(args.config), Path(args.env))
    manager = Manager(settings)
    server = ThreadingHTTPServer((settings.host, settings.port), make_handler(manager))
    print(f"Auto Ontology runner {manager.version} ({manager.commit}) listening on http://{settings.host}:{settings.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
