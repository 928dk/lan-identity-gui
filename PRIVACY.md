# Privacy and what is deliberately not in this repository

This repository is **public**. It is a fresh single-commit extraction from
a private source repository. The source repository's history is not reachable from here,
so no prior commit can leak through a clone.

## Redacted before the first push

| Category | Replacement | Notes |
| --- | --- | --- |
| Absolute home paths | `$HOME` | Every path under the author's account |
| Local account name | `$USER`, or removed from paths | Code still runs |
| Machine and host names | `FLEET_HOST`, `localhost` | Helper scripts targeted one specific box |
| Private and LAN addresses | RFC 5737 documentation ranges | `198.51.100.x`, `203.0.113.x` — reserved, not routable |
| Remote login accounts | `user@` | |
| Stale absolute roots | `/opt/shared` | The old shared-resource roots no longer exist |
| Runtime state | excluded | `data/`, `archives/`, `logs/` are not tracked |
| Build output | excluded | `venv/`, `node_modules/`, `target/`, binaries |

Files rewritten during redaction: none.
Paths renamed during redaction: none. A filename identifies as plainly as
the text inside it, so machine and host names are substituted in paths too.
Machine-specific wording made generic: none.
Host names deliberately **not** substituted: none — a name that is also the
project's subject, where substituting it would corrupt upstream identifiers rather
than remove an identity.

## What is not here, and why

Three kinds of content were held back rather than merely anonymised:

- **Operations tooling.** Helper scripts that reach another machine over the
  network. Anonymising the address still discloses that a second machine exists,
  what it runs and how it is reached. Scripts like that are part of a private
  infrastructure repository, not of this project.
- **Machine and network documentation.** Write-ups of one particular computer's
  hardware, drivers and configuration. Useful to whoever maintains it; not part
  of the project, and not anybody else's business.
- **Internal registers and asset lists.** Device registers and other inventories
  exported from an organisation's own systems, together with the raw exports they
  were parsed from and the runtime state they generate. A row per device, with a
  hostname and a last-seen time, describes an organisation's network whatever its
  identifiers are called — an account number, an asset tag or a room name. Only
  the parser and the schema belong in the project; the register itself does not.

Where a document was worth keeping and only *mentioned* the machine in passing,
the sentence was made generic instead of the document being dropped.

Paths deliberately excluded from this repository:

none

The *values* of anything redacted are deliberately omitted from this document.
Writing a real secret or host name into a public file would defeat the redaction.

## Third-party code

Upstream clones and vendored reference trees are **not** published here. Where the
project depends on one, the dependency is named in the README with its upstream
source and licence.

## Verifying this yourself

```bash
# Substitute the values you want to test for; do not commit them.
git grep -nI -E 'PATTERN' -- '*.md' '*.py' '*.sh' '*.json'
git ls-files | grep -E '\.env$|\.pem$|id_[rd]sa'
git log --format='%an <%ae>' | sort -u
```

## Tokens

No API keys, access tokens or passwords are committed to this repository. A
reusable gate scans the working tree, the git index and every reachable commit
for credential and personal-identifier patterns before any publish, and refuses
to push if it finds any.
