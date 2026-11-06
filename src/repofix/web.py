from __future__ import annotations

import asyncio
import json
import mimetypes
import subprocess
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from pydantic import ValidationError

from repofix import __version__
from repofix.config import HarnessPaths, ModelConfig, write_model_env
from repofix.model_client import OpenAICompatibleClient
from repofix.models import Budget, SessionStatus, TaskSpec
from repofix.runtime import AgentHarness
from repofix.store import SessionStore
from repofix.workspace import WorkspaceManager


class WebApplication:
    """Local controller API used by the dependency-free browser console."""

    def __init__(self, paths: HarnessPaths, docker_image: str = "repofix-runner:py312"):
        self.paths = paths
        self.paths.ensure()
        self.docker_image = docker_image
        self.static_dir = Path(__file__).parent / "web_static"
        self._workers: dict[str, threading.Thread] = {}
        self._worker_loops: dict[str, asyncio.AbstractEventLoop] = {}
        self._worker_tasks: dict[str, asyncio.Task] = {}
        self._worker_lock = threading.Lock()

    def config_view(self) -> dict[str, Any]:
        config = ModelConfig.from_sources(home=self.paths.home)
        key_hint = ""
        if config.api_key:
            key_hint = "••••" + config.api_key[-4:]
        return {
            "model": config.model,
            "base_url": config.base_url,
            "timeout_seconds": config.timeout_seconds,
            "max_retries": config.max_retries,
            "has_api_key": bool(config.api_key),
            "api_key_hint": key_hint,
            "config_path": str(self.paths.home / ".env"),
        }

    def save_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = ModelConfig.from_sources(home=self.paths.home)
        supplied_key = payload.get("api_key")
        config = ModelConfig(
            model=str(payload.get("model", current.model)).strip(),
            base_url=str(payload.get("base_url", current.base_url)).strip().rstrip("/"),
            api_key=str(supplied_key).strip() if supplied_key else current.api_key,
            timeout_seconds=int(payload.get("timeout_seconds", current.timeout_seconds)),
            max_retries=int(payload.get("max_retries", current.max_retries)),
        )
        write_model_env(self.paths.home / ".env", config)
        return self.config_view()

    def inspect_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw = str(payload.get("path", "")).strip().strip('"')
        if not raw:
            raise ValueError("请选择或填写项目文件夹")
        project = Path(raw).expanduser().resolve()
        if not project.is_dir():
            raise ValueError(f"项目文件夹不存在：{project}")
        is_git = WorkspaceManager.is_git_repository(project)
        dirty = False
        root = project
        if is_git:
            result = subprocess.run(
                ["git", "-C", str(project), "rev-parse", "--show-toplevel"],
                text=True,
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0:
                root = Path(result.stdout.strip()).resolve()
            status = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                text=True,
                capture_output=True,
                timeout=10,
            )
            dirty = bool(status.stdout.strip())
        return {
            "path": str(project),
            "project_root": str(root),
            "name": project.name,
            "source_kind": "git" if is_git else "folder",
            "dirty": dirty,
            "ready": not dirty,
            "message": (
                "Git 项目存在未提交修改，请先提交或暂存到其他位置"
                if dirty
                else "Git 项目将使用独立 worktree"
                if is_git
                else "普通文件夹将复制为受控快照，原目录不会被修改"
            ),
        }

    @staticmethod
    def pick_folder() -> dict[str, str]:
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = filedialog.askdirectory(title="选择 RepoFix 要分析的项目文件夹")
            root.destroy()
            return {"path": selected}
        except Exception as exc:
            raise RuntimeError(f"无法打开系统文件夹选择器：{exc}") from exc

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._store() as store:
            return [self._session_summary(item) for item in store.list_sessions()]

    def session_detail(self, session_id: str) -> dict[str, Any]:
        with self._store() as store:
            session = store.get(session_id)
            events = [event.model_dump(mode="json") for event in store.events(session_id)[-200:]]
        patch = ""
        if session.verification and session.verification.get("patch_path"):
            patch_path = Path(session.verification["patch_path"])
            if patch_path.is_file():
                patch = patch_path.read_text(encoding="utf-8", errors="replace")
        elif Path(session.workspace).is_dir():
            try:
                patch = WorkspaceManager.candidate_patch(
                    Path(session.workspace), session.base_commit
                )
            except Exception:
                patch = ""
        return {
            "session": session.model_dump(mode="json"),
            "events": events,
            "patch": patch,
            "worker_active": self.worker_active(session_id),
        }

    def create_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = ModelConfig.from_sources(
            model=str(payload.get("model") or "").strip() or None, home=self.paths.home
        )
        if not config.api_key:
            raise ValueError("请先在“模型配置”中保存 API Key")
        commands = payload.get("acceptance_commands") or ["python -m pytest -q"]
        if isinstance(commands, str):
            commands = [line.strip() for line in commands.splitlines() if line.strip()]
        task = TaskSpec(
            issue=str(payload.get("issue", "")).strip(),
            acceptance_commands=commands,
            protected_tests=str(payload.get("protected_tests") or "").strip() or None,
            allowed_network=bool(payload.get("allowed_network", False)),
        )
        executor = str(payload.get("executor", "docker"))
        if executor not in {"docker", "local"}:
            raise ValueError("执行器必须是 docker 或 local")
        budget = Budget(
            max_turns=int(payload.get("max_turns", 30)),
            max_tool_calls=int(payload.get("max_tool_calls", 100)),
            max_wall_seconds=int(payload.get("max_minutes", 30)) * 60,
        )
        with self._store() as store:
            session = AgentHarness.create_session(
                store,
                Path(str(payload.get("project_path", ""))),
                task,
                model=config.model,
                executor=executor,
                budget=budget,
            )
        self.launch(session.id)
        return self._session_summary(session)

    def launch(self, session_id: str) -> None:
        with self._worker_lock:
            current = self._workers.get(session_id)
            if current and current.is_alive():
                raise ValueError("该任务已经在运行")
            worker = threading.Thread(
                target=self._run_session,
                args=(session_id,),
                name=f"repofix-{session_id}",
                daemon=True,
            )
            self._workers[session_id] = worker
            worker.start()

    def _run_session(self, session_id: str) -> None:
        store = SessionStore(self.paths)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            session = store.get(session_id)
            config = ModelConfig.from_sources(model=session.model, home=self.paths.home)
            client = OpenAICompatibleClient(config)
            task = loop.create_task(
                AgentHarness(store, client, docker_image=self.docker_image).run(session_id)
            )
            with self._worker_lock:
                self._worker_loops[session_id] = loop
                self._worker_tasks[session_id] = task
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            try:
                session = store.get(session_id)
                session.status = SessionStatus.PAUSED
                session.last_error = f"{type(exc).__name__}: {exc}"
                store.save(session)
                store.append_event(session_id, "web_worker_failed", {"reason": session.last_error})
            except Exception:
                pass
        finally:
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            store.close()
            with self._worker_lock:
                self._workers.pop(session_id, None)
                self._worker_loops.pop(session_id, None)
                self._worker_tasks.pop(session_id, None)

    def worker_active(self, session_id: str) -> bool:
        with self._worker_lock:
            worker = self._workers.get(session_id)
            return bool(worker and worker.is_alive())

    def cancel(self, session_id: str) -> dict[str, Any]:
        with self._store() as store:
            store.request_cancel(session_id)
            summary = self._session_summary(store.get(session_id))
        with self._worker_lock:
            loop = self._worker_loops.get(session_id)
            task = self._worker_tasks.get(session_id)
        if loop and task and not task.done():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                pass
        return summary

    def cleanup(self, session_id: str) -> dict[str, Any]:
        if self.worker_active(session_id):
            raise ValueError("运行中的任务不能清理")
        with self._store() as store:
            session = store.get(session_id)
            workspace_source = Path(session.workspace_source or session.source_repo)
            if Path(session.workspace).exists():
                WorkspaceManager.remove(workspace_source, Path(session.workspace))
            if session.source_kind == "folder" and workspace_source.exists():
                WorkspaceManager.delete_managed_source(workspace_source, self.paths.sources)
            session.status = SessionStatus.CLEANED
            store.save(session)
            store.append_event(session_id, "workspace_cleaned", {"workspace": session.workspace})
            return self._session_summary(session)

    @staticmethod
    def _session_summary(session) -> dict[str, Any]:
        return {
            "id": session.id,
            "status": session.status.value,
            "project": session.source_repo,
            "project_name": Path(session.source_repo).name,
            "source_kind": session.source_kind,
            "issue": session.task.issue,
            "model": session.model,
            "executor": session.executor,
            "turns": session.turns,
            "tool_calls": session.tool_calls,
            "updated_at": session.updated_at,
            "verification_passed": bool(
                session.verification and session.verification.get("passed")
            ),
            "last_error": session.last_error,
        }

    def _store(self):
        return _StoreContext(self.paths)


