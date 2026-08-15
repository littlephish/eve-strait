"""IPC between the MCP server process and the running Eve-Strait window.

Claude Desktop spawns ``eve-strait --mcp`` as its own process. That process has
its own copy of the map and no handle on your open window, so on its own it can
answer questions but cannot move a waypoint you can see. This bridge closes
that gap: the GUI listens, the MCP process connects, and the tools that need
live panels are forwarded to the real app.

**Deliberately not a network socket.** A loopback TCP port is reachable by any
process on the machine and has to invent its own authentication. This uses the
OS primitives instead:

* Windows: a named pipe at ``\\\\.\\pipe\\eve-strait-<user>``. Never touches the
  network stack.
* POSIX: a Unix domain socket in the data directory, created 0600, so the
  kernel refuses anyone but the owning user before a byte is read.

On top of that, ``multiprocessing.connection`` performs an HMAC
challenge-response with a key generated on first use and stored 0600 beside the
settings. That matters most on Windows, where a named pipe's default security
descriptor is more permissive than a 0600 file.

If the app is not running, connecting fails immediately rather than hanging, so
the MCP server can say so and fall back to what it can answer headless.
"""
from __future__ import annotations

import os
import secrets
import sys
import threading
from multiprocessing.connection import AuthenticationError, Client, Listener

from .. import config

FAMILY = "AF_PIPE" if sys.platform == "win32" else "AF_UNIX"
TOKEN_PATH = config.DATA_DIR / "bridge-token"
# Long enough that a request which blocks on the UI thread gives up rather than
# wedging Claude Desktop, short enough that a hung app is obvious.
TIMEOUT = 30.0


def address() -> str:
    if sys.platform == "win32":
        # Per-user, so two accounts on one machine never share a pipe.
        user = (os.environ.get("USERNAME") or "user").replace("\\", "_")
        return r"\\.\pipe\eve-strait-" + user
    return str(config.DATA_DIR / "bridge.sock")


def _token() -> bytes:
    """Shared secret, created on first use and readable only by this user."""
    try:
        if TOKEN_PATH.exists():
            data = TOKEN_PATH.read_bytes().strip()
            if len(data) >= 16:
                return data
    except OSError:
        pass
    token = secrets.token_hex(32).encode()
    try:
        TOKEN_PATH.write_bytes(token)
        if sys.platform != "win32":
            os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass
    return token


# ---------------------------------------------------------------------------
# Server side: runs inside the GUI process
# ---------------------------------------------------------------------------
class BridgeServer:
    """Accepts tool calls from the MCP process and runs them on the app."""

    def __init__(self, app):
        self.app = app
        self._listener = None
        self._thread = None
        self._stop = threading.Event()

    def start(self) -> bool:
        if self._thread is not None:
            return True
        addr = address()
        if sys.platform != "win32":
            # A socket file left behind by a crash would block the bind.
            try:
                os.unlink(addr)
            except OSError:
                pass
        try:
            self._listener = Listener(addr, family=FAMILY, authkey=_token())
            if sys.platform != "win32":
                os.chmod(addr, 0o600)
        except OSError:
            return False                    # another instance already owns it
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="eve-strait-bridge")
        self._thread.start()
        return True

    def stop(self):
        """Close the pipe and wait for the accept loop to actually exit.

        Closing the listener is not enough on its own: the serve thread is
        parked inside accept() and stays there, so the pipe keeps answering
        after the feature has been switched off. Waking it with one throwaway
        connection lets it observe the stop flag and return.
        """
        self._stop.set()
        try:
            conn = Client(address(), family=FAMILY, authkey=_token())
            conn.close()
        except Exception:
            pass                            # nothing listening; fine
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        try:
            if self._listener is not None:
                self._listener.close()
                self._listener = None
        except OSError:
            pass
        if sys.platform != "win32":
            try:
                os.unlink(address())
            except OSError:
                pass

    def _serve(self):
        from . import tools
        while not self._stop.is_set():
            try:
                conn = self._listener.accept()
            except Exception:               # closed, or a failed handshake
                if self._stop.is_set():
                    return
                continue
            if self._stop.is_set():
                # The wake-up connection from stop(); nothing to serve.
                try:
                    conn.close()
                except Exception:
                    pass
                return
            try:
                request = conn.recv()
                name = (request or {}).get("tool")
                args = (request or {}).get("args") or {}
                if name == "__ping__":
                    conn.send({"ok": True, "result": "alive"})
                    continue
                tool = tools.BY_NAME.get(name)
                if tool is None:
                    conn.send({"ok": False, "error": f"No such tool {name!r}."})
                    continue
                # run_ai_tool marshals onto the UI thread when the tool writes.
                out = self.app.run_ai_tool(tool, args)
                conn.send({"ok": True, "result": out if isinstance(out, str)
                           else str(out)})
            except Exception as exc:
                try:
                    conn.send({"ok": False,
                               "error": f"{type(exc).__name__}: {exc}"})
                except Exception:
                    pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Client side: runs inside the MCP process
# ---------------------------------------------------------------------------
class NotRunning(RuntimeError):
    """Eve-Strait is not open, so live-map tools cannot be served."""


def call(tool_name: str, args: dict) -> str:
    """Forward one tool call to the running app.

    Raises NotRunning when there is no app to talk to, which the caller turns
    into an explanation rather than a crash.
    """
    try:
        conn = Client(address(), family=FAMILY, authkey=_token())
    except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
        raise NotRunning(
            "Eve-Strait is not running, so I cannot change the map you are "
            "looking at. Start Eve-Strait and try again.") from exc
    except AuthenticationError as exc:
        # AuthenticationError is not an OSError, so it fell through the clause
        # above uncaught and leaked "digest sent was rejected" to whoever
        # called us -- meaningless to a caller expecting "is it running or
        # not". The pipe name is per-OS-user only, not per-install, so this is
        # reachable whenever a second eve-strait process (a different profile,
        # a dev checkout beside an installed build) is bound to the same
        # address with a different bridge-token: the connection succeeds at
        # the transport level and fails the HMAC challenge instead, which is
        # a different failure than "nothing is listening".
        raise NotRunning(
            "Something else answered on Eve-Strait's IPC pipe but did not "
            "recognise this install's key, so this could not be verified as "
            "the same Eve-Strait. If another copy of Eve-Strait is running "
            "under this Windows account, close it and try again.") from exc
    try:
        conn.send({"tool": tool_name, "args": args})
        if not conn.poll(TIMEOUT):
            raise RuntimeError("Eve-Strait did not respond in time.")
        reply = conn.recv()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error", "unknown error"))
    return reply.get("result", "")


def app_is_running() -> bool:
    try:
        return call("__ping__", {}) == "alive"
    except Exception:
        return False
