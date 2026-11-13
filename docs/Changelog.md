# Changelog

## [Unreleased]

### Added

- **The collector CSV is watched.** New rows arrive while the window is open, and
  the startup import is gated on the CSV's mtime, so a restart with an unchanged
  file does nothing. Writes are debounced, so a burst of appends is one import.
- **A drift banner for the generated lists.** A hand edit raises a non-modal
  banner with the diff and three answers — import the edit, overwrite, or ignore
  until the next change. The app's own write stays silent because the digest it
  records is the definition of "not drifted".
- **Post-apply verification.** After a real apply the interface is read back
  (MAC, carrier, IPv4 within the DHCP wait) and the LAN is swept for a duplicate.
  The verdict — `verified`, `absent`, `carrier_down`, `duplicate_mac`, `timeout`,
  `skipped` — is stored per device and shown in the Rotate tab. It reports; it
  never reverts on its own.
- **`src/watcher.py`** — a `QFileSystemWatcher` wrapper that re-arms after every
  event, because every write here is an `os.replace` and the watcher drops the
  path on replacement.

- **Headless CLI** — `--status`, `--scan`, `--import` and `--generate` run with no
  window and no Qt import, so they work over ssh and from a timer. `--json` for
  machine consumers. Exit `2` means "a list file drifted and was not overwritten",
  which is the only safe answer a timer can give.
- **`src/ops.py`** — the operations both front ends call. The window's generate
  and adopt paths now run through it, so the CLI cannot disagree with the GUI
  about backups or drift.
- **The lease is reported whether or not the internet probe is on.**
  `--no-internet-check` used to cancel the lease wait along with the probe, so a
  privacy-preserving apply produced no statement about whether the interface ever
  got a usable address. The wait and its `lease` step now run unconditionally; the
  probe stays behind the flag and is skipped when the lease failed.
- **A changed hostname is republished.** After `hostnamectl set-hostname` the
  helper restarts `avahi-daemon` if it is already running and reports `ok`, `warn`
  or `skipped`. It never starts a service that was not running, and the step
  cannot fail an apply. A restart, not a reload: `avahi-daemon --reload` does not
  re-read `avahi-daemon.conf`.

### Fixed

- **Generation was not deterministic.** The list header carried a timestamp, so
  every regeneration looked like a change and rewrote both files. A human pressing
  a button never noticed; a timer would have churned the files and their backups
  every tick. The header no longer stamps, and the file's mtime carries the time.
- **The importer made a device out of a comment line.** A saved list with a `#`
  note or a `#`-prefixed header row produced a broken device row. Comments are now
  skipped, as they already were in the list parsers.
- **A neighbour-cache leftover was reported as a collision.** `ip neigh` prints a
  state per entry and the parser dropped it, so a `STALE` record — a MAC seen once
  and never refreshed — produced the same `duplicate_mac` verdict as a device that
  answered seconds ago. The state is kept, along with a per-address freshness
  list, and only a live answer (`REACHABLE`, `DELAY`, `PROBE` or an `arp-scan`
  reply) counts as a collision. A `STALE` entry is reported as the cache record it
  is; an entry with no freshness information at all keeps the old, conservative
  verdict.
- **Re-import ordered a device by the spelling of its fields.** The merge compared
  `ip` and `seen_at` with one string `>`, so a newer observation of a lower address
  was dropped, an offset change reversed the order, and a stray `"yesterday"`
  outranked a real timestamp. `ip` and `seen_at` now describe one observation and
  move together, ordered by the instant the timestamp names; an unparseable or
  offset-less value decides no order and is kept verbatim.
- **The revert script claimed success after a failed leg.** It slept a hardcoded
  three seconds whatever `down_wait` said, and `|| echo "MAC restore failed"`
  printed the error and still reached `revert complete` with exit 0. It now emits
  the configured wait, sets a failure flag per leg, attempts the remaining legs,
  and exits 1 with `revert incomplete`.

### Changed

- Adopting hand edits moved from the window into `ops.adopt_from_disk`, shared by
  the "Import the edit" button and `--generate --drift adopt`.

## [0.1.0] - 2026-11-06

First release. Absorbs the inventory work from packet 204, the unused-identity
picker from packet 205, and the list/rotation behaviour of `mac-rotator.sh` into
one desktop app. The importer was developed against a real collector export, then
the suite was cut free of it: committed tests use documentation addresses only and
a real corpus is opt-in.

