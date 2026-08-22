"""QuantConnect REST API v2 client.

Drives the QuantConnect cloud from this repo: create projects, upload files,
compile, launch backtests, and read their results. Authentication follows QC's
timestamped-hash scheme (verified against the v2 docs):

    timestamped = f"{api_token}:{unix_ts}"
    hashed      = sha256(timestamped).hexdigest()
    header      = "Basic " + base64(f"{user_id}:{hashed}")
    Timestamp:  {unix_ts}

Every v2 response carries a ``success`` boolean; non-success raises
``QuantConnectError`` with the ``errors`` payload. Uses ``httpx`` (repo standard,
not ``requests``) and is fully mockable with ``respx`` — no live calls in tests.

Credentials are NOT hardcoded: they come from ``Settings`` (``.env``), so the
token never lives in code or the repo.
"""
from __future__ import annotations

import base64
import hashlib
import time

import httpx

from ..logging_setup import get_logger

log = get_logger(__name__)

QC_API_BASE = "https://www.quantconnect.com/api/v2"


class QuantConnectError(RuntimeError):
    """A QuantConnect API call returned success=false or a transport error."""


class QuantConnectClient:
    """Thin, typed wrapper over the QuantConnect REST API v2."""

    def __init__(
        self,
        user_id: str,
        api_token: str,
        *,
        base_url: str = QC_API_BASE,
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not user_id or not api_token:
            raise QuantConnectError(
                "QuantConnect user_id and api_token are required. Set "
                "QUANTCONNECT_USER_ID and QUANTCONNECT_API_TOKEN in .env."
            )
        self.user_id = user_id
        self.api_token = api_token
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None

    # ----- auth ---------------------------------------------------------------

    def _auth_headers(self, timestamp: int | None = None) -> dict[str, str]:
        ts = int(timestamp if timestamp is not None else time.time())
        hashed = hashlib.sha256(f"{self.api_token}:{ts}".encode()).hexdigest()
        token = base64.b64encode(f"{self.user_id}:{hashed}".encode()).decode()
        return {"Authorization": f"Basic {token}", "Timestamp": str(ts)}

    # ----- transport ----------------------------------------------------------

    def _post(self, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        url = f"{self.base_url}{path}"
        try:
            resp = self._client.post(url, headers=self._auth_headers(), json=payload or {})
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise QuantConnectError(f"QuantConnect request to {path} failed: {e}") from e
        data: dict[str, object] = resp.json()
        if not data.get("success", False):
            raise QuantConnectError(f"QuantConnect {path} returned errors: {data.get('errors')}")
        return data

    # ----- endpoints ----------------------------------------------------------

    def authenticate(self) -> bool:
        """Verify credentials via ``/authenticate``. Returns True or raises."""
        return bool(self._post("/authenticate").get("success", False))

    def create_project(self, name: str, language: str = "Py") -> dict[str, object]:
        """Create a cloud project. ``language`` is 'Py' or 'C#'."""
        return self._post("/projects/create", {"name": name, "language": language})

    def create_file(self, project_id: int, name: str, content: str) -> dict[str, object]:
        """Add a new code file to a project (errors if it already exists)."""
        return self._post(
            "/files/create", {"projectId": project_id, "name": name, "content": content}
        )

    def update_file(self, project_id: int, name: str, content: str) -> dict[str, object]:
        """Overwrite an existing file's contents."""
        return self._post(
            "/files/update", {"projectId": project_id, "name": name, "content": content}
        )

    def put_file(self, project_id: int, name: str, content: str) -> dict[str, object]:
        """Create the file, or update it if QC seeded it already (new Py projects ship
        a default ``main.py``). Robust to both cases."""
        try:
            return self.create_file(project_id, name, content)
        except QuantConnectError:
            return self.update_file(project_id, name, content)

    def compile_project(self, project_id: int) -> dict[str, object]:
        """Kick off a compile; the returned ``compileId`` feeds create_backtest.

        Compilation is asynchronous — the returned ``compileId`` is not
        build-complete yet. Poll with ``wait_for_compile`` before backtesting.
        """
        return self._post("/compile/create", {"projectId": project_id})

    def read_compile(self, project_id: int, compile_id: str) -> dict[str, object]:
        """Read a compile's state ('InQueue' | 'BuildSuccess' | 'BuildError')."""
        return self._post(
            "/compile/read", {"projectId": project_id, "compileId": compile_id}
        )

    def wait_for_compile(
        self,
        project_id: int,
        compile_id: str,
        *,
        poll_seconds: float = 2.0,
        timeout_seconds: float = 180.0,
    ) -> dict[str, object]:
        """Poll ``read_compile`` until the build succeeds, errors, or times out."""
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            data = self.read_compile(project_id, compile_id)
            state = data.get("state")
            if state == "BuildSuccess":
                return data
            if state == "BuildError":
                raise QuantConnectError(f"Compile failed: {data.get('logs')}")
            time.sleep(poll_seconds)
        raise QuantConnectError(f"Compile {compile_id} did not finish within timeout")

    def create_backtest(
        self, project_id: int, compile_id: str, name: str
    ) -> dict[str, object]:
        """Launch a cloud backtest against a successful compile."""
        return self._post(
            "/backtests/create",
            {"projectId": project_id, "compileId": compile_id, "backtestName": name},
        )

    # ----- library (read own account's projects/files) ------------------------

    def list_projects(self) -> list[dict[str, object]]:
        """Return all projects in the account (name, projectId, language, …).

        This is the accessible way to 'search the QC library': read the strategies
        you have cloned from the Strategy Library into your own account. (Alpha
        Streams is retired; there is no global-library search API.)
        """
        data = self._post("/projects/read")
        projects = data.get("projects", [])
        return list(projects) if isinstance(projects, list) else []

    def read_project(self, project_id: int) -> dict[str, object]:
        """Read a single project's metadata."""
        return self._post("/projects/read", {"projectId": project_id})

    def list_files(self, project_id: int) -> list[dict[str, object]]:
        """List a project's files (names + contents)."""
        data = self._post("/files/read", {"projectId": project_id})
        files = data.get("files", [])
        return list(files) if isinstance(files, list) else []

    def read_file(self, project_id: int, name: str) -> str:
        """Read one file's source text from a project."""
        data = self._post("/files/read", {"projectId": project_id, "name": name})
        files = data.get("files", [])
        if isinstance(files, list) and files and isinstance(files[0], dict):
            return str(files[0].get("content", ""))
        return ""

    def read_backtest(self, project_id: int, backtest_id: str) -> dict[str, object]:
        """Read one backtest's status/statistics."""
        return self._post(
            "/backtests/read", {"projectId": project_id, "backtestId": backtest_id}
        )

    def list_backtests(self, project_id: int) -> list[dict[str, object]]:
        """List a project's backtests (id, name, status, completion, …)."""
        data = self._post("/backtests/list", {"projectId": project_id})
        backtests = data.get("backtests", [])
        return list(backtests) if isinstance(backtests, list) else []

    def wait_for_backtest(
        self,
        project_id: int,
        backtest_id: str,
        *,
        poll_seconds: float = 5.0,
        timeout_seconds: float = 900.0,
    ) -> dict[str, object]:
        """Poll ``read_backtest`` until it completes, errors, or times out."""
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            data = self.read_backtest(project_id, backtest_id)
            backtest = data.get("backtest", {})
            if isinstance(backtest, dict) and backtest.get("completed"):
                return data
            if isinstance(backtest, dict) and backtest.get("error"):
                raise QuantConnectError(f"Backtest errored: {backtest.get('error')}")
            time.sleep(poll_seconds)
        raise QuantConnectError(f"Backtest {backtest_id} did not complete within timeout")

    # ----- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> QuantConnectClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
