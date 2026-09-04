"""The daemon: wires the microphone, whisper, the brain, the voice and the panel together.

State is a small machine, and the UI is a pure function of it:

    idle       nothing is happening
    listening  the key is down and the microphone is open
    thinking   whisper or the local agent is working
    speaking   an answer is coming out of the speakers
    error      something broke and the panel should say so

The turn is the key, not a model's guess at one. Audio flows only while F10 is
held: there is no session, no server deciding when a sentence ended, and no
window in which a microphone is open because a panel happens to be on screen.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import sys
import time

import json
from pathlib import Path

from . import config, devices as device_choice, ipc
from .audio import (
    AutoGain,
    Microphone,
    NoiseGate,
    Speaker,
    clipped_samples,
    rms_full_scale,
)
from .brain import Brain
from .config import Config
from .herdr import context as herdr_context
from .listen import Held, transcribe, was_nothing, write_wav
from .speak import speak as speak_locally

log = logging.getLogger("omavoice")

# The waveform redraws at 20 fps. Every 20 ms chunk would be four times that
# for no visible benefit, and each one is a socket write per connected client.
_LEVEL_INTERVAL = 0.05

# A waterfall line is a sentence. Anything past this is not a longer line,
# it is a backend or a transcriber having a bad day at our expense.
_MAX_EVENT_TEXT = 4000

# Four seconds of 24 kHz mono PCM16, in the 960-byte chunks the speaker is fed.
_PLAY_QUEUE_CHUNKS = 200

# Interrupting an answer opens the microphone into a room the speakers have
# just stopped filling. The player is killed the moment F10 goes down, so what
# is left is the room's own tail — and for that stretch the bar for what counts
# as speech goes up, because residual reverberation of the assistant's own
# sentence is exactly the thing that must not be transcribed back to it. Live
# speech clears the raised bar comfortably; an echo does not.
_SPEAKING_GATE_MULTIPLIER = 2.6
_ECHO_TAIL_SECONDS = 0.9

# How long the room keeps ringing after an answer is cut off mid-word. The
# player is killed synchronously before the microphone opens, so this is
# reverberation and nothing else — measured here it arrives at a peak of 0.44,
# which no relative threshold can be expected to reject when the noise floor
# behind the echo canceller is a thousandth of that. Those chunks are dropped
# rather than gated: the alternative is handing whisper the tail of the
# assistant's own sentence, which it duly transcribes and answers.
#
# Short on purpose. It costs the first fraction of a second of a barge-in, and
# nobody has finished drawing breath by then; a longer window would start
# eating the beginning of the question it exists to protect.
_BARGE_QUIET_SECONDS = 0.4

# The preferences file holds two short names, a folder, three lists of at most
# two words each and a flag — a few hundred bytes, and the largest one this
# program has ever written was under four hundred. 64 KiB leaves room for a
# very long workspace path and still refuses anything that has been grown into
# something we would rather not read whole.
_MAX_PREFS_BYTES = 64 * 1024
_PREFS_NAME = "preferences.json"


def _open_dump(path: str):
    """Open one of the audio taps, or None when it is not asked for.

    Not a plain open: these carry the microphone verbatim, and the path comes
    from the environment, so it can name something that is already there. The
    same rule as everywhere else — the last component is not followed, and what
    is at it has to be a plain file of ours.
    """
    if not path:
        return None
    try:
        return config.open_append(path)
    except OSError as exc:
        log.warning("audio tap %s not opened: %s", path, exc)
        return None


def _log_task_failure(task: asyncio.Task) -> None:
    """Surface a background task that died.

    Audio is sent without awaiting — one chunk must never hold up the next.
    The cost is that an exception in there has nobody to raise to, and the
    failure looks like a microphone that stopped working for no reason.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("background task failed: %r", exc)


