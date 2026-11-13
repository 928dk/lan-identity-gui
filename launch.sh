#!/usr/bin/env bash
# Launch lan-identity-gui.  Never run this with sudo: the GUI is a user app and
# the only privileged step is the pkexec helper it invokes on demand.
set -euo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
    :
else
    echo "launch.sh: no graphical session (DISPLAY and WAYLAND_DISPLAY are unset)" >&2
    echo "On a headless box use: src/main.py --check" >&2
    exit 1
fi

exec python3 "$HERE/src/main.py" "$@"
