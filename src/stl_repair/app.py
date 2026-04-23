"""Print Doctor — drag-and-drop mesh repair for 3D printing."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Optional

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedLayout,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .repair import SUPPORTED_EXTENSIONS, discover_mesh_files, fast_face_count
from .worker import RepairWorker

APP_NAME = "Print Doctor"
APP_VERSION = f"v{__version__}"
OUTPUT_SUBDIR = "Repaired"
LARGE_MESH_THRESHOLD = 1_000_000


def _setup_logging() -> None:
    """Attach a FileHandler to root logger at ~/Library/Logs/Print Doctor/repair.log.
    Idempotent — safe to call multiple times."""
    log_dir = os.path.expanduser("~/Library/Logs/Print Doctor")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        return
    log_path = os.path.join(log_dir, "repair.log")
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None) == log_path:
            return
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)

# (combo_label, display_label, scale_to_meters)
UNIT_CHOICES = [
    ("Keep original units", "Original Units", 1.0),
    ("Millimeters",         "Millimeters",    0.001),
    ("Centimeters",         "Centimeters",    0.01),
    ("Inches",              "Inches",         0.0254),
]
DEFAULT_UNIT_INDEX = 0

PHASE_LABELS = {
    "clean":          "basic cleanup",
    "meshfix":        "meshfix",
    "fine_remesh":    "fine voxel",
    "coarse_remesh":  "coarse voxel",
    "failed":         "failed",
}


# --------------------------------------------------------------------------- #
# Theme
# --------------------------------------------------------------------------- #
def palette(dark: bool) -> dict:
    """Clinic-sans palette. Alpha values are 0–255 for Qt stylesheet rgba()."""
    if dark:
        return dict(
            bg="#0f1115", panel="#151821", panel2="#1b1f2a",
            text="#e8ebf0", textDim="#b0b6c1", dim="#7c8494",
            line="#262b36", lineSoft="#1e222c",
            accent="#7cc4ff",
            accentSoft="rgba(124,196,255,36)",
            accentBorder="rgba(124,196,255,72)",
            ok="#7ee0a8", warn="#ffc36a", err="#ff8b8b",
            gradStart="#7cc4ff", gradEnd="#5aa3ef", gradText="#0f1115",
            dangerStart="#e06b6b", dangerEnd="#c44a4a",
        )
    return dict(
        bg="#f6f4ef", panel="#ffffff", panel2="#faf8f3",
        text="#1a1d22", textDim="#4b5161", dim="#6b7280",
        line="#e7e3d8", lineSoft="#efebdf",
        accent="#2d5dd7",
        accentSoft="rgba(45,93,215,20)",
        accentBorder="rgba(45,93,215,56)",
        ok="#2e8b57", warn="#b37800", err="#b23c3c",
        gradStart="#3b6fe8", gradEnd="#2d5dd7", gradText="#ffffff",
        dangerStart="#e06b6b", dangerEnd="#c44a4a",
    )


def _detect_dark() -> bool:
    hints = QApplication.styleHints()
    try:
        return hints.colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:
        # Qt < 6.5 fallback — inspect the application palette.
        return QApplication.palette().color(QPalette.Window).lightness() < 128


MONO_STACK = '"IBM Plex Mono","SF Mono","Menlo","Consolas",monospace'
DISPLAY_STACK = '"Inter","-apple-system","SF Pro Text","Segoe UI",sans-serif'


# --------------------------------------------------------------------------- #
# Utility widgets
# --------------------------------------------------------------------------- #
class Dot(QWidget):
    """Small filled circle. Used for status pips and file-row status dots."""

    def __init__(self, size: int = 6, color: str = "#888", pulse: bool = False,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._size = size
        self._color = QColor(color)
        self._alpha = 255
        self._pulse = pulse
        self.setFixedSize(size + 2, size + 2)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._t = 0.0
        if pulse:
            self.start_pulse()

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def start_pulse(self) -> None:
        self._pulse = True
        self._t = 0.0
        self._timer.start(40)

    def stop_pulse(self) -> None:
        self._pulse = False
        self._alpha = 255
        self._timer.stop()
        self.update()

    def _tick(self) -> None:
        self._t += 0.04
        # Ease in/out between 0.4 and 1.0 over 1.4s period.
        import math
        phase = (self._t % 1.4) / 1.4
        amp = 0.5 + 0.5 * math.cos(2 * math.pi * phase)  # 0..1..0
        self._alpha = int(255 * (0.4 + 0.6 * amp))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        c = QColor(self._color)
        c.setAlpha(self._alpha)
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        cx = (self.width() - self._size) / 2
        cy = (self.height() - self._size) / 2
        p.drawEllipse(QRect(int(cx), int(cy), self._size, self._size))


class Spinner(QWidget):
    """Ring spinner — rotating 270° arc.

    Only ticks while visible: `FileList` creates one spinner per row, and
    with hundreds of rows a constantly-running 30ms timer on each one
    pegs the main thread and freezes the UI mid-batch.
    """

    def __init__(self, size: int = 10, color: str = "#888", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._size = size
        self._color = QColor(color)
        self._angle = 0
        self.setFixedSize(size + 4, size + 4)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def showEvent(self, event):
        self._timer.start(30)
        super().showEvent(event)

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def _tick(self):
        self._angle = (self._angle + 18) % 360
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(self._color, 1.6)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        margin = 2
        rect = QRect(margin, margin, self._size, self._size)
        # drawArc expects 1/16° units; sweep 270°.
        p.drawArc(rect, -self._angle * 16, 270 * 16)


# --------------------------------------------------------------------------- #
# Masthead
# --------------------------------------------------------------------------- #
class Masthead(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        self.title = QLabel("Print Doctor")
        self.title.setObjectName("mastheadTitle")
        layout.addWidget(self.title, 0, Qt.AlignVCenter)

        layout.addStretch(1)

        right = QHBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(8)
        self.dot = Dot(6, "#888")
        self.status_text = QLabel("idle")
        self.status_text.setObjectName("mastheadStatus")
        right.addWidget(self.dot, 0, Qt.AlignVCenter)
        right.addWidget(self.status_text, 0, Qt.AlignVCenter)
        layout.addLayout(right)

    def set_status(self, state: str, colors: dict) -> None:
        """state: idle | ready | running | done | error"""
        self.status_text.setText(state)
        if state == "running":
            self.dot.set_color(colors["warn"])
            self.dot.start_pulse()
        else:
            self.dot.stop_pulse()
            tone = {"idle": colors["dim"], "ready": colors["accent"],
                    "done": colors["ok"], "error": colors["err"]}.get(state, colors["dim"])
            self.dot.set_color(tone)

    def apply_theme(self, c: dict) -> None:
        self.title.setStyleSheet(
            f"font-family:{DISPLAY_STACK}; font-size:22px; font-weight:700; "
            f"letter-spacing:-0.8px; color:{c['text']};"
        )
        self.status_text.setStyleSheet(
            f"font-family:{MONO_STACK}; font-size:11px; color:{c['dim']};"
        )


# --------------------------------------------------------------------------- #
# Source card
# --------------------------------------------------------------------------- #
class SourceCard(QFrame):
    clicked = Signal()
    paths_dropped = Signal(list)
    clear_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sourceCard")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFrameShape(QFrame.NoFrame)
        self.setAcceptDrops(True)
        self._loaded = False
        self._hover = False
        self._running = False
        self._c: dict = palette(False)

        self._stack = QStackedLayout(self)
        self._stack.setContentsMargins(0, 0, 0, 0)

        # Empty view — drop target.
        self._empty = QWidget()
        ev = QVBoxLayout(self._empty)
        ev.setContentsMargins(24, 24, 24, 24)
        ev.setSpacing(10)
        ev.setAlignment(Qt.AlignCenter)

        self._empty_icon = QLabel()
        self._empty_icon.setFixedSize(48, 48)
        self._empty_icon.setAlignment(Qt.AlignCenter)
        ev.addWidget(self._empty_icon, 0, Qt.AlignHCenter)

        self._empty_title = QLabel("Drop a folder or mesh file")
        self._empty_title.setAlignment(Qt.AlignCenter)
        ev.addWidget(self._empty_title)

        self._empty_sub = QLabel("or click to browse")
        self._empty_sub.setAlignment(Qt.AlignCenter)
        ev.addWidget(self._empty_sub)

        # Loaded view — compact row.
        self._loaded_w = QWidget()
        lv = QHBoxLayout(self._loaded_w)
        lv.setContentsMargins(16, 16, 16, 16)
        lv.setSpacing(14)

        self._folder_icon = QLabel()
        self._folder_icon.setFixedSize(40, 40)
        self._folder_icon.setAlignment(Qt.AlignCenter)
        lv.addWidget(self._folder_icon)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)
        self._title_lbl = QLabel("—")
        self._title_lbl.setObjectName("sourceTitle")
        self._sub_lbl = QLabel("")
        self._sub_lbl.setObjectName("sourceSub")
        self._title_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._sub_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        text_col.addWidget(self._title_lbl)
        text_col.addWidget(self._sub_lbl)
        lv.addLayout(text_col, 1)

        self._clear_btn = QPushButton("✕ clear")
        self._clear_btn.setObjectName("clearBtn")
        self._clear_btn.setCursor(Qt.PointingHandCursor)
        self._clear_btn.clicked.connect(self.clear_clicked.emit)
        lv.addWidget(self._clear_btn)

        self._stack.addWidget(self._empty)
        self._stack.addWidget(self._loaded_w)
        self._stack.setCurrentIndex(0)

    # ------------------------------------------------------------------ #
    def apply_theme(self, c: dict) -> None:
        self._c = c
        self._refresh_style()
        self._repaint_icons()

    def _refresh_style(self) -> None:
        c = self._c
        hover = self._hover and not self._loaded
        border_color = c["accent"] if hover else c["line"]
        bg = c["panel"]
        if hover:
            # accentSoft — substitute panel with a faintly tinted background.
            bg = c["accentSoft"].replace("rgba", "rgba")  # placeholder; QSS accepts rgba directly.
            bg = c["accentSoft"]
        if self._loaded:
            self.setStyleSheet(f"""
                #sourceCard {{
                    background:{c['panel']};
                    border:1px solid {c['line']};
                    border-radius:10px;
                }}
                QLabel#sourceTitle {{
                    font-family:{DISPLAY_STACK};
                    font-size:15px; font-weight:600; color:{c['text']};
                }}
                QLabel#sourceSub {{
                    font-family:{MONO_STACK};
                    font-size:11px; color:{c['dim']};
                }}
                QPushButton#clearBtn {{
                    background:transparent;
                    color:{c['dim']};
                    border:1px solid {c['line']};
                    border-radius:6px;
                    padding:6px 10px;
                    font-family:{MONO_STACK};
                    font-size:11px;
                }}
                QPushButton#clearBtn:hover {{
                    color:{c['text']};
                    border-color:{c['accentBorder']};
                }}
                QPushButton#clearBtn:disabled {{
                    color:{c['dim']}; border-color:{c['lineSoft']};
                }}
            """)
        else:
            # Empty (drop target)
            self.setStyleSheet(f"""
                #sourceCard {{
                    background:{bg};
                    border:2px dashed {border_color};
                    border-radius:12px;
                }}
                QLabel {{
                    color:{c['text']};
                }}
                QLabel#emptyTitle {{
                    font-family:{DISPLAY_STACK};
                    font-size:15px; font-weight:600; color:{c['text']};
                }}
                QLabel#emptySub {{
                    font-family:{MONO_STACK};
                    font-size:11px; color:{c['dim']};
                }}
            """)
            self._empty_title.setObjectName("emptyTitle")
            self._empty_sub.setObjectName("emptySub")

    def _repaint_icons(self) -> None:
        self._empty_icon.setPixmap(self._make_empty_icon())
        self._folder_icon.setPixmap(self._make_folder_icon())

    def _make_empty_icon(self):
        from PySide6.QtGui import QPixmap
        size = 48
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        c = self._c
        # Disc background + border.
        p.setPen(QPen(QColor(*_rgba_tuple(c["accentBorder"])), 1))
        p.setBrush(QColor(*_rgba_tuple(c["accentSoft"])))
        p.drawEllipse(0, 0, size, size)
        # Arrow up-into-tray glyph.
        pen = QPen(QColor(c["accent"]), 1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        # Vertical line + downward arrow head + baseline tray.
        # Translate to center 22x22 icon inside 48x48.
        p.translate(13, 13)
        # v = from (11, 3) to (11, 14), arrow at 14 going to 7,10 and 15,10
        path = QPainterPath()
        path.moveTo(11, 3)
        path.lineTo(11, 14)
        path.moveTo(7, 10)
        path.lineTo(11, 14)
        path.lineTo(15, 10)
        path.moveTo(4, 18)
        path.lineTo(18, 18)
        p.drawPath(path)
        p.end()
        return pm

    def _make_folder_icon(self):
        from PySide6.QtGui import QPixmap
        size = 40
        pm = QPixmap(size, size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        c = self._c
        # Rounded-rect background (8 radius) with accent border + soft fill.
        p.setPen(QPen(QColor(*_rgba_tuple(c["accentBorder"])), 1))
        p.setBrush(QColor(*_rgba_tuple(c["accentSoft"])))
        p.drawRoundedRect(0.5, 0.5, size - 1, size - 1, 8, 8)
        # Folder glyph.
        pen = QPen(QColor(c["accent"]), 1.6)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.translate(10, 10)
        path = QPainterPath()
        # Folder: tab on top-left + body.
        path.moveTo(3, 6)
        path.quadTo(3, 4, 5, 4)
        path.lineTo(8, 4)
        path.lineTo(10, 6)
        path.lineTo(15, 6)
        path.quadTo(17, 6, 17, 8)
        path.lineTo(17, 13)
        path.quadTo(17, 15, 15, 15)
        path.lineTo(5, 15)
        path.quadTo(3, 15, 3, 13)
        path.closeSubpath()
        p.drawPath(path)
        p.end()
        return pm

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #
    def show_empty(self) -> None:
        self._loaded = False
        self._stack.setCurrentIndex(0)
        self.setMaximumHeight(16777215)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._refresh_style()

    def show_loaded(self, title: str, sub: str) -> None:
        self._loaded = True
        self._title_lbl.setText(title)
        self._sub_lbl.setText(sub)
        self._stack.setCurrentIndex(1)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMaximumHeight(72)
        self._refresh_style()

    def set_running(self, running: bool) -> None:
        self._running = running
        self._clear_btn.setDisabled(running)

    # ------------------------------------------------------------------ #
    # Drag & drop
    # ------------------------------------------------------------------ #
    def mousePressEvent(self, event) -> None:
        if self._loaded:
            return
        self.clicked.emit()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._accepts(event) and not self._loaded:
            event.acceptProposedAction()
            self._hover = True
            self._refresh_style()
        else:
            event.ignore()

    def dragLeaveEvent(self, _):
        self._hover = False
        self._refresh_style()

    def dropEvent(self, event: QDropEvent) -> None:
        self._hover = False
        self._refresh_style()
        if not self._accepts(event):
            return
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.paths_dropped.emit(paths)

    @staticmethod
    def _accepts(event) -> bool:
        md = event.mimeData()
        if not md.hasUrls():
            return False
        urls = [u for u in md.urls() if u.isLocalFile()]
        if not urls:
            return False
        if len(urls) == 1 and os.path.isdir(urls[0].toLocalFile()):
            return True
        return all(os.path.isfile(u.toLocalFile())
                   and u.toLocalFile().lower().endswith(SUPPORTED_EXTENSIONS)
                   for u in urls)


# --------------------------------------------------------------------------- #
# File list
# --------------------------------------------------------------------------- #
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE    = "done"
STATUS_WARN    = "warn"
STATUS_FAIL    = "fail"

VERDICT = {
    STATUS_PENDING: "QUEUED",
    STATUS_RUNNING: "WORKING",
    STATUS_DONE:    "FIXED",
    STATUS_WARN:    "PARTIAL",
    STATUS_FAIL:    "FAILED",
}


class FileRow(QWidget):
    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.name = name
        self.status = STATUS_PENDING
        self.phase_label = ""

        self._c: dict = palette(False)

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 9, 14, 9)
        row.setSpacing(0)

        # Left cluster: indicator + filename (spinner + dot are both created up
        # front and swapped via show/hide — avoids timer lifetime pitfalls).
        self._indicator_holder = QWidget()
        self._indicator_holder.setFixedSize(14, 14)
        ind_layout = QHBoxLayout(self._indicator_holder)
        ind_layout.setContentsMargins(0, 0, 0, 0)
        self._dot = Dot(6, "#888")
        self._spinner = Spinner(10, "#888")
        self._spinner.hide()
        ind_layout.addWidget(self._dot, 0, Qt.AlignCenter)
        ind_layout.addWidget(self._spinner, 0, Qt.AlignCenter)

        self._name_lbl = QLabel(name)
        self._name_lbl.setObjectName("rowName")
        self._name_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        left = QHBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(8)
        left.addWidget(self._indicator_holder, 0, Qt.AlignVCenter)
        left.addWidget(self._name_lbl, 1, Qt.AlignVCenter)
        row.addLayout(left, 1)

        self._phase_lbl = QLabel("—")
        self._phase_lbl.setObjectName("rowPhase")
        self._phase_lbl.setFixedWidth(120)
        self._phase_lbl.setAlignment(Qt.AlignCenter)
        row.addWidget(self._phase_lbl, 0, Qt.AlignVCenter)

        self._verdict_lbl = QLabel(VERDICT[STATUS_PENDING])
        self._verdict_lbl.setObjectName("rowVerdict")
        self._verdict_lbl.setFixedWidth(64)
        self._verdict_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row.addWidget(self._verdict_lbl, 0, Qt.AlignVCenter)

    def apply_theme(self, c: dict, is_last: bool) -> None:
        self._c = c
        self._restyle(is_last)
        self._sync_status_colors()

    def _restyle(self, is_last: bool) -> None:
        c = self._c
        border = "none" if is_last else f"1px solid {c['lineSoft']}"
        self.setStyleSheet(f"""
            FileRow {{
                border-bottom:{border};
                background:transparent;
            }}
            QLabel#rowName {{
                font-family:{MONO_STACK};
                font-size:12px;
                color:{c['text']};
            }}
            QLabel#rowPhase {{
                font-family:{DISPLAY_STACK};
                font-size:11px;
                color:{c['dim']};
            }}
            QLabel#rowVerdict {{
                font-family:{MONO_STACK};
                font-size:10px;
                letter-spacing:1px;
                color:{c['dim']};
            }}
        """)

    def set_status(self, status: str, phase_label: str = "") -> None:
        self.status = status
        self.phase_label = phase_label
        self._verdict_lbl.setText(VERDICT.get(status, ""))
        self._phase_lbl.setText(phase_label or ("queued" if status == STATUS_PENDING else "—"))

        # Swap indicator: running → spinner, else dot.
        running = status == STATUS_RUNNING
        self._dot.setVisible(not running)
        self._spinner.setVisible(running)

        self._sync_status_colors()

    def _sync_status_colors(self) -> None:
        c = self._c
        tone = {
            STATUS_PENDING: c["dim"],
            STATUS_RUNNING: c["accent"],
            STATUS_DONE:    c["ok"],
            STATUS_WARN:    c["warn"],
            STATUS_FAIL:    c["err"],
        }[self.status]
        self._dot.set_color(tone)
        if self._spinner is not None:
            self._spinner.set_color(tone)
        name_color = c["dim"] if self.status == STATUS_PENDING else c["text"]
        self._name_lbl.setStyleSheet(
            f"font-family:{MONO_STACK}; font-size:12px; color:{name_color};"
        )
        self._verdict_lbl.setStyleSheet(
            f"font-family:{MONO_STACK}; font-size:10px; letter-spacing:1px; color:{tone};"
        )


class FileList(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("fileList")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFrameShape(QFrame.NoFrame)
        self._c: dict = palette(False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._header = QWidget()
        self._header.setObjectName("fileListHeader")
        self._header.setAttribute(Qt.WA_StyledBackground, True)
        h = QHBoxLayout(self._header)
        h.setContentsMargins(14, 8, 14, 8)
        h.setSpacing(0)

        file_lbl = QLabel("FILE")
        file_lbl.setObjectName("headerCell")
        phase_lbl = QLabel("PHASE")
        phase_lbl.setObjectName("headerCell")
        phase_lbl.setFixedWidth(120)
        phase_lbl.setAlignment(Qt.AlignCenter)
        verdict_lbl = QLabel("VERDICT")
        verdict_lbl.setObjectName("headerCell")
        verdict_lbl.setFixedWidth(64)
        verdict_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        h.addWidget(file_lbl, 1)
        h.addWidget(phase_lbl, 0)
        h.addWidget(verdict_lbl, 0)
        outer.addWidget(self._header)

        self._scroll = QScrollArea()
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Let the FileList's panel2 fill show through — the viewport and the
        # inner container both paint the palette Base (white) by default.
        self._scroll.setAttribute(Qt.WA_TranslucentBackground, True)
        self._scroll.viewport().setAutoFillBackground(False)
        self._list_container = QWidget()
        self._list_container.setAttribute(Qt.WA_TranslucentBackground, True)
        self._list_container.setAutoFillBackground(False)
        self._list_layout = QVBoxLayout(self._list_container)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(0)
        self._list_layout.addStretch(1)
        self._scroll.setWidget(self._list_container)
        outer.addWidget(self._scroll, 1)

        self._rows: dict[str, FileRow] = {}

    def apply_theme(self, c: dict) -> None:
        self._c = c
        self.setStyleSheet(f"""
            #fileList {{
                background:{c['panel2']};
                border:1px solid {c['line']};
                border-radius:10px;
            }}
            #fileListHeader {{
                background:{c['panel2']};
                border-bottom:1px solid {c['line']};
                border-top-left-radius:10px;
                border-top-right-radius:10px;
            }}
            #fileList QScrollArea,
            #fileList QScrollArea > QWidget,
            #fileList QScrollArea > QWidget > QWidget {{
                background:transparent;
                border:none;
            }}
            QLabel#headerCell {{
                font-family:{MONO_STACK};
                font-size:9px;
                letter-spacing:1.3px;
                color:{c['dim']};
            }}
            QScrollBar:vertical {{
                background:transparent; width:8px; margin:4px 2px 4px 0;
            }}
            QScrollBar::handle:vertical {{
                background:{c['line']}; border-radius:3px; min-height:24px;
            }}
            QScrollBar::handle:vertical:hover {{ background:{c['dim']}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background:transparent; }}
        """)
        # Also restyle any existing rows.
        rows = list(self._rows.values())
        for i, row in enumerate(rows):
            row.apply_theme(c, is_last=(i == len(rows) - 1))

    def clear(self) -> None:
        for row in list(self._rows.values()):
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()

    def set_paths(self, paths: list[str]) -> None:
        self.clear()
        # Insert before the trailing stretch.
        stretch_index = self._list_layout.count() - 1
        for path in paths:
            row = FileRow(os.path.basename(path))
            self._rows[path] = row
            self._list_layout.insertWidget(stretch_index, row)
            stretch_index += 1
        rows = list(self._rows.values())
        for i, row in enumerate(rows):
            row.apply_theme(self._c, is_last=(i == len(rows) - 1))

    def set_status(self, path: str, status: str, phase_label: str = "") -> None:
        row = self._rows.get(path)
        if row is not None:
            row.set_status(status, phase_label)
            self._scroll.ensureWidgetVisible(row)

    def scroll_to(self, path: str) -> None:
        row = self._rows.get(path)
        if row is not None:
            self._scroll.ensureWidgetVisible(row)


# --------------------------------------------------------------------------- #
# Destination chip
# --------------------------------------------------------------------------- #
class DestChip(QFrame):
    change_clicked = Signal()
    show_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("destChip")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFrameShape(QFrame.NoFrame)
        self._c = palette(False)
        self._path = ""

        h = QHBoxLayout(self)
        h.setContentsMargins(14, 10, 14, 10)
        h.setSpacing(10)

        self._label = QLabel("OUTPUT →")
        self._label.setObjectName("destLabel")
        h.addWidget(self._label)

        self._path_lbl = QLabel("")
        self._path_lbl.setObjectName("destPath")
        self._path_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._path_lbl.setMinimumWidth(80)
        h.addWidget(self._path_lbl, 1)

        self._change_btn = QPushButton("change")
        self._change_btn.setObjectName("destChange")
        self._change_btn.setCursor(Qt.PointingHandCursor)
        self._change_btn.clicked.connect(self.change_clicked.emit)
        h.addWidget(self._change_btn)

        self._show_btn = QPushButton("show")
        self._show_btn.setObjectName("destShow")
        self._show_btn.setCursor(Qt.PointingHandCursor)
        self._show_btn.clicked.connect(self.show_clicked.emit)
        h.addWidget(self._show_btn)

    def apply_theme(self, c: dict) -> None:
        self._c = c
        self.setStyleSheet(f"""
            #destChip {{
                background:{c['panel2']};
                border:1px solid {c['line']};
                border-radius:10px;
            }}
            QLabel#destLabel {{
                font-family:{MONO_STACK};
                font-size:9px;
                letter-spacing:1.3px;
                color:{c['dim']};
            }}
            QLabel#destPath {{
                font-family:{MONO_STACK};
                font-size:11px;
                color:{c['text']};
            }}
            QPushButton#destChange {{
                background:transparent;
                color:{c['text']};
                border:1px solid {c['line']};
                border-radius:5px;
                padding:4px 10px;
                font-family:{MONO_STACK};
                font-size:11px;
            }}
            QPushButton#destChange:hover {{
                border-color:{c['accentBorder']};
            }}
            QPushButton#destChange:disabled {{
                color:{c['dim']}; border-color:{c['lineSoft']};
            }}
            QPushButton#destShow {{
                background:transparent;
                color:{c['dim']};
                border:none;
                padding:4px 2px;
                font-family:{MONO_STACK};
                font-size:11px;
                text-decoration: underline;
            }}
            QPushButton#destShow:hover {{
                color:{c['text']};
            }}
            QPushButton#destShow:disabled {{
                color:{c['dim']};
            }}
        """)
        self._render_path()

    def set_path(self, path: str) -> None:
        self._path = path or ""
        self._render_path()

    def set_enabled_state(self, enabled: bool) -> None:
        self._change_btn.setEnabled(enabled)
        self._show_btn.setEnabled(enabled)
        self.setProperty("disabled", not enabled)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render_path()

    def _render_path(self) -> None:
        if not self._path:
            self._path_lbl.setText("")
            return
        display = self._path.replace(os.path.expanduser("~"), "~")
        fm = QFontMetrics(self._path_lbl.font())
        available = max(40, self._path_lbl.width() - 4)
        elided = fm.elidedText(display, Qt.ElideLeft, available)
        self._path_lbl.setText(elided)


# --------------------------------------------------------------------------- #
# UnitSelect (segmented)
# --------------------------------------------------------------------------- #
class UnitSelect(QFrame):
    changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("unitSelect")
        self._c = palette(False)
        self._index = DEFAULT_UNIT_INDEX

        row = QHBoxLayout(self)
        row.setContentsMargins(3, 3, 3, 3)
        row.setSpacing(4)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: list[QPushButton] = []
        for i, (_, display, _scale) in enumerate(UNIT_CHOICES):
            b = QPushButton(display)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setObjectName("unitPill")
            if i == DEFAULT_UNIT_INDEX:
                b.setChecked(True)
            self._buttons.append(b)
            self._group.addButton(b, i)
            row.addWidget(b, 1)
        self._group.idClicked.connect(self._on_clicked)

    def apply_theme(self, c: dict) -> None:
        self._c = c
        self.setStyleSheet(f"""
            #unitSelect {{
                background:{c['panel']};
                border:1px solid {c['line']};
                border-radius:6px;
            }}
            QPushButton#unitPill {{
                background:transparent;
                color:{c['textDim']};
                border:none;
                border-radius:4px;
                padding:5px 8px;
                font-family:{DISPLAY_STACK};
                font-size:11px;
                font-weight:500;
            }}
            QPushButton#unitPill:checked {{
                background:{c['accentSoft']};
                color:{c['accent']};
                font-weight:600;
            }}
            QPushButton#unitPill:disabled {{
                color:{c['dim']};
            }}
        """)

    def index(self) -> int:
        return self._index

    def set_index(self, i: int) -> None:
        self._index = i
        self._buttons[i].setChecked(True)

    def _on_clicked(self, i: int) -> None:
        self._index = i
        self.changed.emit(i)

    def setDisabled(self, flag: bool) -> None:
        for b in self._buttons:
            b.setDisabled(flag)


# --------------------------------------------------------------------------- #
# QCheck — themed checkbox with sublabel
# --------------------------------------------------------------------------- #
class _CheckIndicator(QWidget):
    """16×16 custom-painted checkbox matching the design's SVG checkmark.

    QCheckBox doesn't let us control the check glyph via QSS (image-based only),
    so we paint the box + tick directly.
    """
    toggled = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self.setCursor(Qt.PointingHandCursor)
        self._checked = False
        self._enabled = True
        self._hover = False
        self._c = palette(False)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, v: bool) -> None:
        v = bool(v)
        if v != self._checked:
            self._checked = v
            self.update()
            self.toggled.emit(v)

    def setDisabled(self, flag: bool) -> None:
        self._enabled = not flag
        self.setCursor(Qt.ArrowCursor if flag else Qt.PointingHandCursor)
        self.update()

    def apply_theme(self, c: dict) -> None:
        self._c = c
        self.update()

    def enterEvent(self, e):
        self._hover = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if self._enabled and e.button() == Qt.LeftButton:
            self.setChecked(not self._checked)
        super().mousePressEvent(e)

    def paintEvent(self, _e):
        c = self._c
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(0.5, 0.5, 15.0, 15.0)

        if self._checked:
            border = QColor(c["accent"])
            fill = QColor(c["accent"])
        else:
            border = QColor(c["line"])
            if self._hover and self._enabled:
                border = QColor(c["accent"])
            fill = QColor(0, 0, 0, 0)

        p.setPen(QPen(border, 1))
        p.setBrush(fill)
        p.drawRoundedRect(rect, 4, 4)

        if self._checked:
            # SVG "M2 5 l2 2 4-4.5" scaled from the design's 10×10 viewBox → 16.
            check = QColor(c["gradText"])  # #fff on light, #0f1115 on dark
            pen = QPen(check, 1.8)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            path = QPainterPath()
            path.moveTo(3.2, 8.0)
            path.lineTo(5.6, 10.4)
            path.lineTo(12.0, 3.2)
            p.drawPath(path)


class QCheck(QWidget):
    toggled = Signal(bool)

    def __init__(self, label: str, sub: str = "", parent=None):
        super().__init__(parent)
        self._c = palette(False)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(10)
        row.setAlignment(Qt.AlignTop)

        self._box = _CheckIndicator()
        self._box.toggled.connect(self.toggled.emit)
        # Nudge down 1px so it aligns with the cap-height of the label, matching
        # the design's `marginTop: 1` on the indicator span.
        row.addSpacing(0)
        box_wrap = QWidget()
        bw = QVBoxLayout(box_wrap)
        bw.setContentsMargins(0, 1, 0, 0)
        bw.setSpacing(0)
        bw.addWidget(self._box)
        row.addWidget(box_wrap, 0, Qt.AlignTop)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        self._label = QLabel(label)
        self._label.setObjectName("qcheckLabel")
        self._label.setWordWrap(True)
        col.addWidget(self._label)
        if sub:
            self._sub = QLabel(sub)
            self._sub.setObjectName("qcheckSub")
            self._sub.setWordWrap(True)
            col.addWidget(self._sub)
        else:
            self._sub = None
        row.addLayout(col, 1)

    def isChecked(self) -> bool:
        return self._box.isChecked()

    def setChecked(self, v: bool) -> None:
        self._box.setChecked(v)

    def setDisabled(self, flag: bool) -> None:
        self._box.setDisabled(flag)
        self.setProperty("disabled", flag)
        self._label.setDisabled(flag)
        if self._sub is not None:
            self._sub.setDisabled(flag)

    def apply_theme(self, c: dict) -> None:
        self._c = c
        self._box.apply_theme(c)
        self._label.setStyleSheet(
            f"font-family:{DISPLAY_STACK}; font-size:12px; color:{c['text']};"
        )
        if self._sub is not None:
            self._sub.setStyleSheet(
                f"font-family:{DISPLAY_STACK}; font-size:11px; color:{c['dim']};"
            )


# --------------------------------------------------------------------------- #
# Advanced panel
# --------------------------------------------------------------------------- #
class _AdvHeader(QFrame):
    clicked = Signal()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class Advanced(QFrame):
    unit_changed = Signal(int)
    include_unmodified_changed = Signal(bool)
    overwrite_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("advanced")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFrameShape(QFrame.NoFrame)
        self._c = palette(False)
        self._open = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._header = _AdvHeader()
        self._header.setObjectName("advHeader")
        self._header.setAttribute(Qt.WA_StyledBackground, True)
        self._header.setCursor(Qt.PointingHandCursor)
        self._header.clicked.connect(self.toggle)

        hh = QHBoxLayout(self._header)
        hh.setContentsMargins(14, 10, 14, 10)
        hh.setSpacing(8)

        self._chevron = QLabel("▶")
        self._chevron.setObjectName("advChevron")
        self._chevron.setFixedWidth(12)
        hh.addWidget(self._chevron)

        self._title = QLabel("ADVANCED SETTINGS")
        self._title.setObjectName("advTitle")
        hh.addWidget(self._title)

        self._summary = QLabel("")
        self._summary.setObjectName("advSummary")
        hh.addWidget(self._summary)
        hh.addStretch(1)

        root.addWidget(self._header)

        # Body (animated collapse via maximumHeight).
        self._body = QFrame()
        self._body.setObjectName("advBody")
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(14, 4, 14, 14)
        body_layout.setSpacing(12)

        unit_col = QVBoxLayout()
        unit_col.setContentsMargins(0, 8, 0, 0)
        unit_col.setSpacing(6)
        self._unit_hdr = QLabel("SOURCE UNITS")
        self._unit_hdr.setObjectName("advSubheader")
        unit_col.addWidget(self._unit_hdr)
        self._unit_hint = QLabel("Try this if your repaired files are the wrong size in your slicer.")
        self._unit_hint.setObjectName("advHint")
        self._unit_hint.setWordWrap(True)
        unit_col.addWidget(self._unit_hint)
        self._unit_select = UnitSelect()
        self._unit_select.changed.connect(self._on_unit_changed)
        unit_col.addWidget(self._unit_select)
        body_layout.addLayout(unit_col)

        divider = QFrame()
        divider.setObjectName("advDivider")
        divider.setFixedHeight(1)
        body_layout.addWidget(divider)
        self._divider = divider

        self._include = QCheck(
            "Include unmodified files in output",
            "Copy files that were already manifold into the destination folder alongside repaired ones.",
        )
        self._include.toggled.connect(self._on_include_changed)
        body_layout.addWidget(self._include)

        self._overwrite = QCheck(
            "Overwrite files in destination",
            "Replace any existing mesh with the same name. When off, duplicates get a numeric suffix.",
        )
        self._overwrite.toggled.connect(self._on_overwrite_changed)
        body_layout.addWidget(self._overwrite)

        self._body.setMaximumHeight(0)
        root.addWidget(self._body)

        self._anim = QPropertyAnimation(self._body, b"maximumHeight")
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    # ------------------------------------------------------------------ #
    def apply_theme(self, c: dict) -> None:
        self._c = c
        self.setStyleSheet(f"""
            #advanced {{
                background:{c['panel']};
                border:1px solid {c['line']};
                border-radius:10px;
            }}
            #advHeader {{
                background:transparent;
                border:none;
            }}
            QLabel#advChevron {{
                color:{c['dim']};
                font-size:9px;
                font-family:{MONO_STACK};
            }}
            QLabel#advTitle {{
                font-family:{MONO_STACK};
                font-size:10px;
                font-weight:600;
                letter-spacing:1.3px;
                color:{c['textDim']};
            }}
            QLabel#advSummary {{
                font-family:{MONO_STACK};
                font-size:11px;
                color:{c['dim']};
            }}
            #advBody {{
                background:transparent;
                border:none;
                border-top:1px solid {c['lineSoft']};
            }}
            QLabel#advSubheader {{
                font-family:{MONO_STACK};
                font-size:9px;
                letter-spacing:1.3px;
                color:{c['dim']};
            }}
            QLabel#advHint {{
                font-family:{DISPLAY_STACK};
                font-size:11px;
                color:{c['dim']};
            }}
            #advDivider {{
                background:{c['lineSoft']};
                border:none;
            }}
        """)
        self._unit_select.apply_theme(c)
        self._include.apply_theme(c)
        self._overwrite.apply_theme(c)
        self._refresh_summary()

    # ------------------------------------------------------------------ #
    def toggle(self) -> None:
        self.set_open(not self._open)

    def set_open(self, open_: bool) -> None:
        self._open = open_
        self._chevron.setText("▼" if open_ else "▶")
        self._anim.stop()
        start = self._body.maximumHeight()
        end = self._body.sizeHint().height() if open_ else 0
        self._anim.setStartValue(start)
        self._anim.setEndValue(end)
        self._anim.start()
        self._refresh_summary()

    def set_running(self, running: bool) -> None:
        self._unit_select.setDisabled(running)
        self._include.setDisabled(running)
        self._overwrite.setDisabled(running)

    # ------------------------------------------------------------------ #
    def unit_index(self) -> int:
        return self._unit_select.index()

    def include_unmodified(self) -> bool:
        return self._include.isChecked()

    def overwrite(self) -> bool:
        return self._overwrite.isChecked()

    def _on_unit_changed(self, i: int) -> None:
        self.unit_changed.emit(i)
        self._refresh_summary()

    def _on_include_changed(self, v: bool) -> None:
        self.include_unmodified_changed.emit(v)
        self._refresh_summary()

    def _on_overwrite_changed(self, v: bool) -> None:
        self.overwrite_changed.emit(v)
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        if self._open:
            self._summary.setText("")
            return
        parts = []
        idx = self._unit_select.index()
        if idx != DEFAULT_UNIT_INDEX:
            parts.append(f"Units: {UNIT_CHOICES[idx][1]}")
        if self._include.isChecked():
            parts.append("Includes unmodified")
        if self._overwrite.isChecked():
            parts.append("Overwrites existing")
        self._summary.setText(("· " + " · ".join(parts)) if parts else "")


# --------------------------------------------------------------------------- #
# Progress rail
# --------------------------------------------------------------------------- #
class ProgressRail(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 0
        self._max = 1
        self._c = palette(False)
        self.setFixedHeight(4)

    def apply_theme(self, c: dict) -> None:
        self._c = c
        self.update()

    def set_progress(self, value: int, maximum: int) -> None:
        self._value = value
        self._max = max(1, maximum)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w = self.width()
        h = self.height()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self._c["lineSoft"]))
        p.drawRoundedRect(0, 0, w, h, 2, 2)
        pct = min(1.0, max(0.0, self._value / self._max))
        if pct > 0:
            p.setBrush(QColor(self._c["accent"]))
            p.drawRoundedRect(0, 0, int(w * pct), h, 2, 2)


# --------------------------------------------------------------------------- #
# Summary bar (done state)
# --------------------------------------------------------------------------- #
class SummaryBar(QFrame):
    reveal_clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("summaryBar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFrameShape(QFrame.NoFrame)
        self._c = palette(False)

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 10, 14, 10)
        row.setSpacing(20)

        self._label = QLabel("SUMMARY")
        self._label.setObjectName("summaryLabel")
        row.addWidget(self._label)

        self._items: list[tuple[str, Dot, QLabel, QLabel]] = []
        for key, label in (("done", "fixed"), ("warn", "partial"), ("fail", "failed")):
            cluster = QHBoxLayout()
            cluster.setSpacing(6)
            dot = Dot(6, "#888")
            value = QLabel("0")
            value.setObjectName("summaryValue")
            lbl = QLabel(label.upper())
            lbl.setObjectName("summaryItemLabel")
            cluster.addWidget(dot, 0, Qt.AlignVCenter)
            cluster.addWidget(value, 0, Qt.AlignVCenter)
            cluster.addWidget(lbl, 0, Qt.AlignVCenter)
            row.addLayout(cluster)
            self._items.append((key, dot, value, lbl))

        row.addStretch(1)

        self._reveal = QPushButton("Reveal output")
        self._reveal.setObjectName("summaryReveal")
        self._reveal.setCursor(Qt.PointingHandCursor)
        self._reveal.clicked.connect(self.reveal_clicked.emit)
        row.addWidget(self._reveal)

    def apply_theme(self, c: dict) -> None:
        self._c = c
        self.setStyleSheet(f"""
            #summaryBar {{
                background:{c['panel2']};
                border:1px solid {c['line']};
                border-radius:10px;
            }}
            QLabel#summaryLabel {{
                font-family:{MONO_STACK};
                font-size:9px;
                letter-spacing:1.3px;
                color:{c['dim']};
            }}
            QLabel#summaryValue {{
                font-family:{MONO_STACK};
                font-size:13px;
                font-weight:600;
                color:{c['text']};
            }}
            QLabel#summaryItemLabel {{
                font-family:{MONO_STACK};
                font-size:10px;
                letter-spacing:1px;
                color:{c['dim']};
            }}
            QPushButton#summaryReveal {{
                background:transparent;
                color:{c['text']};
                border:1px solid {c['line']};
                border-radius:5px;
                padding:5px 12px;
                font-family:{MONO_STACK};
                font-size:11px;
            }}
            QPushButton#summaryReveal:hover {{
                border-color:{c['accentBorder']};
            }}
        """)
        colors = {"done": c["ok"], "warn": c["warn"], "fail": c["err"]}
        for key, dot, _, _ in self._items:
            dot.set_color(colors[key])

    def set_counts(self, done: int, warn: int, fail: int) -> None:
        counts = {"done": done, "warn": warn, "fail": fail}
        for key, _, value_lbl, _ in self._items:
            value_lbl.setText(str(counts[key]))


# --------------------------------------------------------------------------- #
# Primary button (gradient)
# --------------------------------------------------------------------------- #
class PrimaryBtn(QPushButton):
    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setObjectName("primaryBtn")
        self.setMinimumWidth(140)
        self.setFixedHeight(44)
        self.setCursor(Qt.PointingHandCursor)
        self._c = palette(False)
        self._danger = False

    def set_danger(self, danger: bool) -> None:
        self._danger = danger
        self.apply_theme(self._c)

    def apply_theme(self, c: dict) -> None:
        self._c = c
        if self._danger:
            grad = f"qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {c['dangerStart']}, stop:1 {c['dangerEnd']})"
            fg = "#ffffff"
        else:
            grad = f"qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {c['gradStart']}, stop:1 {c['gradEnd']})"
            fg = c["gradText"]
        self.setStyleSheet(f"""
            QPushButton#primaryBtn {{
                background:{grad};
                color:{fg};
                border:none;
                border-radius:10px;
                padding:0 28px;
                font-family:{DISPLAY_STACK};
                font-size:14px;
                font-weight:600;
            }}
            QPushButton#primaryBtn:hover {{
                /* subtle lift */
            }}
            QPushButton#primaryBtn:disabled {{
                background:{c['panel2']};
                color:{c['dim']};
                border:1px solid {c['line']};
            }}
        """)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _rgba_tuple(s: str) -> tuple:
    """Parse a #hex or rgba(...) string into an (r,g,b,a) tuple (0-255)."""
    s = s.strip()
    if s.startswith("#"):
        s = s.lstrip("#")
        if len(s) == 3:
            r, g, b = (int(ch * 2, 16) for ch in s)
            return (r, g, b, 255)
        r = int(s[0:2], 16); g = int(s[2:4], 16); b = int(s[4:6], 16)
        a = int(s[6:8], 16) if len(s) >= 8 else 255
        return (r, g, b, a)
    if s.startswith("rgba"):
        inner = s[s.index("(") + 1 : s.rindex(")")]
        parts = [p.strip() for p in inner.split(",")]
        r, g, b = int(parts[0]), int(parts[1]), int(parts[2])
        a = int(float(parts[3]))
        return (r, g, b, a)
    return (0, 0, 0, 255)


