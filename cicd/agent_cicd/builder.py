"""Pipeline builder: a local web page for shaping an agent's release pipeline (its pipeline.yaml).

    python -m agent_cicd.builder                        # opens http://127.0.0.1:8765
    python -m agent_cicd.builder --port 0 --no-browser  # any free port, don't open a browser

The page shows the pipeline as a diagram that updates as you change settings, any problems, and the exact
files the generator would change (with diffs). Save writes pipeline.yaml in its canonical form and runs the
generator; the page never writes workflows itself. Review and commit the result as usual (pull request, G1).

It listens on 127.0.0.1 only and accepts only localhost Host headers. The page and every API call need a random
token that is only in the URL this command prints (and opens), so neither other websites in your browser nor
other users on the machine can drive it.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import json
import secrets
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml

from . import generate
from .definition import DefinitionError, agent_kind

UI_DIR = Path(__file__).resolve().parent / "builder_ui"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8")}
MAX_BODY = 64 * 1024
MAX_DIFF_LINES = 400


class Builder:
    """Reads and writes pipeline.yaml for the agent folders under agents_dir. No HTTP here."""

    def __init__(self, agents_dir: Path = generate.CICD_DIR / "agents"):
        self.agents_dir = agents_dir

    def agents(self) -> list[dict[str, Any]]:
        out = []
        for agent_dir in sorted(p.parent for p in self.agents_dir.glob("*/agent.json")):
            try:
                kind = agent_kind(agent_dir)
            except (DefinitionError, OSError, ValueError):
                kind = "unknown"
            out.append({"name": agent_dir.name, "kind": kind,
                        "has_pipeline": (agent_dir / generate.SPEC_FILE).is_file()})
        return out

    def _dir(self, name: str) -> Path:
        # Only folders the listing returned; never a path built from user input.
        for agent in self.agents():
            if agent["name"] == name:
                return self.agents_dir / agent["name"]
        raise KeyError(name)

    def load(self, name: str) -> dict[str, Any]:
        agent_dir = self._dir(name)
        path = agent_dir / generate.SPEC_FILE
        data, error = copy.deepcopy(generate.DEFAULT_SPEC), None
        if path.is_file():
            try:
                # A valid file is shown exactly as the generator reads it (missing keys get the generator's defaults).
                data = generate.spec_data(generate.load_spec(agent_dir))
            except generate.SpecError as exc:
                error = "pipeline.yaml has problems; the form shows what could be read: " + "; ".join(exc.problems)
                try:
                    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                    data = _fill_defaults(loaded) if isinstance(loaded, dict) else data
                except yaml.YAMLError:
                    pass
        kind = next(a["kind"] for a in self.agents() if a["name"] == name)
        return {"agent": name, "kind": kind, "exists": path.is_file(), "spec": data, "error": error,
                "presets": generate.presets()}

    def _current_spec(self, agent_dir: Path) -> generate.Spec | None:
        try:
            return generate.load_spec(agent_dir)
        except generate.SpecError:
            return None

    def preview(self, name: str, data: dict[str, Any]) -> dict[str, Any]:
        agent_dir = self._dir(name)
        kind = next(a["kind"] for a in self.agents() if a["name"] == name)
        try:
            spec = generate.parse_spec(data, name, kind if kind != "unknown" else "prompt")
        except generate.SpecError as exc:
            return {"ok": False, "problems": exc.problems}
        if kind == "unknown":
            return {"ok": False, "problems": ["agent.json has no valid kind (prompt or hosted)"]}
        others = {a["name"] for a in self.agents() if a["has_pipeline"]}
        conflicts = generate.tag_conflicts(others | {name})
        if conflicts:
            return {"ok": False, "problems": conflicts}

        files = []
        spec_path = agent_dir / generate.SPEC_FILE
        new_spec_text = generate.dump_spec(spec)
        old_spec_text = spec_path.read_text(encoding="utf-8").replace("\r\n", "\n") if spec_path.is_file() else ""
        if old_spec_text != new_spec_text:
            files.append(_file_entry(spec_path, old_spec_text, new_spec_text, "write"))
        for change in generate.changes_for(agent_dir, spec):
            old = change.path.read_text(encoding="utf-8").replace("\r\n", "\n") if change.path.is_file() else ""
            files.append(_file_entry(change.path, old, change.content, change.action))

        current = self._current_spec(agent_dir)
        known = {r["environment"] for r in current.signoffs} if current else set()
        return {"ok": True, "plan": generate.plan_model(spec), "text": generate.describe(spec), "files": files,
                "new_environments": [r["environment"] for r in spec.signoffs if r["environment"] not in known]}

    def save(self, name: str, data: dict[str, Any]) -> dict[str, Any]:
        result = self.preview(name, data)
        if not result["ok"]:
            return result
        agent_dir = self._dir(name)
        spec = generate.parse_spec(data, name, next(a["kind"] for a in self.agents() if a["name"] == name))
        text = generate.dump_spec(spec)
        # Never write a file that reads back differently; encode first so a bad character can't empty it.
        if generate.spec_data(generate.parse_spec(yaml.safe_load(text), name, spec.kind)) != generate.spec_data(spec):
            return {"ok": False, "problems": ["These settings can't be written to pipeline.yaml faithfully"]}
        (agent_dir / generate.SPEC_FILE).write_bytes(text.encode("utf-8"))
        generate.apply(generate.changes_for(agent_dir, spec))
        return {"ok": True, "written": [f["path"] for f in result["files"]],
                "new_environments": result["new_environments"]}


def _fill_defaults(data: dict[str, Any]) -> dict[str, Any]:
    """Fill sections an invalid pipeline.yaml leaves out, so the form has every field. Uses the values the
    generator assumes for missing keys (schedules off), not the defaults for a brand-new file."""
    defaults = copy.deepcopy(generate.DEFAULT_SPEC)
    defaults["prod"]["drift_check"] = ""
    out = copy.deepcopy(data)
    for key, default in defaults.items():
        if isinstance(default, dict):
            section = out.get(key)
            if isinstance(section, dict):
                for sub, value in default.items():
                    section.setdefault(sub, copy.deepcopy(value))
            elif section is None:
                out[key] = copy.deepcopy(default)
        else:
            out.setdefault(key, default)
    return out


def _file_entry(path: Path, old: str, new: str, action: str) -> dict[str, Any]:
    rel = generate._rel(path)
    if action == "delete":
        diff = [f"--- {rel}", "+++ /dev/null", f"(deleted: {len(old.splitlines())} lines)"]
        status = "deleted"
    else:
        diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), f"a/{rel}", f"b/{rel}", n=2,
                                         lineterm=""))
        status = "changed" if old else "new"
    if len(diff) > MAX_DIFF_LINES:
        diff = diff[:MAX_DIFF_LINES] + [f"... {len(diff) - MAX_DIFF_LINES} more lines"]
    return {"path": rel, "status": status, "diff": "\n".join(diff)}


# ---------------------------------------------------------------- HTTP

def make_server(builder: Builder, port: int = 8765) -> tuple[ThreadingHTTPServer, str]:
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = "PipelineBuilder"
        sys_version = ""

        def log_message(self, fmt: str, *args: Any) -> None:  # keep the console quiet
            pass

        def _origins(self) -> set[str]:
            actual = self.server.server_address[1]
            return {f"127.0.0.1:{actual}", f"localhost:{actual}"}

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

        def _trusted(self, token_value: str | None) -> bool:
            # DNS rebinding: only localhost names. Cross-site requests: an Origin, if sent, must be ours.
            # The token is only in the URL printed by `main`, never served, so other local users can't get it.
            if self.headers.get("Host", "") not in self._origins():
                self._json(HTTPStatus.FORBIDDEN, {"error": "unexpected Host"})
                return False
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc not in self._origins():
                self._json(HTTPStatus.FORBIDDEN, {"error": "cross-origin request"})
                return False
            if token_value is not None and not secrets.compare_digest(token_value.encode(), token.encode()):
                self._json(HTTPStatus.FORBIDDEN,
                           {"error": "missing or wrong token: open the URL printed by `python -m agent_cicd.builder`"})
                return False
            return True

        def _agent(self, name: str) -> bool:
            try:
                builder._dir(name)
                return True
            except KeyError:
                self._json(HTTPStatus.NOT_FOUND, {"error": f"no agent folder '{name}'"})
                return False

        def _run(self, action) -> None:
            try:
                with lock:
                    result = action()
            except Exception as exc:  # noqa: BLE001 - report any failure to the page instead of dropping it
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(exc).__name__}: {exc}"})
                return
            self._json(HTTPStatus.OK, result)

        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            url = urlparse(self.path)
            query = parse_qs(url.query)
            if url.path in STATIC:
                # The page itself needs ?t=<token>; its scripts and styles hold nothing secret.
                needs = (query.get("t") or [""])[0] if url.path == "/" else None
                if not self._trusted(needs):
                    return
                name, content_type = STATIC[url.path]
                self._send(HTTPStatus.OK, (UI_DIR / name).read_bytes(), content_type)
                return
            if not url.path.startswith("/api/"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            if not self._trusted(self.headers.get("X-Builder-Token", "")):
                return
            if url.path == "/api/agents":
                self._run(lambda: {"agents": builder.agents()})
            elif url.path == "/api/spec":
                name = (query.get("agent") or [""])[0]
                if self._agent(name):
                    self._run(lambda: builder.load(name))
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path not in ("/api/preview", "/api/save"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            if not self._trusted(self.headers.get("X-Builder-Token", "")):
                return
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "send JSON"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY:
                self._json(HTTPStatus.BAD_REQUEST, {"error": f"send a JSON body of 1 to {MAX_BODY} bytes"})
                return
            try:
                body = json.loads(self.rfile.read(length))
                name, data = body["agent"], body["spec"]
                if not isinstance(name, str) or not isinstance(data, dict):
                    raise TypeError
            except (ValueError, KeyError, TypeError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "expected {\"agent\": ..., \"spec\": {...}}"})
                return
            if self._agent(name):
                self._run(lambda: builder.preview(name, data) if url.path == "/api/preview"
                          else builder.save(name, data))

    lock = threading.Lock()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)  # port in use: take any free one
    return server, f"http://127.0.0.1:{server.server_address[1]}/?t={token}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    server, url = make_server(Builder(), args.port)
    print(f"Pipeline builder: {url}  (Ctrl+C to stop)", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
