#!/usr/bin/env bash
# Undo scripts/setup.sh. Removes what the setup created; leaves what is yours.
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/omavoice"
VENV="$DATA/venv"
MODELS="$DATA/kokoro"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/omavoice.service"
ENV_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/omavoice/env"
PW_CONF="${XDG_CONFIG_HOME:-$HOME/.config}/pipewire/pipewire.conf.d/99-omavoice-echo-cancel.conf"
LINK="$HOME/.local/bin/omavoice-ctl"

note() { printf '  %s\n' "$*"; }

systemctl --user disable --now omavoice 2>/dev/null || true
rm -f "$UNIT"
systemctl --user daemon-reload
note "service stopped and removed"

rm -rf "$VENV"
note "virtualenv removed"

if [[ -L "$LINK" && "$(readlink -f "$LINK")" == "$PLUGIN_DIR/bin/omavoice-ctl" ]]; then
  rm -f "$LINK"
  note "$LINK removed"
fi

printf '\n\033[1mLeft in place on purpose:\033[0m\n'
# The model especially. It is 339 MB over a slow link and it is not
# configuration — deleting it as a side effect of removing a virtualenv, which
# is what `dirname $VENV` used to do here, meant an uninstall-reinstall cost a
# third of a gigabyte for no reason anybody could see.
[[ -d "$MODELS" ]]   && note "$MODELS — the voice model, 339 MB"
[[ -f "$ENV_FILE" ]] && note "$ENV_FILE — your settings"
[[ -f "$PW_CONF" ]]  && note "$PW_CONF — echo cancellation, which other things may now rely on"
note "Delete any of them by hand if you are sure."

printf '\n\033[1mThe plugin folder itself:\033[0m\n'
note "omarchy plugin remove io.github.baranskyi.omavoice"
echo