def _show_in_finder(path: str) -> None:
    if not path:
        return
    try:
        if os.path.isdir(path):
            subprocess.run(["open", path], check=False)
        elif os.path.exists(path):
            subprocess.run(["open", "-R", path], check=False)
        else:
            # Parent may exist even if subdir not yet created.
            parent = os.path.dirname(path)
            if parent and os.path.isdir(parent):
                subprocess.run(["open", parent], check=False)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(720, 720)
        self.setMinimumSize(640, 620)

        self._dark = _detect_dark()
        self._c = palette(self._dark)

        self.files: list[str] = []
        self.output_dir: Optional[str] = None
        self.worker: Optional[RepairWorker] = None
        self._done = False

        central = QWidget()
        central.setObjectName("rootBg")
        central.setAttribute(Qt.WA_StyledBackground, True)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(28, 28, 28, 28)
        root.setSpacing(16)

        self.masthead = Masthead()
        root.addWidget(self.masthead, 0)

        self.source = SourceCard()
        self.source.clicked.connect(self._browse_folder)
        self.source.paths_dropped.connect(self._on_paths_dropped)
        self.source.clear_clicked.connect(self._clear)
        root.addWidget(self.source, 1)

        self.summary = SummaryBar()
        self.summary.setVisible(False)
        self.summary.reveal_clicked.connect(self._reveal_output)
        root.addWidget(self.summary, 0)

        self.file_list = FileList()
        self.file_list.setVisible(False)
        root.addWidget(self.file_list, 3)

        self.dest = DestChip()
        self.dest.setVisible(False)
        self.dest.change_clicked.connect(self._change_output)
        self.dest.show_clicked.connect(self._reveal_output)
        dest_row = QHBoxLayout()
        dest_row.setContentsMargins(0, 0, 0, 0)
        dest_row.setSpacing(10)
        dest_row.addWidget(self.dest, 1)
        root.addLayout(dest_row)

        self.advanced = Advanced()
        self.advanced.setVisible(False)
        root.addWidget(self.advanced, 0)

        # Progress row.
        self._progress_row = QWidget()
        prow = QVBoxLayout(self._progress_row)
        prow.setContentsMargins(0, 0, 0, 0)
        prow.setSpacing(6)
        pinfo = QHBoxLayout()
        pinfo.setContentsMargins(0, 0, 0, 0)
        self._progress_label = QLabel("")
        self._progress_label.setObjectName("progressLabel")
        self._progress_pct = QLabel("0%")
        self._progress_pct.setObjectName("progressPct")
        pinfo.addWidget(self._progress_label, 1)
        pinfo.addWidget(self._progress_pct, 0)
        prow.addLayout(pinfo)
        self._rail = ProgressRail()
        prow.addWidget(self._rail)
        self._progress_row.setVisible(False)
        root.addWidget(self._progress_row, 0)

        # Button row (right-aligned).
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.addStretch(1)
        self.primary = PrimaryBtn("Repair")
        self.primary.setEnabled(False)
        self.primary.clicked.connect(self._start_or_cancel)
        btn_row.addWidget(self.primary)
        root.addLayout(btn_row)

        self._apply_theme()

        # React to system color scheme changes when Qt 6.5+.
        hints = QApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(self._on_scheme_changed)

        self._update_primary_button()
        self.masthead.set_status("idle", self._c)

    # ------------------------------------------------------------------ #
    # Theme
    # ------------------------------------------------------------------ #
    def _on_scheme_changed(self, *_):
        self._dark = _detect_dark()
        self._c = palette(self._dark)
        self._apply_theme()

    def _apply_theme(self) -> None:
        c = self._c
        # Scope to #rootBg so QSS doesn't cascade into every child QWidget.
        self.centralWidget().setStyleSheet(f"#rootBg {{ background:{c['bg']}; }}")
        self.masthead.apply_theme(c)
        self.source.apply_theme(c)
        self.summary.apply_theme(c)
        self.file_list.apply_theme(c)
        self.dest.apply_theme(c)
        self.advanced.apply_theme(c)
        self._rail.apply_theme(c)
        self.primary.apply_theme(c)

        self._progress_label.setStyleSheet(
            f"font-family:{MONO_STACK}; font-size:11px; color:{c['dim']};"
        )
        self._progress_pct.setStyleSheet(
            f"font-family:{MONO_STACK}; font-size:11px; color:{c['accent']};"
        )

    # ------------------------------------------------------------------ #
    # State transitions
    # ------------------------------------------------------------------ #
    def _browse_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select folder of mesh files")
        if folder:
            self._on_paths_dropped([folder])

    def _on_paths_dropped(self, paths: list[str]) -> None:
        if not paths:
            return
        files: list[str] = []
        title: str
        sub: str

        if len(paths) == 1 and os.path.isdir(paths[0]):
            folder = paths[0]
            names = discover_mesh_files(folder)
            files = [os.path.join(folder, n) for n in names]
            title = os.path.basename(folder.rstrip("/")) or folder
            sub = f"{_tildify(folder)} · {len(files)} file{'s' if len(files) != 1 else ''}"
            self.output_dir = os.path.join(folder, OUTPUT_SUBDIR)
        else:
            files = [p for p in paths if os.path.isfile(p) and p.lower().endswith(SUPPORTED_EXTENSIONS)]
            if not files:
                return
            parent = os.path.dirname(files[0])
            if len(files) == 1:
                title = os.path.basename(files[0])
                sub = _tildify(parent)
            else:
                title = f"{len(files)} mesh files"
                sub = _tildify(parent)
            self.output_dir = os.path.join(parent, OUTPUT_SUBDIR)

        self.files = files
        self._done = False
        self.source.show_loaded(title, sub)
        self.file_list.setVisible(True)
        self.file_list.set_paths(files)
        self.dest.setVisible(True)
        self.dest.set_path(self.output_dir or "")
        self.advanced.setVisible(True)
        self.summary.setVisible(False)
        self._progress_row.setVisible(False)
        self.masthead.set_status("ready", self._c)
        self._update_primary_button()

    def _clear(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        self.files = []
        self.output_dir = None
        self._done = False
        self.source.show_empty()
        self.file_list.clear()
        self.file_list.setVisible(False)
        self.dest.setVisible(False)
        self.advanced.setVisible(False)
        self.summary.setVisible(False)
        self._progress_row.setVisible(False)
        self.masthead.set_status("idle", self._c)
        self._update_primary_button()

    def _change_output(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        start = self.output_dir or (os.path.dirname(self.files[0]) if self.files else "")
        chosen = QFileDialog.getExistingDirectory(self, "Choose output folder", start)
        if chosen:
            self.output_dir = chosen
            self.dest.set_path(chosen)

    def _reveal_output(self) -> None:
        if self.output_dir:
            _show_in_finder(self.output_dir)

    # ------------------------------------------------------------------ #
    # Primary button state
    # ------------------------------------------------------------------ #
    def _update_primary_button(self) -> None:
        running = bool(self.worker and self.worker.isRunning())
        if running:
            self.primary.setText("Cancel")
            self.primary.set_danger(True)
            self.primary.setEnabled(True)
            return
        self.primary.set_danger(False)
        if self._done:
            self.primary.setText("Open folder")
            self.primary.setEnabled(True)
        elif self.files:
            n = len(self.files)
            self.primary.setText(f"Repair {n} file{'s' if n != 1 else ''} →")
            self.primary.setEnabled(True)
        else:
            self.primary.setText("Repair")
            self.primary.setEnabled(False)

    # ------------------------------------------------------------------ #
    # Run / cancel
    # ------------------------------------------------------------------ #
    def _prompt_large_mesh_policy(self) -> Optional[bool]:
        """If any input STL has >LARGE_MESH_THRESHOLD faces, ask the user whether
        to decimate those meshes before repair. Returns:
            True  — simplify large meshes
            False — keep full detail
            None  — user cancelled; abort the batch
        Non-STL files are skipped (header probe only supports binary STL)."""
        oversized = []
        for p in self.files:
            if not p.lower().endswith(".stl"):
                continue
            n = fast_face_count(p)
            if n is not None and n > LARGE_MESH_THRESHOLD:
                oversized.append((os.path.basename(p), n))
        if not oversized:
            return False

        preview = "\n".join(f"  • {n}  ({count:,} faces)" for n, count in oversized[:5])
        more = f"\n  … and {len(oversized) - 5} more" if len(oversized) > 5 else ""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle(APP_NAME)
        box.setText(f"{len(oversized)} mesh(es) exceed {LARGE_MESH_THRESHOLD:,} faces.")
        box.setInformativeText(
            f"{preview}{more}\n\n"
            "Repairing meshes this dense can be very slow.\n"
            "Simplify them first (quadric decimation to ~500k faces)?\n\n"
            "• Simplify — much faster; loses some surface detail.\n"
            "• Keep full detail — slow but preserves geometry.\n"
            "• Cancel — abort the batch."
        )
        simplify_btn = box.addButton("Simplify", QMessageBox.AcceptRole)
        keep_btn = box.addButton("Keep full detail", QMessageBox.RejectRole)
        cancel_btn = box.addButton("Cancel", QMessageBox.DestructiveRole)
        box.setDefaultButton(simplify_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is simplify_btn:
            return True
        if clicked is keep_btn:
            return False
        return None

    def _start_or_cancel(self) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.primary.setEnabled(False)
            self._progress_label.setText("Cancelling…")
            return
        if self._done:
            self._reveal_output()
            return
        if not self.files or not self.output_dir:
            return

        idx = self.advanced.unit_index()
        scale = UNIT_CHOICES[idx][2]
        include_unmodified = self.advanced.include_unmodified()
        overwrite = self.advanced.overwrite()

        simplify_large = self._prompt_large_mesh_policy()
        if simplify_large is None:
            return

        for path in self.files:
            self.file_list.set_status(path, STATUS_PENDING, "queued")

        self._progress_row.setVisible(True)
        self._rail.set_progress(0, len(self.files))
        self._progress_pct.setText("0%")
        self._progress_label.setText(f"[0/{len(self.files)}] starting…")
        self.source.set_running(True)
        self.advanced.set_running(True)
        self.dest.set_enabled_state(False)
        self.summary.setVisible(False)
        self._done = False

        self.worker = RepairWorker(
            self.files, self.output_dir, scale,
            include_unmodified=include_unmodified, overwrite=overwrite,
            simplify_large=simplify_large,
        )
        self.worker.file_started.connect(self._on_file_started)
        self.worker.file_phase.connect(self._on_file_phase)
        self.worker.file_finished.connect(self._on_file_finished)
        self.worker.batch_finished.connect(self._on_batch_finished)
        self.worker.batch_error.connect(self._on_batch_error)
        self.worker.start()

        self.masthead.set_status("running", self._c)
        self._update_primary_button()

    # ------------------------------------------------------------------ #
    # Worker signals
    # ------------------------------------------------------------------ #
    def _path_for_name(self, name: str) -> Optional[str]:
        for p in self.files:
            if os.path.basename(p) == name:
                return p
        return None

    def _on_file_started(self, i: int, total: int, name: str) -> None:
        path = self._path_for_name(name)
        if path:
            self.file_list.set_status(path, STATUS_RUNNING, "working…")
        self._progress_label.setText(f"[{i}/{total}] {name}")
        pct = int(100 * (i - 1) / max(1, total))
        self._progress_pct.setText(f"{pct}%")
        self._rail.set_progress(i - 1, total)

    def _on_file_phase(self, i: int, total: int, name: str, phase: str) -> None:
        path = self._path_for_name(name)
        if path:
            label = PHASE_LABELS.get(phase, phase)
            self.file_list.set_status(path, STATUS_RUNNING, label)

    def _on_file_finished(self, result) -> None:
        name = os.path.basename(result.input_path)
        path = result.input_path
        if result.success:
            phase_label = PHASE_LABELS.get(result.phase, result.phase)
            if result.phase == "coarse_remesh" and result.errors_remaining > 0:
                status = STATUS_WARN
            else:
                status = STATUS_DONE
        else:
            status = STATUS_FAIL
            phase_label = "failed"
        self.file_list.set_status(path, status, phase_label)

        # Bump progress rail.
        done = self._rail._value + 1
        self._rail.set_progress(done, len(self.files))
        pct = int(100 * done / max(1, len(self.files)))
        self._progress_pct.setText(f"{pct}%")

    def _on_batch_finished(self, success: int, total: int, failed: list) -> None:
        # Count partials (coarse-remesh with residual errors).
        done_ct = 0
        warn_ct = 0
        fail_ct = 0
        for path, row in self.file_list._rows.items():
            if row.status == STATUS_DONE:
                done_ct += 1
            elif row.status == STATUS_WARN:
                warn_ct += 1
            elif row.status == STATUS_FAIL:
                fail_ct += 1

        self._progress_row.setVisible(False)
        self.source.set_running(False)
        self.advanced.set_running(False)
        self.dest.set_enabled_state(True)
        self.worker = None

        self._done = True
        self.summary.set_counts(done_ct, warn_ct, fail_ct)
        self.summary.setVisible(True)
        self.masthead.set_status("done", self._c)
        self._update_primary_button()

    def _on_batch_error(self, msg: str) -> None:
        self._progress_row.setVisible(False)
        self.source.set_running(False)
        self.advanced.set_running(False)
        self.dest.set_enabled_state(True)
        self.worker = None
        self._done = False
        self.masthead.set_status("error", self._c)
        self._update_primary_button()
        QMessageBox.critical(self, APP_NAME, msg)


def _tildify(path: str) -> str:
    home = os.path.expanduser("~")
    if path.startswith(home):
        return "~" + path[len(home):]
    return path


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #
REQUIRED_MODULES = ["trimesh", "pymeshfix", "numpy", "networkx", "scipy", "skimage"]


def _check_dependencies() -> list[str]:
    import importlib
    missing = []
    for name in REQUIRED_MODULES:
        try:
            importlib.import_module(name)
        except ImportError:
            missing.append(name)
    return missing


def main() -> int:
    _setup_logging()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    missing = _check_dependencies()
    if missing:
        QMessageBox.critical(
            None, APP_NAME,
            "Missing Python packages:\n\n  " + ", ".join(missing) +
            f"\n\nThis Python interpreter:\n  {sys.executable}\n\n"
            "Install the project's dependencies:\n"
            "  pip install -e \".[build]\"\n\n"
            "If you're running from source with a venv, activate it first."
        )
        return 1

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
