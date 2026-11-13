# AGENTS.md — lan-identity-gui

Guidance for an AI agent working in **this repository**. Fleet-wide rules live in the
machine's own `~/AGENTS.md`; this file only covers this checkout. Where the two differ, the
stricter one wins.

## What this is

A PySide6 desktop app that owns a SQLite catalog of the local network's devices and the MAC +
hostname rotation pools, and drives `mac-rotator.sh`-style rotation. It repairs malformed MACs by
proposal, suggests hostnames offline, generates `mac-list.txt` and `hostnamelist.txt` from the
catalog, and applies one identity through a narrow `pkexec` helper.

The same operations are available without a window: `src/ops.py` is the Qt-free layer and
`src/cli.py` renders it, so a timer or an `ssh` session can ask what the app thinks. The window
additionally follows its inputs — the collector CSV and the two generated lists — through a
debounced, re-arming watcher, and reads back an applied identity after the fact.

It replaces **nothing**: `mac-rotator.sh`, `lan-mac-inventory.sh` and `mac-rotate-unused.sh` stay
where they are and are only ever *read*.

## Commands

```bash
./run-tests.sh                     # full suite, headless (offscreen Qt)
./launch.sh                        # run the app
python3 src/main.py --check        # environment report, no window
python3 src/main.py --status       # counts, live identity, drift (no Qt)
python3 src/main.py --scan         # sweep the LAN and record it
python3 src/main.py --import       # import the collector CSV
python3 src/main.py --generate     # write both lists; refuses drift (exit 2)
python3 src/main.py --json         # machine-readable form of status/scan/generate
./scripts/privacy-check.sh         # leak gate; exit 1 = something to remove
./scripts/install-desktop-entry.sh # render + install the menu entry locally
```

Exit codes are a contract: `0` ok, `2` drift (nothing written), `3` error (including usage),
`4` reserved for verification. Nothing in this repo needs network access, and no test may acquire
it.

## Layout

| Path | Responsibility |
|------|----------------|
| `src/main.py` | entry point; dispatches to the CLI **before** any Qt import so every headless verb runs without a display |
| `src/cli.py` | argument parsing, renderers, exit codes and JSON for the headless verbs |
| `src/ops.py` | the operations both front ends call; Qt-free, so a timer and the window cannot diverge |
| `src/watcher.py` | `PathWatcher` over `QFileSystemWatcher`: debounced, re-armed after every event, directory-watched for files that do not exist yet |
| `src/verify.py` | post-apply verification; injected reader/scanner/sleep, so tests need no root and no LAN |
| `src/workers.py` | Qt threads: `ScanWorker`, `ApplyWorker`, `VerifyWorker` (read-only) |
| `src/catalog.py` | SQLite: devices, pools, `is_own` marker, verification columns, migrations, import/export data |
| `src/listfiles.py` | render/parse the two rotator files, drift detection, backups, prune |
| `src/rotator.py` | rotation arithmetic (`RotationEngine`) and the countdown |
| `src/scanner.py` | `arp-scan` / `ip neigh` parsing; proxy-ARP detection |
| `src/collision.py` | verdict rules: `in_use`, `proxy`, `unknown`, `own` |
| `src/applier.py` | revert-script writer, `pkexec` invocation, step report parsing |
| `src/hostnames.py` | offline suggestion from `assets/hostname-pool.json` |
| `src/config.py` | `Settings` dataclass, paths, interface/hostname/MAC probes |
| `src/vendor.py` | OUI lookup from the system prefix table only |
| `src/ui/` | PySide6 panels; `main_window.py` wires them and owns the guards, `drift_banner.py` is the non-modal drift report |
| `bin/apply-identity` | the only privileged code; run as root via `pkexec` |
| `tests/` | headless; `test_privacy.py` and `test_isolation.py` are the guards |

## Invariants — break one of these and the app lies

1. **The catalog is the source of truth.** `mac-list.txt` and `hostnamelist.txt` are generated
   exports. Never edit them as a way to change state; import or adopt instead.
