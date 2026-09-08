"""uvicorn runners for the approval shim.

The FastAPI app lives in :mod:`.approval_asgi`; this module just spins
uvicorn against it — either blocking (``run_shim``) or in a daemon
thread (``start_shim_thread``, used by the paper scripts).

Uvicorn's own ``run()`` installs signal handlers, which requires the
main thread. For the thread-launched variant we build a
``uvicorn.Config`` + ``uvicorn.Server`` manually and run its ``serve()``
coroutine on a fresh event loop inside the thread.
"""
from __future__ import annotations

import asyncio
import secrets
import sys
import threading
from pathlib import Path

from ..intel.vs_engine import DEFAULT_WRITEUP_DIR
from .approval import CardRegistry, InMemoryApprovalStore
from .approval_asgi import create_app, mint_auth_token  # noqa: F401 (re-export)


def run_shim(
    store: InMemoryApprovalStore,
    registry: CardRegistry,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    writeup_dir: Path = DEFAULT_WRITEUP_DIR,
    auth_token: str | None = None,
) -> None:
    """Blocking uvicorn.run() — call from the main thread only."""
    import uvicorn
    app = create_app(store, registry,
                     writeup_dir=writeup_dir, auth_token=auth_token)
    sys.stderr.write(
        f"approval shim listening on http://{host}:{port}"
        f"{' (auth required)' if auth_token else ' (OPEN — loopback only)'}\n"
    )
    uvicorn.run(app, host=host, port=port, log_level="warning",
                access_log=False)


def start_shim_thread(
    store,                       # InMemoryApprovalStore | SqliteApprovalStore
    registry,                    # CardRegistry | SqliteCardRegistry
    host: str,
    port: int,
    writeup_dir: Path | None = None,
    auth_token: str | None = None,
) -> threading.Thread:
    """Daemon-thread wrapper. Runs uvicorn.Server on its own event loop so
    it doesn't compete with the main thread's signal handling."""
    import uvicorn
    resolved = writeup_dir if writeup_dir is not None else DEFAULT_WRITEUP_DIR
    app = create_app(store, registry, writeup_dir=resolved, auth_token=auth_token)
    config = uvicorn.Config(app, host=host, port=port,
                            log_level="warning", access_log=False,
                            lifespan="off")
    server = uvicorn.Server(config)

    def _serve() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(server.serve())
        except Exception as e:  # pragma: no cover
            sys.stderr.write(f"[approval-shim] died: {e}\n")
        finally:
            loop.close()

    t = threading.Thread(target=_serve, name="approval-shim", daemon=True)
    t.start()
    return t