class Daemon:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.brain = Brain(cfg)
        self.server = ipc.Server(cfg.socket_path, self._on_command)

        self.speaker = Speaker(cfg, on_level=self._on_output_level)
        self.mic = Microphone(cfg, self._on_input_chunk)
        # Brought back rather than reinvented. A microphone in a display sits an
        # arm's length further away than one in a laptop lid, and no constant is
        # right for both — this measures how loud speech actually is here and
        # scales towards a target. It does two jobs at once: whisper is handed a
        # signal it can read, and "is this a voice" becomes a question about the
        # chunk after gain instead of about the device.
        self.autogain = AutoGain(chunk_ms=cfg.chunk_ms)
        self.devices: device_choice.Devices | None = None
        # Microphones that failed to produce audio while this daemon has been
        # up. Remembered so a broken device is discovered once, not at the
        # start of every conversation.
        self._bad_inputs: set[str] = set()
        self._mic_dump = _open_dump(cfg.mic_dump)
        self._dump = _open_dump(cfg.dump_path)
        self._voice_dump = _open_dump(cfg.voice_dump)
        # The gate no longer decides when a turn ends — the key does. What is
        # left of its job is one verdict per chunk, "was that a voice", which
        # `Held` counts to tell a question from a knock on the desk. The
        # hangover keeps that verdict steady across the pauses inside a
        # sentence.
        self.gate = NoiseGate(
            cfg.gate_level,
            hangover_ms=cfg.silence_ms + 300,
            chunk_ms=cfg.chunk_ms,
        )

        # Playback runs on its own task, fed by this queue, so that a full
        # speaker buffer stalls only the thing producing audio and never the
        # command socket.
        # Bounded, because Kokoro is a fast producer: it synthesises at about
        # a third of playback time, so an
        # unbounded queue would hold a whole answer in memory before a word of
        # it had been heard. Four seconds of audio is enough to keep pw-play
        # fed and little enough to drop instantly on barge-in.
        self._play_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_PLAY_QUEUE_CHUNKS)
        self._play_task: asyncio.Task | None = None
        # True while a local answer is being synthesised. The level pump ends
        # `speaking` when the speaker falls silent, and at the start of an
        # utterance it has been silent all along — the first sample is a second
        # or so away, in another process.
        self._local_speech = False
        # The audio of the question being asked, while F10 is down. None the
        # rest of the time, which is also how the mic pump knows to stay out of
        # the way.
        self._held: Held | None = None
        self._turn_task: asyncio.Task | None = None

        # What the agent is doing, while it is doing it. Not kept and not
        # replayed to a late-joining panel: it is a window onto a process that
        # is happening now, and a trace from a question already answered would
        # be a lie told quietly.
        self.brain.watch(self._on_trace)
        # Whether the five cards explaining this have been seen. Kept with the
        # preferences rather than in the plugin's own config: it is a fact about
        # the person, like the folder and the grants, and it should survive a
        # plugin being removed and put back.
        self.onboarded = False
        # Kept for saying where it is; nothing opens it by this name. The reads
        # and writes go through the state directory's descriptor instead.
        self._prefs_path = cfg.state_dir / _PREFS_NAME
        self._load_preferences()

        self.state = "idle"
        # Wall-clock moment after which the room can be trusted again.
        self._quiet_after = 0.0
        # True while the panel is not on screen. Nothing hangs off it any more
        # except what the panel is told: the microphone follows the key.
        self.backgrounded = False
        self._level_task: asyncio.Task | None = None
        self._pending_level = 0.0
        self._pending_bands = [0.0, 0.0, 0.0, 0.0]
        self._stopping = asyncio.Event()

    # -- preferences ----------------------------------------------------------

    def _state_fd(self) -> int | None:
        """The state directory, held open since startup. None if it is not usable."""
        try:
            return config.state_dir_fd()
        except OSError as exc:
            log.warning("cannot use %s: %s", self.cfg.state_dir, exc)
            return None

    def _save_preferences(self) -> None:
        """Remember voice and backend across restarts.

        Kept next to the daemon's own state rather than in shell.json: these
        are the daemon's settings, and writing to the shell's config from here
        would race with the bar rewriting it.

        Written whole and renamed into place. Several of these fields are
        permissions, and a permission read back from a file that was truncated
        and then not finished is a permission nobody granted.
        """
        fd = self._state_fd()
        if fd is None:
            return
        blob = json.dumps(
            {
                "voice": self.cfg.voice,
                "backend": self.brain.backend,
                # "" means follow the system; see devices.resolve.
                "input": self.cfg.input_target,
                # The folder the agent works in, and which backends
                # have been allowed to answer at all. Both are answers
                # to a question the person was asked in plain words,
                # which is why they are stored as given rather than
                # derived from anything: nothing else in this program
                # is entitled to infer them.
                "workspace": str(self.cfg.brain_cwd or ""),
                "consented": sorted(self.cfg.consented),
                "unrestricted": sorted(self.cfg.unrestricted),
                "onboarded": bool(self.onboarded),
            },
            indent=2,
        )
        try:
            config.write_private(fd, _PREFS_NAME, blob + "\n")
        except OSError as exc:
            log.warning("could not save preferences: %s", exc)

    def _load_preferences(self) -> None:
        fd = self._state_fd()
        if fd is None:
            return
        raw = config.read_private(fd, _PREFS_NAME, _MAX_PREFS_BYTES)
        if raw is None:
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("%s is not valid JSON — starting from defaults", self._prefs_path)
            return
        # A JSON document is not necessarily an object, and everything below
        # asks this one for keys.
        if not isinstance(data, dict):
            log.warning("%s is not a JSON object — starting from defaults", self._prefs_path)
            return
        voice = str(data.get("voice") or "")
        if voice in config.VOICE_GENDER:
            self.cfg.voice = voice
        backend = str(data.get("backend") or "")
        if backend in ("codex", "claude"):
            self.brain.backend = backend
        self.onboarded = bool(data.get("onboarded"))
        # An environment variable is a deliberate override and outranks a
        # remembered choice from the settings window.
        if not os.environ.get("OMAVOICE_INPUT"):
            self.cfg.input_target = str(data.get("input") or "")

        # A folder can be deleted, renamed, or live on a disk that is not
        # mounted this morning. Checking it here rather than trusting the file
        # matters more than it looks: an agent started in a directory that no
        # longer exists does not fail loudly, it starts somewhere else.
        if self.cfg.brain_cwd is None:
            remembered = str(data.get("workspace") or "")
            if remembered:
                folder = Path(remembered).expanduser()
                if folder.is_dir():
                    self.cfg.brain_cwd = folder
                else:
                    log.warning(
                        "the folder %s is gone — asking again before anything is asked "
                        "of the agent", remembered,
                    )
        consented = data.get("consented")
        if isinstance(consented, list):
            self.cfg.consented = {
                name for name in consented if name in ("codex", "claude")
            }
        unrestricted = data.get("unrestricted")
        if isinstance(unrestricted, list):
            # Only for an agent that is allowed at all. The wider permission is
            # meaningless without the narrower one, and a file edited by hand
            # should not be able to assemble a state the screen cannot show.
            self.cfg.unrestricted = {
                name for name in unrestricted if name in self.cfg.consented
            }

    def _on_trace(self, text: str) -> None:
        """One line of the agent's working, pushed straight to the panel.

        Called from the pipe reader, so it must not await and must not raise.
        `broadcast` already promises both.
        """
        self.server.broadcast(
            {
                "type": "trace",
                "text": text if len(text) <= _MAX_EVENT_TEXT else text[:_MAX_EVENT_TEXT],
            }
        )

    # -- access ---------------------------------------------------------------

    def _access(self) -> dict:
        """What the person has agreed to, as the panel needs to see it.

        One message rather than two, because the two halves are not
        independent: a permission is a permission to work *somewhere*, and
        showing either without the other invites agreeing to the wrong thing.
        """
        return {
            "type": "access",
            "workspace": str(self.cfg.brain_cwd or ""),
            "consented": sorted(self.cfg.consented),
            "unrestricted": sorted(self.cfg.unrestricted),
            # Named so the panel can raise the consent screen by itself rather
            # than working the condition out again and getting it half right.
            "needed": bool(self.brain.denial()),
            # Carried here because it is the same kind of thing and arrives at
            # the same moment: what the person has already been asked and
            # answered. The panel raises the tour off this exactly as it raises
            # the consent screen off `needed`.
            "onboarded": bool(self.onboarded),
        }

    def _broadcast_access(self) -> None:
        self.server.broadcast(self._access())

    def _folders(self) -> list[dict]:
        """Somewhere to start from: the folders directly inside home.

        QML on this desktop cannot read a directory and there is no file
        dialog in the shell, so the choice has to be assembled here. Only one
        level deep and only real directories — this is a starting point for a
        person who mostly knows where their work lives, not a file manager.
        """
        home = Path.home()
        out: list[dict] = []
        try:
            entries = sorted(home.iterdir(), key=lambda e: e.name.lower())
        except OSError as exc:
            log.warning("cannot list %s: %s", home, exc)
            return out
        for entry in entries:
            if entry.name.startswith(".") or not entry.is_dir():
                continue
            out.append({"path": str(entry), "label": entry.name})
            if len(out) >= 40:
                break
        return out

    # -- the event stream ----------------------------------------------------

    def _emit(self, kind: str, text: str = "", **extra) -> None:
        """One line for the panel's waterfall.

        This is a Linux desktop, not a black box: showing what was heard, what
        was asked of the agent and how long it took is more reassuring while
        you wait than a spinner is. One short line per event.

        One line, and the length is enforced rather than assumed. Most of what
        arrives here is a transcript from the far end — text this program did
        not write and cannot vouch for — and a tail of these events is kept to
        replay to a panel that opens late. Something held and re-sent should
        not be able to decide its own size.
        """
        payload = {
            "type": "event",
            "kind": kind,
            "text": text if len(text) <= _MAX_EVENT_TEXT else text[:_MAX_EVENT_TEXT].rstrip() + " …",
            "at": time.time(),
        }
        payload.update(extra)
        self.server.broadcast(payload)

    # -- state ---------------------------------------------------------------

    def _set_state(self, state: str, *, message: str = "") -> None:
        if state == self.state:
            return
        self.state = state
        log.info("state -> %s%s", state, f" ({message})" if message else "")
        payload = {"type": "state", "state": state}
        if message:
            payload["message"] = message
        self.server.broadcast(payload)

    # -- audio ---------------------------------------------------------------

    def _on_input_chunk(self, chunk: bytes, level: float, bands: list[float]) -> None:
        """One 20 ms chunk from the microphone. Called from the mic pump — keep
        it cheap and never await here.

        The microphone is open only while F10 is held, so there is exactly one
        thing to do with a chunk: keep it. Everything that used to live here —
        the raised threshold while the assistant spoke, the gain, the silence
        substituted for the server's benefit — existed to protect a model that
        was listening continuously and deciding turns for itself. A key that is
        either down or up protects all of it for free.
        """
        if self._mic_dump is not None:
            # Before anything of ours has touched it. Paired with the tap after
            # the gate, this turns "somewhere between the microphone and
            # whisper" into a question with a byte offset for an answer.
            self._mic_dump.write(chunk)

        held = self._held
        if held is None:
            return

        # Interrupting an answer opens the microphone into a room the speakers
        # have just stopped filling. What is in it is the assistant's own voice,
        # not a question.
        if self._room_is_loud():
            return

        # The audio is the question and goes to whisper exactly as it arrived.
        # The gate is asked but never applied: its verdict only answers whether
        # anybody actually spoke, which is what stops a held key in a quiet room
        # being transcribed as "Thanks for watching!".
        # Measured before the gain, so the gate keeps judging the microphone
        # against its own noise floor rather than against how loud we made it.
        loudness = rms_full_scale(chunk)
        # Raised only while the room is still ringing from an answer that was
        # cut off. Explicit rather than folded into the gate's own estimate, so
        # this stretch cannot walk the measured floor up behind us.
        threshold = None
        if self._room_is_loud():
            threshold = self.gate.opening_level * _SPEAKING_GATE_MULTIPLIER
        passed = self.gate.step(
            level, threshold, can_open=self.autogain.is_speech_after_gain(loudness)
        )
        # Never trained on the assistant's own voice: gain learned from an echo
        # is gain applied to it, which is the loop feeding itself.
        self.autogain.observe(
            loudness, threshold is None and level >= self.gate.opening_level
        )
        # Amplified on the way in, and every chunk of it — not only the ones
        # that passed. whisper is given a continuous recording, and a gain that
        # switched on and off between words would be an edit rather than a
        # question.
        held.add(self.autogain.apply(chunk), passed)
        self._pending_level = max(self._pending_level, level)
        self._pending_bands = bands

        if self._dump is not None:
            self._dump.write(chunk)

        if self.cfg.debug:
            # The one number that settles "why did it not hear me": the level
            # the microphone actually delivered, against the threshold it had
            # to clear. Once a second, so a question stays readable.
            self._peak_level = max(getattr(self, "_peak_level", 0.0), level)
            self._clipped = getattr(self, "_clipped", 0) + clipped_samples(chunk)
            self._level_chunks = getattr(self, "_level_chunks", 0) + 1
            if self._level_chunks * self.cfg.chunk_ms >= 1000:
                log.debug(
                    "mic: peak=%.4f rms=%.4f clip=%d gate=%.4f floor=%.4f voiced=%.2fs of %.2fs",
                    self._peak_level,
                    rms_full_scale(chunk),
                    self._clipped,
                    self.gate.opening_level,
                    self.gate.noise_floor,
                    held.voiced_seconds,
                    held.seconds,
                )
                self._peak_level = 0.0
                self._clipped = 0
                self._level_chunks = 0

    def _room_is_loud(self) -> bool:
        """Is the assistant's own sound still in the room?"""
        if self.speaker.playing:
            self._quiet_after = (
                asyncio.get_running_loop().time()
                + self.speaker.remaining
                + _ECHO_TAIL_SECONDS
            )
            return True
        return asyncio.get_running_loop().time() < self._quiet_after

    def _on_output_level(self, level: float, bands: list[float]) -> None:
        self._pending_level = max(self._pending_level, level)
        # While the assistant speaks, its own voice drives the figure — the
        # panel should look like it is talking, not like it is waiting.
        self._pending_bands = bands

    async def _level_pump(self) -> None:
        """Coalesce levels into a steady, low-rate stream for the waveform."""
        try:
            while True:
                await asyncio.sleep(_LEVEL_INTERVAL)

                # The one place that knows the answer has stopped being heard.
                # It ends at idle rather than at listening: nothing is open
                # after an answer, because the key is not down.
                if self.state == "speaking" and not self._local_speech and not self.speaker.playing:
                    self._set_state("idle")

                self.server.broadcast(
                    {
                        "type": "level",
                        "rms": round(self._pending_level, 3),
                        "bands": [round(b, 3) for b in self._pending_bands],
                    }
                )
                self._pending_level = 0.0
        except asyncio.CancelledError:
            pass

    # -- speaking ------------------------------------------------------------

    async def _play_pump(self) -> None:
        """Drain the playback queue into pw-play, at the speed of sound."""
        try:
            while True:
                pcm = await self._play_queue.get()
                if pcm:
                    await self.speaker.write(pcm)
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("playback pump died")

    async def say(self, text: str) -> None:
        """Speak a line locally, and stay in `speaking` until it has been heard."""
        said = " ".join(str(text or "").split())
        if not said:
            return
        self._local_speech = True
        try:
            await self._ensure_playback()
            self._set_state("speaking")
            self._emit("said", said)
            self.server.broadcast(
                {"type": "transcript", "role": "assistant", "text": said, "final": True}
            )
            await speak_locally(said, self.cfg.voice, self._enqueue_audio)
            # The worker has ended, but what it wrote is still in the pipe.
            while self.speaker.playing:
                await asyncio.sleep(0.1)
        finally:
            self._local_speech = False
        self._set_state("idle")

    async def ptt(self, down: bool) -> dict:
        """The key is the turn. Down opens the microphone, up asks the question."""
        if down:
            if self._held is not None:
                return {"ok": True, "listening": True}
            # Pressing while an answer is playing cuts it off. Cancelling the
            # turn is what stops it: the Kokoro worker dies with the task that
            # was reading it, so a long answer is not still being synthesised
            # into a queue nobody will hear.
            self._cancel_turn()
            self.autogain.reset()
            if self.state == "speaking":
                self._drop_queued_audio()
                await self.speaker.flush_now()
                self._quiet_after = (
                    asyncio.get_running_loop().time() + _BARGE_QUIET_SECONDS
                )
                self._emit("barge", "interrupted")
            await self._ensure_devices()
            self.gate.reset()
            self._held = Held(self.cfg.sample_rate, self.cfg.channels)
            await self.mic.start()
            self._set_state("listening")
            return {"ok": True, "listening": True}

        held, self._held = self._held, None
        if held is None:
            return {"ok": True, "listening": False}
        await self.mic.stop()
        median, p95, peak = held.levels()
        log.info("held %.2fs: voiced %.2fs · rms median %.4f p95 %.4f peak %.4f · gain %.1fx",
                 held.seconds, held.voiced_seconds, median, p95, peak, self.autogain.gain)
        if not held.worth_hearing():
            log.info("nothing worth transcribing (%.2fs, %.2fs of it voiced)",
                     held.seconds, held.voiced_seconds)
            self._set_state("idle")
            return {"ok": True, "heard": ""}
        # Answering takes as long as the question deserves, and the key that
        # started it has already been released. Nothing waits on this.
        self._turn_task = asyncio.create_task(self._turn(held), name="turn")
        return {"ok": True, "heard": None}

    def _cancel_turn(self) -> None:
        task, self._turn_task = self._turn_task, None
        if task and not task.done():
            task.cancel()

    async def _turn(self, held: Held) -> None:
        """Transcribe what was said, ask the agent, speak the answer."""
        self._set_state("thinking")
        path = write_wav(held.pcm(), self.cfg.sample_rate, self.cfg.channels)
        try:
            heard = await transcribe(path)
        finally:
            # Speech, and it has served its purpose the moment it is text.
            path.unlink(missing_ok=True)

        if not heard or was_nothing(heard):
            log.info("heard nothing in %.2fs of audio (%s)", held.seconds, heard or "silence")
            self._set_state("idle")
            return

        log.info("heard: %s", heard)
        self._emit("heard", heard)
        self.server.broadcast(
            {"type": "transcript", "role": "user", "text": heard, "final": True}
        )
        answer = await self.ask_brain(heard)
        await self.say(answer.spoken)

    async def _ensure_devices(self) -> None:
        """Decide which microphone and which sink, once, and say so.

        Headphones come and go and so does the display they compete with, and
        `echo-cancel-source` looks identical in every case — which is exactly
        why a substitution used to be invisible. Resolved on the first key press
        rather than at startup so the answer describes the desk as it is now,
        and cached, because `pactl` in the path between pressing a key and the
        microphone opening is latency a person can feel. `input` and a
        microphone fault both drop the cache.
        """
        if self.devices is not None:
            return
        self.devices = await device_choice.resolve(
            self.cfg.input_target, self.cfg.output_target, avoid=self._bad_inputs
        )
        log.info("audio: %s", self.devices.describe())
        self.mic.target = self.devices.input_target
        self.mic.fallback_target = self.devices.fallback_input
        self.mic.on_fault = self._on_mic_fault
        self.mic.verify_target = device_choice.node_exists
        self.speaker.target = self.devices.output_target
        await self._broadcast_audio()

    async def _ensure_playback(self) -> None:
        """Have a speaker and a playback pump, starting them if this is the
        first thing to want them."""
        await self.speaker.start()
        if self._play_task is None or self._play_task.done():
            self._play_task = asyncio.create_task(self._play_pump(), name="playback")

    async def _enqueue_audio(self, pcm: bytes) -> None:
        """Hand one chunk to the speaker, waiting when the queue is full.

        Waiting is the point: the queue's size is what keeps a whole answer out
        of memory, and the worker blocks on its own pipe while we are behind.
        """
        if self._voice_dump is not None:
            self._voice_dump.write(pcm)
        await self._play_queue.put(pcm)

    def _drop_queued_audio(self) -> None:
        """Throw away audio that has not reached the speaker yet."""
        while not self._play_queue.empty():
            try:
                self._play_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def ask_brain(self, query: str):
        """The one path to the agent, so the waterfall sees every question.

        Both the voice tool call and the text-mode `ask` command come through
        here; otherwise a typed question would leave the panel blank while the
        agent worked.
        """
        log.info("brain(%s): %s", self.brain.backend, query)
        self._emit("agent", query, backend=self.brain.backend)
        started = time.monotonic()

        # What the desktop is doing rides along with every question: it costs
        # about 190 tokens and 8 ms, and guessing which questions are about the
        # panes would be wrong more often than carrying it always.
        answer = await self.brain.ask(query, await herdr_context())

        self._emit(
            "result",
            answer.spoken,
            backend=self.brain.backend,
            seconds=round(time.monotonic() - started, 1),
            links=len(answer.links),
            files=len(answer.files),
        )
        self.server.broadcast(answer.as_ui_payload())
        return answer

    async def shutdown(self) -> None:
        """Put everything down. There is no session to end — only pipes."""
        self._cancel_turn()
        self._held = None
        await self.mic.stop()
        self._drop_queued_audio()
        play_task, self._play_task = self._play_task, None
        if play_task:
            play_task.cancel()
        await self.speaker.flush_now()
        with contextlib.suppress(Exception):
            await self.brain.cancel()
        if self.state != "error":
            self._set_state("idle")

    def _on_mic_fault(self, message: str) -> None:
        """A microphone problem, said where it can be seen.

        The panel showing "listening" over a dead capture device was the single
        most misleading thing this program has done.
        """
        log.warning("microphone: %s", message)
        if self.devices and self.devices.input_target:
            self._bad_inputs.add(self.devices.input_target)
        # Chosen again on the next press, now that this one is known bad.
        self.devices = None
        self._emit("error", message)
        self.server.broadcast({"type": "error", "message": message})

    async def _broadcast_audio(self) -> None:
        """Tell the panel what the audio path is and what else it could be.

        Broadcast rather than answered on request: the IPC replays the last
        message of each type to a late joiner, so a settings window opened at
        any moment already knows, without a round trip.
        """
        self.server.broadcast(
            {
                "type": "audio",
                "input": self.cfg.input_target,
                # The log gets the full technical line; the panel gets the
                # short one. Putting `describe()` in a settings window was a
                # mistake — it read as a stack trace where a name belonged.
                "resolved": self.devices.summary if self.devices else "",
                "headphones": bool(self.devices and self.devices.headphones),
                "sources": await device_choice.list_sources(),
            }
        )

    # -- commands from the panel --------------------------------------------

    async def _on_command(self, message: dict) -> dict | None:
        command = str(message.get("cmd") or "")

        if command == "background":
            # The panel goes away and nothing else changes. There is nothing to
            # stop: the microphone follows the key, not the window.
            self.backgrounded = True
            self._emit("background", "the panel is away")
            self.server.broadcast({"type": "background", "background": True})
            return {"ok": True, "background": True}

        if command == "foreground":
            self.backgrounded = False
            self._emit("foreground", "panel back")
            self.server.broadcast({"type": "background", "background": False})
            return {"ok": True, "background": False}

        if command == "reset":
            # Start over without closing the panel. Two memories have to go, or
            # "forget that" only half works:
            #   the agent's thread, which codex/claude resume by id
            #   the panel's own transcript and waterfall
            # There is no third one any more. The conversation used to live on
            # a websocket that could only be forgotten by reconnecting it.
            self._cancel_turn()
            self.brain.reset()
            self.server.forget("answer", "transcript", "error")
            self.server.broadcast({"type": "reset"})
            if self.state == "error":
                self._set_state("idle")
            self._emit("reset", "new conversation")
            return {"ok": True}

        if command == "cancel":
            # Shut the assistant up, and let go of the key. Cancelling the turn
            # ends the agent and the voice worker with it; dropping the queue
            # and flushing the speaker is what makes it stop mid-word rather
            # than at the end of the sentence already in the pipe.
            #
            # The microphone is closed too, because a lost key release is a real
            # failure mode — Hyprland's `bindr` does not fire if the window
            # focus moves under a held key — and a microphone that cannot be
            # shut from the interface is the worst state this program has.
            held, self._held = self._held, None
            if held is not None:
                await self.mic.stop()
            self._cancel_turn()
            self._drop_queued_audio()
            await self.speaker.flush_now()
            await self.brain.cancel()
            self._set_state("idle")
            return {"ok": True}

        if command == "voice":
            name = str(message.get("value") or "")
            if name not in config.VOICE_GENDER:
                return {"ok": False, "error": f"unknown voice: {name}"}
            if name == self.cfg.voice:
                return {"ok": True, "voice": name}

            self.cfg.voice = name
            self._save_preferences()
            # Nothing to rebuild: the voice is a name passed to the next worker,
            # and the next worker is spawned for the next thing said.
            self._emit("voice", name)
            self.server.broadcast({"type": "voice", "voice": name})
            return {"ok": True, "voice": name}

        if command == "backend":
            value = str(message.get("value") or "")
            ok = self.brain.set_backend(value)
            if ok:
                self._save_preferences()
                self.server.broadcast({"type": "backend", "backend": self.brain.backend})
            return {"ok": ok, "backend": self.brain.backend}

        if command == "ask":
            # Text-only path: no microphone, no speech. This is how
            # omavoice-ctl exercises the brain on its own.
            answer = await self.ask_brain(str(message.get("query") or ""))
            return {"ok": True, "spoken": answer.spoken, **answer.as_ui_payload()}

        if command == "ptt":
            # One command, two edges. Hyprland sends the press with `bind` and
            # the release with `bindr`, and nothing else in the daemon carries
            # that distinction.
            return await self.ptt(bool(message.get("down")))

        if command == "say":
            # Debug handle: make the assistant speak a specific line, so the
            # voice can be tested without a person in the room. Registered as
            # the current turn so that cancel and the next key press cut it off
            # exactly as they would an answer.
            self._cancel_turn()
            self._turn_task = asyncio.create_task(
                self.say(str(message.get("text") or "Testing, one two.")), name="say"
            )
            with contextlib.suppress(asyncio.CancelledError):
                await self._turn_task
            return {"ok": True}

        if command == "audio":
            # Everything the settings window needs to show the audio path and
            # let someone change it: what is in use, what it resolved to and
            # why, and what else this machine could listen on.
            return {
                "ok": True,
                "input": self.cfg.input_target,
                "resolved": self.devices.summary if self.devices else "",
                "headphones": bool(self.devices and self.devices.headphones),
                "sources": await device_choice.list_sources(),
            }

        if command == "input":
            # "" restores following the system, which is the default and the
            # right answer for almost everyone: it picks the headset when one
            # is worn and the echo canceller when the room is in play.
            value = str(message.get("value") or "")
            if value == self.cfg.input_target:
                return {"ok": True, "input": value}
            self.cfg.input_target = value
            self._save_preferences()
            # Dropping the cached choice is the whole of the change: the next
            # key press resolves the path again and says what it picked.
            self.devices = None
            await self._broadcast_audio()
            return {"ok": True, "input": value}

        if command == "access":
            # Everything the consent screen needs in one round trip: what was
            # chosen, what was allowed, and what this machine offers to choose
            # from.
            return {"ok": True, **self._access(), "folders": self._folders()}

        if command == "onboarded":
            # Sent when the tour is dismissed, by any of its exits — finishing
            # it, skipping it, Esc. Skipping counts: a person who closed it
            # chose to, and showing it again the next morning would be the
            # program disagreeing with them.
            self.onboarded = bool(message.get("value", True))
            self._save_preferences()
            self._broadcast_access()
            return {"ok": True, "onboarded": self.onboarded}

        if command == "workspace":
            value = str(message.get("value") or "").strip()
            if not value:
                return {"ok": False, "error": "no folder given"}
            folder = Path(value).expanduser()
            if not folder.is_dir():
                return {"ok": False, "error": f"{folder} is not a folder"}
            folder = folder.resolve()
            if folder != self.cfg.brain_cwd:
                self.cfg.brain_cwd = folder
                self._save_preferences()
                # A conversation with the agent is anchored to the directory it
                # started in — codex indexes its sessions by it and refuses
                # --cd on resume, claude keeps its transcript per project. So a
                # thread begun in the old folder cannot be continued in the new
                # one; carrying it over would work for exactly one more turn
                # and then fail, which is the hardest kind of bug to place.
                self.brain.reset()
                self._emit("workspace", str(folder))
                self._broadcast_access()
            return {"ok": True, "workspace": str(folder)}

        if command == "consent":
            name = str(message.get("backend") or "")
            if name not in ("codex", "claude"):
                return {"ok": False, "error": f"unknown agent: {name}"}
            granted = bool(message.get("granted"))
            if granted:
                self.cfg.consented.add(name)
            else:
                self.cfg.consented.discard(name)
                # Withdrawing the first permission takes the second with it.
                self.cfg.unrestricted.discard(name)
            self._save_preferences()
            # Withdrawing is not only about the next question. The thread this
            # agent was holding is the record of the previous ones.
            if not granted:
                self.brain.reset()
            self._emit("consent", f"{name} · {'allowed' if granted else 'not allowed'}")
            self._broadcast_access()
            return {"ok": True, **self._access()}

        if command == "unrestrict":
            # The second permission: everything this agent can normally do,
            # rather than only the folder. Refused for an agent that has not
            # been allowed at all, because the screen offers them in that order
            # and a state it cannot draw is a state nobody can withdraw.
            name = str(message.get("backend") or "")
            if name not in ("codex", "claude"):
                return {"ok": False, "error": f"unknown agent: {name}"}
            granted = bool(message.get("granted"))
            if granted and name not in self.cfg.consented:
                return {"ok": False, "error": f"{name} is not allowed to answer yet"}
            if granted:
                self.cfg.unrestricted.add(name)
            else:
                self.cfg.unrestricted.discard(name)
            self._save_preferences()
            # The command line changes, and codex will not resume a thread that
            # was started under a different one.
            self.brain.reset()
            self._emit("access", f"{name} · {'unrestricted' if granted else 'held to the folder'}")
            self._broadcast_access()
            return {"ok": True, **self._access()}

        if command == "status":
            return {
                "ok": True,
                "workspace": str(self.cfg.brain_cwd or ""),
                "consented": sorted(self.cfg.consented),
                "unrestricted": sorted(self.cfg.unrestricted),
                "state": self.state,
                "backend": self.brain.backend,
                "voice": self.cfg.voice,
                "listening": self._held is not None,
                "background": self.backgrounded,
                # Which microphone is actually feeding whisper. Worth a line
                # of its own: every device in play can present itself under the
                # same name, and a silent substitution reads as a broken
                # assistant rather than a changed desk.
                "input": self.devices.input_target if self.devices else "",
                "output": self.devices.output_target if self.devices else "",
                "headphones": bool(self.devices and self.devices.headphones),
                "audio": self.devices.describe() if self.devices else "not chosen yet",
            }

        log.warning("unknown command: %s", command)
        return {"ok": False, "error": f"unknown command: {command}"}

    # -- run -----------------------------------------------------------------

    async def run(self) -> int:
        await self.server.start()
        self._level_task = asyncio.create_task(self._level_pump(), name="levels")
        self.server.broadcast({"type": "state", "state": "idle"})
        self.server.broadcast({"type": "backend", "backend": self.brain.backend})
        self.server.broadcast({"type": "voice", "voice": self.cfg.voice})
        self._broadcast_access()
        # The catalogue lives in one place — here — so the panel never has a
        # stale copy of which voices exist.
        self.server.broadcast(
            {
                "type": "voices",
                "voices": [
                    {"name": name, "gender": gender, "label": label}
                    for name, gender, label in config.VOICES
                ],
            }
        )

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stopping.set)

        log.info("ready (backend=%s, voice=%s)", self.brain.backend, self.cfg.voice)
        # An empty audio message rather than none, so a settings window opened
        # before the first key press shows "not chosen yet" instead of nothing.
        await self._broadcast_audio()
        await self._stopping.wait()

        log.info("shutting down")
        if self._level_task:
            self._level_task.cancel()
        try:
            await asyncio.wait_for(self.shutdown(), timeout=4)
        except asyncio.TimeoutError:
            log.warning("audio did not stop cleanly; leaving it")
        await self.server.stop()
        return 0


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="omavoice", description="Voice assistant daemon for Omarchy")
    parser.add_argument("--backend", choices=("codex", "claude"), default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    cfg = config.load()

    # OPENAI_API_KEY used to be stripped here, because the daemon owned one and
    # the agents it spawns had no business inheriting it. It owns none now, and
    # a key in this environment belongs to whoever put it there — codex, most
    # likely — so taking it away would be this program breaking something that
    # is not its business.

    if args.backend:
        cfg.backend = args.backend
    if args.debug:
        cfg.debug = True

    logging.basicConfig(
        level=logging.DEBUG if cfg.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    runner = Daemon(cfg).run()
    try:
        return asyncio.run(runner)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
