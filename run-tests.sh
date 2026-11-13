#!/usr/bin/env bash
# Run the whole test suite.
#
# QT_QPA_PLATFORM=offscreen keeps the UI smoke test headless, so this works over
# ssh.  LAN_IDENTITY_HOME redirects the app's config/state into a throwaway
# directory: the smoke test exercises the code paths that call save_settings(),
# and without the redirect those writes land in the user's real
# ~/.config/lan-identity-gui/config.json.
set -euo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$HERE"

export QT_QPA_PLATFORM=${QT_QPA_PLATFORM:-offscreen}
export PYTHONPATH="$HERE/src${PYTHONPATH:+:$PYTHONPATH}"

TEST_HOME=$(mktemp -d "${TMPDIR:-/tmp}/lan-identity-test.XXXXXXXX")
cleanup() { rm -rf -- "$TEST_HOME"; }
trap cleanup EXIT

export LAN_IDENTITY_HOME="$TEST_HOME"

python3 -m unittest discover -s tests -v "$@"
