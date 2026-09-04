"""Run the Kokoro worker for one answer and stream its audio to the speaker.

The model is not held open between questions. Loading it costs about a second
and keeping it resident costs half a gigabyte for however long the machine sits
idle, so the worker is spawned per utterance and ends with the sentence.

Everything here follows the process discipline the rest of the daemon uses: the
child is its own session so a stray grandchild cannot outlive it, setpriv gives
it the daemon's death signal, stderr is read under a cap so a chatty child
cannot wedge on a full pipe, and the reap runs in `finally` on every path
including cancellation.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable

from .devices import read_capped, terminate_and_reap, trusted_binary

log = logging.getLogger("omavoice.speak")

# One read is one write to the speaker, and the waveform is drawn from whatever
# arrives. 20 ms of PCM16 at 24 kHz matches the cadence the microphone already
# produces, so the figure moves at the same rate whichever way audio is flowing.
CHUNK_BYTES = 960

# Kokoro synthesises at roughly a third of playback time, so this is generous
# for anything the answer schema permits. It exists to end a wedged worker, not
# to bound normal work.
DEADLINE = 120.0

_STDERR_MAX_BYTES = 8 * 1024


def _argv(voice: str) -> list[str]:
    # sys.executable rather than trusted_binary: the interpreter lives in the
    # venv under ~/.local/share, which is not a trusted directory and cannot be.
    # It is the same interpreter already running this code, which is the
    # strongest statement available about what it is.
    return [
        trusted_binary("setpriv"), "--pdeathsig", "TERM",
        sys.executable, "-m", "omavoice.tts_worker", "--voice", voice,
    ]


async def speak(
    text: str,
    voice: str,
    on_pcm: Callable[[bytes], Awaitable[None] | None],
    *,
    deadline: float = DEADLINE,
) -> None:
    """Synthesise `text` and hand the audio to `on_pcm`, one chunk at a time."""
    said = " ".join(str(text or "").split())
    if not said:
        return

    proc = await asyncio.create_subprocess_exec(
        *_argv(voice),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        await asyncio.wait_for(_run(proc, said, on_pcm), timeout=deadline)
    except asyncio.TimeoutError:
        log.warning("the voice did not finish within %.0fs", deadline)
    finally:
        await terminate_and_reap(proc)


async def _run(proc, said: str, on_pcm) -> None:
    # Both pipes are read at once: a worker that fills stderr while we are
    # reading stdout would otherwise deadlock.
    errors = asyncio.create_task(read_capped(proc.stderr, _STDERR_MAX_BYTES, drain_rest=True))
    try:
        if proc.stdin is not None:
            proc.stdin.write(said.encode("utf-8"))
            await proc.stdin.drain()
            # EOF is how the worker knows the sentence is complete.
            proc.stdin.close()
    except (BrokenPipeError, ConnectionResetError):
        pass

    while True:
        chunk = await proc.stdout.read(CHUNK_BYTES)
        if not chunk:
            break
        result = on_pcm(chunk)
        if asyncio.iscoroutine(result):
            await result

    await proc.wait()
    kept, dropped = await errors
    if proc.returncode:
        log.warning("the voice exited %s: %s", proc.returncode,
                    kept.decode("utf-8", "replace").strip()[-300:])
    elif dropped:
        log.debug("dropped %d bytes of voice output", dropped)
