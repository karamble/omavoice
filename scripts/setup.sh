#!/usr/bin/env bash
# omavoice — setup after `omarchy plugin add`.
#
# Installing the plugin only puts QML in place. The voice itself is a daemon,
# and a daemon needs a Python environment, a speech model, a systemd unit and
# an echo canceller. This script sets those up. It never uses sudo and never
# overwrites a config you already have.
#
# It takes steps: `setup.sh` alone does everything, `setup.sh venv models` does
# those two. The wizard in the panel calls individual steps, which is why they
# exist — a person who is missing only the model should not be made to sit
# through a virtualenv being rebuilt to get it.
#
#   venv      the virtualenv and the pinned packages
#   models    the Kokoro voice, 339 MB, checked against a pinned sha256
#   unit      the systemd user service
#   pipewire  the echo cancelling and noise suppression drop-in
#   ctl       a symlink to omavoice-ctl in ~/.local/bin
#   all       every one of the above, in that order (the default)
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/omavoice"
VENV="$DATA/venv"
MODELS="$DATA/kokoro"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/omavoice.service"
PW_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/pipewire/pipewire.conf.d"
PW_CONF="$PW_DIR/99-omavoice-echo-cancel.conf"

# The voice, pinned by version and by digest. kokoro-onnx publishes these as
# release assets; the URL names the release rather than a branch, so the bytes
# behind it cannot change under us — and the digest is checked anyway.
MODEL_BASE="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
ONNX_SHA=7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5
VOICES_SHA=bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }

step_venv() {
  # It lives outside the plugin folder on purpose: Omarchy's validator rejects
  # a plugin containing symlinks, and every virtualenv has a few.
  #
  # It is built from an interpreter already on this machine, never a downloaded
  # one. This used to be `uv venv --python 3.13`, and on a machine without 3.13
  # that has uv fetch a standalone CPython: executable interpreter code arriving
  # outside the lockfile and outside --require-hashes, which is the one hole the
  # rest of this file exists to close. Telling uv never to download an
  # interpreter makes the refusal uv's own rule rather than a property of how we
  # happen to call it, and it covers the install step below as well.
  export UV_PYTHON_DOWNLOADS=never

  say "Python environment"
  if [[ -x "$VENV/bin/python" ]]; then
    note "already at $VENV"
  else
    local python
    python="$(command -v python3 || true)"
    if [[ -z "$python" ]]; then
      echo "no python3 on PATH — install Python 3.11 or newer, then run this again" >&2
      return 1
    fi
    if ! "$python" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
      echo "$python is $("$python" -V 2>&1) — omavoice needs Python 3.11 or newer" >&2
      return 1
    fi
    mkdir -p "$(dirname "$VENV")"
    "$python" -m venv "$VENV"
    note "created $VENV from $python ($("$python" -V 2>&1 | cut -d' ' -f2))"
  fi

  # Only what the lockfile names, and only if its digest matches. This used to
  # resolve a lower bound at install time and, on the fallback path, upgrade pip
  # to whatever was current first — two ways for an unreviewed release to end up
  # inside a service that runs on every login. --require-hashes refuses a
  # mismatch outright rather than warning about it, and pip is left alone.
  local lock="$PLUGIN_DIR/daemon/requirements.lock"
  if [[ ! -f "$lock" ]]; then
    echo "missing $lock — refusing to install anything unpinned" >&2
    return 1
  fi

  note "installing onnxruntime and kokoro-onnx — a few hundred megabytes, once"
  if command -v uv >/dev/null; then
    uv pip install --quiet --python "$VENV/bin/python" --require-hashes -r "$lock"
  else
    # --no-deps is safe only because the lockfile is the full transitive
    # closure. If it ever stops being that, this line installs a package with
    # its dependencies silently missing.
    "$VENV/bin/python" -m pip install --quiet --require-hashes --no-deps -r "$lock"
  fi
  note "dependencies installed from $lock, digests verified"
}

fetch_model() { # name sha
  local name="$1" want="$2" target="$MODELS/$1" tmp
  if [[ -f "$target" ]] && printf '%s  %s\n' "$want" "$target" | sha256sum -c --status; then
    note "$name already here and verified"
    return 0
  fi
  tmp="$target.part"
  # Into a temporary name and moved into place only once the digest matches. A
  # half-downloaded model is not a smaller model: onnxruntime opens it, fails
  # somewhere inside itself, and nothing in the message points here.
  rm -f "$tmp"
  if ! curl --fail --location --progress-bar --output "$tmp" "$MODEL_BASE/$name"; then
    rm -f "$tmp"
    echo "could not download $name" >&2
    return 1
  fi
  if ! printf '%s  %s\n' "$want" "$tmp" | sha256sum -c --status; then
    rm -f "$tmp"
    echo "$name does not match its expected sha256 — refusing it" >&2
    return 1
  fi
  mv "$tmp" "$target"
  note "$name verified and installed"
}

