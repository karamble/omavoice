# omavoice

A voice assistant for [Omarchy](https://omarchy.org). Hold `F10`, talk, let go.
A window with a pixel waveform comes up, and it talks back. The thing Siri kept
promising to be.

What makes it different from the voice mode in the ChatGPT app is that **all of
it is local**. The hearing, the speaking and the thinking all happen on this
machine: no API key, no account, and nothing you say leaves it. The agent
searches the web when a question needs it, but it also searches *this machine* —
files, projects, configs, running processes. You can ask "how many plugins do I
have in omarchy" out loud, and the answer is a real one instead of an invented
one.

![The panel mid-conversation](preview.png)

## How it works

Two parts, and the split between them is the whole design.

**The ears** are [voxtype](https://voxtype.io), whisper `base.en` on the CPU.
It transcribes what you said while the key was down — about a fifth of real
time, so a five second question is text in one.

**The voice** is [Kokoro](https://github.com/thewh1teagle/kokoro-onnx), an 82M
model through ONNX Runtime, also on the CPU. It is spawned for each answer and
gone again: nothing is resident between questions. Neither of them knows
anything — they hear and speak, and that is all.

**The brain** is `claude -p` or `codex exec` on the subscription you already
have, switchable at runtime. Every question goes to it. It reads the filesystem
and the web and returns strict JSON: what to say out loud, what to put on
screen, which buttons to offer.

The split is what makes the assistant genuinely local. There is no key to buy,
no per-minute meter, and no audio on any wire.

```
hold F10 ─► omavoice-ptt down ─┬─► open the window
                               └─► omavoice-ctl ptt down
                                        │
   ┌─ QML plugin (Quickshell) ───────────────────────┐
   │  Overlay.qml   the window: waveform, PTT, MD    │
   │  BarWidget.qml state icon in the bar            │
   │  Client.qml    Unix socket, NDJSON              │
   └─────────────────────────────────────────────────┘
                               │  $XDG_RUNTIME_DIR/omavoice.sock
   ┌─ omavoiced (Python, systemd --user) ────────────┐
   │  pw-record ─► held while the key is down        │
   │      └─► voxtype ─► claude -p / codex exec      │
   │                          └─► Kokoro ─► pw-play  │
   └─────────────────────────────────────────────────┘
```

There is no audio in the QML, and that is not a stylistic choice: the plugin
shares a process with the bar, so anything slow in there would hang the whole
desktop. The panel is an ordinary Wayland window, so the compositor tiles it,
`SUPER+F` fullscreens it and `SUPER+G` groups it like anything else.

## Requirements

Everything here is external to the plugin, and **`bin/omavoice-check` tells you
which of it you are missing** — in a terminal, or on the setup card the panel
shows on first run, with the command to fix each one.

| | Why | Note |
|---|---|---|
| **Omarchy 4.0+** with `omarchy-shell` | the plugin is Quickshell QML | already there if you run Omarchy |
| **`voxtype`** | the ears | `sudo pacman -S voxtype-bin`, or from [voxtype.io](https://voxtype.io) |
| **`codex` or `claude` on your `PATH`** | this is the brain | whichever you already use; both run on your existing subscription |
| **PipeWire** with `pw-record` / `pw-play` / `pactl` | audio in and out | standard on Omarchy |
| **`setpriv`** (util-linux) | so a helper dies with the daemon | standard on Omarchy |
| **Python 3.11+** | the daemon | the virtualenv is built from your own `python3`; `uv`, if present, only installs the pinned packages |
| **339 MB of disk** | the Kokoro voice model | downloaded once by `setup.sh`, verified against a pinned `sha256` |

**No API key, and no account.** Nothing in this plugin authenticates to
anything, and the daemon opens no outbound connection at all. The agent reaches
the web only if you widen it — see Security and privacy.

The daemon itself imports nothing outside the standard library, so it starts and
reports what is missing even before the virtualenv exists. What needs the
virtualenv is the voice: `onnxruntime`, `kokoro-onnx` and `numpy`, installed
under `~/.local/share/omavoice/` from `daemon/requirements.lock`, where every
one of the 31 packages is pinned and bound to a digest, with `--require-hashes`
— which refuses a mismatch rather than warning about it. `pip` is not upgraded.
Nothing is installed system wide and nothing asks for `sudo`.

The virtualenv is built with the `python3` already on your machine and never
with a downloaded interpreter: setup exports `UV_PYTHON_DOWNLOADS=never` and
stops with a message if `python3` is missing or older than 3.11.

## Install

```bash
omarchy plugin add https://github.com/baranskyi/omavoice --enable
bash ~/.config/omarchy/plugins/io.github.baranskyi.omavoice/scripts/setup.sh
```

`omarchy plugin add` only puts the QML in place. `setup.sh` does the rest, and
takes steps if you want only one of them — `venv`, `models`, `unit`, `pipewire`,
`ctl`, or `all`, which is the default:

- builds the virtualenv and installs the pinned packages
- downloads the Kokoro voice model and checks its `sha256`
- installs the systemd user unit
- drops in the echo cancellation config
- links `omavoice-ctl` into `~/.local/bin`

Then two things it deliberately does not do for you:

1. **Start the daemon**
   ```bash
   systemctl --user enable --now omavoice
   ```
2. **The hotkey.** Two bindings, because it is hold-to-talk — press and release
   are separate. In `~/.config/hypr/bindings.lua`:
   ```lua
   local omavoice = "/home/you/.config/omarchy/plugins/io.github.baranskyi.omavoice/bin"
   o.bind("F10", "Ask the assistant", omavoice .. "/omavoice-ptt down")
   o.bind("F10", "Ask the assistant (release)", omavoice .. "/omavoice-ptt up", { release = true })
   ```
   Holding the key opens the window if it is not already up, and lets go when
   you do. It never closes it — pressing while it is open just listens.

`bin/omavoice-check` will tell you if any of that did not take.

### Echo cancellation — the important part

It does two jobs, and the second is the one you notice. Without the canceller
the assistant hears its own voice through the speakers when you talk over it.
Without the noise suppression that comes with it, a still room transcribes as
confident sentences nobody said. The config ships with the plugin and
`setup.sh` installs it at

```
~/.config/pipewire/pipewire.conf.d/99-omavoice-echo-cancel.conf
```

Reload PipeWire once afterwards: `systemctl --user restart pipewire`.

**Both ends have to go through the canceller.** It subtracts a reference signal
from the microphone — precisely what went through *its own* sink. Listening on
`echo-cancel-source` while playing to the system default output leaves it with
silence as its reference: it subtracts nothing, and the loop comes back. So
`OMAVOICE_INPUT=echo-cancel-source` and
`OMAVOICE_OUTPUT=omavoice_playback`, both at once. Measured
suppression on this machine is around −41 dB (RMS 0.0438 → 0.0004).

### Check the microphone gain before blaming anything else

If a silent room comes back as fluent nonsense — *"6 hours, 7 hours, one day."*,
*"Thanks for watching!"* — the first thing to check is not the gate, the model
or the canceller. It is the analog gain.

On the machine this was built on, ALSA `Capture` sat at 63/63 = **+30 dB** with
`Internal Mic Boost` adding **+10 dB** on top. The signal clipped inside the ADC
before PipeWire ever saw it, so room noise arrived at speech level and whisper
did what whisper does with saturated noise. Measured through
`echo-cancel-source`: median RMS **0.383**, peak 1.000, 1261 clipped samples in
four seconds. With the boost off and `Capture` at a sane setting, the same still
room measures median **0.00123**, peak 0.258, **zero** clipped samples — a
factor of 312.

```bash
amixer -c 0 sget Capture                 # pinned at 100%?
amixer -c 0 sget 'Internal Mic Boost'    # boost on top of it?
amixer -c 0 sset 'Internal Mic Boost' 0
amixer -c 0 sset Capture 50              # ~+20 dB here; aim for peaks well under 1.0
```

Confirm it with the line the daemon logs for every question:

```
held 4.86s: voiced 4.82s · rms median 0.1064 p95 0.4199 peak 0.7458 · gain 5.3x
```

`peak` at or near 1.000 is clipping. Speech wants headroom; `gain` makes up
whatever is left. Note this is live ALSA state — a reboot or a profile switch
puts it back, so make it stick if it was wrong.

Neither node becomes a system default — only omavoice goes through them,
and the rest of the system never notices.

Two more layers sit on top of the canceller, because one is not enough:

- **A noise gate**, which measures its own threshold. A microphone hears the
  fan, the keyboard and the room; transcription turns that into confident
  nonsense — `はい。`, `Gjiliv.` — and the assistant duly answers it. Its verdict
  is counted rather than applied: whisper is handed the recording as it was, and
  the count only answers "did anybody actually speak" before a question is
  transcribed at all. A chunk counts as a voice when it stands out from the room
  **and** would still look like speech once `AutoGain` has amplified it, which
  is a question about speech rather than about one microphone.

  The threshold is not a constant, because a constant is only ever right for
  the microphone it was picked on. A laptop mic at talking distance and a
  display mic across the desk are an order of magnitude apart, and the
  canceller's noise suppression moves the floor again; tuned for one, the same
  number either answers the fan or swallows half a sentence. So the gate tracks
  the room's noise floor and opens at eight times it — falling fast, so a
  different microphone is adopted in seconds, and rising only while the gate is
  shut, so a long answer of your own cannot walk the threshold up behind you.
  `OMAVOICE_GATE` takes a number if you would rather pin it.

  It holds open for longer than the server waits before calling a turn
  finished (`OMAVOICE_SILENCE_MS`, 1100 ms). That ordering matters: a shorter
  hold means the gate substitutes digital silence for the pause between two
  words, handing the server a cleaner silence than the room ever produced and
  talking it into ending a sentence you are still in the middle of.
- **Counting real playback time.** The model sends its answer far faster than it
  is spoken, so "the last chunk arrived" and "the room went quiet" are different
  moments, sometimes seconds apart. The microphone's threshold stays raised
  until `pw-play` has drained its buffer, plus 0.9 s for room reverberation.
  Without that, echo slipped through exactly in the gap: two seconds after "How
  can I help?" the assistant would hear "Why can you help me?" and answer it.

None of this stops you interrupting: live speech clears the threshold with room
to spare and residual echo does not. If something still gets through, pin
`OMAVOICE_GATE` to a number above what leaks; headphones remove the question
entirely.

When it misbehaves, run the daemon with `OMAVOICE_DEBUG=1` and read one line
per second while the key is held:

```
mic: peak=0.3241 rms=0.1064 clip=0 gate=0.0032 floor=0.0002 voiced=1.04s of 2.40s
```

`peak` against `gate` settles "why did it not hear me" in one glance, `clip`
settles whether the gain is too high, and `voiced` settles whether the question
was ever going to be transcribed at all.

## Using it

| Action | How |
|---|---|
| **Ask something** | **hold `F10`, anywhere** — or hold the button in the window, or hold `Space` while it has focus |
| Open the window | holding `F10` opens it; or click the crystal in the bar |
| Send it away | `Esc`, or the same bar icon |
| Fold the transcript away | the eye beside the agent badge |
| Interrupt an answer | `I` in the window, **right-click the crystal in the bar**, or `omavoice-ctl cancel` |
| Start a new conversation | `N` in the window, or `omavoice-ctl reset` |
| Settings | ⚙ in the window, or `omarchy-shell io.github.baranskyi.omavoice settings` |
| How it works | `H` in the window, or `omarchy-shell io.github.baranskyi.omavoice help` |
| Show the setup checks | the tour's fourth card, or `bin/omavoice-check` |
| Change the voice | ⚙ → the voice chip, or `omavoice-ctl voice af_heart` |
| Hear a voice | ⚙ → **Hear it**, or `omavoice-ctl say "..."` |
| List the voices | `omavoice-ctl voice` |
| Switch agent | `Tab` in the window, click the badge, or `omavoice-ctl backend claude` |
| Ask in writing | `omavoice-ctl ask "..."` |
| Inspect the state | `omavoice-ctl status` · `make logs` |

Right-clicking the crystal is also the way out of a stuck key: Hyprland's
`bindr` does not fire if focus moves while `F10` is held, and cancel closes the
microphone as well as stopping the answer.

### The bar says when it can hear you

The crystal and the word beside it glow while the microphone is open, and only
then — which now means: while you are holding the key. Passive, they sit quietly
in the theme's own foreground; live, they turn the same green the window uses
and breathe.

It is wired to the microphone rather than to the window, so it is honest even
when the window is not on screen. A steady light in a bar full of steady icons
stops being noticed within a day, which is why this one moves.

### Hold the key

There is no session and no conversation to be in or out of. The key is the
turn: press it and the microphone opens, let go and what you said goes to
whisper and then to the agent. Between turns nothing is listening, and nothing
is resident — no connection, no model in memory.

That is the whole model, and most of the old controls went with it:

- **`Esc` sends the window away.** Nothing stops. The agent finishes what it
  started and the answer is still spoken out loud. This is for when the question
  turns out to be a long one and there is no reason to sit in front of the
  window while it is computed.
- **`I` interrupts** an answer that is running long. It also lets go of the key,
  which matters when a release was never delivered.
- **`N` forgets** the conversation and starts clean.
- **Holding `F10` while it is speaking** cuts the answer off and starts a new
  question. The window's own button does the same.

All of them are plain letters rather than chords, because the window has no
text entry to compete with — and because a system-wide combination assigned
elsewhere would simply vanish here. `Ctrl+Space`, for one, is Omarchy's
dictation key, and `F9` stays voxtype's.

When you return you can see what happened while you were away: the daemon keeps
the last fourteen waterfall lines and replays them to a newly opened window
along with the answer. An empty log after coming back would hide exactly the
work the window was dismissed for.

### A new conversation

`N` wipes the context and starts clean. Two memories have to be forgotten, or
"forget everything" only half works:

- the agent's thread, which `codex`/`claude` resume by id;
- the transcript and the waterfall in the window itself.

There used to be a third — the conversation the Realtime API kept on its
connection, which could only be cleared by reconnecting. There is no connection
now, so there is nothing to reconnect. The voice, the folder and the grants
survive it.

### The waterfall

The panel does not hide its work behind a spinner. Every event is a line: what
was heard, what was asked of the agent, how long it thought, what it answered.
New on top, old sinking and fading.

```
22:48:31  ←  There are 8 plugins in the directory   claude · 9.9s
22:48:21  →  How many plugins in omarchy?           claude
22:48:19  ‹  how many plugins do i have
```

While the agent works, the status line counts — `Working · 1m 20s` — and its own
narration shows faintly behind the waveform: the tool it reached for, the
sentence it just wrote. Waiting two minutes is fine when you can see what is
being computed; the same two minutes facing a still panel feel like a hang.

That narration is also how a stuck question is told from a slow one. The daemon
ends a question after two minutes of **complete silence** from the agent rather
than after a fixed wall-clock limit, so an answer that is still working never
gets cut off however long it takes (`OMAVOICE_BRAIN_IDLE`, and
`OMAVOICE_BRAIN_TIMEOUT` as a fifteen-minute backstop).

The arrows are literal: `‹` inbound from the microphone, `→` out to the agent,
`←` back from it, `›` out to the speakers.

### Voice

Kokoro's English voices, American and British, listed with the gender each was
trained on so the picker can be scanned. It runs on the CPU, spawned for each
answer and gone again, so changing the voice needs no reconnection — the next
thing said uses it. The choice is remembered in
`~/.local/state/omavoice/preferences.json`.

The agent badge wears vendor colours: terracotta `#D97757` and the Anthropic
star for `claude`, a lavender-blue gradient and the Codex `>_` for `codex`.

## Removal

```bash
bash ~/.config/omarchy/plugins/io.github.baranskyi.omavoice/scripts/uninstall.sh
omarchy plugin remove io.github.baranskyi.omavoice
```

The script stops and removes the systemd unit, deletes the virtualenv at
`~/.local/share/omavoice/venv`, and removes the `omavoice-ctl` symlink from
`~/.local/bin` if it put one there.

Three things are left behind on purpose, and it tells you so:

- `~/.local/share/omavoice/kokoro/` — the voice model, 339 MB. It is not
  configuration, and re-downloading it because a virtualenv was removed would
  be a poor trade.
- `~/.config/omavoice/env` — your settings, if you wrote any.
- `~/.config/pipewire/pipewire.conf.d/99-omavoice-echo-cancel.conf` — echo
  cancellation, which other things on the machine may be relying on by now.

Delete any of them by hand once you are sure. Also drop the two `bindings.lua`
lines if you added them.

## Development

```bash
make sync       # copy the checkout into the plugins directory
make watch      # re-copy on every save; the shell reloads by itself
make validate   # Omarchy manifest check, symlink check, qmllint
make logs       # journalctl for the daemon
```

The parts are testable separately, bottom up:

```bash
# what is installed and what is missing
bin/omavoice-check                 # or --json, which is what the tour reads

# the audio path, no agent
~/.local/share/omavoice/venv/bin/python -m omavoice.audio --loopback

# the brain, no microphone
~/.local/share/omavoice/venv/bin/python -m omavoice.brain "how much disk space?"

# the voice, no panel and no microphone
echo "testing one two" | ~/.local/share/omavoice/venv/bin/python -m omavoice.tts_worker --voice af_heart | \
  pw-play --rate=24000 --channels=1 --format=s16 --raw -

# a whole turn, in writing
bin/omavoice-ctl ask "how many plugins do i have"
```

The daemon deliberately imports nothing outside the standard library, and that
is worth keeping: it means it runs before anything is installed, which is what
lets the tour report what is missing. Check it after touching imports:

```bash
PYTHONPATH=daemon /usr/bin/python3 -c "import omavoice.__main__"
```

Traps that cost time:

- **`compileall` will not catch a missing import.** `make lint` is
  `compileall`, which proves the file parses and nothing more — a name used but
  never imported passes it and fails at runtime, inside a handler, as a
  question that silently does nothing. Import the module and exercise the path.
- **Kokoro's `create_stream` does not stream.** It returns nothing until the
  whole text is synthesised, so a 22-second answer sat silent for 8.3 s. The
  worker splits sentences itself and calls `create()` per sentence, which makes
  the wait the length of the *first* sentence however long the answer runs.
- **The QML engine caches compiled types**, and editing a file that is not an
  entry point (`Waveform.qml`) is picked up neither by saving nor by
  `rescanPlugins`. It needs `omarchy-restart-shell`. If a QML error points at a
  line that is no longer in the file, that is the type cache, not your code.
- **A virtualenv cannot live inside the plugin folder.** Omarchy's validator
  rejects a plugin containing symlinks, and every virtualenv has a few
  (`bin/python`, `lib64`). That is why the environment lives under
  `~/.local/share/omavoice/` and the daemon is reached through
  `PYTHONPATH`.
- **When it behaves strangely, the first question is what it thinks it heard.**
  `journalctl --user -u omavoice | grep -E "held |heard:"` answers it
  immediately: if `heard:` contains its own last sentence, echo is leaking; if
  it is fluent nonsense from a quiet room, look at `peak` on the `held` line
  before anything else — see the gain note above.
- **`pgrep -f` matches the shell that ran it**, and `setsid nohup … &` gives
  `$!` the wrapper's pid rather than the daemon's. Both of those kill the wrong
  process and leave the real one running. Match on the exact argv instead.

## Layout

```
manifest.json       kinds: overlay + bar-widget, keepLoaded
Overlay.qml         the window, and the four screens inside it
SettingsWindow.qml  voice, agent, audio input, access
OnboardingWindow.qml the six-card tour, including the setup checks
ConsentWindow.qml   the folder and the per-agent grants
HelpWindow.qml      how it works, in the window
Waveform.qml        a figure of points on a Canvas
EventLog.qml        the event waterfall
Undertext.qml       the agent's narration, behind the figure
PrimeRadiant.qml    the logo and the bar icon
AgentBadge.qml      the vendor badge
BarWidget.qml       the state icon
Client.qml          socket and state

daemon/omavoice/
  __main__.py   the state machine, where everything is joined
  listen.py     the held audio, the WAV, voxtype
  speak.py      supervises the voice worker
  tts_worker.py Kokoro, spawned per utterance and gone again
  audio.py      pw-record / pw-play, RMS, the gate, AutoGain
  brain.py      claude -p / codex exec, parsing the answer
  herdr.py      what the desktop is doing, as context
  devices.py    which microphone and which sink, and why
  ipc.py        Unix socket, NDJSON, broadcast
  ctl.py        omavoice-ctl
  schemas/answer.json   the shape of the agent's answer

systemd/    the user unit, as a template setup.sh fills in
pipewire/   the echo cancellation config
scripts/    setup.sh (venv, models, unit, pipewire, ctl), uninstall.sh
bin/        omavoice-ctl, omavoice-ptt, omavoice-check
```

## Security and privacy

This plugin runs unsandboxed, like every Omarchy plugin, and it does more than
draw a widget. Plainly, what it does:

- **It installs a systemd user unit** (`omavoice.service`) that runs a
  Python daemon. `setup.sh` writes it; nothing is enabled without you running
  `systemctl --user enable`.
- **It records audio only while you hold the key**, and never sends it
  anywhere. The recording is written to a mode-600 file in `$XDG_RUNTIME_DIR`,
  handed to `voxtype` on this machine, and unlinked the moment it is text. The
  microphone is closed on the key release, on cancel, and if the daemon stops.
  There is no session and no connection: between turns nothing is listening.
- **The daemon opens no outbound connection at all.** It authenticates to
  nothing, holds no credential, and has none to hold. Whether anything reaches
  the network at all is entirely a question about the agent you allow, below.
- **Nothing is asked of an agent until you have said so.** On first run the
  panel asks for a folder and for permission, and refuses every request until
  both are answered. Permission is per agent, by name — allowing `codex` says
  nothing about `claude`. Both live in Settings ▸ Access afterwards and either
  can be withdrawn; `omavoice-ctl access`, `workspace`, `allow` and `revoke` do
  the same from a terminal.
- **By default the folder is a wall, not a hint.** A path outside it is not
  read:

  - `codex` runs under a permission profile built for that one invocation and
    passed with `-c`, so your own `~/.codex/config.toml` and ChatGPT login are
    untouched. It grants `read` on the folder and on nothing else, which also
    makes the whole filesystem unwritable and leaves the sandbox with no
    network. A path outside the folder does not merely fail — it does not
    exist.
  - `codex` also runs with `--disable apps --disable plugins`. This matters
    more than the per-server disabling next to it: beyond the MCP servers
    written in `~/.codex/config.toml`, codex carries a built-in server holding
    every connector the ChatGPT account has authorised — on the machine this
    was found on, 464 tools across 18 accounts, `gmail.send_email` among them.
    None of it appears in the config file or in `codex mcp list`, so naming
    servers one at a time reached none of it, and until v0.9.0 a sentence said
    near the laptop could have sent mail. Verified by listing the live servers
    with and without the switches.
  - `claude` runs under `--permission-mode dontAsk` with `--strict-mcp-config`,
    `--setting-sources ""`, and the write and web tools denied by name. Both
    halves matter. `dontAsk` refuses what would have asked — but not what you
    have already allowed, and Claude Code accumulates permissions per project,
    so in a directory you have worked in for weeks it refuses nothing.
    `--setting-sources ""` loads none of those files, so there is nothing
    pre-allowed and the mode has something to refuse. It costs the shell: with
    no accumulated permissions, `Bash` is denied too. That is the right way
    round — an unconfined shell makes any file scoping decorative.

  Two notes for anyone reading the code. In bounded mode codex is deliberately
  **not** given `--sandbox read-only`: passing that flag discards
  `default_permissions` and silently drops the profile, while still printing
  `sandbox: read-only`. And the profile's `:minimal` preset does grant the
  system paths — `/etc`, `/usr`, `/bin`, `/lib` — so "outside the folder" means
  your files, not every byte on the disk. No user data outside the folder is
  reachable: `~/.ssh`, `~/.codex`, `~/.config/omavoice/key`, `/tmp` and every
  other project all come back as though they did not exist.
- **Everything above is measured, not quoted** — and measured from the
  evidence rather than from the agent's own account of itself. Each agent was
  handed a canary file outside the folder, in both settings; what is recorded
  here is what the session transcript and the run envelope's
  `permission_denials` show, not what the model said it was allowed to do.
  Asking the model was how two earlier claims in this file came to be wrong:
  the "read-only" sandbox that bounded only writes, and a `dontAsk` that
  reported a refusal it had not actually been given.
- **You can lift it, per agent, and it says so plainly.** The second permission
  in the same screen — `omavoice-ctl unrestrict <agent>`, undone with
  `restrict` — hands the agent everything it can normally do: your MCP
  connectors, its web search, the rest of the machine. It is off until you
  turn it on, and turning it on is what the screen describes rather than what
  it hides. In that mode the command line is exactly what it was before any of
  this scoping existed, so there is no second configuration to drift.
- **Requests that read as a destructive command** (`rm -rf`, `mkfs`,
  `shutdown`) are refused before the agent is asked. A guard against accident,
  not against an attacker: it is a pattern match over a transcript, and "delete
  everything in my home directory" walks straight through it. What actually
  prevents the damage is that a bounded agent cannot write at all.
- **There is no key.** Earlier versions kept an OpenAI credential in
  `~/.config/omavoice/key`, mode 600, and went to some trouble to keep it out of
  the environment so no child could inherit it. None of that is needed now: the
  hearing and the speaking are local, and the agent uses the subscription you
  have already logged into. `OPENAI_API_KEY` is deliberately **not** stripped
  from the daemon's environment any more — if one is there it belongs to
  whoever put it there, most likely codex, and taking it away would be this
  program breaking something that is not its business.
- **Each question is a fresh process.** `claude -p` and `codex exec` are spawned
  per question and exit when they answer, which is what makes
  `--setting-sources ""` a boundary rather than a gesture: the reset happens
  every time, not once. It also means the agent cannot wait for anything or
  report back later, and it is told so in its system prompt — it used to promise
  otherwise, having messaged another session whose reply arrived after it had
  already exited.
- **The framing is a system prompt, not part of the question.** What arrives
  from whisper is untrusted text and frequently garbled; rules that have to hold
  are appended to the system prompt rather than sitting in the same message as
  the input they govern.
- **While the agent works, its own narration and the commands it runs appear
  faintly behind the waveform.** They are streamed as the lines arrive, shown,
  and dropped — never stored, and never replayed to a window that opens later,
  since that would show someone an agent working on a question answered minutes
  ago. Each line is trimmed to 180 characters, and the ceiling on the pipe it
  comes from is unchanged. Both backends now stream: `codex exec --json`, and
  `claude --output-format stream-json`, which also gives the daemon the only
  evidence that a long question is still alive rather than wedged.
- **What the agent hands back is bounded before it is kept.** The daemon runs
  for weeks and the agent it starts runs for a minute, so everything the short
  process writes would otherwise be held in the long one and pushed to the
  panel. Each pipe is read to a ceiling and the process is ended at it rather
  than drained; the answer file is refused if it is oversized rather than read
  in part; and every field that survives into an answer — spoken text,
  markdown, the number of links and files and the length of each — is cut to
  size before it is retained or broadcast.
- **Links and paths from an answer** are opened through an argv vector, never
  a shell string, because their source is a retelling of untrusted content. A
  link must also match `http(s)://`. A path is only checked for being absolute
  — `xdg-open` then dispatches it by type, so a file the agent names is opened
  by whatever handles it. You have to click the button, and the button's label
  came from the agent too.
- **Images are stripped out of markdown** — the inline form, the reference
  form and inline `<img>` — so the shell does not fetch a URL chosen by a model
  that is retelling untrusted content. Only the inline form was covered until
  v0.9.0; the reference form renders in Qt and was getting through.
- **No `sudo`, no `curl | sh`, no package installation outside the virtualenv.**

## What it costs

Nothing per minute, and nothing per question. There is no key, no meter and no
audio on any wire — the hearing, the speaking and the thinking all run on
hardware you already own, against a subscription you already pay for.

What it costs is CPU and a little disk. Measured on an i7-1065G7, four cores:

| | |
|---|---|
| voxtype `base.en` | 1.35 s for 6.6 s of audio (about a fifth of real time) |
| Kokoro fp32, 4 threads | about a third of playback time — 3x faster than it speaks |
| Kokoro cold start to first word | 1.52 s |
| Kokoro peak memory while speaking | 534 MB |
| Kokoro memory when idle | **0** — the worker is spawned per answer and exits |
| Disk | 339 MB of model, plus the virtualenv |

Kokoro at **fp32** rather than int8 is not an oversight. The quantised model is
89 MB against 311 MB and is slower than playback on this CPU — RTF 1.49 against
0.36 — despite the machine having AVX-512 VNNI. Four threads beat eight;
hyperthreading hurts.

What dominates a turn is none of the above: the agent does. Speech in and out
costs roughly two and a half seconds together, and the agent takes as long as
the question deserves.

## License

MIT — see [LICENSE](LICENSE).
