"""Hardware capture gain — the one thing no amount of software can repair.

`AutoGain` can rescue a microphone that is too quiet. It cannot rescue one that
is too loud, and not because nobody wrote the code: clipping happens inside the
ADC, before any process sees a sample. What comes out is flat-topped, and the
information that was in those peaks is gone. Attenuating afterwards is a
multiply, and a multiply preserves the ratio between the room and the voice that
clipping already destroyed. Measured on the machine this was written on: at
+40 dB of analog gain the room sat at 0.383 against speech at 0.95 — 2.5:1, an
SNR of 7.9 dB. At +20 dB the same room measured 0.0012 against 0.20 — 167:1,
44.4 dB. Thirty-six decibels, recovered by turning a knob down.

So the knob has to be turned, and this module is the only thing here that knows
how. Three things it took measuring to get right:

**Drive PipeWire, never amixer.** On a card with hardware capture volume,
PipeWire's volume *is* the ALSA control rather than a digital gain layered over
it: the route reports `route.hw-volume = true` and `softVolumes = [1.0, 1.0]`,
and the level read back through `amixer` matches to the decibel. Going through
PipeWire also picks the right control for the active port, and works on USB and
Bluetooth devices where there is no `amixer` to run.

What it does *not* do — learned by getting it wrong — is drive a separate
microphone boost. PipeWire moves one control, the capture volume, and leaves
`Internal Mic Boost` and its siblings exactly where they are. Ask the route for
more than the capture control can deliver and PipeWire quietly makes up the
difference in software rather than refusing, which shows up as `softVolumes`
above 1.0. That is worth reading, because software gain is precisely the thing
that cannot fix clipping — but it means "the request went past the hardware",
not "there is no hardware", and treating the two as the same thing made this
module refuse a microphone it could perfectly well have turned down.

**WirePlumber is what makes it stick, and it needs no root.** `alsactl store`
writes a root-owned file, and would lose anyway: on this machine
`/var/lib/alsa/asound.state` still holds a clipping configuration that
`alsa-restore` applies at every boot, and WirePlumber overwrites it a second
later from `~/.local/state/wireplumber/default-routes`. Setting the volume as a
user action — which `wpctl` sends and a raw param write does not — is what gets
it written there.

**Never the virtual node.** `echo-cancel-source` is what the daemon usually
records from, and its volume is a software multiply downstream of both the ADC
and the canceller. Turning it down makes clipped samples quieter, not cleaner,
and unsettles a canceller running with its own gain control switched off. The
hardware node behind it has to be found by walking the graph, because the
echo-cancel config names no capture target — whatever was the default source
when the module loaded is what it took.

Nothing in here runs on its own. Gain moves when a person asks for it to move,
because it is a control shared with every other program on the machine — the
same reason `AutoGain` refuses to touch it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass

from .devices import read_capped, terminate_and_reap, trusted_binary

log = logging.getLogger("omavoice.mixer")

# The graph on a laptop is a hundred objects; a machine with a lot of streams is
# larger but not unboundedly so. Past this something is wrong and a truncated
# JSON document is worse than none.
_DUMP_MAX_BYTES = 4 * 1024 * 1024
_DUMP_TIMEOUT = 4.0
_WPCTL_TIMEOUT = 3.0

# How far the walk from a virtual node to real hardware may go before we decide
# the graph is a loop. Two hops covers a filter chain; four is generous.
_MAX_HOPS = 4


@dataclass(frozen=True)
class Gain:
    """What the hardware behind a source can do, and where it is set now."""

    node: int
    name: str
    # Decibels above this device's own unity point, which is what both ALSA and
    # the route express. 0.0 means no gain at all, not silence.
    db: float
    max_db: float
    # How much of `db` PipeWire could not get from the hardware and is applying
    # in software. Zero on a healthy setting. Above zero means the request has
    # gone past what the capture control can do — which matters here, because
    # software gain is exactly the thing that cannot fix clipping.
    soft_db: float
    # False when the level is not the hardware's: Bluetooth, most USB, anything
    # PipeWire drives with a software multiply. Adjusting it cannot fix
    # clipping, so saying so is more use than moving it.
    adjustable: bool
    reason: str = ""

    @property
    def hardware_db(self) -> float:
        """The part of the gain that is really in the hardware."""
        return self.db - self.soft_db

    def describe(self) -> str:
        if not self.adjustable:
            return f"{self.name}: {self.reason}"
        if self.soft_db > 0.1:
            return (f"{self.name}: {self.db:+.2f} dB, but {self.soft_db:+.2f} of it "
                    f"is software — the hardware is maxed out")
        return f"{self.name}: {self.db:+.2f} dB of {self.max_db:+.0f} dB"


def _props(obj: dict) -> dict:
    return (obj.get("info") or {}).get("props") or {}


async def _run(tool: str, *args: str, timeout: float) -> bytes:
    """One read of an external tool, bounded and never left running."""
    try:
        proc = await asyncio.create_subprocess_exec(
            trusted_binary(tool), *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            # Its own group, so the cleanup below reaches anything it started.
            start_new_session=True,
        )
    except OSError as exc:
        log.warning("%s did not run: %s", tool, exc)
        return b""
    assert proc.stdout is not None
    try:
        out, _ = await asyncio.wait_for(
            read_capped(proc.stdout, _DUMP_MAX_BYTES), timeout=timeout
        )
        if len(out) >= _DUMP_MAX_BYTES:
            log.warning("%s went past %d bytes — discarding it", tool, _DUMP_MAX_BYTES)
            return b""
        return out
    except (asyncio.TimeoutError, OSError):
        log.warning("%s did not finish in %.0fs", tool, timeout)
        return b""
    finally:
        await terminate_and_reap(proc)


async def _dump() -> list[dict]:
    """The PipeWire graph, or an empty list. Never raises."""
    raw = await _run("pw-dump", timeout=_DUMP_TIMEOUT)
    if not raw:
        return []
    try:
        objects = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("pw-dump did not return JSON: %s", exc)
        return []
    return objects if isinstance(objects, list) else []


def _node_named(dump: list[dict], name: str) -> dict | None:
    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        if _props(obj).get("node.name") == name:
            return obj
    return None


def _hardware_behind(dump: list[dict], name: str) -> dict | None:
    """Walk from whatever we record on to the ALSA node underneath it.

    A filter such as `echo-cancel-source` presents itself as a source and hides
    a capture stream in the same `node.link-group`; that stream is what is
    actually wired to the hardware. So: find the sibling stream, follow its
    incoming links back, and repeat until something claims `device.api = alsa`.

    Deliberately not the `node.driver-id` shortcut, which points at the right
    node here only because it happens to be the graph's clock. That is a
    coincidence — the driver is just as often the sink, or a dummy while capture
    is suspended.
    """
    node = _node_named(dump, name)
    if node is None:
        log.warning("no node called %r in the graph", name)
        return None

    # It has to be something we record from. The echo canceller puts its
    # playback sink in the same link-group as its capture stream, so a walk that
    # did not check this answered "what is the gain of omavoice_playback?" with
    # the microphone's — and would then have calibrated a capture device because
    # somebody asked about a speaker.
    media = str(_props(node).get("media.class") or "")
    if "Source" not in media and media != "Stream/Input/Audio":
        log.info("%r is not something to record from (%s)", name, media or "unknown")
        return None

    for _ in range(_MAX_HOPS):
        props = _props(node)
        if props.get("device.api") == "alsa":
            return node

        # A filter's capture side lives in the same link-group.
        group = props.get("node.link-group")
        if group and props.get("media.class") != "Stream/Input/Audio":
            sibling = next(
                (
                    o for o in dump
                    if o.get("type") == "PipeWire:Interface:Node"
                    and _props(o).get("node.link-group") == group
                    and _props(o).get("media.class") == "Stream/Input/Audio"
                ),
                None,
            )
            if sibling is not None:
                node = sibling
                continue

        # Otherwise follow what feeds this node.
        node_id = node.get("id")
        upstream = next(
            (
                o for o in dump
                if o.get("type") == "PipeWire:Interface:Link"
                and (o.get("info") or {}).get("input-node-id") == node_id
            ),
            None,
        )
        if upstream is None:
            break
        producer = (upstream.get("info") or {}).get("output-node-id")
        found = next(
            (o for o in dump
             if o.get("type") == "PipeWire:Interface:Node" and o.get("id") == producer),
            None,
        )
        if found is None:
            break
        node = found

    log.info("nothing with hardware gain behind %r", name)
    return None


def _input_route(dump: list[dict], device_id, profile_device) -> dict | None:
    """The device's Input route for the port this capture device sits on.

    Matched on `card.profile.device` rather than taken as the first Input route:
    a card with an internal microphone and a headset jack has one per port, and
    only the active one is in circuit.
    """
    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Device" or obj.get("id") != device_id:
            continue
        routes = ((obj.get("info") or {}).get("params") or {}).get("Route") or []
        for route in routes:
            if route.get("direction") != "Input":
                continue
            if profile_device is not None and route.get("device") != profile_device:
                continue
            return route
    return None


def _route_info(route: dict) -> dict:
    """The route's `info`, which arrives as [count, key, value, key, value…]."""
    raw = route.get("info")
    if not isinstance(raw, list) or len(raw) < 3:
        return {}
    body = raw[1:]
    return {str(body[i]): str(body[i + 1]) for i in range(0, len(body) - 1, 2)}


