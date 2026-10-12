"""Force the e2b SDK onto HTTP/1.1 — eliminates gateway GOAWAY on long tasks.

Ported from agentic_cl_research/src/rollout/e2b_http1_patch.py (battle-tested
there against the same Tencent AGS gateway):

Tencent AGS's ingress gateway recycles an HTTP/2 connection with GOAWAY after
~1000 cumulative streams. The e2b SDK builds its httpx transport with
``http2=True`` and long-lived keepalive connections, so during a LONG task
(codex exec can run 10-30 min) the envd command/filesystem streaming channel
hits GOAWAY — surfacing as RemoteProtocolError / ConnectionState.CLOSED,
killing the in-flight item.

HTTP/1.1 has no per-connection stream limit (one request per connection,
keepalive manages recycling), so flipping the default removes the failure
mode at the root.

Implementation: e2b's transport factories
(``e2b.api.client_async.get_transport`` / ``get_envd_transport`` and their
sync twins) declare ``http2: bool = True`` as a DEFAULT PARAMETER that no
caller ever overrides — flipping the ``__defaults__`` entry makes it global.
Idempotent; set CODEX_PARALLEL_E2B_HTTP2=1 to keep HTTP/2.
"""

from __future__ import annotations

import os

_PATCHED = False


def _flip_http2_default(func) -> bool:
    import inspect

    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    params = list(sig.parameters.values())
    defaults = list(func.__defaults__ or ())
    if not defaults:
        return False
    defaulted = [p for p in params if p.default is not inspect.Parameter.empty]
    for idx, p in enumerate(defaulted):
        if p.name == "http2" and idx < len(defaults):
            if defaults[idx] is False:
                return False
            defaults[idx] = False
            func.__defaults__ = tuple(defaults)
            return True
    return False


def install() -> None:
    """Monkey-patch e2b transport factories to ``http2=False``. Idempotent."""
    global _PATCHED
    if _PATCHED:
        return
    if os.getenv("CODEX_PARALLEL_E2B_HTTP2", "0") == "1":
        return  # opt back into HTTP/2 explicitly

    try:
        from e2b.api import client_async, client_sync
    except Exception:  # noqa: BLE001 — e2b not installed (local-only dev box)
        return

    flipped = []
    for mod, name in (
        (client_async, "get_transport"),
        (client_async, "get_envd_transport"),
        (client_sync, "get_transport"),
        (client_sync, "get_envd_transport"),
    ):
        func = getattr(mod, name, None)
        if func is not None and _flip_http2_default(func):
            flipped.append(f"{mod.__name__.rsplit('.', 1)[-1]}.{name}")

    _PATCHED = True
    if flipped:
        import logging

        logging.getLogger("codex_parallel.backends.e2b").info(
            "e2b transport forced to HTTP/1.1 (GOAWAY guard): %s",
            ", ".join(flipped),
        )


install()