step_models() {
  say "The voice"
  if ! command -v curl >/dev/null; then
    echo "curl is needed to download the voice model" >&2
    return 1
  fi
  mkdir -p "$MODELS"
  fetch_model kokoro-v1.0.onnx "$ONNX_SHA"
  fetch_model voices-v1.0.bin "$VOICES_SHA"
  note "in $MODELS — nothing loads it until something is spoken"
}

step_unit() {
  # The PATH is captured from this shell rather than hardcoded: the daemon
  # shells out to codex or claude, and version managers keep those somewhere
  # systemd's own PATH will never look.
  say "systemd unit"
  mkdir -p "$(dirname "$UNIT")"
  sed -e "s|@PLUGIN_DIR@|$PLUGIN_DIR|g" \
      -e "s|@VENV@|$VENV|g" \
      -e "s|@PATH@|$PATH|g" \
      "$PLUGIN_DIR/systemd/omavoice.service" > "$UNIT"
  chmod 644 "$UNIT"
  systemctl --user daemon-reload
  note "installed $UNIT"
  note "start it with: systemctl --user enable --now omavoice"
}

step_pipewire() {
  # Two jobs. Without the canceller the assistant hears itself through the
  # speakers, takes that for your voice, and answers its own goodbye until you
  # stop it. The noise suppression that comes with it is the quieter half and
  # the one you notice more: without it a still room transcribes as confident
  # sentences nobody said.
  say "Echo cancelling and noise suppression"
  mkdir -p "$PW_DIR"
  if [[ ! -e "$PW_CONF" ]]; then
    cp "$PLUGIN_DIR/pipewire/99-omavoice-echo-cancel.conf" "$PW_CONF"
    note "installed $PW_CONF"
    note "restart PipeWire to load it: systemctl --user restart pipewire"
  elif cmp -s "$PLUGIN_DIR/pipewire/99-omavoice-echo-cancel.conf" "$PW_CONF"; then
    note "already up to date"
  else
    note "$PW_CONF exists and differs — left untouched."
    note "Compare it yourself:"
    note "  diff $PW_CONF $PLUGIN_DIR/pipewire/99-omavoice-echo-cancel.conf"
  fi
}

step_ctl() {
  say "omavoice-ctl"
  local bin_dir="$HOME/.local/bin" link
  link="$bin_dir/omavoice-ctl"
  mkdir -p "$bin_dir"
  if [[ -L "$link" && "$(readlink -f "$link")" == "$PLUGIN_DIR/bin/omavoice-ctl" ]]; then
    note "already linked"
  elif [[ -e "$link" ]]; then
    note "$link exists and is not ours — left untouched."
    note "Call it directly instead: $PLUGIN_DIR/bin/omavoice-ctl"
  else
    ln -s "$PLUGIN_DIR/bin/omavoice-ctl" "$link"
    note "linked $link"
  fi
}

steps=("$@")
[[ ${#steps[@]} -eq 0 ]] && steps=(all)
[[ "${steps[0]}" == all ]] && steps=(venv models unit pipewire ctl)

for step in "${steps[@]}"; do
  case "$step" in
    venv)     step_venv ;;
    models)   step_models ;;
    unit)     step_unit ;;
    pipewire) step_pipewire ;;
    ctl)      step_ctl ;;
    *)
      echo "unknown step: $step" >&2
      echo "usage: setup.sh [venv|models|unit|pipewire|ctl|all]..." >&2
      exit 2
      ;;
  esac
done

# What is left is what this script deliberately does not do for you: start a
# service, and edit a file you wrote yourself.
say "Done. Two things left:"
note "1. systemctl --user enable --now omavoice"
note "2. Bind the key in ~/.config/hypr/bindings.lua — press and release are"
note "   separate bindings, because it is hold-to-talk:"
note "     o.bind(\"F10\", \"Ask the assistant\", \"$PLUGIN_DIR/bin/omavoice-ptt down\")"
note "     o.bind(\"F10\", \"Ask the assistant (release)\", \"$PLUGIN_DIR/bin/omavoice-ptt up\", { release = true })"
note ""
note "$PLUGIN_DIR/bin/omavoice-check says what is still missing at any point."
echo