async def read(source_name: str) -> Gain | None:
    """What the hardware behind `source_name` is set to, if anything is."""
    dump = await _dump()
    if not dump:
        return None
    node = _hardware_behind(dump, source_name)
    if node is None:
        return None

    props = _props(node)
    name = str(props.get("node.name") or source_name)
    node_id = node.get("id")
    route = _input_route(dump, props.get("device.id"), props.get("card.profile.device"))
    if route is None:
        return Gain(node_id, name, 0.0, 0.0, False,
                    "PipeWire reports no input route for it")

    rprops = route.get("props") or {}
    info = _route_info(route)
    volumes = rprops.get("channelVolumes") or []
    base = rprops.get("volumeBase")
    soft = rprops.get("softVolumes") or []

    if not volumes or not isinstance(base, (int, float)) or base <= 0:
        return Gain(node_id, name, 0.0, 0.0, 0.0, False,
                    "PipeWire does not expose a gain for it")
    # The one test that decides whether there is a hardware control at all.
    # Bluetooth and most USB microphones fail it, and on those no amount of
    # setting a volume will undo clipping.
    if info.get("route.hw-volume") != "true":
        return Gain(node_id, name, 0.0, 0.0, 0.0, False,
                    "its level is applied in software, not by the hardware")

    # `softVolumes` is a separate question from adjustability, and conflating
    # the two was wrong. PipeWire drives one control — the capture volume — and
    # leaves any separate microphone boost alone. Ask for more than the capture
    # control can give and it makes up the difference in software rather than
    # refusing, so this is a reading of "the request has gone past the
    # hardware", not of "there is no hardware".
    soft = soft[0] if soft else 1.0
    soft_db = 20.0 * math.log10(max(soft, 1e-12)) if soft > 0 else 0.0

    # `volumeBase` is the device's unity point expressed against the route's
    # maximum, so it is also exactly how much gain the route can ask for.
    db = 20.0 * math.log10(max(volumes[0], 1e-12) / base)
    max_db = 20.0 * math.log10(1.0 / base)
    return Gain(node_id, name, db, max_db, max(0.0, soft_db), True)


