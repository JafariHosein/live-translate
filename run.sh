#!/usr/bin/env bash
# Launch Live Translate from its project virtual environment.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

if [[ ! -x .venv/bin/python ]]; then
    echo "error: .venv is missing. Create it with:" >&2
    echo "  python -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

# If the account was added to the "input" group after this login, the running
# session still has the old groups and evdev cannot open /dev/input/event*.
# newgrp activates the new membership for this launch, without a logout.
if ! id -nG | tr ' ' '\n' | grep -qx input \
    && id -nG "$USER" | tr ' ' '\n' | grep -qx input \
    && command -v newgrp >/dev/null 2>&1; then
    echo "Activating the 'input' group for this launch (log out/in to make it permanent)…" >&2
    printf -v command '%q ' .venv/bin/python main.py "$@"
    exec newgrp input -c "exec $command"
fi

exec .venv/bin/python main.py "$@"
