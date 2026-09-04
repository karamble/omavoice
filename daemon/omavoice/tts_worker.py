"""Kokoro speaks one answer, then this process ends.

The daemon spawns this per utterance rather than holding the model open. Loading
it costs about a second on a laptop CPU, and the alternative is half a gigabyte
resident for the hours between questions.

The contract is deliberately thin, because the parent has to survive this
process dying at any moment: text arrives on stdin until EOF, raw PCM16 leaves
on stdout until EOF, and anything worth reading goes to stderr. There is no
framing and no handshake — end of audio is end of file.

PCM16 little-endian, 24 kHz, mono, which is what Kokoro emits and what the
Speaker's pw-play already expects, so nothing resamples anywhere. The float to
int16 conversion happens here so numpy is never imported into the daemon.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Four threads, not eight: measured on an i7-1065G7 the quantised model runs at
# 1.49x real time and the full one at 0.36x, and hyperthreading makes both worse.
DEFAULT_THREADS = 4
DEFAULT_VOICE = "af_heart"
DEFAULT_LANG = "en-us"

# Enough of an answer to be worth speaking, not so much that a runaway agent
# reply becomes minutes of uninterruptible audio.
MAX_CHARS = 2000


def _model_dir() -> Path:
    root = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(root) / "omavoice" / "kokoro"


def _build(model: Path, voices: Path, threads: int):
    # Imported here rather than at module scope so --help works without the
    # model or the wheels being present.
    import onnxruntime as ort
    from kokoro_onnx import Kokoro

    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(model), options, providers=["CPUExecutionProvider"])
    return Kokoro.from_session(session, str(voices))


# Split on sentence ends, keeping the punctuation: Kokoro reads a question
# differently from a statement. Anything without a sentence end is spoken whole.
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE.split(text) if p.strip()]
    return parts or ([text] if text else [])


def _speak(kokoro, text: str, voice: str, speed: float, lang: str) -> None:
    """Synthesise sentence by sentence, writing each one as it is ready.

    Not create_stream: measured, it returns nothing until the whole text is
    synthesised, so a twenty-second answer stayed silent for eight seconds.
    A sentence at a time makes the wait the length of the first sentence
    however long the answer is.
    """
    import numpy as np

    out = sys.stdout.buffer
    for sentence in _sentences(text):
        samples, _rate = kokoro.create(sentence, voice=voice, speed=speed, lang=lang)
        pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        try:
            out.write(pcm)
            out.flush()
        except BrokenPipeError:
            # The parent stopped listening — barge-in, or it died. Either way
            # there is nothing left to say.
            return


def main() -> int:
    parser = argparse.ArgumentParser(description="Speak stdin with Kokoro, then exit.")
    parser.add_argument("--voice", default=os.environ.get("OMAVOICE_TTS_VOICE", DEFAULT_VOICE))
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--lang", default=DEFAULT_LANG)
    parser.add_argument("--threads", type=int, default=DEFAULT_THREADS)
    parser.add_argument("--model", type=Path, default=_model_dir() / "kokoro-v1.0.onnx")
    parser.add_argument("--voices", type=Path, default=_model_dir() / "voices-v1.0.bin")
    parser.add_argument("--text", default=None, help="speak this instead of reading stdin")
    args = parser.parse_args()

    text = args.text if args.text is not None else sys.stdin.read()
    text = " ".join(text.split())[:MAX_CHARS]
    if not text:
        return 0

    for path in (args.model, args.voices):
        if not path.is_file():
            print(f"omavoice.tts_worker: missing {path}", file=sys.stderr)
            return 2

    kokoro = _build(args.model, args.voices, max(1, args.threads))
    _speak(kokoro, text, args.voice, args.speed, args.lang)
    return 0


if __name__ == "__main__":
    sys.exit(main())
