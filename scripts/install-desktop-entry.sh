#!/usr/bin/env bash
# Install a menu entry for this checkout.
#
# The committed template carries no launcher path: naming a home directory in
# the repository would publish the user's own layout, and the app can live
# anywhere.  The real, absolute path is substituted here, at install time, into
# ~/.local/share/applications.  Nothing is written outside that directory.
#
# Usage:
#   scripts/install-desktop-entry.sh                  # install
#   scripts/install-desktop-entry.sh --print          # render to stdout
#   scripts/install-desktop-entry.sh --target-dir DIR # install elsewhere
#
# Exit 0 = installed (or printed), 2 = usage error.

set -euo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
TEMPLATE="$HERE/assets/lan-identity.desktop.in"
LAUNCH="$HERE/launch.sh"
TARGET_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
PRINT=0

while (($#)); do
    case "$1" in
        --print)      PRINT=1; shift ;;
        --target-dir) TARGET_DIR="${2:?--target-dir needs a directory}"; shift 2 ;;
        -h|--help)    sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)            echo "usage: ${0##*/} [--print | --target-dir DIR]" >&2; exit 2 ;;
    esac
done

[[ -f "$TEMPLATE" ]] || { echo "${0##*/}: $TEMPLATE is missing" >&2; exit 2; }
[[ -x "$LAUNCH" ]] || chmod +x "$LAUNCH"

# sed treats these as special in the replacement, and a path may contain them.
escaped=${LAUNCH//\\/\\\\}
escaped=${escaped//&/\\&}
escaped=${escaped//|/\\|}

render() {
    sed "s|@LAUNCH@|$escaped|g" "$TEMPLATE"
}

if ((PRINT)); then
    render
    exit 0
fi

mkdir -p "$TARGET_DIR"
OUT="$TARGET_DIR/lan-identity.desktop"
render > "$OUT.tmp"
mv "$OUT.tmp" "$OUT"

if command -v desktop-file-validate >/dev/null 2>&1; then
    desktop-file-validate "$OUT" || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$TARGET_DIR" >/dev/null 2>&1 || true
fi

echo "installed $OUT"
echo "launch.sh: $LAUNCH"
