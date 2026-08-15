---
name: eve-strait-mcp
description: Interact with Eve-Strait's embedded MCP server - enable it, drive it headlessly over stdio JSON-RPC for testing, wire it into Claude Desktop, or read its 16-tool catalogue. Use whenever the task involves eve-strait --mcp, the MCP tool list, the IPC bridge, mcp-audit.log, or connecting Claude Desktop to this repo's running map.
---

# Eve-Strait MCP server

Source of truth: [`src/eve_strait/ai/mcp_server.py`](../../../src/eve_strait/ai/mcp_server.py),
[`tools.py`](../../../src/eve_strait/ai/tools.py),
[`bridge.py`](../../../src/eve_strait/ai/bridge.py). Re-read those before
trusting anything below if the code has moved on — this skill is a map, not
the territory.

## What this is

`eve-strait --mcp` runs the app headless as a JSON-RPC 2.0 server over
stdin/stdout. No network listener, no port — the whole security model rests
on that. It is how Claude Desktop can read the map and, if allowed, edit the
route a user has open, without ever taking an API key from them.

Two things are easy to conflate and are not the same server:

- **This MCP server** (`--mcp`) — general-purpose, speaks MCP, meant for
  Claude Desktop or any MCP client.
- **The IPC bridge** (`bridge.py`) — a private Windows named pipe /
  Unix socket between this server process and a *running GUI instance*.
  Only the MCP server talks to it; nothing external connects to it directly.

## Before anything else: is it enabled?

Disabled by default, and the check is server-side, not just in the UI —
`serve()` refuses to speak the protocol at all if `config.get_mcp_enabled()`
is false. It writes one line to stderr and exits 2. Do not try to work around
that from outside the app; the fix is enabling it from inside the app.

In the app: **Settings → Assistant**:
- **Enable the MCP server** — required for anything below to answer at all.
- **Allow it to change things (notes, avoid list)** — gates `mcp_allow_writes`.
  Without it, every `writes=True` tool (see catalogue below) is left out of
  `tools/list` entirely, not merely refused.
- **Allow it to read your characters, locations and standings** — gates
  `mcp_allow_private`. As of this writing `_PRIVATE` in `mcp_server.py` is an
  empty set, so nothing is currently gated by it — check the code, don't
  assume that's still true.

