"""Rotation control: start/stop, countdown, per-step status, connectivity proof.

The loop lives in this process (a QTimer), which is exactly what
``mac-rotator.sh`` does in a terminal: close the window and rotation stops.
For a rotation that outlives the window there is a Konsole handoff button that
re-runs the original script under sudo with the same settings.

Guard rails, all of them learned from the script's own comments:

* refuse to start while another rotator process is running
* refuse to rotate the interface carrying an SSH session
* a failed rotation logs and the loop continues, as the script does
* dry-run is on by default, so the first Start cannot change anything
"""

from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QListWidget, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

import applier
import config
from catalog import Catalog
from config import Settings
from rotator import Countdown, RotationEngine, RotationError

STEP_FAIL = "\u2717"
STEP_WARN = "!"
STEP_OK = "\u2713"
STEP_SKIP = "\u2013"


class RotationPanel(QWidget):
    apply_requested = Signal(str, str)      # mac, hostname (may be "")
    settings_changed = Signal()
    status_message = Signal(str)

    def __init__(self, catalog: Catalog, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.catalog = catalog
        self.settings = settings
        self.engine = RotationEngine([], [], settings.pairing, settings.rotate_hostname)
        self.countdown = Countdown(settings.interval)
        self.running = False
        self._busy = False
        self._steps_completed = 0
        #: Installed by the main window: judges a whole rotation pool before the
        #: Konsole handoff starts one that would outlive this window.
        self.pool_guard = None

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

        self._build()
        self.reload_engine()

    # ------------------------------------------------------------------ #

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)

        controls = QGroupBox("Rotation")
        controls_layout = QVBoxLayout(controls)

        top = QHBoxLayout()
        self.start_button = QPushButton("Start rotation")
        self.start_button.clicked.connect(self.start)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop)
        self.stop_button.setEnabled(False)
        self.once_button = QPushButton("Rotate once")
        self.once_button.clicked.connect(self.rotate_once)
        top.addWidget(self.start_button)
        top.addWidget(self.stop_button)
        top.addWidget(self.once_button)
        top.addStretch(1)
        controls_layout.addLayout(top)

        self.countdown_label = QLabel("idle")
        self.countdown_label.setTextInteractionFlags(
            self.countdown_label.textInteractionFlags()
        )
        controls_layout.addWidget(self.countdown_label)

        self.next_label = QLabel("")
        self.next_label.setWordWrap(True)
        controls_layout.addWidget(self.next_label)

        self.combination_label = QLabel("")
        self.combination_label.setWordWrap(True)
        controls_layout.addWidget(self.combination_label)

        form = QFormLayout()
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(10, 86400)
        self.interval_spin.setSuffix(" s")
        self.interval_spin.setValue(self.settings.interval)
        self.interval_spin.valueChanged.connect(self._interval_changed)
        form.addRow("Interval:", self.interval_spin)

        self.iface_combo = QComboBox()
        self.iface_combo.setEditable(True)
        self._fill_interfaces()
        self.iface_combo.setCurrentText(self.settings.iface)
        self.iface_combo.currentTextChanged.connect(self._iface_changed)
        form.addRow("Interface:", self.iface_combo)

        self.dry_run_check = QCheckBox("Dry run — print the steps, change nothing")
        self.dry_run_check.setChecked(self.settings.dry_run)
        self.dry_run_check.toggled.connect(self._dry_run_changed)
        form.addRow(self.dry_run_check)

        self.hostname_check = QCheckBox("Rotate the hostname too")
        self.hostname_check.setChecked(self.settings.rotate_hostname)
        self.hostname_check.toggled.connect(self._hostname_changed)
        form.addRow(self.hostname_check)

        self.internet_check = QCheckBox("Verify internet after each rotation")
        self.internet_check.setChecked(self.settings.internet_check)
        self.internet_check.toggled.connect(self._internet_changed)
        form.addRow(self.internet_check)

        self.down_spin = QSpinBox()
        self.down_spin.setRange(0, 120)
        self.down_spin.setSuffix(" s")
        self.down_spin.setValue(self.settings.down_wait)
        self.down_spin.valueChanged.connect(self._waits_changed)
        form.addRow("Link-down wait:", self.down_spin)

        self.dhcp_spin = QSpinBox()
        self.dhcp_spin.setRange(5, 600)
        self.dhcp_spin.setSuffix(" s")
        self.dhcp_spin.setValue(self.settings.dhcp_wait)
        self.dhcp_spin.valueChanged.connect(self._waits_changed)
        form.addRow("DHCP wait:", self.dhcp_spin)

        self.check_spin = QSpinBox()
        self.check_spin.setRange(5, 300)
        self.check_spin.setSuffix(" s")
        self.check_spin.setValue(self.settings.check_timeout)
        self.check_spin.valueChanged.connect(self._waits_changed)
        form.addRow("Probe timeout:", self.check_spin)

        controls_layout.addLayout(form)

        handoff = QHBoxLayout()
        self.handoff_button = QPushButton("Run in Konsole (sudo)…")
        self.handoff_button.clicked.connect(self._handoff)
        self.handoff_once = QPushButton("Konsole: one rotation")
        self.handoff_once.clicked.connect(lambda: self._handoff(once=True))
        handoff.addWidget(self.handoff_button)
        handoff.addWidget(self.handoff_once)
        handoff.addStretch(1)
        controls_layout.addLayout(handoff)

        outer.addWidget(controls)

        steps_box = QGroupBox("Last rotation")
        steps_layout = QVBoxLayout(steps_box)
        self.steps_list = QListWidget()
        steps_layout.addWidget(self.steps_list)
        self.result_label = QLabel("")
        self.result_label.setWordWrap(True)
        steps_layout.addWidget(self.result_label)
        self.verdict_label = QLabel("")
        self.verdict_label.setWordWrap(True)
        self.verdict_label.setVisible(False)
        steps_layout.addWidget(self.verdict_label)
        outer.addWidget(steps_box, 1)

    def _fill_interfaces(self) -> None:
        try:
            names = sorted(
                path.name for path in __import__("pathlib").Path("/sys/class/net").iterdir()
            )
        except OSError:
            names = []
        for name in names:
            if name == "lo":
                continue
            self.iface_combo.addItem(name)

    # ------------------------------------------------------------------ #
    # engine
    # ------------------------------------------------------------------ #

    def reload_engine(self) -> None:
        self.engine = RotationEngine(
            self.catalog.pool_macs(),
            self.catalog.pool_hostnames(),
            self.settings.pairing,
            self.settings.rotate_hostname,
        )
        self.combination_label.setText(self.engine.describe())
        self._update_next_label()
        if not self.engine.macs and self.running:
            self.stop()
        self.start_button.setEnabled(bool(self.engine.macs))
        self.once_button.setEnabled(bool(self.engine.macs))

    def _update_next_label(self) -> None:
        try:
            upcoming = self.engine.peek(1)
        except RotationError:
            self.next_label.setText("No MAC in the pool — add one in the Pool tab.")
            return
        if not upcoming:
            self.next_label.setText("")
            return
        pair = upcoming[0]
        text = f"Next: {pair.mac}"
        if pair.hostname:
            text += f"  +  {pair.hostname}"
        if self.settings.dry_run:
            text += "   [dry run]"
        self.next_label.setText(text)

    # ------------------------------------------------------------------ #
    # loop
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        if self.running:
            return
        try:
            self.engine.validate()
        except RotationError as exc:
            self.status_message.emit(str(exc))
            return

        running = applier.rotator_running()
        if running:
            self.status_message.emit(
                "another rotator is already running: " + "; ".join(running[:2])
            )
            return
        ssh = applier.ssh_on_interface(self.settings.iface)
        if ssh:
            self.status_message.emit(
                f"refusing to rotate {self.settings.iface}: this SSH session rides on it ({ssh})"
            )
            return

        if self.pool_guard is not None:
            names = list(self.engine.hostnames) if self.settings.rotate_hostname else []
            if not self.pool_guard(
                [(mac, "") for mac in self.engine.macs], names=names
            ):
                self.status_message.emit(
                    "rotation not started: the pool contains an address that is in use"
                )
                return

        if self.settings.dry_run:
            self.status_message.emit("starting in dry-run mode — nothing will change")

        self.reload_engine()
        self.running = True
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.countdown.arm()
        self._timer.start()
        self.status_message.emit("rotation started")

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        self._timer.stop()
        self.countdown.disarm()
        self.start_button.setEnabled(bool(self.engine.macs))
        self.stop_button.setEnabled(False)
        self.countdown_label.setText("idle")
        self.status_message.emit("rotation stopped")

    def rotate_once(self) -> None:
        if self._busy:
            return
        try:
            self.engine.validate()
        except RotationError as exc:
            self.status_message.emit(str(exc))
            return
        ssh = applier.ssh_on_interface(self.settings.iface)
        if ssh:
            self.status_message.emit(
                f"refusing to rotate {self.settings.iface}: this SSH session rides on it"
            )
            return
        self._fire()

    def _tick(self) -> None:
        left = self.countdown.seconds_left()
        self.countdown_label.setText(f"next rotation in {int(left)}s")
        if self.countdown.due() and not self._busy:
            self._fire()
            self.countdown.rearm()

    def _fire(self) -> None:
        pair = self.engine.current()
        self._busy = True
        self.steps_list.clear()
        self.result_label.setText("applying…")
        self.steps_list.addItem(f"→ {pair.mac}" + (f"  +  {pair.hostname}" if pair.hostname else ""))
        self.apply_requested.emit(pair.mac, pair.hostname or "")

    def on_apply_finished(self, result) -> None:
        """Called by the main window when an ApplyWorker completes."""
        self._busy = False
        self.steps_list.clear()
        for line in result.lines():
            self.steps_list.addItem(line)
        if result.revert_path:
            self.steps_list.addItem(f"revert script: {result.revert_path}")
        if result.ok:
            self.result_label.setText("applied" if not result.dry_run else "dry run complete")
        else:
            self.result_label.setText(f"failed: {result.error or 'see the steps above'}")
        # The original script keeps going after a failed rotation; so do we.
        self.engine.advance()
        self._update_next_label()
        if self.running:
            self.countdown.rearm()

    def show_verdict(self, verdict) -> None:
        """Show what verification read, without claiming more than it read.

        Deliberately no revert here: the button that exists in the window is a
        human click, because a reading that failed is not proof of a bad
        identity (see ADR-005).
        """
        self.verdict_label.setText(verdict.note or verdict.state)
        self.verdict_label.setStyleSheet(
            "color: #2e7d32;" if verdict.ok else "color: #b8860b;"
        )
        self.verdict_label.setVisible(True)

    def on_apply_failed(self, message: str) -> None:
        self._busy = False
        self.result_label.setText(message)
        self.steps_list.addItem(f"{STEP_FAIL} {message}")
        if self.running:
            self.countdown.rearm()

    # ------------------------------------------------------------------ #
    # settings
    # ------------------------------------------------------------------ #

    def _interval_changed(self, value: int) -> None:
        self.settings.interval = value
        self.countdown.interval = max(1, value)
        if self.running:
            self.countdown.rearm()
        self.settings_changed.emit()

    def _iface_changed(self, text: str) -> None:
        if text.strip():
            self.settings.iface = text.strip()
            self.settings_changed.emit()

    def _dry_run_changed(self, checked: bool) -> None:
        self.settings.dry_run = checked
        self._update_next_label()
        self.settings_changed.emit()

    def _hostname_changed(self, checked: bool) -> None:
        self.settings.rotate_hostname = checked
        self.reload_engine()
        self.settings_changed.emit()

    def _internet_changed(self, checked: bool) -> None:
        self.settings.internet_check = checked
        self.settings_changed.emit()

    def _waits_changed(self) -> None:
        self.settings.down_wait = self.down_spin.value()
        self.settings.dhcp_wait = self.dhcp_spin.value()
        self.settings.check_timeout = self.check_spin.value()
        self.settings_changed.emit()

    def _handoff(self, once: bool = False) -> None:
        if applier.rotator_running():
            self.status_message.emit("a rotator is already running; stop it first")
            return
        if self.pool_guard is not None:
            names = list(self.engine.hostnames) if self.settings.rotate_hostname else []
            if not self.pool_guard(
                [(mac, "") for mac in self.engine.macs], names=names
            ):
                self.status_message.emit(
                    "handoff refused: the pool contains an address that is in use"
                )
                return
        ok, message = applier.launch_konsole(self.settings, once=once)
        self.status_message.emit(message)
