# LAN Identity

A desktop app for the MAC address and the hostname a Linux machine presents on a
LAN. It keeps one SQLite catalog of the devices on the network, repairs and
extends that catalog without hand-editing text files, and generates the two lists
a rotation loop reads.

## What you can use it for

- **Understand your own network.** Import a scan once and keep a readable inventory
  of the devices, their vendor and the names they answer to, instead of reading a
  neighbour table by hand.
- **Clean up a broken inventory.** A malformed MAC comes back with a verdict and a
  proposed address you accept or reject; hostnames are suggested offline from the
  OUI vendor table and from the naming style already in use on this LAN.
- **Change the identity you present, one step at a time.** One named interface, the
  exact commands shown before anything runs, and a revert script written to disk
  first.
- **Check whether a target is safe before you take it.** An address that answered
  the network, or a MAC doing proxy ARP for the gateway, blocks the rotation; an
  address that only went quiet warns instead of being called free.
- **Rotate on a schedule or by hand.** Run the loop in the window with a countdown
  and a dry-run switch, or hand the real loop to a terminal.
- **Feed a script or a timer.** The same operations run headless with an exit-code
  contract, and a generated list that drifted is refused rather than overwritten.

Runs on Plasma 6 / Wayland as your normal user; the only privileged step is an
on-demand `pkexec` helper.

## What it does

| Area | Capability |
|------|-----------|
| Inventory | Import the LAN inventory collector CSV and any saved `mac,hostname,ip,seen_at` list. Re-importing is idempotent. |
| Repair | Every malformed MAC gets a verdict: `needs_separator_fix`, `needs_substitution`, `multicast`, `unrecoverable`. A proposed address must be accepted by you before it can be pooled or applied. |
| Hostnames | Offline suggestions from the local OUI vendor table, the naming style already used on this LAN, bundled vendor-default patterns, and a small bundled name bank (lakes, planets, Greek letters, …). No network lookup, ever. |
| Live identity | The address this interface carries right now is marked `YOU` and kept in the pool. It is a rotation target like any other — rotating back to it is how you become yourself again. |
| Collision check | Before an apply or a pool rotation, the targets are judged against a LAN scan. An address that **answered** — now, not once — or a MAC doing proxy ARP for the gateway **blocks** the rotation. A neighbour-cache leftover that stopped answering is reported as a `STALE` record and is not counted as a collision. A silent address only **warns**: on a LAN with proxy ARP, silence is not proof of freedom. |
| Pools | Add/remove/reorder/shuffle both rotation pools. Pairing is switchable: *independent* (each list wraps on its own length, like the script) or *paired*. |
| Generated files | `~/scripts/mac-list.txt` and `~/scripts/hostnamelist.txt` are **exports** of the catalog. Written on every change, with a drift check and timestamped backups. |
| Rotation | In-app `QTimer` loop with a countdown and a dry-run switch, plus **Run in Konsole** to hand the real `mac-rotator.sh` to a terminal under sudo. |
| Apply | **Copy commands** (default, zero privilege), **pkexec** to `bin/apply-identity`, or the Konsole handoff. A revert script is written to disk before anything changes, it waits what the apply waited, and it exits non-zero when a leg failed. After a hostname change the helper republishes the name. |
| Guardrails | Refuses to rotate on the interface carrying your SSH session; refuses to start a second rotator; flags your own address and refuses to delete it; warns when a hostname shadows another machine on your network. |

## Quick start

```bash
git clone …/lan-identity-gui.git      # or use a local checkout
cd lan-identity-gui
./launch.sh                            # desktop
python3 src/main.py --check            # environment report, no window
```

Optional menu entry: `./scripts/install-desktop-entry.sh` renders the bundled
template with this checkout's absolute path into
`~/.local/share/applications/`. The template itself names no path, so the
repository carries nothing about your home directory.