Programmatically (for a test harness, not for a real user's install):
```python
from eve_strait import config
config.set_mcp_enabled(True)
```

## Talking to it directly (headless testing)

Three methods only: `initialize`, `tools/list`, `tools/call`. Everything else
gets `-32601 Method not found`. `notifications/initialized` and `ping` are
accepted and otherwise no-ops.

```bash
uv run eve-strait --mcp
```
feed it newline-delimited JSON-RPC on stdin, e.g. from a Python harness:

```python
import json, subprocess

p = subprocess.Popen(["uv", "run", "eve-strait", "--mcp"],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                     text=True, bufsize=1)

def rpc(method, params=None, id=1):
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": id,
                              "method": method, "params": params or {}}) + "\n")
    p.stdin.flush()
    return json.loads(p.stdout.readline())

print(rpc("initialize"))
print(rpc("tools/list"))
print(rpc("tools/call", {"name": "find_system", "arguments": {"name": "Jita"}}))
```

Use reader threads if you need to send and read concurrently — a naive
synchronous read/write pair deadlocks once a reply is larger than the pipe
buffer. (This bit a prior test harness in this project; don't repeat it.)

Every accepted call is appended to `mcp-audit.log` beside the app's config
(`config.DATA_DIR`). Refused calls are logged too, prefixed `REFUSED`. Read
that file rather than guessing why something didn't run.

## The tool catalogue

16 tools total, defined once in `tools.py` and filtered per-request by
`_servable()` in `mcp_server.py` against the two opt-ins above. Three
categories, and a tool can be in more than one:

**Always available once MCP is on** (read-only, headless, no running GUI
needed — served by a throwaway `_Headless` app object that loads public ESI
data on first use):
`find_system`, `system_intel`, `cyno_activity`, `list_ships`,
`systems_in_jump_range`, `list_saved_routes`

**UI-only** (`_UI_ONLY` in `mcp_server.py`) — forwarded over the IPC bridge
to a real, running Eve-Strait window. If no window is open, the bridge raises
`NotRunning` and the tool call comes back as an error explaining that rather
than mutating a route nobody can see:
`get_route`, `get_setup`, `add_waypoint`, `remove_waypoint`,
`clear_waypoints`, `set_ship`, `set_options`, `auto_route`

**Writes** (`writes=True` on the `Tool`, gated by `mcp_allow_writes`) — a
tool can be UI-only *and* a write at once (e.g. `add_waypoint`), in which
case both gates apply:
`add_waypoint`, `remove_waypoint`, `clear_waypoints`, `set_ship`,
`set_options`, `auto_route`, `set_system_note`, `avoid_system`

Read-only, non-UI, always-on if MCP is enabled at all: `find_system`,
`system_intel`, `cyno_activity`, `list_ships`, `systems_in_jump_range`,
`list_saved_routes`.

Call `get_setup` and `get_route` before changing anything — that's not a
suggestion, it's what the tool descriptions themselves tell a calling model
to do, and `get_setup`/`get_route` are what `SYSTEM_PROMPT` in `tools.py`
tells the in-app assistant to lean on for current state before acting.

**Known gap, as of this writing:** none of the 16 tools know about
wormholes, EVE-Scout, Dotlan, or Wanderer chains. A route through Thera or
Turnur is invisible to this tool set even though the desktop UI understands
it. Say so if asked to route through J-space via MCP — don't imply the tool
covers it.

## Wiring up Claude Desktop

Point it at the **built exe**, not a bundled Python copy — this project
deliberately keeps MCP config pointed at `eve-strait.exe --mcp` so it matches
whatever the user actually has installed, rather than a dev checkout that
can drift out of sync.

`claude_desktop_config.json` (Windows: `%APPDATA%\Claude\`):
```json
{
  "mcpServers": {
    "eve-strait": {
      "command": "C:\\Path\\To\\eve-strait.exe",
      "args": ["--mcp"]
    }
  }
}
```

Alternatively, `scripts/build_mcpb.py` produces `dist/eve-strait.mcpb`, a
self-contained bundle (its own vendored `requests`/`urllib3`/`certifi`/`idna`
— deliberately not the AI provider SDKs; see that script's own comments for
why `charset_normalizer` is excluded) installable via Claude Desktop's
Settings → Extensions → Advanced settings → install a custom bundle. Rebuild
it after any change to `tools.py` or `mcp_server.py`:
```bash
uv run python scripts/build_mcpb.py
```

## To see it drive a live window

1. Run the desktop app normally (`uv run eve-strait`) and leave it open.
2. Enable MCP + writes in Settings → Assistant, in that same running instance.
3. From a *separate* process, run `eve-strait --mcp` and call a UI-only tool
   (`add_waypoint` is the clearest to see land). The bridge should connect
   to the open window's named pipe / socket and the waypoint should appear
   on screen in real time.
4. If it times out instead: confirm the same OS user owns both processes,
   and that the GUI's MCP toggle is actually on — a headless `--mcp` process
   with MCP *disabled in the GUI* still refuses to serve at all, per the
   check in `serve()` above.
5. If a UI-only tool instead comes back saying something answered on the
   pipe but was not recognised as the same install: the pipe/socket address
   is per-**OS-user** only, not per-install, but the authkey is per-`DATA_DIR`
   (`config.DATA_DIR / "bridge-token"`). Two eve-strait processes on the same
   Windows account with different data directories — a dev checkout run
   alongside an installed build, or two profiles in testing — bind to the
   same address and fail the HMAC challenge instead of one simply not being
   there. `bridge.call()` turns that into a `NotRunning` explaining exactly
   this rather than leaking the raw `AuthenticationError`; if you ever see
   the raw `"digest ... rejected"` text instead, that catch regressed.
