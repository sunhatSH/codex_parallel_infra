"""CLI entrypoint: codex-parallel <command>.

Standalone CLI package, decoupled from the backend (`codex_parallel`):
it talks to the hub over HTTP, so it can be installed on a laptop while
the hub runs on the deployment machine. The one exception is `serve`,
which imports the backend to start it locally.

Usage:
  python -m codex_parallel_cli <command>      (from repo root with ./cli on path)
  codex-parallel <command>                    (installed console script)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import typer

app = typer.Typer(no_args_is_help=True, help="codex-parallel hub control")
token_app = typer.Typer(no_args_is_help=True, help="token pool management")
task_app = typer.Typer(no_args_is_help=True, help="task management")
app.add_typer(token_app, name="token")
app.add_typer(task_app, name="task")

DEFAULT_URL = "http://127.0.0.1:8787"


def _url(base: str) -> str:
    return base.rstrip("/")


def _client(base: str) -> httpx.Client:
    return httpx.Client(base_url=_url(base), timeout=30.0)


def _print(data, raw: bool) -> None:
    if raw:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2))


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", help="bind host"),
    port: int = typer.Option(8787, help="bind port"),
    home: str = typer.Option(None, help="data dir (CODEX_PARALLEL_HOME)"),
) -> None:
    """Start the hub service (blocks; run under tmux/nohup/systemd)."""
    import uvicorn

    from codex_parallel.config import load_config
    from codex_parallel.server import create_app

    cfg = load_config(host=host, port=port, root=Path(home).expanduser() if home else None)
    uvicorn.run(create_app, factory=True, host=cfg.host, port=cfg.port)


@app.command()
def status(base: str = typer.Option(DEFAULT_URL, "--url")) -> None:
    """Hub health + token pool + task summary + active alerts."""
    with _client(base) as c:
        health = c.get("/api/health").json()
        tokens = c.get("/api/tokens/stats").json()
        tasks = c.get("/api/tasks").json()
        alerts = c.get("/api/alerts").json()
    running = [t for t in tasks if t["status"] == "running"]
    print(f"hub: {health.get('hub_id')} ({'UP' if health.get('ok') else 'DOWN'})")
    print(
        f"tokens: {tokens['active']} active / {tokens['banned']} banned / "
        f"{tokens['discarded']} discarded / {tokens['in_use']} in-use"
    )
    print(f"tasks: {len(tasks)} total, {len(running)} running")
    if alerts:
        print()
        typer.secho("⚠ ACTIVE ALERTS:", fg=typer.colors.RED, err=True)
        for a in alerts:
            typer.secho(f"  [#{a['id']}] {a['message']}", fg=typer.colors.RED, err=True)
        typer.secho(
            "  补充 token: codex-parallel token add-bulk <file>", fg=typer.colors.YELLOW, err=True
        )


@app.command()
def alerts(
    base: str = typer.Option(DEFAULT_URL, "--url"),
    resolve: int = typer.Option(None, "--resolve", help="alert id to resolve"),
) -> None:
    """List (or resolve) token-pool alerts."""
    with _client(base) as c:
        if resolve is not None:
            r = c.post(f"/api/alerts/{resolve}/resolve")
            r.raise_for_status()
            _print(r.json(), False)
            return
        _print(c.get("/api/alerts").json(), False)


# ── token commands ──────────────────────────────────────────────────── #

@token_app.command("list")
def token_list(
    base: str = typer.Option(DEFAULT_URL, "--url"),
    raw: bool = typer.Option(False, "--raw", help="raw JSON"),
) -> None:
    with _client(base) as c:
        _print(c.get("/api/tokens").json(), raw)


@token_app.command("add")
def token_add(
    label: str = typer.Argument(...),
    secret: str = typer.Option(..., "--secret", help="token value; or - for stdin"),
    max_concurrency: int = typer.Option(1),
    base: str = typer.Option(DEFAULT_URL, "--url"),
) -> None:
    if secret == "-":
        secret = sys.stdin.read().strip()
    with _client(base) as c:
        r = c.post(
            "/api/tokens",
            json={"label": label, "secret": secret, "max_concurrency": max_concurrency},
        )
        r.raise_for_status()
        _print(r.json(), False)


@token_app.command("add-bulk")
def token_add_bulk(
    file: Path = typer.Argument(..., help="file with one token per line; - for stdin"),
    label_prefix: str = typer.Option("bulk"),
    max_concurrency: int = typer.Option(1),
    base: str = typer.Option(DEFAULT_URL, "--url"),
) -> None:
    """Split newline-separated tokens into individual pool entries."""
    if str(file) == "-":
        blob = sys.stdin.read()
    else:
        blob = file.read_text()
    with _client(base) as c:
        r = c.post(
            "/api/tokens/bulk",
            json={
                "blob": blob,
                "label_prefix": label_prefix,
                "max_concurrency": max_concurrency,
            },
        )
        r.raise_for_status()
        _print(r.json(), False)


@token_app.command("import-xlsx")
def token_import_xlsx(
    file: Path = typer.Argument(..., help="xlsx on the HUB machine (10个gpt.xlsx layout)"),
    exchange: bool = typer.Option(
        True, "--exchange/--no-exchange",
        help="RT→auth.json exchange for expired tokens (needs network to auth.openai.com)",
    ),
    base: str = typer.Option(DEFAULT_URL, "--url"),
) -> None:
    """Import provider xlsx accounts (col A label, col P token bundle)."""
    with _client(base) as c:
        r = c.post(
            "/api/tokens/import-xlsx",
            json={"path": str(file.resolve()), "exchange": exchange},
        )
        r.raise_for_status()
        d = r.json()
    print(f"added:   {len(d['added'])}")
    for label in d["added"]:
        print(f"  + {label}")
    if d["skipped"]:
        print(f"skipped: {len(d['skipped'])}")
        for label, why in d["skipped"]:
            print(f"  - {label}: {why}")
    print(f"pool:    {d['stats']}")


@token_app.command("disable")
def token_disable(
    token_id: int = typer.Argument(...),
    base: str = typer.Option(DEFAULT_URL, "--url"),
) -> None:
    with _client(base) as c:
        r = c.post(f"/api/tokens/{token_id}/status", json={"status": "disabled"})
        r.raise_for_status()
        _print(r.json(), False)


@token_app.command("enable")
def token_enable(
    token_id: int = typer.Argument(...),
    base: str = typer.Option(DEFAULT_URL, "--url"),
) -> None:
    with _client(base) as c:
        r = c.post(f"/api/tokens/{token_id}/status", json={"status": "active"})
        r.raise_for_status()
        _print(r.json(), False)


# ── task commands ───────────────────────────────────────────────────── #

@task_app.command("submit")
def task_submit(
    name: str = typer.Option(..., "--name"),
    prompt_template: str = typer.Option(..., "--prompt", help="must contain {{item}}"),
    items_file: Path = typer.Option(..., "--items", help="one item per line"),
    concurrency: int = typer.Option(8, "--concurrency", "-c"),
    backend: str = typer.Option("local", "--backend"),
    model: str = typer.Option(None, "--model"),
    base: str = typer.Option(DEFAULT_URL, "--url"),
) -> None:
    with _client(base) as c:
        codex_config = {}
        if model:
            codex_config["model"] = model
        r = c.post(
            "/api/tasks",
            json={
                "name": name,
                "prompt_template": prompt_template,
                "items_file": str(items_file),
                "concurrency": concurrency,
                "backend": backend,
                "codex_config": codex_config,
            },
        )
        r.raise_for_status()
        _print(r.json(), False)


@task_app.command("list")
def task_list(
    base: str = typer.Option(DEFAULT_URL, "--url"),
    raw: bool = typer.Option(False, "--raw"),
) -> None:
    with _client(base) as c:
        _print(c.get("/api/tasks").json(), raw)


@task_app.command("show")
def task_show(
    task_id: int = typer.Argument(...),
    base: str = typer.Option(DEFAULT_URL, "--url"),
    raw: bool = typer.Option(False, "--raw"),
) -> None:
    with _client(base) as c:
        _print(c.get(f"/api/tasks/{task_id}").json(), raw)


@task_app.command("stop")
def task_stop(
    task_id: int = typer.Argument(...),
    base: str = typer.Option(DEFAULT_URL, "--url"),
) -> None:
    with _client(base) as c:
        r = c.post(f"/api/tasks/{task_id}/stop")
        r.raise_for_status()
        _print(r.json(), False)


@task_app.command("cancel")
def task_cancel(
    task_id: int = typer.Argument(...),
    base: str = typer.Option(DEFAULT_URL, "--url"),
) -> None:
    with _client(base) as c:
        r = c.post(f"/api/tasks/{task_id}/cancel")
        r.raise_for_status()
        _print(r.json(), False)


@task_app.command("results")
def task_results(
    task_id: int = typer.Argument(...),
    base: str = typer.Option(DEFAULT_URL, "--url"),
    raw: bool = typer.Option(False, "--raw"),
) -> None:
    with _client(base) as c:
        _print(c.get(f"/api/tasks/{task_id}/results").json(), raw)


if __name__ == "__main__":
    app()