First launch imports `~/.local/state/lan-mac-inventory/hosts.csv` when the
catalog is empty, then seeds both pools from the existing list files so the first
regeneration is a no-op instead of a surprise diff.

## Headless commands

The verbs below run without a display — the CLI never imports PySide6 — so they
work over ssh and from a timer.

```bash
python3 src/main.py --status                     # counts, live identity, drift state
python3 src/main.py --status --json              # the same, machine-readable
python3 src/main.py --scan --no-arp-scan         # neighbour table only
python3 src/main.py --import [PATH]              # default: the collector CSV
python3 src/main.py --generate                   # write both rotator lists
python3 src/main.py --generate --drift report    # show what would change
python3 src/main.py --generate --drift adopt     # take the on-disk edit as truth
```

`--generate` refuses a list file that changed on disk since the app last wrote
it: the file is left alone, the diff goes to stderr, and the exit code is `2`.
That is deliberate — a timer must never discard a hand edit.

| Exit | Meaning |
|------|---------|
| `0` | done |
| `2` | a generated list file drifted; nothing was overwritten |
| `3` | usage error, or an operation failed |
| `4` | reserved for `--verify` |

Generation is deterministic: the same catalog produces the same bytes, so a
timer that runs `--generate` every few minutes rewrites nothing when nothing
changed.

## Automation in the window

The window follows the files it depends on, rather than waiting to be asked:

- **The collector CSV is imported when it changes.** A new row appears in the
  table while the window is open — no button, no restart. The startup import is
  gated on the file's mtime, so reopening the app with an unchanged CSV does
  nothing.
- **A hand-edited list file raises a banner, not a dialog.** The banner names the
  file, can show the unified diff, and offers *Import the edit*, *Overwrite* and
  *Ignore*. Nothing is overwritten until you choose. *Ignore* silences that file
  until it changes again. The app's own generate never raises it: the digest it
  records when it writes is what "not drifted" means.
- **An apply is read back.** After a successful, non-dry-run apply the app reads
  the interface — MAC, carrier, IPv4 within the configured DHCP wait — and sweeps
  the LAN for the same address at a second IP. The verdict is stored per device
  and shown in the Rotate tab: `verified`, `absent`, `carrier_down`,
  `duplicate_mac`, `timeout` or `skipped`. A `duplicate_mac` needs a live answer;
  a stale neighbour entry is noted as a cache leftover, not as a collision.

Verification reports; it does not act. A failed reading offers the existing
Revert button and nothing else, because a missing carrier or an absent DHCP lease
is not the same claim as a bad address — and on a proxy-ARP LAN, an address the
sweep did not see is written "not seen", never "free".

### The apply path

`bin/apply-identity` is the only code that runs as root, and it changes one named
interface and nothing else. Three details are worth knowing before you press the
button:

- **Both waits are yours to set.** `down_wait` is how long the link stays down;
  `dhcp_wait` is how long the helper waits for an address afterwards. The lease wait
  runs even with `--no-internet-check`, because the lease is evidence about the
  interface and the probe is not. The probe is skipped when the lease failed —
  reachability means nothing without an address.
- **The revert waits what the apply waited**, and it reports honestly: a leg that
  fails makes the script print `revert incomplete` and exit non-zero, instead of
  reaching `revert complete` anyway. An old revert script keeps its old text, which
  still restores the MAC.
- **A changed hostname is republished.** When the apply sets a hostname the helper
  restarts `avahi-daemon` if it is already running, reporting `ok`, `warn` or
  `skipped`. It never starts a service you do not run, and none of those three can
  fail an apply that already took effect. It is a restart and not a reload:
  `avahi-daemon --reload` does not re-read `avahi-daemon.conf`, so a reload would
  report success and keep publishing the old name.

## Requirements

