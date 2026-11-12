#!/usr/bin/env zsh
set -euo pipefail
root="${0:A:h}"
venv="$root/.venv"
if [[ ! -x "$venv/bin/python" ]]; then
  python3 -m venv "$venv"
  "$venv/bin/python" -m pip install --quiet --editable "$root"
fi
exec "$venv/bin/python" -m workspace_telemetry "$@"
