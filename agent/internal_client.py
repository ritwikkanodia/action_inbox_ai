"""A tiny HTTP client for the app's `/internal/*` routes.

Used by the agent's tools when they run in a process that must not open the
database — the per-user Hermes in the cloud, and later a worker on the user's
Mac. One bearer token per turn (`db.mint_run_token`), JSON in and out, and
every failure is an `InternalApiError` the tool layer turns into an `Error:`
line. Standard library only: the MCP server that imports this is spawned
inside Hermes' startup budget, and `requests` would cost an import for nothing.
"""

import json
import urllib.error
import urllib.parse
import urllib.request


class InternalApiError(Exception):
    """A non-2xx from the internal API, or no API at all (status 0)."""

    def __init__(self, status: int, message: str):
        super().__init__(f"internal API {status}: {message}" if status else message)
        self.status = status
        self.message = message


class InternalClient:
    def __init__(self, api_url: str, token: str, timeout: float = 10.0):
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _call(self, method: str, path: str, params: dict | None = None, body=None):
        url = self.api_url + path
        if params:
            query = {k: v for k, v in params.items() if v is not None}
            if query:
                url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read() or b"{}").get("error") or exc.reason
            except Exception:
                message = exc.reason
            raise InternalApiError(exc.code, str(message)) from None
        except urllib.error.URLError as exc:
            raise InternalApiError(0, f"internal API unreachable: {exc.reason}") from None
        return json.loads(raw) if raw else None

    def get(self, path: str, params: dict | None = None):
        return self._call("GET", path, params=params)

    def post(self, path: str, body):
        return self._call("POST", path, body=body)

    def patch(self, path: str, body):
        return self._call("PATCH", path, body=body)