- Python 3.11+ (developed against 3.14.9), PySide6 6.x
- `ip`, `hostnamectl` (util-linux / systemd)
- `arp-scan` for live scans (`cap_net_raw`), else the neighbour table is used
- `konsole` for the handoff buttons
- `pkexec` + a polkit agent for the apply button
- Optional: `/usr/share/nmap/nmap-mac-prefixes` or `/usr/share/arp-scan/ieee-oui.txt` for vendor names
- Optional: `avahi-daemon` for mDNS — restarted after a hostname change when it is
  already running, skipped when it is not — and `avahi-resolve` for hostname
  collision lookup

## State

| Path | What |
|------|------|
| `~/.config/lan-identity-gui/config.json` | settings you may hand-edit |
| `~/.local/state/lan-identity-gui/catalog.db` | the catalog: devices, hostnames, pools, applies |
| `~/.local/state/lan-identity-gui/exports/` | backups of the generated list files (last 5 per file) |
| `~/.local/state/lan-identity-gui/revert/` | revert scripts, written before each apply |
| `~/.local/state/lan-identity-gui/logs/` | apply log |

Everything the app owns stays under your home; nothing is written system-wide.
`LAN_IDENTITY_HOME` redirects this whole tree (used by the test suite).

### Settings worth knowing

`config.json` is plain JSON. Two fields are about *your* network, not the app:

- `fleet_hostnames` — other machines' hostnames. A suggestion that would shadow
  one is flagged in the UI. The repository ships this as an empty list on purpose;
  fill it in locally.
- `interface` — which NIC the app reads and writes. Defaults to the one carrying
  the default route.

## Privacy

The catalog, the collector CSV and the generated list files describe a real
network: MACs, hostnames, addresses. None of that belongs in a repository.

- Test fixtures use only documentation-reserved values (RFC 7042 MACs such as
  `00:00:5E:00:53:xx`, addresses from `192.0.2.0/24`) and names from the bundled
  bank. Tests over a real file are opt-in via `LAN_IDENTITY_SAMPLE`.
- `.gitignore` keeps the live database, config, exports, revert scripts and logs
  out of git.
- `scripts/privacy-check.sh` scans the tree for real-address shapes and fails if
  one appears. Run it before publishing anything. Local hostnames are not listed
  in the script — they come from your `config.json` (`fleet_hostnames`), because
  a committed deny list would publish the names it exists to catch.
- No committed file names an absolute home path either: the menu-entry template
  is rendered by `scripts/install-desktop-entry.sh` at install time, so the
  checkout's own location stays local.

## Tests

```bash
./run-tests.sh          # offscreen; no display needed
```

The suite runs with `QT_QPA_PLATFORM=offscreen` and a throwaway
`LAN_IDENTITY_HOME`, so it cannot touch your real config or catalog.

```bash
./scripts/privacy-check.sh   # no real LAN data in the tree
```

## Documentation

| File | Description |
|------|-------------|
| [docs/Changelog.md](docs/Changelog.md) | Change log |
| [docs/Lessons_learned.md](docs/Lessons_learned.md) | What the build taught, including the failures |
| `~/.opencode/specs/213-lan-identity-hardening/` (fleet checkout, not in this repository) | the five measured findings that hardened the helper, the scanner, the catalog merge and the revert |

## Known limits

- **The pkexec apply path has not been exercised end to end.** `pkexec` and
  `sudo` both prompt for a password on this host, which an agent cannot answer.
  Copy-commands and the Konsole handoff are verified by building the argv; the
  privileged run itself needs one human click.
- Rotation pairs are not stable in *independent* mode — each list advances on its
  own length, which is what `mac-rotator.sh` does. Use *paired* if you want them
  to move together.
- A MAC that is silent at pick time may not be absent later. Impersonation is
  inherently a race; the app makes it visible, not impossible.
- **Proxy ARP means a scan can never prove an address is free.** The app reports
  `unknown` and lets you proceed; it never claims a target is unused.
- **The mDNS republish has not been watched on the wire.** The apply reports the
  restart's exit status; that the new name then appears in an mDNS answer is a
  property of avahi, not something this suite measured.
