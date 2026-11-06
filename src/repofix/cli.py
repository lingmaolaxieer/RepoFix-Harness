from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from repofix.config import HarnessPaths, ModelConfig
from repofix.context import handoff_summary
from repofix.evaluation import run_evaluation
from repofix.model_client import OpenAICompatibleClient
from repofix.models import Budget, SessionStatus, TaskSpec
from repofix.runtime import AgentHarness
from repofix.store import SessionStore
from repofix.verifier import IndependentVerifier
from repofix.workspace import WorkspaceManager

app = typer.Typer(help="RepoFix: a verifiable coding-agent harness", no_args_is_help=True)
console = Console()


def _paths(home: Path | None) -> HarnessPaths:
    configured = os.getenv("REPOFIX_HOME")
    default = Path(configured) if configured else Path.home() / ".repofix"
    return HarnessPaths.under(home or default)


def _client(model: str | None, paths: HarnessPaths) -> tuple[OpenAICompatibleClient, ModelConfig]:
    config = ModelConfig.from_sources(model=model, home=paths.home)
    return OpenAICompatibleClient(config), config


def _print_session(session) -> None:
    console.print(f"[bold]{session.id}[/bold]  status={session.status.value}")
    console.print(f"workspace: {session.workspace}")
    console.print(f"turns={session.turns} tools={session.tool_calls}")
    if session.verification:
        console.print(f"verification: {session.verification.get('summary')}")
        if session.verification.get("patch_path"):
            console.print(f"patch: {session.verification['patch_path']}")
    if session.last_error:
        console.print(f"[yellow]{session.last_error}[/yellow]")


@app.command()
def web(
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8765,
    image: Annotated[str, typer.Option()] = "repofix-runner:py312",
    home: Annotated[Path | None, typer.Option()] = None,
    no_open: Annotated[
        bool, typer.Option("--no-open", help="Do not open the browser automatically")
    ] = False,
) -> None:
    """Start the local browser control panel."""
    from repofix.web import serve

    serve(_paths(home), port=port, docker_image=image, open_browser=not no_open)


@app.command()
def run(
    repo: Annotated[
        Path,
        typer.Option(
            "--project",
            "--repo",
            help="Git project or ordinary local folder",
            exists=True,
            file_okay=False,
            resolve_path=True,
        ),
    ],
    issue: Annotated[str, typer.Option(prompt=True)],
    accept: Annotated[
        list[str] | None, typer.Option("--accept", help="Verifier command; repeatable")
    ] = None,
    protected_tests: Annotated[Path | None, typer.Option(exists=True, resolve_path=True)] = None,
    executor: Annotated[str, typer.Option(help="docker or local (trusted code only)")] = "docker",
    model: Annotated[str | None, typer.Option()] = None,
    image: Annotated[str, typer.Option()] = "repofix-runner:py312",
    home: Annotated[Path | None, typer.Option()] = None,
    max_turns: Annotated[int, typer.Option()] = 30,
    max_tool_calls: Annotated[int, typer.Option()] = 100,
    max_minutes: Annotated[int, typer.Option()] = 30,
) -> None:
    """Create an isolated workspace and run a new repair task."""
    if executor not in {"docker", "local"}:
        raise typer.BadParameter("executor must be docker or local")
    paths = _paths(home)
    client, config = _client(model, paths)
    store = SessionStore(paths)
    task = TaskSpec(
        issue=issue,
        acceptance_commands=accept or ["pytest -q"],
        protected_tests=str(protected_tests) if protected_tests else None,
    )
    budget = Budget(
        max_turns=max_turns, max_tool_calls=max_tool_calls, max_wall_seconds=max_minutes * 60
    )
    try:
        session = AgentHarness.create_session(
            store, repo, task, model=config.model, executor=executor, budget=budget
        )
        console.print(f"Created session [bold]{session.id}[/bold]")
        outcome = asyncio.run(AgentHarness(store, client, docker_image=image).run(session.id))
        _print_session(outcome)
        raise typer.Exit(code=0 if outcome.status == SessionStatus.COMPLETED else 2)
    finally:
        store.close()


