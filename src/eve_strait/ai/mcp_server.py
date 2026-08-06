"""MCP server: let Claude Desktop drive Eve-Strait with no API key.

Runs as ``eve-strait --mcp``, a child process Claude Desktop spawns. Transport
is JSON-RPC 2.0 over stdin/stdout, which is the whole security story: there is
**no network listener**, no port, no socket. Nothing here is reachable from
another machine, and nothing is reachable from another user's session.

The protocol subset is implemented directly rather than pulling in an SDK. It
is three methods, and a desktop app that ships through Nuitka does not need
another dependency tree for that.

What it deliberately does NOT do:

* Serve anything unless ``mcp_enabled`` is true. The check is here, not only
  in the UI, so a stale Claude Desktop entry pointing at this exe stops
  working the moment the user unticks the box.
* Expose write tools unless ``mcp_allow_writes`` is true.
* Expose ESI-authenticated data (characters, locations, structure names,
  standings) unless ``mcp_allow_private`` is true. That is the real
  intelligence leak and it is off even when the server is on.
* Touch the running GUI *directly*. This is a separate process with its own
  copy of the map. Tools that need live panels are forwarded to the open
  window over the IPC bridge (see bridge.py: a Windows named pipe or a Unix
  domain socket, never a network port). If the app is not running they are
  still listed, and calling one explains that rather than silently mutating a
  route nobody can see.

Every call is appended to ``mcp-audit.log`` beside the settings, so the user
can see exactly what was asked for and when.
"""
from __future__ import annotations

import json
import sys
import time

from .. import config

PROTOCOL = "2024-11-05"

# Tools that need the running Qt app. Served by forwarding to the open window
# over the bridge rather than executed in this process.
_UI_ONLY = {"add_waypoint", "remove_waypoint", "clear_waypoints", "set_ship",
            "set_options", "auto_route", "get_route", "get_setup"}
# Tools that read ESI-authenticated data about your characters.
_PRIVATE: set[str] = set()          # get_setup is UI-only already
# Tools that change stored state.
_WRITES = {"set_system_note", "avoid_system"}


def _audit(message: str) -> None:
    try:
        with open(config.DATA_DIR / "mcp-audit.log", "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except OSError:
        pass


class _Headless:
    """The slice of MainWindow the servable tools actually touch.

    Only the read-only, config-backed surface. Anything needing a panel is
    excluded from the tool list, so nothing here has to fake a widget.
    """

    def __init__(self):
        from ..data.universe import Universe
        self.universe = Universe.load()
        self.universe.set_bridges(config.get_bridges())
        self.avoided_ids = {s.id for s in
                            (self.universe.by_name(n) for n in config.get_avoided())
                            if s is not None}
        self.kill_activity: dict = {}
        self.jump_activity: dict = {}
        self.activity_totals = {"jumps": {}, "kills": {}, "hours": 0}
        self.sov_defense: dict = {}
        self.industry_index: dict = {}
        self._intel_loaded = False

    # -- the bits tools call -------------------------------------------
    def load_intel(self):
        """Public ESI only. Deferred until a tool actually needs it."""
        if self._intel_loaded:
            return
        self._intel_loaded = True
        from ..esi import client
        try:
            activity = client.system_activity()
            self.kill_activity = activity.get("kills", {})
            self.jump_activity = activity.get("jumps", {})
            self.sov_defense = client.sovereignty_defense()
            self.industry_index = client.industry_indices()
        except Exception:                       # intel is best-effort
            pass

    def sov_of(self, system_id):
        return None                             # needs authenticated contacts

    def is_avoided(self, system_id) -> bool:
        return system_id in self.avoided_ids

    def note_for(self, system_id) -> str:
        s = self.universe.systems.get(system_id)
        return config.get_system_notes().get(s.name, "") if s else ""

    def refresh_notes(self):
        pass                                    # no map to redraw

    def toggle_avoid(self, system_id):
        s = self.universe.systems.get(system_id)
        if s is None:
            return
        names = set(config.get_avoided())
        if s.name in names:
            names.discard(s.name)
            self.avoided_ids.discard(s.id)
        else:
            names.add(s.name)
            self.avoided_ids.add(s.id)
        config.set_avoided(sorted(names))

    def system_intel(self, system_id) -> dict:
        self.load_intel()
        k = self.kill_activity.get(system_id, {})
        return {"jumps_1h": self.jump_activity.get(system_id, 0),
                "jumps_24h": 0, "history_hours": 0,
                "ship_kills_1h": k.get("ship", 0), "pod_kills_1h": k.get("pod", 0),
                "npc_kills_1h": k.get("npc", 0),
                "adm": (self.sov_defense.get(system_id) or {}).get("adm"),
                "industry": self.industry_index.get(system_id, {})}

    def run_ai_tool(self, tool, args):
        return tool.fn(self, **args)


def _servable():
    """The tools this process is allowed to offer, given the user's opt-ins."""
    from . import tools
    allow_writes = config.get_mcp_allow_writes()
    allow_private = config.get_mcp_allow_private()
    out = []
    for t in tools.TOOLS:
        if t.name in _UI_ONLY:
            # Live-map tools that change things still need the writes opt-in.
            if t.writes and not allow_writes:
                continue
            out.append(t)
            continue
        if t.name in _WRITES and not allow_writes:
            continue
        if t.name in _PRIVATE and not allow_private:
            continue
        out.append(t)
    return out


def _send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _reply(req_id, result) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": req_id,
           "error": {"code": code, "message": message}})


