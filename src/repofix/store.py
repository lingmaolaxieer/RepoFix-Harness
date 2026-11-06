from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

from repofix.config import HarnessPaths
from repofix.models import Event, SessionRecord, SessionStatus, utc_now

_TRACE_LOCK = threading.RLock()


class SessionStore:
    """SQLite metadata plus append-only JSONL traces and file artifacts."""

    def __init__(self, paths: HarnessPaths):
        self.paths = paths
        self.paths.ensure()
        self._lock = threading.RLock()
        self._db = sqlite3.connect(paths.database, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                status TEXT NOT NULL,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                session_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(session_id, seq)
            );
            CREATE TABLE IF NOT EXISTS pending_tools (
                session_id TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                name TEXT NOT NULL,
                arguments TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                result TEXT,
                PRIMARY KEY(session_id, tool_call_id)
            );
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def create(self, session: SessionRecord) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO sessions(id,data,status,updated_at) VALUES(?,?,?,?)",
                (session.id, session.model_dump_json(), session.status.value, utc_now()),
            )
            self._db.commit()
        self.append_event(session.id, "session_created", session.model_dump(mode="json"))

    def get(self, session_id: str) -> SessionRecord:
        row = self._db.execute("SELECT data FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown session: {session_id}")
        return SessionRecord.model_validate_json(row["data"])

    def list_sessions(self, limit: int = 100) -> list[SessionRecord]:
        rows = self._db.execute(
            "SELECT data FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [SessionRecord.model_validate_json(row["data"]) for row in rows]

    def save(self, session: SessionRecord) -> None:
        session.updated_at = utc_now()
        with self._lock:
            self._db.execute(
                "UPDATE sessions SET data=?,status=?,updated_at=? WHERE id=?",
                (session.model_dump_json(), session.status.value, session.updated_at, session.id),
            )
            self._db.commit()

    def set_status(
        self, session_id: str, status: SessionStatus, *, error: str | None = None
    ) -> SessionRecord:
        session = self.get(session_id)
        session.status = status
        session.last_error = error
        self.save(session)
        self.append_event(session_id, "status_changed", {"status": status.value, "error": error})
        return session

    def request_cancel(self, session_id: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE sessions SET cancel_requested=1,status=?,updated_at=? WHERE id=?",
                (SessionStatus.CANCEL_REQUESTED.value, utc_now(), session_id),
            )
            self._db.commit()
        session = self.get(session_id)
        session.status = SessionStatus.CANCEL_REQUESTED
        self.save(session)
        self.append_event(session_id, "cancel_requested", {})

    def cancellation_requested(self, session_id: str) -> bool:
        row = self._db.execute(
            "SELECT cancel_requested FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        return bool(row and row[0])

    def append_event(self, session_id: str, kind: str, payload: dict[str, Any]) -> Event:
        # RepoFix 0.3 is a single-controller process. This process-wide lock keeps
        # SQLite sequence allocation and the JSONL mirror in the same order even
        # when Web workers use separate SessionStore connections.
        with _TRACE_LOCK, self._lock:
            try:
                # Serialize sequence allocation across independent SessionStore connections.
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute(
                    "SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE session_id=?", (session_id,)
                ).fetchone()
                event = Event(seq=int(row[0]), session_id=session_id, kind=kind, payload=payload)
                self._db.execute(
                    "INSERT INTO events(session_id,seq,kind,payload,created_at) VALUES(?,?,?,?,?)",
                    (
                        session_id,
                        event.seq,
                        kind,
                        json.dumps(payload, ensure_ascii=False),
                        event.created_at,
                    ),
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
            trace_dir = self.paths.sessions / session_id
            trace_dir.mkdir(parents=True, exist_ok=True)
            with (trace_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(event.model_dump_json() + "\n")
            return event

    def events(self, session_id: str) -> list[Event]:
        rows = self._db.execute(
            "SELECT * FROM events WHERE session_id=? ORDER BY seq", (session_id,)
        ).fetchall()
        return [
            Event(
                seq=row["seq"],
                session_id=session_id,
                kind=row["kind"],
                payload=json.loads(row["payload"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def start_tool(self, session_id: str, call_id: str, name: str, arguments: dict) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO pending_tools(session_id,tool_call_id,name,arguments,started_at,completed_at,result) VALUES(?,?,?,?,?,NULL,NULL)",
                (session_id, call_id, name, json.dumps(arguments, ensure_ascii=False), utc_now()),
            )
            self._db.commit()
        self.append_event(
            session_id, "tool_started", {"id": call_id, "name": name, "arguments": arguments}
        )

    def finish_tool(self, session_id: str, call_id: str, result: dict) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE pending_tools SET completed_at=?,result=? WHERE session_id=? AND tool_call_id=?",
                (utc_now(), json.dumps(result, ensure_ascii=False), session_id, call_id),
            )
            self._db.commit()
        self.append_event(session_id, "tool_finished", result)

    def indeterminate_tools(self, session_id: str) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM pending_tools WHERE session_id=? AND completed_at IS NULL",
            (session_id,),
        ).fetchall()
        return [
            {
                "id": row["tool_call_id"],
                "name": row["name"],
                "arguments": json.loads(row["arguments"]),
                "started_at": row["started_at"],
            }
            for row in rows
        ]

    def mark_tool_indeterminate(self, session_id: str, call_id: str) -> None:
        result = {"status": "indeterminate", "summary": "No durable result; not replayed"}
        with self._lock:
            self._db.execute(
                "UPDATE pending_tools SET completed_at=?,result=? "
                "WHERE session_id=? AND tool_call_id=? AND completed_at IS NULL",
                (utc_now(), json.dumps(result), session_id, call_id),
            )
            self._db.commit()

    def write_artifact(self, session_id: str, name: str, content: str) -> str:
        folder = self.paths.artifacts / session_id
        folder.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(c if c.isalnum() or c in ".-_" else "_" for c in name)
        path = folder / safe_name
        path.write_text(content, encoding="utf-8", errors="replace")
        return str(path)