async def set_db(gain: Gain, target_db: float) -> float | None:
    """Move the hardware gain, and report what it actually reached.

    Reported rather than assumed because the steps are coarse — fractions of a
    decibel on the main control, and ten decibels at a time once a device
    crosses into its boost — so what is asked for and what lands are different
    numbers, and a loop that believed the first would never converge.
    """
    if not gain.adjustable:
        return None

    base = 1.0 / (10.0 ** (gain.max_db / 20.0))
    wanted = min(max(target_db, 0.0), gain.max_db)
    linear = min(1.0, base * (10.0 ** (wanted / 20.0)))
    # PipeWire's volume is linear; wpctl and pactl speak the cubic scale.
    cubic = linear ** (1.0 / 3.0)

    # Through wpctl on purpose. The value has to arrive as a user's decision for
    # WirePlumber to write it to default-routes, which is the whole of how this
    # survives a reboot; a raw parameter write changes the level and is
    # forgotten.
    await _run("wpctl", "set-volume", str(gain.node), f"{cubic:.6f}",
               timeout=_WPCTL_TIMEOUT)

    fresh = await read(gain.name)
    if fresh is None or not fresh.adjustable:
        return None
    log.info("capture gain %s: asked %+.2f dB, reached %+.2f dB",
             fresh.name, wanted, fresh.db)
    return fresh.db
