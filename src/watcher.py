"""A debounced file watcher that survives atomic replacement.

``QFileSystemWatcher`` is the right transport for "this file changed" and the
wrong transport for "this file changed *again*": it stops watching a path when
the file is replaced, and every write in this app replaces the file (write a
``.tmp``, then ``os.replace``).  A watcher that is not re-armed after the app's
own generate goes silent on the first one, and the failure looks exactly like
"nothing changed".

Three deliberate properties:

* **It reports, it does not decide.**  The signal names the path; the window
  decides whether that means import, drift, or nothing.
* **It watches the parent directory too**, because a missing file cannot be
  watched and the collector CSV may not exist yet.
* **It debounces.**  A save, a rename and a truncation can arrive as several
  events; one import per burst is the intent.
"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

#: How long to wait for a burst of events to settle before reporting.
DEFAULT_DEBOUNCE_MS = 2000


def _fingerprint(path: str) -> tuple[int, int] | None:
    """What we can observe about a path without reading it."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


class PathWatcher(QObject):
    """Watch a fixed set of paths and emit once per settled burst of changes.

    ``changed`` carries the list of paths that differ from the snapshot taken at
    the previous emit.  A path that is deleted is reported as changed; a path
    that is created where there was none is reported as changed.
    """

    changed = Signal(list)

    def __init__(
        self,
        paths: list[str] | tuple[str, ...] = (),
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._debounce_ms = max(0, int(debounce_ms))
        self._paths: list[str] = []
        self._snapshot: dict[str, tuple[int, int] | None] = {}
        self._pending: set[str] = set()

        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_file_changed)
        self._watcher.directoryChanged.connect(self._on_directory_changed)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self._debounce_ms)
        self._timer.timeout.connect(self._flush)

        for path in paths:
            self.add(path)

    # ------------------------------------------------------------------ #
    # public surface
    # ------------------------------------------------------------------ #

    @property
    def paths(self) -> list[str]:
        return list(self._paths)

    def add(self, path: str | Path) -> None:
        resolved = str(Path(path))
        if resolved in self._paths:
            return
        self._paths.append(resolved)
        self._snapshot[resolved] = _fingerprint(resolved)
        self._arm(resolved)

    def stop(self) -> None:
        self._timer.stop()
        self._pending.clear()
        files = self._watcher.files()
        if files:
            self._watcher.removePaths(files)
        directories = self._watcher.directories()
        if directories:
            self._watcher.removePaths(directories)

    # ------------------------------------------------------------------ #
    # arming
    # ------------------------------------------------------------------ #

    def _arm(self, path: str) -> None:
        """(Re-)register a path and its parent directory.

        The file is re-added only if it currently exists; a missing file is
        detected through the directory watch instead.  Calling this on every
        event is what makes ``os.replace`` work: the replacement drops the file
        path from the watcher, and the next event would never arrive without
        this call.
        """
        parent = str(Path(path).parent)
        if parent and parent not in self._watcher.directories():
            self._watcher.addPath(parent)
        if Path(path).exists() and path not in self._watcher.files():
            self._watcher.addPath(path)

    # ------------------------------------------------------------------ #
    # events
    # ------------------------------------------------------------------ #

    def _on_file_changed(self, path: str) -> None:
        path = str(Path(path))
        if path not in self._paths:
            return
        self._pending.add(path)
        self._arm(path)
        self._schedule()

    def _on_directory_changed(self, directory: str) -> None:
        directory = str(Path(directory))
        for path in self._paths:
            if str(Path(path).parent) != directory:
                continue
            if _fingerprint(path) != self._snapshot.get(path):
                self._pending.add(path)
                self._arm(path)
        self._schedule()

    def _schedule(self) -> None:
        if not self._pending:
            return
        # Restarting the timer on every event is the debounce: only a quiet
        # period of ``debounce_ms`` ends the burst.
        self._timer.start()

    def _flush(self) -> None:
        if not self._pending:
            return
        changed = sorted(self._pending)
        self._pending.clear()
        for path in changed:
            self._snapshot[path] = _fingerprint(path)
        self.changed.emit(changed)