2. **Drift is refused, never silently overwritten.** `listfiles.has_drift`, the modal in
   `MainWindow._do_generate` and the non-modal `DriftBanner` are the safety property; `--generate`
   refuses with exit `2`. Any new write path gets the same treatment. A headless run never adopts
   drift — adoption is an explicit request (`--drift adopt` or the banner's button).
3. **The live identity is flagged, not forbidden.** `is_own` marks the machine's current address
   and keeps it in the pool deliberately; that row cannot be deleted. It must **not** be set
   `protected` — the point is that "be yourself" stays selectable.
4. **Only what the scan proves blocks an apply.** `collision.BLOCKING = {in_use, proxy}`.
   `unknown` warns and allows. On a proxy-ARP network absence is never provable, so never render
   "free" — say "not seen".
5. **Repairs are proposals.** `macaddr.check` → `accept_repair`/`reject_repair`. Never
   auto-apply a repair, never substitute `B`/`A`/`F` (only `O S I i l t G g £` are confusion
   candidates), and re-validate: a "repair" that lands on a multicast address is not one.
6. **No privilege creep.** Rotation runs in-process or is handed to Konsole; apply goes through
   `bin/apply-identity`. No standing sudo, no systemd unit of our own, no daemon of our own. After
   a hostname change the helper restarts an existing `avahi-daemon` and reports `skipped` when the
   unit is not running — it installs, enables and starts nothing.
7. **`--check` and every headless flag must run without a display.** `main.py` dispatches to
   `cli.py` before any Qt import, and `PySide6` imports stay inside functions. `test_cli.py`
   asserts `PySide6` is absent from `sys.modules` — keep that test, it is the guard.
8. **A watcher re-arms or it lies.** Every write here is a `write .tmp` + `os.replace`, which drops
   the path from `QFileSystemWatcher`. `PathWatcher._arm` runs on every event and the parent
   directory is watched so a file that does not exist yet is still noticed. A watcher that goes
   silent after one write looks exactly like "nothing changed".
9. **Verification reports evidence; it never reverts.** `verify.verify_interface` reads only and
   stores a verdict (`verified`, `absent`, `carrier_down`, `duplicate_mac`, `timeout`, `skipped`).
   A failed reading offers the human Revert button and nothing else: a missing carrier or lease is
   not proof of a bad address, and an address the sweep did not see is "not seen", never "free".
10. **A cache entry is not an answer.** `scanner.parse_ip_neigh` keeps the kernel's neighbour
    state. `REACHABLE`, `DELAY` and `PROBE` are answers; `STALE` records one that stopped
    answering. `verify` only calls a duplicate on an address the sweep confirmed, and reports a
    `STALE` leftover as such. An entry with no freshness information at all keeps the conservative
    verdict: an import or a hand-built map proves neither way, and losing collision detection
    silently is worse than a leftover false positive that now says what it is.

## The apply contract

- `--down-wait` is how long the link stays down; the revert script is written with the same
  number, so a restore waits what the apply waited.
- `--dhcp-wait` is the lease wait. It runs whether or not `--no-internet-check` is given: the
  lease is evidence about the interface, and a privacy-preserving apply must not lose it.
- `--no-internet-check` skips the reachability probe and nothing else. The probe is also skipped
  when the lease failed — reachability means nothing without an address.
- After a hostname change the helper restarts `avahi-daemon` if it is running, reported as `ok` or
  `warn`, and `skipped` when the unit is not running or the name did not change. It is a restart,
  not a reload: `avahi-daemon --reload` does not re-read `avahi-daemon.conf`, so it cannot publish
  a new name.
- The revert script reports `revert incomplete` and exits non-zero when any leg failed; a restore
  that worked prints `revert complete` and exits 0. A leg that failed no longer hides behind a
  success line.

## Data contract

- Committed fixtures use documentation addresses only: MACs from RFC 7042's
  `00-00-5E-00-53-xx` block (and locally administered variants), IPv4 from `192.0.2.0/24`,
  hostnames from the bundled name bank.
- Never commit a real MAC, a private-range address, a real hostname, a machine-specific interface
  name, or an absolute home path. `scripts/privacy-check.sh` fails the tree on each of those shapes
  and `tests/test_privacy.py` proves it does.
- Which hostnames are private is itself host data: the deny list lives in the user's
  `config.json` (`fleet_hostnames`), which `.gitignore` keeps out of history. Do not move it into
  the repository "for convenience".
- Runtime state is installed state, not repository content: `~/.config/lan-identity-gui/`,
  `~/.local/state/lan-identity-gui/`, `exports/`, `revert/`. All git-ignored.

## Adding a setting

1. Add a typed field with a default to `Settings` in `src/config.py` (no positional surprises).
2. Read/write it through `load_settings`/`save_settings`; `from_dict` already ignores unknown keys,
   so old configs keep working.
3. If it affects generated files, thread it through `listfiles.render_*` and add a regeneration
   test proving the second run is byte-identical.

## Testing

- `./run-tests.sh` runs headless; Qt tests use the offscreen platform — do not require a display.
- Tests that touch config, state, catalog or list files must use `tempfile` and point the app at
  it explicitly. `LAN_IDENTITY_CONFIG=/dev/null` is how the privacy test keeps a developer's real
  deny list out of the result.
- The real corpus test is opt-in (`LAN_IDENTITY_SAMPLE`); the default suite must never need real
  network data.
- Test behaviours, not message strings — except where a message is the contract (refusal reasons).
- The watcher tests drive `PathWatcher` directly (write, atomic replace, duplicate event, deletion);
  `test_drift_watch.py` proves the app's own write is silent and a hand edit is not. Do not replace
  those with a "it emitted once" assertion — the failure they exist for is a watcher that stops.
- `test_verify.py` injects its reader, scanner and sleep. Never let verification reach the real
  interface or the LAN in the suite.

## Publishing

This repository is published **publicly**, and only from the host that owns the sanitising
pipeline, with a **fresh single-commit history**:

1. `scripts/privacy-check.sh` must be clean locally.
2. Remove any previous staging directory, then run the pipeline's `publish-project.sh` against
   this checkout (public visibility).
3. Verify by cloning the published repository and running the pipeline's
   `git-privacy-check.sh` on the clone — not by reading the pipeline's own report.

Never `git push` this repository to a public remote. A branch push publishes its whole ancestry,
and the pre-publish history is not sanitised.

## Known open checks (a human must do these)

- One `pkexec` apply, to confirm the step report, the revert script and a real verification
  verdict. The verdict rules themselves are covered by `tests/test_verify.py` with an injected
  reader, scanner and sleep; what is missing is the machine answering.
- One live collision refusal and one `unknown` warning against the real network.

The drift banner and the collector import were measured on a live Wayland session (2026-11-09,
throwaway `LAN_IDENTITY_HOME`): a collector row written by `os.replace` imported without a button,
a hand-edited `mac-list.txt` raised the banner with the diff, and the file kept the edit. That
check is closed — do not re-open it on the strength of the offscreen tests alone.

## Anti-patterns seen here already

| Pattern | Why it bit |
|---------|------------|
| Committed menu entry with an absolute home path | Published the author's home directory; the entry is now a template rendered at install time |
| Deny list of local hostnames inside the leak gate | The gate became the leak; the list moved to the uncommitted config |
| `MacAddress` string comparisons before normalisation | `00:00:00:00:00:00` normalises under `MULTICAST`; compare `check.usable`, not raw text |
| Treating a quiet scan as proof of availability | Proxy ARP answers with the gateway MAC for most addresses |
| Generating lists without a backup | `listfiles.write_file` writes the backup first; keep that order |
| A watcher that does not re-arm after `os.replace` | Atomic writers replace the inode and the watch silently dies |