@app.command()
def resume(
    session_id: str,
    model: Annotated[str | None, typer.Option()] = None,
    image: Annotated[str, typer.Option()] = "repofix-runner:py312",
    home: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Resume from durable messages, repository state, and execution records."""
    paths = _paths(home)
    client, _ = _client(model, paths)
    store = SessionStore(paths)
    try:
        outcome = asyncio.run(AgentHarness(store, client, docker_image=image).run(session_id))
        _print_session(outcome)
        raise typer.Exit(code=0 if outcome.status == SessionStatus.COMPLETED else 2)
    finally:
        store.close()


@app.command()
def inspect(
    session_id: str,
    home: Annotated[Path | None, typer.Option()] = None,
    limit: Annotated[int, typer.Option()] = 30,
) -> None:
    """Show session state and recent trace events."""
    store = SessionStore(_paths(home))
    try:
        session = store.get(session_id)
        _print_session(session)
        table = Table("seq", "kind", "summary/time")
        for event in store.events(session_id)[-limit:]:
            summary = (
                event.payload.get("summary") or event.payload.get("status") or event.created_at
            )
            table.add_row(str(event.seq), event.kind, str(summary)[:100])
        console.print(table)
    finally:
        store.close()


@app.command()
def cancel(
    session_id: str,
    home: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Request cooperative cancellation at the next safe runtime boundary."""
    store = SessionStore(_paths(home))
    try:
        store.request_cancel(session_id)
        console.print(f"Cancellation requested for {session_id}")
    finally:
        store.close()


@app.command()
def verify(
    session_id: str,
    image: Annotated[str, typer.Option()] = "repofix-runner:py312",
    home: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Run the independent verifier without another model turn."""
    store = SessionStore(_paths(home))
    try:
        session = store.get(session_id)
        result = asyncio.run(
            IndependentVerifier(
                Path(session.workspace),
                session.task,
                store,
                session.id,
                session.executor,
                image,
                session.base_commit,
            ).verify()
        )
        console.print_json(result.model_dump_json())
        raise typer.Exit(code=0 if result.passed else 2)
    finally:
        store.close()


@app.command("export-handoff")
def export_handoff(
    session_id: str,
    home: Annotated[Path | None, typer.Option()] = None,
) -> None:
    store = SessionStore(_paths(home))
    try:
        session = store.get(session_id)
        content = handoff_summary(store.events(session_id), Path(session.workspace), session.task)
        path = store.write_artifact(session_id, "handoff.md", content)
        console.print(path)
    finally:
        store.close()


@app.command()
def replay(
    session_id: str,
    output: Annotated[Path | None, typer.Option()] = None,
    home: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Export the recorded trace; this does not re-execute model calls or tools."""
    store = SessionStore(_paths(home))
    try:
        payload = [event.model_dump(mode="json") for event in store.events(session_id)]
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        if output:
            output.write_text(rendered, encoding="utf-8")
            console.print(output.resolve())
        else:
            console.print_json(rendered)
    finally:
        store.close()


@app.command()
def cleanup(
    session_id: str,
    home: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Remove a task worktree after preserving its trace and exported artifacts."""
    store = SessionStore(_paths(home))
    try:
        session = store.get(session_id)
        if session.status == SessionStatus.RUNNING:
            raise typer.BadParameter("A running session cannot be cleaned")
        workspace_source = Path(session.workspace_source or session.source_repo)
        WorkspaceManager.remove(workspace_source, Path(session.workspace))
        if session.source_kind == "folder" and workspace_source.exists():
            WorkspaceManager.delete_managed_source(workspace_source, store.paths.sources)
        session.status = SessionStatus.CLEANED
        store.save(session)
        store.append_event(session_id, "workspace_cleaned", {"workspace": session.workspace})
        console.print(f"Removed worktree {session.workspace}; trace and artifacts were preserved")
    finally:
        store.close()


@app.command("eval")
def evaluate(
    manifest: Annotated[Path, typer.Argument(exists=True, dir_okay=False, resolve_path=True)],
    executor: Annotated[str, typer.Option()] = "docker",
    model: Annotated[str | None, typer.Option()] = None,
    image: Annotated[str, typer.Option()] = "repofix-runner:py312",
    home: Annotated[Path | None, typer.Option()] = None,
    output: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Run a reproducible task manifest and report completion metrics."""
    paths = _paths(home)
    client, config = _client(model, paths)
    store = SessionStore(paths)
    try:
        report = asyncio.run(
            run_evaluation(
                manifest,
                store,
                client,
                model_name=config.model,
                executor=executor,
                docker_image=image,
            )
        )
        rendered = report.model_dump_json(indent=2)
        if output:
            output.write_text(rendered, encoding="utf-8")
        console.print_json(rendered)
    finally:
        store.close()


if __name__ == "__main__":
    app()
