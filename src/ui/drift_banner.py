"""A non-modal banner for "a list file changed on disk".

The Generate-time dialog asks before overwriting a hand edit, which is right for
a deliberate action.  It is the wrong shape for an edit made while the window is
open: a modal pops up for something the user may not have finished doing.  This
banner states the fact, shows the diff on request, and leaves three buttons.

It never acts.  Adopt, overwrite and ignore are signals the window handles; the
banner has no reference to the catalog or to the files.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class DriftBanner(QFrame):
    adopt_requested = Signal()
    overwrite_requested = Signal()
    ignore_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("driftBanner")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            "#driftBanner { background: #4a3a10; border: 1px solid #b8860b; "
            "border-radius: 4px; }"
        )

        self.message = QLabel("")
        self.message.setWordWrap(True)

        self.diff_button = QPushButton("Show diff")
        self.diff_button.setCheckable(True)
        self.adopt_button = QPushButton("Import the edit")
        self.overwrite_button = QPushButton("Overwrite")
        self.ignore_button = QPushButton("Ignore")

        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setVisible(False)
        self.detail.setMaximumHeight(150)

        buttons = QHBoxLayout()
        buttons.addWidget(self.diff_button)
        buttons.addWidget(self.adopt_button)
        buttons.addWidget(self.overwrite_button)
        buttons.addWidget(self.ignore_button)
        buttons.addStretch(1)

        top = QHBoxLayout()
        top.addWidget(self.message, 1)
        top.addLayout(buttons)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.addLayout(top)
        layout.addWidget(self.detail)

        self.diff_button.toggled.connect(self.detail.setVisible)
        self.adopt_button.clicked.connect(self.adopt_requested)
        self.overwrite_button.clicked.connect(self.overwrite_requested)
        self.ignore_button.clicked.connect(self.ignore_requested)
        self.setVisible(False)

    # ------------------------------------------------------------------ #

    def show_drift(self, name: str, diff: str) -> None:
        self.message.setText(
            f"{name} changed on disk since the app last wrote it. "
            "Nothing has been overwritten."
        )
        self.detail.setPlainText(diff)
        self.diff_button.setChecked(False)
        self.detail.setVisible(False)
        self.setVisible(True)

    def clear(self) -> None:
        self.detail.clear()
        self.diff_button.setChecked(False)
        self.setVisible(False)

    def is_reporting(self) -> bool:
        return not self.isHidden()
