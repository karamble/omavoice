"""omavoice-ctl — a terminal handle on the running daemon.

Everything here is a thin wrapper over one IPC command, so the daemon stays the
only place that knows how any of this works.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import config, ipc


async def _send(message: dict, *, collect: str | None = None, timeout: float = 90.0) -> int:
    cfg = config.load()
    if not cfg.socket_path.exists():
        print(
            "The daemon is not running. Start it: systemctl --user start omavoice",
            file=sys.stderr,
        )
        return 1
    try:
        reply = await ipc.request(cfg.socket_path, message, collect=collect, timeout=timeout)
    except (ConnectionRefusedError, FileNotFoundError):
        print("The daemon is not answering — the socket is there but nothing is listening.", file=sys.stderr)
        return 1
    except asyncio.TimeoutError:
        print("The daemon did not answer in time.", file=sys.stderr)
        return 1

    if reply is None:
        print("The daemon closed the connection without answering.", file=sys.stderr)
        return 1

    if reply.get("ok") is False:
        print(reply.get("error") or "The command failed.", file=sys.stderr)
        return 1
    return _render(reply)


def _render(reply: dict) -> int:
    if reply.get("spoken"):
        print(reply["spoken"])
        if reply.get("markdown"):
            print()
            print(reply["markdown"])
        for link in reply.get("links") or []:
            print(f"\n[link] {link['label']} -> {link['url']}")
        for entry in reply.get("files") or []:
            print(f"[file] {entry['label']} -> {entry['path']}")
        return 0

    printable = {k: v for k, v in reply.items() if k not in ("id", "ok")}
    if printable:
        print(json.dumps(printable, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="omavoice-ctl")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="what the daemon is doing right now")
    sub.add_parser("cancel", help="cut off the answer being spoken")
    sub.add_parser("calibrate", help="measure the microphone and set its gain")
    sub.add_parser("reset", help="forget this conversation and start a fresh one")
    sub.add_parser("background", help="the panel has gone away")
    sub.add_parser("foreground", help="the panel is back")

    ask = sub.add_parser("ask", help="ask the local agent in text, no microphone")
    ask.add_argument("query", nargs="+")

    backend = sub.add_parser("backend", help="switch the local agent")
    backend.add_argument("name", choices=("codex", "claude"))

    ptt = sub.add_parser("ptt", help="hold-to-talk: `down` opens the microphone, `up` asks")
    ptt.add_argument("edge", choices=("down", "up"))

    say = sub.add_parser("say", help="make the assistant speak a line (echo testing)")
    say.add_argument("text", nargs="+")

    voice = sub.add_parser("voice", help="pick the Kokoro voice")
    voice.add_argument("name", nargs="?", help="omit to list what is available")

    sub.add_parser("access", help="the folder in use and which agents are allowed")

    workspace = sub.add_parser("workspace", help="the folder the agent works in")
    workspace.add_argument("folder", help="an existing directory")

    allow = sub.add_parser("allow", help="let an agent answer by voice")
    allow.add_argument("name", choices=("codex", "claude"))

    revoke = sub.add_parser("revoke", help="withdraw an agent's permission")
    revoke.add_argument("name", choices=("codex", "claude"))

    unrestrict = sub.add_parser(
        "unrestrict", help="let an agent use everything it can, not only the folder"
    )
    unrestrict.add_argument("name", choices=("codex", "claude"))

    restrict = sub.add_parser("restrict", help="hold an agent to the chosen folder again")
    restrict.add_argument("name", choices=("codex", "claude"))

    args = parser.parse_args()

    if args.command == "ask":
        return asyncio.run(_send({"cmd": "ask", "query": " ".join(args.query)}, timeout=360))
    if args.command == "backend":
        return asyncio.run(_send({"cmd": "backend", "value": args.name}))
    if args.command == "ptt":
        # Short timeout on purpose: the key release starts the answer and does
        # not wait for it, so anything slow here is a daemon that is stuck.
        return asyncio.run(_send({"cmd": "ptt", "down": args.edge == "down"}, timeout=10))
    if args.command == "say":
        return asyncio.run(_send({"cmd": "say", "text": " ".join(args.text)}))
    if args.command == "workspace":
        return asyncio.run(_send({"cmd": "workspace", "value": args.folder}))
    if args.command == "allow":
        return asyncio.run(_send({"cmd": "consent", "backend": args.name, "granted": True}))
    if args.command == "revoke":
        return asyncio.run(_send({"cmd": "consent", "backend": args.name, "granted": False}))
    if args.command == "unrestrict":
        return asyncio.run(_send({"cmd": "unrestrict", "backend": args.name, "granted": True}))
    if args.command == "restrict":
        return asyncio.run(_send({"cmd": "unrestrict", "backend": args.name, "granted": False}))
    if args.command == "voice":
        if not args.name:
            from .config import VOICES

            for name, gender, label in VOICES:
                print(f"  {name:<12} {gender:<7} {label}")
            return 0
        return asyncio.run(_send({"cmd": "voice", "value": args.name}))
    return asyncio.run(_send({"cmd": args.command}))


if __name__ == "__main__":
    raise SystemExit(main())
