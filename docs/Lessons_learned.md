# Lessons Learned

What this build cost, written down so the next pass does not pay again. Every
address and hostname below is documentation-reserved (RFC 7042 MACs, hostnames
from the bundled name bank); the real inventory never appears in this repository.

## Data

### A "saved list" can be two formats glued together
The first real corpus was a block of `mac,hostname,ip,seen_at` rows followed by a
long run of bare MAC lines — no delimiter change, no comment, no marker. Parse by
*shape per line* (does it contain a comma?) rather than by "the file is CSV".
Sniffing only the first line would have thrown away most of the list. The two
blocks also overlap: exact repeats, lowercase repeats of an uppercase row, and one
malformed spelling that resolves onto a row already imported all collapse into the
same device.

### "Repairable" and "usable" are different questions
A malformed value can strip to twelve hex digits and still be unusable: some
substitution repairs *self-invalidate* because the guessed address is itself
multicast (`Ot:6C:OC:…` → `07:6C:0C:…`, `ES:C3:86C8:…` → `E5:…`). A repair that
produces an unassignable address is not a repair — re-validate the output, never
just the shape of the input.

### Never substitute `B`
The obvious OCR table is `O→0 S→5 I→1 i→1 l→1 t→7 G→6 g→9 £→E`. `B` looks like `8`,
but it is also a legal hex digit: turning it into `8` corrupts valid addresses.
Substitute only characters that cannot appear in a MAC.

### An unconfirmed repair is not an identity
Proposed fixes live in `repair_to` until a human accepts one. `Device.effective_mac`
and `Device.ready` require `status == ok`, so a guess can never reach a pool or an
apply. A malformed spelling of a row that is already catalogued becomes a note on
that row, not a second device.

### The catalog also stores addresses that can never be assigned
`device_by_mac()` therefore keys on `macaddr.check().normalized`, not on
`normalize()` — otherwise multicast rows (which have a canonical form but no
usable one) would be invisible to lookup and re-imported forever.

### One MAC answering for sixty addresses
`arp-scan --localnet` reported 86 responders and only 34 distinct MACs; 60 of them
answered with the gateway's address. That is proxy ARP: the router answers for
hosts that are off or on another segment. Two consequences:

- the number of distinct MACs is **not** the number of devices;
- "this MAC did not answer" is **not** evidence that it is free.

Collapsing each MAC to one IP hid this completely (the gateway's 60 answers became
one row). Keep every address a MAC answered for, and flag the MACs that answer for
many. The app then blocks a rotation to a MAC that is provably in use or is that
proxy, warns when a target is merely silent, and never calls a silent address free.

## Qt / PySide6

### Every filter-invalidation slot is deprecated in 6.12
`invalidateFilter()`, `invalidateRowsFilter()` **and** `invalidateColumnsFilter()`
all raise `DeprecationWarning` under PySide6 6.12.0. Plain `invalidate()` does
not, and it re-runs both axes, which is a superset of what a row-only filter
needs. Do not "migrate" to `invalidateRowsFilter()` — that is the same warning
with a longer name.

### A widget that writes settings will write them in tests too
The smoke test constructs the real `MainWindow`, and the window saves settings on
every change. With no isolation, a test run replaced the user's real
`~/.config/lan-identity-gui/config.json` with `iface: "lo"` and `/tmp/tmpXXXX`
paths — and the app then reported an interface that does not exist. Fix: an
`LAN_IDENTITY_HOME` override for the app's own tree, `run-tests.sh` sets it to a
temp dir, and `test_isolation.py` fails if it is ever unset.

### Redirect the app's state, not everyone else's
First attempt redirected *everything* under `LAN_IDENTITY_HOME`, including the
packet-204 collector's `hosts.csv`. The first-run import then silently found no
inventory. Only this app's config/catalog/exports belong to this app; the
collector's file and `~/scripts/*.sh` follow the real home.

### An empty argument is not a shell string
`mac-rotator.sh` takes the hostname prefix as `-p ''`. Building the Konsole
command as `bash -c "… -p '' …"` produced `-p ''''` after two levels of quoting,
so the prefix became a literal `''`. Build an argument vector (`subprocess.Popen`
with a list) and let the OS do the quoting.

### Ctrl-C in a Qt app is not wired by default
Python's `SIGINT` handler never runs while Qt's event loop is blocked in C++, so
the terminal appears dead. Install `signal.signal(SIGINT, app.quit)` and a short
timer that touches Python regularly, or the only way out is a signal to the PID.

## Behaviour worth remembering

