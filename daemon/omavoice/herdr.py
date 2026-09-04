"""What the desktop is doing, as a few lines the agent can read.

Herdr keeps the terminals this machine works in: workspaces, panes, and the
coding agent living in each one. Asking it costs 8 ms and the whole picture fits
in about 190 tokens, so it rides along with every question rather than being
gated on guessing which questions are about it.

Read-only, deliberately. The agent is told what the panes are; it is not given
the herdr command. Reading a summary needs no shell, so this works in the
restricted mode where Bash is refused — and a sentence said near this laptop
cannot prompt the agents in those panes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil

from .devices import read_capped, terminate_and_reap

log = logging.getLogger("omavoice.herdr")

# Herdr answers in single figures of milliseconds. This is the timeout for a
# server that is wedged, not for one that is working.
TIMEOUT = 2.0

_MAX_OUTPUT = 256 * 1024
MAX_CONTEXT = 8 * 1024


async def _ask(*args: str) -> dict | None:
    tool = shutil.which("herdr")
    if not tool:
        return None
    # The pane variables say which terminal a command came from, and nothing is
    # being driven here. Dropping them keeps this a question about the session
    # rather than an action inside it.
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
    proc = await asyncio.create_subprocess_exec(
        tool, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
        start_new_session=True,
    )
    try:
        raw, _ = await asyncio.wait_for(read_capped(proc.stdout, _MAX_OUTPUT), timeout=TIMEOUT)
        await proc.wait()
    except (asyncio.TimeoutError, OSError):
        return None
    finally:
        await terminate_and_reap(proc)
    if proc.returncode:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return None


def _rows(workspaces: list, agents: list) -> list[str]:
    lines = []
    for w in workspaces:
        lines.append(
            f'workspace {w.get("workspace_id", "?")} "{w.get("label", "")}" — '
            f'{w.get("tab_count", 0)} tabs, agent {w.get("agent_status", "unknown")}'
        )
    for a in agents:
        title = str(a.get("terminal_title_stripped") or "")[:60]
        lines.append(
            f'  {a.get("pane_id", "?")} {a.get("agent", "?")} '
            f'[{a.get("agent_status", "unknown")}] {title} — {a.get("cwd", "")}'
        )
    return lines


async def context() -> str:
    """The desktop's panes and agents. Empty when herdr is not answering."""
    try:
        workspaces, agents = await asyncio.gather(
            _ask("workspace", "list"), _ask("agent", "list")
        )
    except Exception:  # noqa: BLE001
        log.debug("herdr did not answer", exc_info=True)
        return ""
    if not workspaces and not agents:
        return ""
    lines = _rows(
        (workspaces or {}).get("result", {}).get("workspaces", []),
        (agents or {}).get("result", {}).get("agents", []),
    )
    if not lines:
        return ""
    return "\n".join(lines)[:MAX_CONTEXT]