class _StoreContext:
    def __init__(self, paths: HarnessPaths):
        self.store = SessionStore(paths)

    def __enter__(self) -> SessionStore:
        return self.store

    def __exit__(self, *_args) -> None:
        self.store.close()


class RepoFixRequestHandler(BaseHTTPRequestHandler):
    server_version = f"RepoFixWeb/{__version__}"
    app: WebApplication

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/api/health":
                self._json({"ok": True, "version": __version__})
            elif path == "/api/config":
                self._json(self.app.config_view())
            elif path == "/api/sessions":
                self._json(self.app.list_sessions())
            elif path.startswith("/api/sessions/"):
                session_id = unquote(path.removeprefix("/api/sessions/")).strip("/")
                self._json(self.app.session_detail(session_id))
            elif path == "/":
                self._static("index.html")
            elif path.startswith("/static/"):
                self._static(path.removeprefix("/static/"))
            else:
                self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._error(exc)

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            payload = self._body()
            if path == "/api/config":
                self._json(self.app.save_config(payload))
            elif path == "/api/project/inspect":
                self._json(self.app.inspect_project(payload))
            elif path == "/api/project/pick":
                self._json(self.app.pick_folder())
            elif path == "/api/sessions":
                self._json(self.app.create_session(payload), HTTPStatus.CREATED)
            elif path.startswith("/api/sessions/"):
                parts = path.strip("/").split("/")
                if len(parts) != 4:
                    raise ValueError("未知的会话操作")
                session_id, action = parts[2], parts[3]
                if action == "resume":
                    self.app.launch(session_id)
                    self._json({"ok": True})
                elif action == "cancel":
                    self._json(self.app.cancel(session_id))
                elif action == "cleanup":
                    self._json(self.app.cleanup(session_id))
                else:
                    raise ValueError("未知的会话操作")
            else:
                self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._error(exc)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise ValueError("请求内容过大")
        raw = self.rfile.read(length) if length else b"{}"
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求必须是 JSON 对象")
        return value

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, relative: str) -> None:
        candidate = (self.app.static_dir / relative).resolve()
        if self.app.static_dir.resolve() not in candidate.parents:
            self._json({"error": "Invalid static path"}, HTTPStatus.BAD_REQUEST)
            return
        if not candidate.is_file():
            self._json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        body = candidate.read_bytes()
        mime = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK.value)
        self.send_header("Content-Type", f"{mime}; charset=utf-8")
        self.send_header(
            "Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'"
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, exc: Exception) -> None:
        status = HTTPStatus.BAD_REQUEST
        if isinstance(exc, KeyError):
            status = HTTPStatus.NOT_FOUND
        elif not isinstance(exc, (ValueError, ValidationError, json.JSONDecodeError)):
            status = HTTPStatus.INTERNAL_SERVER_ERROR
        self._json({"error": str(exc)}, status)

    def log_message(self, _format: str, *_args) -> None:
        return


def make_server(app: WebApplication, port: int) -> ThreadingHTTPServer:
    handler = type("BoundRepoFixHandler", (RepoFixRequestHandler,), {"app": app})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def serve(
    paths: HarnessPaths,
    *,
    port: int = 8765,
    docker_image: str = "repofix-runner:py312",
    open_browser: bool = True,
) -> None:
    app = WebApplication(paths, docker_image=docker_image)
    server = make_server(app, port)
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"RepoFix Web is running at {url}")
    print(f"Persistent configuration: {paths.home / '.env'}")
    print("Press Ctrl+C to stop the Web server.")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