### Added

- **Catalog** — one SQLite database owning devices, hostnames, both rotation
  pools, scans and apply history. Re-import merges instead of duplicating.
- **MAC repair** — statuses `ok`, `needs_separator_fix`, `needs_substitution`,
  `multicast`, `unrecoverable`, each with a confidence and a re-validation of the
  proposed address. Repairs are proposals until accepted.
- **Hostname suggestions** — offline only: local OUI vendor table, LAN naming
  patterns, bundled vendor defaults (`DESKTOP-…`, `android-…`, `-01` style), and a
  small bundled name bank with attribution to Greg Ercolano's hostname gist.
- **Device table** — sortable, filterable by scope (`assignable + absent`,
  `rows needing attention`, `rotation pool`, `seen on the LAN`) and free text.
- **Detail panel** — accept/reject a repair, set a MAC by hand, suggest and set a
  hostname, notes, protect, pool membership, CSV preview, copy/apply/delete.
- **Pool panel** — add/remove/reorder/shuffle both pools, pairing mode, hostname
  prefix, regenerate the list files on demand.
- **Rotation panel** — engine that reproduces `mac-rotator.sh` ordering, dry-run
  switch, interval and timing knobs, single rotation, start/stop with countdown,
  and **Run in Konsole** handoff to the real script.
- **Applying** — three routes: copy commands, `pkexec bin/apply-identity` (JSON
  step report), Konsole handoff. Revert script written to disk first.
- **Live identity** — the address the interface carries is recorded (`is_own`),
  shown as `YOU`, and kept in the MAC pool so rotating back to it stays one click
  away. Flagged, never withheld; the row cannot be deleted.
- **Collision check** — before an apply or a pool rotation, targets are judged
  against a fresh scan. An answering MAC or a proxy-ARP gateway blocks; a silent
  address warns and is allowed. Hostnames are checked with `getent`/`avahi` when
  the apply asks for a name change.
- **Scanner** — keeps every address a MAC answered for, so proxy ARP is visible
  (`proxy_macs`, `ScanResult.answers`) instead of collapsing to one row.
- **Privacy** — `.gitignore` for the live state, `scripts/privacy-check.sh` to
  fail on real-address shapes, documentation-only MAC/IP fixtures, an empty
  `fleet_hostnames` default filled from `config.json`, and a name bank instead of
  the LAN's own hostnames.
- **Guards** — SSH-on-target-interface refusal, running-rotator refusal,
  protected devices, hostname shadowing warning, drift detection on the generated
  files with backup-before-write.
- **Tests** — parsing, repair, catalog merging, pool ordering, rotation engine,
  collision verdicts, scanner parsing, apply command construction, privacy, and an
  offscreen UI smoke test.

### Fixed

- Generated file writes leaked into the real config during tests; the suite now
  runs under a redirected `LAN_IDENTITY_HOME` and a test enforces it.
- Vendor names were only resolved on scan or row-open, leaving the Vendor column
  empty after an import; they are now resolved at insert time and backfilled on
  re-import.
- The Konsole handoff built a shell string, which mangled the empty hostname
  prefix (`-p ''`); it now builds an argument vector.
- `parse_arp_scan` deduplicated a MAC to its last address, so the gateway's
  answers looked like a single device and "absent" looked provable. It now keeps
  every address.
- An all-zero MAC normalizes to a value while being unassignable; the live-identity
  marker now requires `usable`, not merely `normalized`.
- The bundled menu entry hard-coded the author's home directory. It is now a
  template (`assets/lan-identity.desktop.in`) rendered by
  `scripts/install-desktop-entry.sh`, and `scripts/privacy-check.sh` fails on an
  absolute home path so it cannot come back.
- The privacy check hard-coded the local network's hostnames, so the file that
  guards the repository was itself the leak. The deny list now comes from the
  uncommitted `config.json` (`fleet_hostnames`); without it the hostname rule is
  skipped and every address rule still runs.

### Notes

- `mac-rotator.sh`, `lan-mac-inventory.sh` and `mac-rotate-unused.sh` are
  untouched.
- `mac-list.txt` and `hostnamelist.txt` change role: they are now generated
  exports of the catalog.
- The GUI has been driven offscreen and the Konsole handoff dry-run was observed
  in a real terminal; the visual layout and the privileged apply path still need a
  human.