def serve() -> int:
    """Read requests from stdin until EOF. Returns a process exit code."""
    if not config.get_mcp_enabled():
        # Refuse loudly on stderr (Claude Desktop shows it) and exit. Do not
        # speak the protocol at all, so there is nothing to interrogate.
        sys.stderr.write(
            "Eve-Strait: the MCP server is disabled.\n"
            "Enable it in Eve-Strait under File -> AI assistant.\n")
        return 2

    _audit(f"server start (writes={config.get_mcp_allow_writes()}, "
           f"private={config.get_mcp_allow_private()})")
    app = None

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, req_id = req.get("method"), req.get("id")

        if method == "initialize":
            _reply(req_id, {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "eve-strait", "version": _version()},
            })
        elif method in ("notifications/initialized", "initialized"):
            continue                                    # notification, no reply
        elif method == "ping":
            _reply(req_id, {})
        elif method == "tools/list":
            _reply(req_id, {"tools": [
                {"name": t.name, "description": t.description,
                 "inputSchema": t.schema} for t in _servable()]})
        elif method == "tools/call":
            params = req.get("params") or {}
            name = params.get("name", "")
            args = params.get("arguments") or {}
            allowed = {t.name: t for t in _servable()}
            tool = allowed.get(name)
            if tool is None:
                _audit(f"REFUSED {name} {args}")
                _reply(req_id, {"isError": True, "content": [
                    {"type": "text",
                     "text": f"{name!r} is not available. It is either a "
                             "UI-only tool or disabled in Eve-Strait's AI "
                             "settings."}]})
                continue
            _audit(f"call {name} {json.dumps(args, default=str)}")
            try:
                if name in _UI_ONLY:
                    from .bridge import call as bridge_call
                    out = bridge_call(name, args)
                else:
                    if app is None:
                        app = _Headless()
                    out = tool.fn(app, **args)
                _reply(req_id, {"content": [{"type": "text", "text": str(out)}]})
            except Exception as exc:
                _audit(f"  error {type(exc).__name__}: {exc}")
                _reply(req_id, {"isError": True, "content": [
                    {"type": "text", "text": f"{type(exc).__name__}: {exc}"}]})
        elif req_id is not None:
            _error(req_id, -32601, f"Method not found: {method}")

    _audit("server stop")
    return 0


def _version() -> str:
    from .. import __version__
    return __version__
