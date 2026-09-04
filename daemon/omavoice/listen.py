"""Hearing, on this machine: a held key, then whisper.

The Realtime API decided when a turn ended, from the audio. A held key says it
outright, which removes the guess and with it the reason for a server-side VAD:
the microphone is open while F10 is down and shut the rest of the time.

Transcription is voxtype, which is already installed for dictation and already
carries the whisper models. It reads a WAV and prints the text, so this is a
subprocess and a temporary file rather than a second copy of a speech stack.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import wave
from pathlib import Path

from .audio import clipped_samples, rms_full_scale
from .devices import read_capped, terminate_and_reap, trusted_binary

log = logging.getLogger("omavoice.listen")

# A minute of speech is a long question. The cap is against a key that got
# stuck, not against anybody who talks at length.
MAX_SECONDS = 60

# Below this there is nothing to transcribe, and whisper will confabulate a
# sentence out of a click if given the chance.
MIN_SECONDS = 0.3

# And below this much of it actually being speech. Whisper answers silence with
# stock phrases — "Thank you.", "I don't know, I love you" — said with complete
# confidence, and the agent then spends fifteen seconds answering them.
# How much of the held audio has to look like a voice before it is worth
# transcribing at all. Raised from 0.25: whisper invents fluent sentences out
# of near-silence — "6 hours, 7 hours, one day.", "What a morning, Tom." — and
# a quarter second is a keystroke, not a question.
MIN_VOICED_SECONDS = 0.35

# base.en transcribes at about a fifth of real time here, so a minute of audio
# is a dozen seconds of work. This ends a wedged run, not a slow one.
TIMEOUT = 90.0

_MAX_OUTPUT = 256 * 1024
_MAX_STDERR = 8 * 1024


def _runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")


def write_wav(pcm: bytes, rate: int, channels: int) -> Path:
    """Put raw PCM16 where voxtype can read it. Owner-only: this is speech."""
    path = _runtime_dir() / f"omavoice-heard-{os.getpid()}.wav"
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)
    os.chmod(path, 0o600)
    return path


async def transcribe(path: Path) -> str:
    """What was said, or an empty string. Never raises into the daemon."""
    try:
        tool = trusted_binary("voxtype")
    except Exception:  # noqa: BLE001
        log.warning("voxtype is not installed; nothing can be heard")
        return ""

    proc = await asyncio.create_subprocess_exec(
        tool, "transcribe", str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        # Both pipes at once: voxtype narrates model loading on stderr, and a
        # full pipe there would stop it writing the transcript.
        errors = asyncio.create_task(read_capped(proc.stderr, _MAX_STDERR, drain_rest=True))
        raw, _ = await asyncio.wait_for(read_capped(proc.stdout, _MAX_OUTPUT), timeout=TIMEOUT)
        await proc.wait()
        kept, _ = await errors
    except asyncio.TimeoutError:
        log.warning("voxtype did not finish within %.0fs", TIMEOUT)
        return ""
    finally:
        await terminate_and_reap(proc)

    if proc.returncode:
        log.warning("voxtype exited %s: %s", proc.returncode,
                    kept.decode("utf-8", "replace").strip()[-200:])
        return ""
    return _transcript(raw.decode("utf-8", "replace"))


# voxtype narrates on stdout, not stderr: the model it loaded, the resampling
# it did, and a log line quoting the first fifty characters of the result. The
# transcript is the last thing it prints, on a line of its own.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _transcript(out: str) -> str:
    lines = [_ANSI.sub("", line).strip() for line in out.splitlines()]
    for line in reversed(lines):
        if line:
            return " ".join(line.split())
    return ""


# What whisper produces from silence, verbatim and with total confidence. The
# gate above should stop most of it ever reaching the model; this is the line
# after that, and it is cheap.
_NOTHING_WAS_SAID = {
    "you", "thank you.", "thanks for watching!", "thank you for watching!",
    "i don't know, i love you", "bye.", ".", "so", "[blank_audio]",
    "you're welcome.", "okay.", "oh.", "hmm.",
}


def was_nothing(heard: str) -> bool:
    return heard.strip().strip('"').lower() in _NOTHING_WAS_SAID


class Held:
    """The audio captured while the key was down."""

    def __init__(self, rate: int, channels: int) -> None:
        self.rate = rate
        self.channels = channels
        self._chunks: list[bytes] = []
        self._bytes = 0
        self._voiced = 0
        # Counted as it arrives rather than derived afterwards: a clipped
        # recording is the one fault no later stage can repair, and RMS cannot
        # tell it apart from a merely loud one.
        self._clipped = 0
        self._ceiling = int(MAX_SECONDS * rate * channels * 2)

    @property
    def seconds(self) -> float:
        return self._bytes / (self.rate * self.channels * 2)

    def add(self, chunk: bytes, voiced: bool = True) -> None:
        # Stop growing rather than stop recording: the key is still down, and
        # dropping the earlier audio would lose the question's beginning.
        if self._bytes >= self._ceiling:
            return
        self._chunks.append(chunk)
        self._bytes += len(chunk)
        self._clipped += clipped_samples(chunk)
        # The gate's verdict is counted but not applied: whisper should hear
        # the room as it was, and the count is only there to answer "did
        # anybody actually say anything".
        #
        # One verdict, and it already carries both halves: the gate asks
        # whether this stands out from the room, and it is only allowed to open
        # when AutoGain says the chunk would still look like speech once
        # amplified. That second test is what makes the judgement a statement
        # about speech rather than about this particular microphone.
        if voiced:
            self._voiced += len(chunk)

    def levels(self) -> tuple[float, float, float]:
        """median, p95 and peak chunk RMS of what was held, full scale.

        Reported per turn because the two numbers that decide whether a
        question is heard at all — the room's level and a voice's level — are
        properties of a microphone and its gain, not constants. Guessing them
        from anything other than this machine's own recordings is how a
        working assistant came to answer nothing at all.
        """
        levels = sorted(
            rms_full_scale(c) for c in self._chunks if len(c) >= 2
        )
        if not levels:
            return (0.0, 0.0, 0.0)
        return (levels[len(levels) // 2],
                levels[min(len(levels) - 1, int(len(levels) * 0.95))],
                levels[-1])

    @property
    def clipped(self) -> int:
        """Samples that hit the rail. Anything above zero is a gain fault."""
        return self._clipped

    def pcm(self) -> bytes:
        return b"".join(self._chunks)

    @property
    def voiced_seconds(self) -> float:
        return self._voiced / (self.rate * self.channels * 2)

    def worth_hearing(self) -> bool:
        return self.seconds >= MIN_SECONDS and self.voiced_seconds >= MIN_VOICED_SECONDS