### `mac-rotator.sh` help and code disagree on the prefix
Help text says `-p` defaults to `pc-`; the code sets `HOST_PREFIX=""`. The
generated files already contain complete names, so run the rotator with `-p ''`.
The app passes `""` explicitly rather than relying on either default.

### Independent pairing is not pairing
In the script's model, the MAC list and the hostname list each wrap on their own
length — 2 MACs × 13 names is 26 combinations, and consecutive rotations do not
preserve a MAC↔name pairing. That surprised the expectation "these two belong
together". The app makes it a setting: *independent* (script behaviour, the
default) or *paired*.

### Vendor names are a data join, not a UI detail
Resolving the OUI at scan time or when a row is opened left the Vendor column
empty for every imported device — and the hostname suggester had nothing to work
with. Resolve at insert, and backfill on re-import for rows that predate the
change.

### Flag your own address, do not forbid it
The first design protected the address the machine carries. That is wrong for a
rotation app: returning to your own address is the *way back*, so protection would
block the one rotation you most need. The flag (`is_own`) is visibility only — the
table says `YOU`, the pool says `⚠ this is you`, deleting the row is refused — and
the address stays a normal, usable pool entry. What actually protects the session
is refusing to touch the interface that carries `SSH_CONNECTION`.

### A house database is not publishable
The catalog, the collector CSV and the generated list files all contain the LAN's
MACs, names and addresses. The repository ships none of them: a fixture set built
only from RFC 7042 documentation MACs, `192.0.2.0/24` addresses and names from the
bundled gist bank; `.gitignore` for the live files; and `scripts/privacy-check.sh`
to fail the build if a real address ever creeps back in. The fleet hostnames are
the same problem in text form: an empty default tuple in `config.py`, filled from
`config.json` on the machine that has one.

### A cache entry is not an answer
`ip neigh` prints a state per entry and the parser threw it away, so a `STALE`
leftover — a MAC recorded once and never refreshed — looked exactly like a device
that answered a second ago. Verification then called a duplicate on expired
evidence. Keep the state and every address the MAC answered for, and require a
*live* answer (`REACHABLE`, `DELAY`, `PROBE`, or an `arp-scan` reply) before calling
a collision. The converse matters as much: an entry with no state at all proves
nothing, and treating it as stale would trade a visible false positive for an
invisible false negative.

### A reload is not a restart
`avahi-daemon --reload` re-reads `/etc/resolv.conf` and the files in
`/etc/avahi/services/`, and the manual says in as many words that it will **not**
reload `avahi-daemon.conf`. The shipped config leaves `host-name` commented out, so
the published name comes from the system hostname at startup. A refresh built on
`--reload` would have run, printed `ok`, and kept announcing the old name for as
long as the daemon lived. Read what the reload path actually re-reads before
trusting the verb.

### An exit status is part of the artifact
The generated revert script restored the MAC and swallowed a failed hostname leg:
`|| echo "… failed"` printed the error and the script still reached
`revert complete` with status 0. In review that reads exactly like error handling.
It is not — a log line is not a status, and nobody reads the log of a run that
says it worked. Accumulate a flag, attempt the remaining legs, and exit non-zero
with a line that says what the exit code means.

### Two fields describing one observation move together
Re-import kept `ip` and `seen_at` apart, each refreshed when it alone looked newer,
which can state an address with a timestamp from a different observation. They are
one fact: refresh them together, ordered by the instant the timestamp names — and
let a value that names no instant decide no order rather than guessing. Comparing
the strings instead produced three separate bugs (a lower address losing to an
older observation, an offset change reversing the order, `"yesterday"` outranking
a timestamp).

## Process

- **Offscreen Qt proves structure, not looks.** `QT_QPA_PLATFORM=offscreen`
  catches constructors, signal wiring and model/proxy crashes. Only a human can
  say the layout is usable.
- **A privileged path you cannot run is unverified.** `pkexec` and `sudo` both
  prompt for a password here. The argv was tested, the helper was syntax-checked,
  and the run remains an open item — recorded as such, not as "works".
- **Count assertions are cheap and catch real drift** — but they must not encode a
  private corpus. Tests over a real file are opt-in (`LAN_IDENTITY_SAMPLE`) and
  assert invariants, not totals.
- **A control test earns its place by failing on your first draft.** "No hostname
  means no name-service step" passed on the old code and looked redundant next to
  the seven tests that failed. Its first run against the new code failed: the call
  site was unconditional and emitted a `skipped` step. Keep the case that describes
  the absence — it is the one that catches the overreach.
- **Measure the premise of a fix before writing it.** The name-service phase was
  planned as "restart or reload" and the manual decided between them, which changed
  the code before a line of it existed. A fix written from the plan would have been
  a step that reported success and did nothing.
