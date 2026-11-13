#!/usr/bin/env bash
# Fail if anything in the tree looks like a real LAN address.
#
# Committed fixtures use documentation-reserved values: MACs from RFC 7042's
# 00-00-5E-00-53-xx block (and locally administered variants of it), addresses
# from 192.0.2.0/24, and hostnames from the bundled name bank.  Anything else is
# treated as a leak, including an absolute home path: the checkout's own
# location is installed state, not repository content.
#
# Hostnames are the exception that proves the rule.  Which names belong to the
# local network is itself host data, so the deny list is not committed: it is
# read from the app's own config.json (`fleet_hostnames`, which `.gitignore`
# keeps out of history).  Committing the names here would publish the very
# thing the rule exists to catch.
#
# Usage:
#   scripts/privacy-check.sh          # scan this checkout
#   scripts/privacy-check.sh DIR      # scan another tree (the test suite does this)
#
# Exit 0 = clean, 1 = something to remove.

set -euo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd -- "${1:-$HERE/..}" && pwd)
SELF="$ROOT/scripts/privacy-check.sh"

mapfile -d '' FOUND < <(
    find "$ROOT" -type f \
        \( -name '*.py' -o -name '*.md' -o -name '*.sh' -o -name '*.json' \
           -o -name '*.txt' -o -name '*.desktop' -o -name '*.in' \
           -o -name '.gitignore' \) \
        -not -path '*/.git/*' -not -path '*/__pycache__/*' \
        -not -path '*/.venv/*' -not -path '*/node_modules/*' \
        -not -path "$ROOT/exports/*" -not -path "$ROOT/revert/*" \
        -not -path "$ROOT/logs/*" \
        -print0 2>/dev/null || true
)

FILES=()
for path in "${FOUND[@]}"; do
    # This script carries the patterns themselves; it cannot scan itself.
    [[ "$path" == "$SELF" ]] && continue
    FILES+=("$path")
done

if ((${#FILES[@]} == 0)); then
    echo "privacy-check: no text files under $ROOT"
    exit 0
fi

problems=0
report() {
    printf '  %s\n' "$*"
    problems=$((problems + 1))
}
strip_root() { sed "s|^$ROOT/||"; }

# --- MAC addresses --------------------------------------------------------- #
# Documentation MACs are 00:00:5E:00:53:xx, plus the same value made locally
# administered (02:/0A:), plus the two special values every parser is tested on.
mapfile -t MACS < <(
    grep -hoE '\b([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b' "${FILES[@]}" 2>/dev/null \
        | tr 'a-f' 'A-F' | sort -u || true
)
for mac in "${MACS[@]}"; do
    case "$mac" in
        00:00:5E:*|02:00:5E:*|0A:00:5E:*|C0:00:5E:*|01:00:5E:*) continue ;;
        00:00:00:00:00:00|FF:FF:FF:FF:FF:FF) continue ;;
    esac
    while IFS= read -r hit; do
        report "real-looking MAC $mac at $(printf '%s' "$hit" | strip_root)"
    done < <(grep -inH -- "$mac" "${FILES[@]}" 2>/dev/null || true)
done

# --- everything else ------------------------------------------------------- #
# Which hostnames are private is host data, so the list is not committed: the
# app already keeps it in config.json (`fleet_hostnames`), and this script reads
# the same file.  Writing the names here would publish exactly what the rule
# exists to catch.  Without a config the rule is skipped and the address rules
# still run.
CONFIG="${LAN_IDENTITY_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/lan-identity-gui/config.json}"
LOCAL_NAMES=()
if [[ -f "$CONFIG" ]] && command -v python3 >/dev/null 2>&1; then
    while IFS= read -r name; do
        [[ -n "$name" ]] && LOCAL_NAMES+=("$name")
    done < <(python3 - "$CONFIG" <<'PY' 2>/dev/null || true
import json, re, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(0)
for item in data.get("fleet_hostnames") or []:
    name = str(item).strip()
    if name and re.fullmatch(r"[A-Za-z0-9._-]+", name):
        print(name)
PY
    )
fi

checks=(
    'private IPv4 address|\b(10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}|192\.168\.[0-9]{1,3}\.[0-9]{1,3})\b'
    'machine-specific NIC name|\benp[0-9]+s[0-9]+[a-z0-9]*\b'
    'saved-list filename|\bListe_[0-9]{4,}\b'
    'absolute home path|/(home|Users)/[A-Za-z0-9._-]+/'
)
if ((${#LOCAL_NAMES[@]})); then
    joined=$(IFS='|'; printf '%s' "${LOCAL_NAMES[*]}")
    checks+=("local hostname|\\b($joined)\\b")
    echo "privacy-check: ${#LOCAL_NAMES[@]} local name(s) from $CONFIG"
else
    echo "privacy-check: no local name list at $CONFIG; skipping the hostname rule"
fi
for entry in "${checks[@]}"; do
    label=${entry%%|*}
    pattern=${entry#*|}
    hits=$(grep -nHE "$pattern" "${FILES[@]}" 2>/dev/null || true)
    if [[ -n "$hits" ]]; then
        while IFS= read -r hit; do
            report "$label at $(printf '%s' "$hit" | strip_root | cut -c1-160)"
        done <<< "$hits"
    fi
done

if ((problems > 0)); then
    echo
    echo "privacy-check: $problems problem(s) — remove the real network data above."
    exit 1
fi

echo "privacy-check: clean (${#FILES[@]} files scanned, documentation addresses only)"
