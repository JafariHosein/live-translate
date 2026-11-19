"""Frameless, glassmorphic PyQt6 overlay for Live Translate.

The window is intentionally a ``Qt.Tool`` with ``WindowStaysOnTopHint`` so it
behaves like a floating HUD.  On Wayland the compositor ultimately decides
stacking; see README.md for per-compositor window rules.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from PyQt6.QtCore import (
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSettings,
    QSize,
    Qt,
    QTimer,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizeGrip,
    QSizePolicy,
    QSlider,
    QStackedLayout,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from config import APP_ID, APP_NAME, Settings

if TYPE_CHECKING:  # pragma: no cover
    from audio_recorder import InputDevice

log = logging.getLogger(__name__)

FONT_STACK = ["Vazirmatn", "Vazir", "Noto Sans Arabic", "DejaVu Sans", "Sans Serif"]
PANEL_MARGIN = 8
PANEL_RADIUS = 22

STATE_COLORS = {
    "idle": "#34d399",
    "listening": "#fb7185",
    "translating": "#fbbf24",
    "error": "#f87171",
    "warning": "#fbbf24",
}

STATE_TEXT = {
    "idle": "Ready",
    "listening": "Listening…",
    "translating": "Translating…",
    "error": "Error",
    "warning": "Limited",
}

STATE_TEXT_COLORS = {
    "idle": "#6ee7b7",
    "listening": "#fda4af",
    "translating": "#fcd34d",
    "error": "#fca5a5",
    "warning": "#fcd34d",
}

COMPACT_BASE_HEIGHT = 54
COMPACT_DETAIL_TIMEOUT_MS = 10_000


def persian_font(
    size_px: int = 19,
    weight: QFont.Weight = QFont.Weight.Medium,
) -> QFont:
    font = QFont()
    font.setFamilies(FONT_STACK)
    font.setPixelSize(size_px)
    font.setWeight(weight)
    return font


def make_app_icon(size: int = 64) -> QIcon:
    """Draw the app icon in code so the project needs no binary assets."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    rect = QRectF(2, 2, size - 4, size - 4)
    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    gradient.setColorAt(0.0, QColor("#8b5cf6"))
    gradient.setColorAt(1.0, QColor("#06b6d4"))
    path = QPainterPath()
    path.addRoundedRect(rect, size * 0.28, size * 0.28)
    painter.fillPath(path, gradient)

    painter.setPen(
        QPen(
            QColor(255, 255, 255, 235),
            max(2.0, size * 0.055),
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
        )
    )
    center_y = size / 2
    for offset, height in ((-0.24, 0.22), (-0.08, 0.42), (0.08, 0.52), (0.24, 0.28)):
        x = size / 2 + offset * size
        half = size * height / 2
        painter.drawLine(QPointF(x, center_y - half), QPointF(x, center_y + half))
    painter.end()
    return QIcon(pixmap)


def _repolish(widget: QWidget) -> None:
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


class ElidedLabel(QLabel):
    """A one-line label that elides its full text instead of hard-clipping."""

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        elide: Qt.TextElideMode = Qt.TextElideMode.ElideRight,
    ) -> None:
        super().__init__(parent)
        self._full_text = ""
        self._elide = elide
        self._updating = False
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.set_full_text(text)

    def full_text(self) -> str:
        return self._full_text

    def set_full_text(self, text: str) -> None:
        self._full_text = str(text or "")
        self.setToolTip(self._full_text)
        self._apply_elision()

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API
        self.set_full_text(text)

    def _apply_elision(self) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            width = max(16, self.width())
            elided = self.fontMetrics().elidedText(
                self._full_text, self._elide, width
            )
            super().setText(elided)
        finally:
            self._updating = False

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._apply_elision()


class LogoMark(QWidget):
    def __init__(self, size: int = 38, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.5, 0.5, self._size - 1, self._size - 1)
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0.0, QColor("#8b5cf6"))
        gradient.setColorAt(1.0, QColor("#06b6d4"))
        path = QPainterPath()
        path.addRoundedRect(rect, 12, 12)
        painter.fillPath(path, gradient)
        painter.setPen(
            QPen(
                QColor(255, 255, 255, 240),
                2.6,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        cy = self._size / 2
        for offset, height in ((-0.24, 0.24), (-0.08, 0.46), (0.08, 0.58), (0.24, 0.30)):
            x = self._size / 2 + offset * self._size
            half = self._size * height / 2
            painter.drawLine(QPointF(x, cy - half), QPointF(x, cy + half))
        painter.end()


class StatusDot(QWidget):
    """Small state dot with a compositor-safe pulsing halo."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self._color = QColor(STATE_COLORS["idle"])
        self._pulse = 0.0
        self._active = False
        self._animation = QPropertyAnimation(self, b"pulse", self)
        self._animation.setDuration(1300)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setLoopCount(-1)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def _get_pulse(self) -> float:
        return self._pulse

    def _set_pulse(self, value: float) -> None:
        self._pulse = float(value)
        self.update()

    pulse = pyqtProperty(float, fget=_get_pulse, fset=_set_pulse)

    def set_state(self, color: str, active: bool) -> None:
        self._color = QColor(color)
        if active and not self._active:
            self._animation.start()
        elif not active:
            self._animation.stop()
            self._pulse = 0.0
        self._active = active
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center = QPointF(self.width() / 2, self.height() / 2)
        base_radius = 4.3
        if self._active:
            halo = QColor(self._color)
            halo.setAlpha(int(90 * (1.0 - self._pulse)))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(halo)
            radius = base_radius + 1.0 + 6.0 * self._pulse
            painter.drawEllipse(center, radius, radius)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._color)
        painter.drawEllipse(center, base_radius, base_radius)
        painter.end()


class StatusPill(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("StatusPill")
        self.setProperty("state", "idle")
        self.setFixedHeight(28)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(9, 0, 12, 0)
        layout.setSpacing(7)
        self.dot = StatusDot(self)
        self.label = QLabel(STATE_TEXT["idle"], self)
        self.label.setObjectName("StatusText")
        layout.addWidget(self.dot)
        layout.addWidget(self.label)
        self._apply_style()

    def set_state(self, state: str, text: str | None = None) -> None:
        state = state if state in STATE_COLORS else "idle"
        self.setProperty("state", state)
        self._state = state
        label = text or STATE_TEXT[state]
        self.label.setText(label)
        self.dot.set_state(
            STATE_COLORS[state], state in {"listening", "translating"}
        )
        self._apply_style()

    def _apply_style(self) -> None:
        state = self.property("state") or "idle"
        palettes = {
            "idle": ("rgba(52, 211, 153, 0.10)", "rgba(52, 211, 153, 0.22)"),
            "listening": ("rgba(251, 113, 133, 0.14)", "rgba(251, 113, 133, 0.30)"),
            "translating": ("rgba(251, 191, 36, 0.12)", "rgba(251, 191, 36, 0.28)"),
            "error": ("rgba(248, 113, 113, 0.13)", "rgba(248, 113, 113, 0.30)"),
            "warning": ("rgba(251, 191, 36, 0.12)", "rgba(251, 191, 36, 0.28)"),
        }
        background, border = palettes.get(state, palettes["idle"])
        self.setStyleSheet(
            "QFrame#StatusPill {"
            f" background: {background};"
            f" border: 1px solid {border};"
            " border-radius: 14px;"
            "}"
        )
        self.label.setStyleSheet(
            f"color: {STATE_TEXT_COLORS.get(state, '#e2e8f0')};"
            " font-size: 11px;"
            " font-weight: 600;"
            " background: transparent;"
        )


class DragBar(QWidget):
    """Header widget that moves the frameless window natively on Wayland."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._drag_offset = None
        self.setCursor(Qt.CursorShape.SizeAllCursor)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton:
            window = self.window()
            handle = window.windowHandle() if window else None
            if handle is not None:
                try:
                    if handle.startSystemMove():
                        event.accept()
                        return
                except Exception:
                    pass
            self._drag_offset = (
                event.globalPosition().toPoint() - window.frameGeometry().topLeft()
            )
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt API
        if (
            self._drag_offset is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            self.window().move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._drag_offset = None
        super().mouseReleaseEvent(event)


class NoticeBar(QFrame):
    """Dismissible inline banner used for warnings and provider errors."""

    KIND_STYLES = {
        "warning": (
            "rgba(245, 158, 11, 0.11)",
            "rgba(245, 158, 11, 0.30)",
            "#fcd34d",
        ),
        "error": ("rgba(239, 68, 68, 0.12)", "rgba(239, 68, 68, 0.32)", "#fca5a5"),
        "info": ("rgba(56, 189, 248, 0.11)", "rgba(56, 189, 248, 0.28)", "#bae6fd"),
    }

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("NoticeBar")
        self._kind = "warning"
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 8, 8)
        layout.setSpacing(8)
        self._icon = QLabel("!", self)
        self._icon.setObjectName("NoticeIcon")
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._icon.setFixedSize(20, 20)
        self._message = QLabel("", self)
        self._message.setObjectName("NoticeText")
        self._message.setWordWrap(True)
        self._message.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        close = QToolButton(self)
        close.setObjectName("NoticeClose")
        close.setText("×")
        close.setToolTip("Dismiss")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setFixedSize(22, 22)
        close.clicked.connect(lambda: self.hide())
        layout.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self._message, 1)
        layout.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        self.hide()
        self._apply_kind()

    def show_message(
        self, text: str, kind: str = "warning", timeout_ms: int = 0
    ) -> None:
        self._kind = kind if kind in self.KIND_STYLES else "warning"
        self._message.setText(text)
        self._icon.setText("i" if self._kind == "info" else "!")
        self._apply_kind()
        self.show()
        if timeout_ms > 0:
            QTimer.singleShot(timeout_ms, self._hide_if_same, text)

    def _hide_if_same(self, text: str) -> None:
        if self._message.text() == text:
            self.hide()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().hideEvent(event)

    def _apply_kind(self) -> None:
        background, border, color = self.KIND_STYLES[self._kind]
        self.setStyleSheet(
            "QFrame#NoticeBar {"
            f" background: {background};"
            f" border: 1px solid {border};"
            " border-radius: 11px;"
            "}"
        )
        self._icon.setStyleSheet(
            f"color: {color};"
            " background: rgba(255, 255, 255, 0.08);"
            " border-radius: 10px;"
            " font-size: 11px;"
            " font-weight: 800;"
        )
        self._message.setStyleSheet(
            f"color: {color}; background: transparent; font-size: 11px;"
        )

    @property
    def kind(self) -> str:
        return self._kind


class HistoryCard(QFrame):
    def __init__(
        self,
        english: str,
        persian: str,
        timestamp: datetime,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("HistoryCard")
        self._persian = persian
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(5)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        english_label = QLabel(english, self)
        english_label.setObjectName("HistoryEnglish")
        english_label.setWordWrap(True)
        english_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        english_label.setTextFormat(Qt.TextFormat.PlainText)
        english_label.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        time_label = QLabel(timestamp.strftime("%H:%M"), self)
        time_label.setObjectName("HistoryTime")
        copy_button = QToolButton(self)
        copy_button.setObjectName("CopyButton")
        copy_button.setText("Copy")
        copy_button.setToolTip("Copy the Persian translation")
        copy_button.setCursor(Qt.CursorShape.PointingHandCursor)
        copy_button.clicked.connect(
            lambda _checked=False, button=copy_button, text=persian: (
                _copy_with_feedback(button, text)
            )
        )
        top.addWidget(english_label, 1)
        top.addWidget(time_label, 0, Qt.AlignmentFlag.AlignTop)
        top.addWidget(copy_button, 0, Qt.AlignmentFlag.AlignTop)

        persian_label = QLabel(persian, self)
        persian_label.setObjectName("HistoryPersian")
        persian_label.setWordWrap(True)
        persian_label.setFont(persian_font(15, QFont.Weight.Normal))
        persian_label.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        persian_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
        )
        persian_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        persian_label.setTextFormat(Qt.TextFormat.PlainText)

        layout.addLayout(top)
        layout.addWidget(persian_label)


def _copy_with_feedback(button: QToolButton, text: str) -> None:
    clipboard = QApplication.clipboard()
    if clipboard is not None:
        clipboard.setText(text)
    original = button.text()
    button.setText("Copied")
    QTimer.singleShot(
        1200, lambda: _set_button_text_safe(button, original)
    )


def _set_button_text_safe(button: QToolButton, text: str) -> None:
    try:
        button.setText(text)
    except RuntimeError:
        # The history card may have been cleared before the timer fired.
        pass


class MicComboBox(QComboBox):
    """QComboBox with a code-drawn chevron that matches the dark theme."""

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(
            QPen(
                QColor(148, 163, 184, 200),
                1.6,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        x = self.width() - 14.0
        y = self.height() / 2.0
        painter.drawLine(QPointF(x - 3.5, y - 2.0), QPointF(x, y + 1.8))
        painter.drawLine(QPointF(x, y + 1.8), QPointF(x + 3.5, y - 2.0))
        painter.end()


class OverlayWindow(QWidget):
    """Main floating window.  All long-running work happens outside this class."""

    ptt_pressed = pyqtSignal()
    ptt_released = pyqtSignal()
    mic_changed = pyqtSignal(int)
    opacity_changed = pyqtSignal(int)
    manual_submitted = pyqtSignal(str)
    quit_requested = pyqtSignal()

    def __init__(
        self,
        settings: Settings,
        ptt_label: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._ptt_label = ptt_label
        self._qsettings = QSettings("LiveTranslate", "LiveTranslate")
        self._state = "idle"
        self._english_value = ""
        self._persian_value = ""
        self._history_cards: list[HistoryCard] = []
        self._collapsed = False
        self._compact = False
        self._compact_detail_visible = False
        self._compact_bar_height = COMPACT_BASE_HEIGHT
        self._expanded_size: QSize | None = None
        self._compact_restore_size: QSize | None = None
        self._tray_notice_shown = False
        self._tray: QSystemTrayIcon | None = None
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self._notice_timeout)
        self._notice_kind = ""

        self._compact_detail_timer = QTimer(self)
        self._compact_detail_timer.setSingleShot(True)
        self._compact_detail_timer.timeout.connect(self._compact_detail_timeout)

        self._panel_opacity = 1.0
        saved_opacity = self._qsettings.value(
            "ui/opacity", int(settings.window_opacity * 100), type=int
        )
        self._panel_opacity = max(36, min(100, int(saved_opacity))) / 100.0

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(make_app_icon())
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMinimumWidth(420)

        self._build_ui()
        self._build_tray()
        self._restore_geometry()
        self.setWindowOpacity(1.0)

        if bool(self._qsettings.value("ui/compact", False, type=bool)):
            self._set_compact(True, initial=True)

    # --------------------------------------------------------------------- build
    def _build_ui(self) -> None:
        self._outer_layout = QVBoxLayout(self)
        self._outer_layout.setContentsMargins(22, 18, 22, 18)
        self._outer_layout.setSpacing(0)

        self._stack = QStackedLayout()
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._outer_layout.addLayout(self._stack)

        self._full_page = QWidget(self)
        root = QVBoxLayout(self._full_page)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        self._header = DragBar(self._full_page)
        header_layout = QVBoxLayout(self._header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(10)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(10)
        logo = LogoMark(38, self._header)
        title_column = QVBoxLayout()
        title_column.setContentsMargins(0, 0, 0, 0)
        title_column.setSpacing(1)
        title = QLabel(APP_NAME, self._header)
        title.setObjectName("AppTitle")
        self._subtitle = QLabel(
            f"EN → FA  ·  Hold {self._ptt_label}", self._header
        )
        self._subtitle.setObjectName("AppSubtitle")
        title_column.addWidget(title)
        title_column.addWidget(self._subtitle)

        self._status_pill = StatusPill(self._header)

        mini = QToolButton(self._header)
        mini.setObjectName("ModeButton")
        mini.setText("Mini")
        mini.setToolTip("Switch to the one-line overlay")
        mini.setCursor(Qt.CursorShape.PointingHandCursor)
        mini.setFixedSize(46, 28)
        mini.clicked.connect(lambda: self._set_compact(True))
        self._mini_button = mini

        minimize = QToolButton(self._header)
        minimize.setObjectName("WindowButton")
        minimize.setText("−")
        minimize.setToolTip("Minimize (keeps running)")
        minimize.setCursor(Qt.CursorShape.PointingHandCursor)
        minimize.setFixedSize(28, 28)
        minimize.clicked.connect(self._on_minimize)

        close = QToolButton(self._header)
        close.setObjectName("CloseButton")
        close.setText("×")
        close.setToolTip("Quit Live Translate")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setFixedSize(28, 28)
        close.clicked.connect(self.close)

        title_row.addWidget(logo, 0, Qt.AlignmentFlag.AlignTop)
        title_row.addLayout(title_column, 1)
        title_row.addWidget(self._status_pill, 0, Qt.AlignmentFlag.AlignVCenter)
        title_row.addWidget(mini, 0, Qt.AlignmentFlag.AlignTop)
        title_row.addWidget(minimize, 0, Qt.AlignmentFlag.AlignTop)
        title_row.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)

        controls_row = QHBoxLayout()
        controls_row.setContentsMargins(0, 0, 0, 0)
        controls_row.setSpacing(8)
        self._mic_combo = MicComboBox(self._header)
        self._mic_combo.setObjectName("MicCombo")
        self._mic_combo.setToolTip("Microphone used for push-to-talk")
        self._mic_combo.setMinimumWidth(150)
        self._mic_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self._mic_combo.setMinimumContentsLength(18)
        self._mic_combo.currentIndexChanged.connect(self._on_mic_changed)

        dim_label = QLabel("DIM", self._header)
        dim_label.setObjectName("FieldLabel")
        dim_label.setToolTip("Overlay transparency")
        self._opacity_slider = QSlider(
            Qt.Orientation.Horizontal, self._header
        )
        self._opacity_slider.setObjectName("OpacitySlider")
        self._opacity_slider.setRange(36, 100)
        self._opacity_slider.setValue(int(round(self._panel_opacity * 100)))
        self._opacity_slider.setFixedWidth(86)
        self._opacity_slider.setToolTip("Overlay opacity")
        self._opacity_slider.valueChanged.connect(self._on_opacity_changed)

        controls_row.addWidget(self._mic_combo, 1)
        controls_row.addWidget(dim_label, 0)
        controls_row.addWidget(self._opacity_slider, 0)

        header_layout.addLayout(title_row)
        header_layout.addLayout(controls_row)

        self._notice = NoticeBar(self)

        root.addWidget(self._header)
        root.addWidget(self._notice)

        divider = QFrame(self)
        divider.setObjectName("Divider")
        divider.setFixedHeight(1)
        root.addWidget(divider)

        self._body = QWidget(self)
        body = QVBoxLayout(self._body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(12)

        body.addWidget(self._build_translation_card())

        manual_row = QHBoxLayout()
        manual_row.setContentsMargins(0, 0, 0, 0)
        manual_row.setSpacing(8)
        self._manual_input = QLineEdit(self._body)
        self._manual_input.setObjectName("ManualInput")
        self._manual_input.setPlaceholderText("Type English and press Enter…")
        self._manual_input.setClearButtonEnabled(True)
        self._manual_input.setMaxLength(5_000)
        self._manual_input.setFixedHeight(38)
        self._manual_input.setToolTip(
            "Type or paste English here, then press Enter to translate"
        )
        self._manual_input.returnPressed.connect(self._submit_manual)
        self._manual_button = QPushButton("Translate", self._body)
        self._manual_button.setObjectName("TranslateButton")
        self._manual_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._manual_button.setFixedSize(92, 38)
        self._manual_button.setToolTip("Translate the typed English (Enter)")
        self._manual_button.clicked.connect(self._submit_manual)
        manual_row.addWidget(self._manual_input, 1)
        manual_row.addWidget(self._manual_button, 0)
        body.addLayout(manual_row)

        self._hold_button = QPushButton(
            f"Hold to Talk   ·   {self._ptt_label}", self._body
        )
        self._hold_button.setObjectName("HoldButton")
        self._hold_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hold_button.setFixedHeight(56)
        self._hold_button.setProperty("listening", False)
        self._hold_button.setToolTip(
            f"Hold {self._ptt_label} anywhere, or hold this button"
        )
        self._hold_button.pressed.connect(self.ptt_pressed.emit)
        self._hold_button.released.connect(self.ptt_released.emit)
        body.addWidget(self._hold_button)

        history_header = QHBoxLayout()
        history_header.setContentsMargins(2, 2, 2, 0)
        history_header.setSpacing(8)
        history_title = QLabel("History", self._body)
        history_title.setObjectName("SectionTitle")
        self._history_count = QLabel("0", self._body)
        self._history_count.setObjectName("CountBadge")
        self._provider_badge = QLabel(
            self._settings.provider_label, self._body
        )
        self._provider_badge.setObjectName("ProviderBadge")
        self._clear_button = QToolButton(self._body)
        self._clear_button.setObjectName("ClearButton")
        self._clear_button.setText("Clear")
        self._clear_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear_button.clicked.connect(self._on_clear_clicked)
        history_header.addWidget(history_title)
        history_header.addWidget(self._history_count)
        history_header.addStretch(1)
        history_header.addWidget(self._provider_badge)
        history_header.addWidget(self._clear_button)
        body.addLayout(history_header)
        body.addWidget(self._build_history_area(), 1)

        root.addWidget(self._body, 1)

        self._clear_reset_timer = QTimer(self)
        self._clear_reset_timer.setSingleShot(True)
        self._clear_reset_timer.timeout.connect(self._reset_clear_button)

        self._size_grip = QSizeGrip(self)
        self._size_grip.setFixedSize(16, 16)
        self._size_grip.setToolTip("Resize")
        self._size_grip.raise_()

        self._stack.addWidget(self._full_page)
        self._compact_page = self._build_compact_bar()
        self._stack.addWidget(self._compact_page)
        self._stack.setCurrentWidget(self._full_page)

        self._apply_stylesheet()
        self._update_opacity_ui()
        self.set_state("idle")

    def _build_compact_bar(self) -> DragBar:
        """Build the one-line mode: status, latest pair and essential controls.

        Below the slim row there is a hidden detail panel that is shown
        temporarily when a translation does not fit on one line, so the full
        English text and Persian translation can be read without leaving the
        compact layout.
        """
        bar = DragBar(self)
        bar.setObjectName("CompactBar")
        outer = QVBoxLayout(bar)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        logo = LogoMark(24, bar)
        self._compact_dot = StatusDot(bar)
        self._compact_dot.setToolTip("Ready")

        self._compact_caption = QLabel("Full translation", bar)
        self._compact_caption.setObjectName("CompactCaption")
        self._compact_caption.setFont(persian_font(11, QFont.Weight.DemiBold))
        self._compact_caption.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self._compact_caption.hide()

        self._compact_english = ElidedLabel(
            f"Hold {self._ptt_label} or type", bar, Qt.TextElideMode.ElideRight
        )
        self._compact_english.setObjectName("CompactEnglish")
        self._compact_english.setFont(persian_font(12, QFont.Weight.Medium))
        self._compact_english.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._compact_english.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self._compact_english.setMinimumWidth(70)

        self._compact_arrow = QLabel("→", bar)
        self._compact_arrow.setObjectName("CompactArrow")
        self._compact_arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._compact_arrow.setFixedWidth(18)

        self._compact_persian = ElidedLabel(
            "ترجمه اینجا", bar, Qt.TextElideMode.ElideLeft
        )
        self._compact_persian.setObjectName("CompactPersian")
        self._compact_persian.setFont(persian_font(14, QFont.Weight.Medium))
        self._compact_persian.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self._compact_persian.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._compact_persian.setMinimumWidth(70)

        self._compact_hold = QToolButton(bar)
        self._compact_hold.setObjectName("CompactHold")
        self._compact_hold.setText("Hold")
        self._compact_hold.setToolTip(
            f"Hold {self._ptt_label} anywhere, or hold this button"
        )
        self._compact_hold.setCursor(Qt.CursorShape.PointingHandCursor)
        self._compact_hold.setFixedSize(58, 30)
        self._compact_hold.setProperty("listening", False)
        self._compact_hold.pressed.connect(self.ptt_pressed.emit)
        self._compact_hold.released.connect(self.ptt_released.emit)

        full = QToolButton(bar)
        full.setObjectName("ModeButton")
        full.setText("Full")
        full.setToolTip("Expand the overlay, history and manual input")
        full.setCursor(Qt.CursorShape.PointingHandCursor)
        full.setFixedSize(46, 30)
        full.clicked.connect(lambda: self._set_compact(False))

        close = QToolButton(bar)
        close.setObjectName("CloseButton")
        close.setText("×")
        close.setToolTip("Quit Live Translate")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setFixedSize(28, 28)
        close.clicked.connect(self.close)

        row.addWidget(logo, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self._compact_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self._compact_caption, 1)
        row.addWidget(self._compact_english, 1)
        row.addWidget(self._compact_arrow, 0)
        row.addWidget(self._compact_persian, 1)
        row.addWidget(self._compact_hold, 0)
        row.addWidget(full, 0)
        row.addWidget(close, 0)
        outer.addLayout(row)

        self._compact_detail = QFrame(bar)
        self._compact_detail.setObjectName("CompactDetail")
        detail_layout = QVBoxLayout(self._compact_detail)
        detail_layout.setContentsMargins(12, 9, 12, 10)
        detail_layout.setSpacing(6)

        self._compact_detail_english = QLabel(self._compact_detail)
        self._compact_detail_english.setObjectName("CompactDetailEnglish")
        self._compact_detail_english.setFont(
            persian_font(13, QFont.Weight.Normal)
        )
        self._compact_detail_english.setTextFormat(Qt.TextFormat.PlainText)
        self._compact_detail_english.setWordWrap(True)
        self._compact_detail_english.setLayoutDirection(
            Qt.LayoutDirection.LeftToRight
        )
        self._compact_detail_english.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self._compact_detail_english.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
        )
        self._compact_detail_english.setMinimumWidth(0)

        self._compact_detail_persian = QLabel(self._compact_detail)
        self._compact_detail_persian.setObjectName("CompactDetailPersian")
        self._compact_detail_persian.setFont(
            persian_font(16, QFont.Weight.Medium)
        )
        self._compact_detail_persian.setTextFormat(Qt.TextFormat.PlainText)
        self._compact_detail_persian.setWordWrap(True)
        self._compact_detail_persian.setLayoutDirection(
            Qt.LayoutDirection.RightToLeft
        )
        self._compact_detail_persian.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
        )
        self._compact_detail_persian.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
        )
        self._compact_detail_persian.setMinimumWidth(0)

        detail_layout.addWidget(self._compact_detail_english)
        detail_layout.addWidget(self._compact_detail_persian)
        self._compact_detail.hide()
        outer.addWidget(self._compact_detail)
        return bar

    def _build_translation_card(self) -> QFrame:
        card = QFrame(self._body)
        card.setObjectName("TranslationCard")
        card.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 13, 16, 14)
        layout.setSpacing(7)

        english_header = QHBoxLayout()
        english_header.setContentsMargins(0, 0, 0, 0)
        english_header.setSpacing(6)
        english_field = QLabel("ENGLISH", card)
        english_field.setObjectName("FieldLabel")
        self._copy_english = QToolButton(card)
        self._copy_english.setObjectName("CopyButton")
        self._copy_english.setText("Copy")
        self._copy_english.setToolTip("Copy the English transcript")
        self._copy_english.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_english.setEnabled(False)
        self._copy_english.clicked.connect(self._copy_english_text)
        english_header.addWidget(english_field)
        english_header.addStretch(1)
        english_header.addWidget(self._copy_english)

        self._english_label = QLabel(
            "Hold the key and speak in English — your transcript appears here.",
            card,
        )
        self._english_label.setObjectName("EnglishText")
        self._english_label.setProperty("placeholder", True)
        self._english_label.setWordWrap(True)
        self._english_label.setMinimumHeight(28)
        self._english_label.setTextFormat(Qt.TextFormat.PlainText)
        self._english_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._english_label.setLayoutDirection(Qt.LayoutDirection.LeftToRight)
        self._english_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )

        inner_divider = QFrame(card)
        inner_divider.setObjectName("InnerDivider")
        inner_divider.setFixedHeight(1)

        persian_header = QHBoxLayout()
        persian_header.setContentsMargins(0, 0, 0, 0)
        persian_header.setSpacing(6)
        persian_field = QLabel("PERSIAN", card)
        persian_field.setObjectName("FieldLabel")
        fa_tag = QLabel("فارسی", card)
        fa_tag.setObjectName("FaTag")
        fa_tag.setFont(persian_font(12, QFont.Weight.Medium))
        fa_tag.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self._copy_persian = QToolButton(card)
        self._copy_persian.setObjectName("CopyButton")
        self._copy_persian.setText("Copy")
        self._copy_persian.setToolTip("Copy the Persian translation")
        self._copy_persian.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_persian.setEnabled(False)
        self._copy_persian.clicked.connect(self._copy_persian_text)
        persian_header.addWidget(persian_field)
        persian_header.addStretch(1)
        persian_header.addWidget(fa_tag)
        persian_header.addWidget(self._copy_persian)

        self._persian_label = QLabel(
            "ترجمه فارسی اینجا نمایش داده می‌شود", card
        )
        self._persian_label.setObjectName("PersianText")
        self._persian_label.setProperty("placeholder", True)
        self._persian_label.setWordWrap(True)
        self._persian_label.setMinimumHeight(34)
        self._persian_label.setFont(persian_font(20, QFont.Weight.Medium))
        self._persian_label.setTextFormat(Qt.TextFormat.PlainText)
        self._persian_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._persian_label.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self._persian_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
        )

        layout.addLayout(english_header)
        layout.addWidget(self._english_label)
        layout.addSpacing(2)
        layout.addWidget(inner_divider)
        layout.addSpacing(2)
        layout.addLayout(persian_header)
        layout.addWidget(self._persian_label)
        return card

    def _build_history_area(self) -> QScrollArea:
        self._history_scroll = QScrollArea(self._body)
        self._history_scroll.setObjectName("HistoryScroll")
        self._history_scroll.setWidgetResizable(True)
        self._history_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._history_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._history_scroll.setMinimumHeight(120)
        self._history_scroll.viewport().setAutoFillBackground(False)

        self._history_content = QWidget(self._history_scroll)
        self._history_content.setObjectName("HistoryContent")
        self._history_content.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, True
        )
        self._history_layout = QVBoxLayout(self._history_content)
        self._history_layout.setContentsMargins(0, 0, 6, 0)
        self._history_layout.setSpacing(8)

        self._empty_state = QLabel(
            "No translations yet.\nHold the key and speak to get started.",
            self._history_content,
        )
        self._empty_state.setObjectName("EmptyState")
        self._empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_state.setWordWrap(True)
        self._history_layout.addWidget(self._empty_state)
        self._history_layout.addStretch(1)
        self._history_scroll.setWidget(self._history_content)
        return self._history_scroll

    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.info("System tray is not available; minimize will collapse the window")
            return
        tray = QSystemTrayIcon(make_app_icon(), self)
        tray.setToolTip(APP_NAME)
        menu = QMenu()
        show_action = menu.addAction("Show / Hide")
        show_action.triggered.connect(self._toggle_visible)
        menu.addSeparator()
        quit_action = menu.addAction("Quit Live Translate")
        quit_action.triggered.connect(self.close)
        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self._tray = tray
        self._tray_menu = menu

    # ------------------------------------------------------------------ styles
    def _apply_stylesheet(self) -> None:
        self.setStyleSheet(
            """
            QWidget {
                color: #e2e8f0;
                font-size: 13px;
            }
            QLabel { background: transparent; }
            QLabel#AppTitle {
                color: #f8fafc;
                font-size: 17px;
                font-weight: 700;
            }
            QLabel#AppSubtitle {
                color: rgba(203, 213, 225, 0.55);
                font-size: 10px;
                font-weight: 500;
            }
            QLabel#FieldLabel {
                color: rgba(203, 213, 225, 0.46);
                font-size: 9px;
                font-weight: 700;
                letter-spacing: 1px;
            }
            QLabel#FaTag {
                color: rgba(203, 213, 225, 0.48);
            }
            QLabel#Divider, QFrame#Divider {
                background: rgba(255, 255, 255, 0.07);
                border: none;
            }
            QFrame#InnerDivider {
                background: rgba(255, 255, 255, 0.06);
                border: none;
            }
            QToolButton#WindowButton, QToolButton#CloseButton {
                background: rgba(255, 255, 255, 0.055);
                border: 1px solid rgba(255, 255, 255, 0.07);
                border-radius: 8px;
                color: rgba(226, 232, 240, 0.72);
                font-size: 15px;
                font-weight: 600;
                padding: 0;
            }
            QToolButton#WindowButton:hover {
                background: rgba(255, 255, 255, 0.12);
                color: #ffffff;
            }
            QToolButton#CloseButton:hover {
                background: rgba(244, 63, 94, 0.22);
                border-color: rgba(244, 63, 94, 0.42);
                color: #fecdd3;
            }
            QComboBox#MicCombo {
                background: rgba(255, 255, 255, 0.055);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 8px;
                padding: 5px 26px 5px 10px;
                color: rgba(226, 232, 240, 0.88);
                font-size: 11px;
                min-height: 18px;
            }
            QComboBox#MicCombo:hover {
                border-color: rgba(139, 92, 246, 0.50);
                background: rgba(255, 255, 255, 0.08);
            }
            QComboBox#MicCombo::drop-down {
                border: none;
                width: 22px;
            }
            QComboBox#MicCombo::down-arrow {
                image: none;
                width: 0;
                height: 0;
            }
            QComboBox#MicCombo QAbstractItemView {
                background: #1b1b29;
                color: #e2e8f0;
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 8px;
                selection-background-color: rgba(139, 92, 246, 0.35);
                outline: 0;
                padding: 4px;
            }
            QSlider#OpacitySlider::groove:horizontal {
                height: 4px;
                background: rgba(255, 255, 255, 0.11);
                border-radius: 2px;
            }
            QSlider#OpacitySlider::sub-page:horizontal {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #8b5cf6, stop:1 #06b6d4);
                border-radius: 2px;
            }
            QSlider#OpacitySlider::handle:horizontal {
                width: 12px;
                height: 12px;
                margin: -4px 0;
                border-radius: 6px;
                background: #f8fafc;
            }
            QFrame#TranslationCard {
                background: rgba(255, 255, 255, 0.045);
                border: 1px solid rgba(255, 255, 255, 0.075);
                border-radius: 15px;
            }
            QLabel#EnglishText {
                color: #dce5f0;
                font-size: 13px;
            }
            QLabel#EnglishText[placeholder="true"] {
                color: rgba(203, 213, 225, 0.34);
            }
            QLabel#PersianText {
                color: #ffffff;
            }
            QLabel#PersianText[placeholder="true"] {
                color: rgba(226, 232, 240, 0.28);
            }
            QPushButton#HoldButton {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #7c3aed, stop:0.55 #6366f1, stop:1 #0891b2);
                border: 1px solid rgba(255, 255, 255, 0.16);
                border-radius: 15px;
                color: #ffffff;
                font-size: 14px;
                font-weight: 700;
                padding: 0 18px;
            }
            QPushButton#HoldButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #8b5cf6, stop:0.55 #818cf8, stop:1 #06b6d4);
            }
            QPushButton#HoldButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #6d28d9, stop:1 #0e7490);
            }
            QPushButton#HoldButton[listening="true"] {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #e11d48, stop:0.6 #f43f5e, stop:1 #f97316);
                border-color: rgba(255, 228, 230, 0.30);
            }
            QLineEdit#ManualInput {
                background: rgba(255, 255, 255, 0.055);
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 10px;
                padding: 0 12px;
                color: #e2e8f0;
                font-size: 12px;
                selection-background-color: rgba(139, 92, 246, 0.45);
            }
            QLineEdit#ManualInput:hover {
                background: rgba(255, 255, 255, 0.07);
            }
            QLineEdit#ManualInput:focus {
                background: rgba(255, 255, 255, 0.075);
                border-color: rgba(139, 92, 246, 0.60);
            }
            QPushButton#TranslateButton {
                background: rgba(139, 92, 246, 0.18);
                border: 1px solid rgba(139, 92, 246, 0.36);
                border-radius: 10px;
                color: #ddd6fe;
                font-size: 11px;
                font-weight: 700;
            }
            QPushButton#TranslateButton:hover {
                background: rgba(139, 92, 246, 0.30);
                border-color: rgba(139, 92, 246, 0.55);
                color: #ffffff;
            }
            QPushButton#TranslateButton:pressed {
                background: rgba(139, 92, 246, 0.42);
            }
            QPushButton#TranslateButton:disabled {
                background: rgba(255, 255, 255, 0.03);
                border-color: rgba(255, 255, 255, 0.05);
                color: rgba(203, 213, 225, 0.25);
            }
            QToolButton#CompactHold {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #7c3aed, stop:0.55 #6366f1, stop:1 #0891b2);
                border: 1px solid rgba(255, 255, 255, 0.16);
                border-radius: 9px;
                color: #ffffff;
                font-size: 10px;
                font-weight: 700;
                padding: 0;
            }
            QToolButton#CompactHold:hover {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #8b5cf6, stop:0.55 #818cf8, stop:1 #06b6d4);
            }
            QToolButton#CompactHold[listening="true"] {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #e11d48, stop:0.6 #f43f5e, stop:1 #f97316);
                border-color: rgba(255, 228, 230, 0.30);
            }
            QToolButton#ModeButton {
                background: rgba(255, 255, 255, 0.055);
                border: 1px solid rgba(255, 255, 255, 0.07);
                border-radius: 8px;
                color: rgba(226, 232, 240, 0.62);
                font-size: 9px;
                font-weight: 700;
                padding: 0;
            }
            QToolButton#ModeButton:hover {
                background: rgba(255, 255, 255, 0.12);
                color: #ffffff;
            }
            QLabel#CompactEnglish {
                color: #dce5f0;
            }
            QLabel#CompactPersian {
                color: #ffffff;
            }
            QLabel#CompactArrow {
                color: rgba(203, 213, 225, 0.34);
                font-size: 13px;
            }
            QLabel#CompactCaption {
                color: rgba(203, 213, 225, 0.56);
                font-size: 11px;
                font-weight: 600;
            }
            QFrame#CompactDetail {
                background: rgba(255, 255, 255, 0.045);
                border: 1px solid rgba(255, 255, 255, 0.075);
                border-radius: 12px;
            }
            QLabel#CompactDetailEnglish {
                color: rgba(220, 229, 240, 0.78);
            }
            QLabel#CompactDetailPersian {
                color: #ffffff;
            }
            QLabel#SectionTitle {
                color: #e2e8f0;
                font-size: 12px;
                font-weight: 700;
            }
            QLabel#CountBadge {
                color: rgba(203, 213, 225, 0.55);
                background: rgba(255, 255, 255, 0.07);
                border-radius: 8px;
                padding: 1px 7px;
                font-size: 9px;
                font-weight: 700;
            }
            QLabel#ProviderBadge {
                color: rgba(203, 213, 225, 0.38);
                font-size: 9px;
                font-weight: 600;
            }
            QToolButton#ClearButton {
                color: rgba(203, 213, 225, 0.55);
                background: transparent;
                border: 1px solid transparent;
                border-radius: 7px;
                padding: 3px 9px;
                font-size: 10px;
                font-weight: 600;
            }
            QToolButton#ClearButton:hover {
                color: #fca5a5;
                background: rgba(244, 63, 94, 0.10);
                border-color: rgba(244, 63, 94, 0.24);
            }
            QToolButton#ClearButton[armed="true"] {
                color: #fecdd3;
                background: rgba(244, 63, 94, 0.22);
                border-color: rgba(244, 63, 94, 0.45);
            }
            QToolButton#CopyButton {
                color: rgba(203, 213, 225, 0.50);
                background: rgba(255, 255, 255, 0.045);
                border: 1px solid rgba(255, 255, 255, 0.06);
                border-radius: 6px;
                padding: 1px 6px;
                font-size: 9px;
                font-weight: 600;
            }
            QToolButton#CopyButton:hover {
                color: #ddd6fe;
                background: rgba(139, 92, 246, 0.18);
                border-color: rgba(139, 92, 246, 0.35);
            }
            QToolButton#CopyButton:disabled {
                color: rgba(203, 213, 225, 0.18);
                background: transparent;
                border-color: rgba(255, 255, 255, 0.03);
            }
            QFrame#HistoryCard {
                background: rgba(255, 255, 255, 0.042);
                border: 1px solid rgba(255, 255, 255, 0.062);
                border-radius: 11px;
            }
            QFrame#HistoryCard:hover {
                background: rgba(255, 255, 255, 0.065);
                border-color: rgba(139, 92, 246, 0.28);
            }
            QLabel#HistoryEnglish {
                color: rgba(203, 213, 225, 0.60);
                font-size: 10px;
            }
            QLabel#HistoryPersian {
                color: #f1f5f9;
            }
            QLabel#HistoryTime {
                color: rgba(203, 213, 225, 0.30);
                font-size: 9px;
            }
            QLabel#EmptyState {
                color: rgba(203, 213, 225, 0.30);
                font-size: 11px;
                padding: 26px 10px;
            }
            QScrollArea#HistoryScroll {
                background: transparent;
                border: none;
            }
            QScrollArea#HistoryScroll > QWidget > QWidget {
                background: transparent;
            }
            QScrollArea#HistoryScroll QScrollBar:vertical {
                background: transparent;
                width: 7px;
                margin: 2px 0;
            }
            QScrollArea#HistoryScroll QScrollBar::handle:vertical {
                background: rgba(255, 255, 255, 0.15);
                border-radius: 3px;
                min-height: 26px;
            }
            QScrollArea#HistoryScroll QScrollBar::handle:vertical:hover {
                background: rgba(255, 255, 255, 0.28);
            }
            QScrollArea#HistoryScroll QScrollBar::add-line:vertical,
            QScrollArea#HistoryScroll QScrollBar::sub-line:vertical,
            QScrollArea#HistoryScroll QScrollBar::add-page:vertical,
            QScrollArea#HistoryScroll QScrollBar::sub-page:vertical {
                background: none;
                height: 0;
            }
            QToolButton#NoticeClose {
                color: rgba(255, 255, 255, 0.45);
                background: transparent;
                border: none;
                font-size: 14px;
                font-weight: 700;
            }
            QToolButton#NoticeClose:hover { color: #ffffff; }
            QToolTip {
                background-color: #1b1b29;
                color: #e2e8f0;
                border: 1px solid rgba(255, 255, 255, 0.14);
                border-radius: 6px;
                padding: 4px 7px;
                font-size: 10px;
            }
            """
        )

    # ------------------------------------------------------------------ painting
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(
            PANEL_MARGIN, PANEL_MARGIN, -PANEL_MARGIN, -PANEL_MARGIN
        )
        if rect.width() <= 0 or rect.height() <= 0:
            painter.end()
            return
        path = QPainterPath()
        path.addRoundedRect(rect, PANEL_RADIUS, PANEL_RADIUS)

        opacity = self._panel_opacity
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(
            0.0, QColor(31, 31, 46, int(248 * opacity))
        )
        gradient.setColorAt(
            1.0, QColor(13, 13, 22, int(252 * opacity))
        )
        painter.fillPath(path, gradient)

        painter.save()
        painter.setClipPath(path)
        glow = QRadialGradient(
            QPointF(rect.left() + rect.width() * 0.18, rect.top() + 20),
            rect.width() * 0.62,
        )
        glow.setColorAt(0.0, QColor(139, 92, 246, int(38 * opacity)))
        glow.setColorAt(1.0, QColor(139, 92, 246, 0))
        painter.fillRect(rect, glow)
        cool = QRadialGradient(
            QPointF(rect.right() - 30, rect.bottom() - 40),
            rect.width() * 0.45,
        )
        cool.setColorAt(0.0, QColor(6, 182, 212, int(20 * opacity)))
        cool.setColorAt(1.0, QColor(6, 182, 212, 0))
        painter.fillRect(rect, cool)
        painter.restore()

        border = QPen(QColor(255, 255, 255, int(34 * max(opacity, 0.55))), 1.1)
        painter.setPen(border)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(path)

        highlight = QPainterPath()
        highlight.addRoundedRect(rect.adjusted(1, 1, -1, -1), PANEL_RADIUS - 1, PANEL_RADIUS - 1)
        top_gradient = QLinearGradient(rect.topLeft(), rect.topLeft() + QPointF(0, 40))
        top_gradient.setColorAt(0.0, QColor(255, 255, 255, 24))
        top_gradient.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setPen(QPen(Qt.PenStyle.NoPen))
        painter.setBrush(top_gradient)
        painter.drawPath(highlight)
        painter.end()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        if hasattr(self, "_size_grip"):
            self._size_grip.move(
                self.width() - self._size_grip.width() - 12,
                self.height() - self._size_grip.height() - 12,
            )
            self._size_grip.raise_()
        self._schedule_geometry_save()

    def moveEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().moveEvent(event)
        self._schedule_geometry_save()

    # ------------------------------------------------------------------- geometry
    def _schedule_geometry_save(self) -> None:
        if not hasattr(self, "_save_geometry_timer"):
            self._save_geometry_timer = QTimer(self)
            self._save_geometry_timer.setSingleShot(True)
            self._save_geometry_timer.timeout.connect(self._save_geometry)
        self._save_geometry_timer.start(600)

    def _save_geometry(self) -> None:
        try:
            key = "ui/compact_geometry" if self._compact else "ui/geometry"
            self._qsettings.setValue(key, self.saveGeometry())
        except Exception:
            pass

    def _screen_available(self) -> QRect:
        screen = QGuiApplication.screenAt(self.geometry().center())
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        return screen.availableGeometry() if screen else QRect(0, 0, 1280, 800)

    def _ensure_on_screen(self) -> None:
        available = self._screen_available()
        width = min(self.width(), max(320, available.width() - 16))
        height = min(self.height(), max(320, available.height() - 16))
        if width != self.width() or height != self.height():
            self.resize(width, height)
        x = min(
            max(self.x(), available.left() + 8),
            max(available.left() + 8, available.right() - self.width() - 8),
        )
        y = min(
            max(self.y(), available.top() + 8),
            max(available.top() + 8, available.bottom() - self.height() - 8),
        )
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)

    def _resize_full_default(self) -> None:
        available = self._screen_available()
        width = min(self._settings.window_width, max(420, available.width() - 40))
        height = min(
            self._settings.window_height, max(480, available.height() - 60)
        )
        self.resize(width, height)

    def _set_compact(self, compact: bool, initial: bool = False) -> None:
        """Switch between the full overlay and the slim one-line bar."""
        compact = bool(compact)
        if not initial and compact == self._compact:
            return
        if not initial:
            if not compact:
                self._collapse_compact_detail()
            self._save_geometry()

        if compact:
            if not self._compact:
                if self._collapsed:
                    self._toggle_collapse()
                self._compact_restore_size = self.size()
            self._compact = True
            self._qsettings.setValue("ui/compact", True)
            self._compact_detail_timer.stop()
            self._compact_detail_visible = False
            self._compact_detail.hide()
            self._compact_caption.hide()
            self._compact_english.show()
            self._compact_arrow.show()
            self._compact_persian.show()
            self._stack.setCurrentWidget(self._compact_page)
            self._outer_layout.setContentsMargins(18, 12, 18, 12)
            self.setMinimumWidth(560)
            height = max(
                COMPACT_BASE_HEIGHT, self._compact_page.sizeHint().height() + 24
            )
            self._compact_bar_height = height
            self.setFixedHeight(height)
            if initial:
                saved = self._qsettings.value("ui/compact_geometry")
                restored = False
                if saved is not None:
                    try:
                        restored = self.restoreGeometry(saved)
                    except Exception:
                        restored = False
                if not restored:
                    available = self._screen_available()
                    self.resize(min(800, max(560, available.width() - 40)), height)
            else:
                available = self._screen_available()
                self.resize(max(700, min(900, available.width() - 40)), height)
            # restoreGeometry() may restore a taller window; keep the slim bar.
            self.resize(self.width(), height)
            self._size_grip.hide()
            self._ensure_on_screen()
            self._save_geometry()
            return

        self._compact = False
        self._qsettings.setValue("ui/compact", False)
        self._stack.setCurrentWidget(self._full_page)
        self._outer_layout.setContentsMargins(22, 18, 22, 18)
        self.setMinimumWidth(420)
        self.setMinimumHeight(0)
        self.setMaximumHeight(16777215)
        self._size_grip.show()

        expanded_size, self._compact_restore_size = self._compact_restore_size, None
        if expanded_size is not None:
            self.resize(expanded_size)
        else:
            saved = self._qsettings.value("ui/geometry")
            restored = False
            if saved is not None:
                try:
                    restored = self.restoreGeometry(saved)
                except Exception:
                    restored = False
            if not restored:
                self._resize_full_default()
        self._ensure_on_screen()
        self._save_geometry()

    # ------------------------------------------------------- compact detail panel
    def _compact_detail_width(self) -> int:
        """Usable text width inside the compact detail panel."""
        return max(180, self.width() - 60)

    def _wrapped_text_height(self, label: QLabel, text: str, width: int) -> int:
        metrics = label.fontMetrics()
        rect = metrics.boundingRect(
            QRect(0, 0, width, 100_000),
            Qt.TextFlag.TextWordWrap,
            text,
        )
        return max(metrics.height(), rect.height())

    def _compact_detail_target_height(self, english: str, persian: str) -> int:
        width = self._compact_detail_width()
        english_height = self._wrapped_text_height(
            self._compact_detail_english, english, width
        )
        persian_height = self._wrapped_text_height(
            self._compact_detail_persian, persian, width
        )
        # top row + outer margins + panel chrome + both wrapped texts
        detail_height = 9 + english_height + 6 + persian_height + 10 + 6
        total = 24 + 30 + 8 + detail_height
        available = self._screen_available()
        maximum = max(COMPACT_BASE_HEIGHT + 80, available.height() - 24)
        return max(COMPACT_BASE_HEIGHT, min(total, maximum))

    def _compact_text_needs_detail(self, english: str, persian: str) -> bool:
        """True when either part of the pair cannot fit on one compact row."""
        if "\n" in english or "\n" in persian:
            return True
        if self._compact_detail_visible:
            # The one-line labels are hidden while the detail panel is open, so
            # estimate their width instead of reading stale geometry.
            share = max(90, (self.width() - 300) // 2)
            english_width = persian_width = share
        else:
            english_width = self._compact_english.width()
            persian_width = self._compact_persian.width()
            if english_width < 80 or persian_width < 80:
                # The layout has not run yet (e.g. a very early result); estimate
                # each label's share of the row instead.
                share = max(90, (self.width() - 300) // 2)
                english_width = persian_width = share
        return (
            self._compact_english.fontMetrics().horizontalAdvance(english)
            > english_width
            or self._compact_persian.fontMetrics().horizontalAdvance(persian)
            > persian_width
        )

    def _maybe_expand_compact(self, english: str, persian: str) -> None:
        """Temporarily enlarge the mini bar so a long pair is fully readable."""
        if not self._compact or not self.isVisible():
            return
        if not self._compact_text_needs_detail(english, persian):
            self._collapse_compact_detail()
            return
        self._compact_detail_english.setText(english)
        self._compact_detail_persian.setText(persian)
        self._compact_english.hide()
        self._compact_arrow.hide()
        self._compact_persian.hide()
        self._compact_caption.show()
        self._compact_detail.show()
        self._compact_detail_visible = True
        height = self._compact_detail_target_height(english, persian)
        self.setFixedHeight(height)
        self.resize(self.width(), height)
        self._ensure_on_screen()
        self._compact_detail_timer.start(COMPACT_DETAIL_TIMEOUT_MS)

    def _collapse_compact_detail(self) -> None:
        """Return the mini bar to its one-line height."""
        self._compact_detail_timer.stop()
        if not self._compact_detail_visible:
            return
        self._compact_detail_visible = False
        self._compact_detail.hide()
        self._compact_caption.hide()
        self._compact_english.show()
        self._compact_arrow.show()
        self._compact_persian.show()
        if self._compact:
            height = max(
                COMPACT_BASE_HEIGHT, self._compact_bar_height
            )
            self.setFixedHeight(height)
            self.resize(self.width(), height)

    def _compact_detail_timeout(self) -> None:
        # Keep a long translation open while the pointer rests on the overlay,
        # so it does not disappear in the middle of reading.
        if self.isVisible() and self.underMouse():
            self._compact_detail_timer.start(COMPACT_DETAIL_TIMEOUT_MS)
            return
        self._collapse_compact_detail()

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._compact and self._compact_detail_visible:
            self._compact_detail_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._compact and self._compact_detail_visible:
            self._compact_detail_timer.start(COMPACT_DETAIL_TIMEOUT_MS)
        super().leaveEvent(event)

    def _restore_geometry(self) -> None:
        screen = QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen else QRect(0, 0, 1280, 800)
        width = min(self._settings.window_width, max(420, available.width() - 40))
        height = min(
            self._settings.window_height, max(480, available.height() - 60)
        )
        self.resize(width, height)

        saved = self._qsettings.value("ui/geometry")
        if saved is not None:
            try:
                self.restoreGeometry(saved)
            except Exception:
                pass

        # Keep the window on a connected screen after monitor changes.
        center = self.geometry().center()
        if screen is not None and not screen.availableGeometry().contains(center):
            on_screen = any(
                s.availableGeometry().contains(center)
                for s in QGuiApplication.screens()
            )
            if not on_screen:
                self.move(available.center() - self.rect().center())

        if "wayland" not in QGuiApplication.platformName().lower():
            if saved is None:
                self.move(
                    available.right() - self.width() - 24,
                    available.bottom() - self.height() - 24,
                )

    # ---------------------------------------------------------------------- tray
    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._toggle_visible()

    def _toggle_visible(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show()
            self.raise_()

    def _on_minimize(self) -> None:
        if self._tray is not None:
            self.hide()
            if not self._tray_notice_shown:
                self._tray.showMessage(
                    APP_NAME,
                    "Still running. Use the tray icon to reopen or quit.",
                    QSystemTrayIcon.MessageIcon.Information,
                    3000,
                )
                self._tray_notice_shown = True
            return
        if self._compact:
            self._set_compact(False)
        self._toggle_collapse()

    def _toggle_collapse(self) -> None:
        if self._collapsed:
            self._body.show()
            self._collapsed = False
            if self._expanded_size is not None:
                self.resize(self._expanded_size)
            self._size_grip.show()
        else:
            self._expanded_size = self.size()
            self._body.hide()
            self._collapsed = True
            self._size_grip.hide()
            self.layout().activate()
            self.resize(self.width(), self.sizeHint().height())

    # ------------------------------------------------------------------- public API
    def set_ptt_key(self, ptt_label: str) -> None:
        self._ptt_label = ptt_label
        self._subtitle.setText(f"EN → FA  ·  Hold {ptt_label}")
        self._hold_button.setText(f"Hold to Talk   ·   {ptt_label}")
        self._hold_button.setToolTip(
            f"Hold {ptt_label} anywhere, or hold this button"
        )
        if not self._english_value:
            self._compact_english.set_full_text(f"Hold {ptt_label} or type")
        self._compact_hold.setToolTip(
            f"Hold {ptt_label} anywhere, or hold this button"
        )

    def set_provider_label(self, label: str) -> None:
        self._provider_badge.setText(label)

    def set_state(self, state: str, detail: str | None = None, queued: int = 0) -> None:
        state = state if state in STATE_COLORS else "idle"
        self._state = state
        if state == "translating" and queued > 0:
            self._status_pill.set_state(state, f"Translating +{queued}")
        else:
            self._status_pill.set_state(state)
        self._compact_dot.set_state(
            STATE_COLORS[state], state in {"listening", "translating"}
        )
        self._compact_dot.setToolTip(detail or STATE_TEXT[state])
        self._set_hold_listening(state == "listening")
        if state == "listening":
            self._collapse_compact_detail()

        if state == "error":
            self.show_notice(detail or "Something went wrong.", "error")
        elif state == "warning":
            self.show_notice(detail or "Limited functionality.", "warning")

    def set_result(self, english: str, persian: str) -> None:
        english = english.strip()
        persian = persian.strip()
        if not english or not persian:
            return
        self._english_value = english
        self._persian_value = persian

        self._english_label.setProperty("placeholder", False)
        self._english_label.setText(english)
        self._repolish(self._english_label)

        self._persian_label.setProperty("placeholder", False)
        self._persian_label.setText(persian)
        self._repolish(self._persian_label)

        self._compact_english.set_full_text(english)
        self._compact_persian.set_full_text(persian)

        self._copy_english.setEnabled(True)
        self._copy_persian.setEnabled(True)
        self.add_history(english, persian)
        if self._notice_kind == "error":
            self.clear_notice()
        self.set_state("idle")
        if self._compact:
            self._maybe_expand_compact(english, persian)

    def add_history(self, english: str, persian: str) -> None:
        card = HistoryCard(english, persian, datetime.now(), self._history_content)
        self._history_layout.insertWidget(0, card)
        self._history_cards.insert(0, card)
        self._empty_state.hide()
        while len(self._history_cards) > self._settings.history_limit:
            oldest = self._history_cards.pop()
            self._history_layout.removeWidget(oldest)
            oldest.deleteLater()
        self._history_count.setText(str(len(self._history_cards)))
        if not self._history_cards:
            self._empty_state.show()
        self._history_scroll.verticalScrollBar().setValue(0)

    def clear_history(self) -> None:
        for card in self._history_cards:
            self._history_layout.removeWidget(card)
            card.deleteLater()
        self._history_cards.clear()
        self._history_count.setText("0")
        self._empty_state.show()

    def show_notice(
        self, text: str, kind: str = "warning", timeout_ms: int = 0
    ) -> None:
        self._notice_kind = kind
        self._notice_timer.stop()
        self._notice.show_message(text, kind)
        if timeout_ms > 0:
            self._notice_timer.start(timeout_ms)

    def clear_notice(self) -> None:
        self._notice_timer.stop()
        self._notice_kind = ""
        self._notice.hide()

    @property
    def notice_kind(self) -> str:
        """Kind of the currently configured notice ("" when none)."""
        return self._notice_kind

    def _notice_timeout(self) -> None:
        self._notice_kind = ""
        self._notice.hide()

    def set_microphones(self, devices: "list[InputDevice]") -> None:
        saved_name = str(self._qsettings.value("audio/device_name", "") or "")
        self._mic_combo.blockSignals(True)
        self._mic_combo.clear()
        self._mic_combo.addItem("System default microphone", -1)
        self._mic_combo.setItemData(
            0, "Follow the PipeWire/PulseAudio default input", Qt.ItemDataRole.ToolTipRole
        )
        duplicate_names = {
            d.name for d in devices if [x.name for x in devices].count(d.name) > 1
        }
        selected_index = 0
        for device in devices:
            label = device.name
            if device.name in duplicate_names and device.host_api:
                label = f"{device.name} · {device.host_api}"
            self._mic_combo.addItem(label, device.index)
            row = self._mic_combo.count() - 1
            self._mic_combo.setItemData(
                row,
                f"{device.host_api or 'Audio'} · {device.channels} input channel(s)",
                Qt.ItemDataRole.ToolTipRole,
            )
            self._mic_combo.setItemData(
                row, device.name, Qt.ItemDataRole.UserRole + 1
            )
            if saved_name and device.name == saved_name:
                selected_index = row
        self._mic_combo.setCurrentIndex(selected_index)
        self._mic_combo.blockSignals(False)

    def selected_microphone(self) -> int:
        data = self._mic_combo.currentData()
        try:
            return int(data)
        except (TypeError, ValueError):
            return -1

    def set_microphone_enabled(self, enabled: bool) -> None:
        self._mic_combo.setEnabled(enabled)

    def saved_opacity(self) -> int:
        return int(round(self._panel_opacity * 100))

    # ------------------------------------------------------------------- handlers
    def _on_mic_changed(self, index: int) -> None:
        if index < 0:
            return
        name = self._mic_combo.itemData(index, Qt.ItemDataRole.UserRole + 1)
        if name:
            self._qsettings.setValue("audio/device_name", str(name))
        self._mic_combo.setToolTip(
            f"Microphone: {self._mic_combo.itemText(index)}"
        )
        self.mic_changed.emit(self.selected_microphone())

    def _on_opacity_changed(self, value: int) -> None:
        self._panel_opacity = max(36, min(100, int(value))) / 100.0
        self._qsettings.setValue("ui/opacity", int(value))
        self._update_opacity_ui()
        self.opacity_changed.emit(int(value))

    def _update_opacity_ui(self) -> None:
        self._opacity_slider.setValue(int(round(self._panel_opacity * 100)))
        self.update()

    def _set_hold_listening(self, listening: bool) -> None:
        if (
            self._hold_button.property("listening") == listening
            and self._compact_hold.property("listening") == listening
        ):
            return
        self._hold_button.setProperty("listening", listening)
        self._compact_hold.setProperty("listening", listening)
        if listening:
            self._hold_button.setText("Listening…  release to translate")
            self._compact_hold.setText("Rec")
        else:
            self._hold_button.setText(f"Hold to Talk   ·   {self._ptt_label}")
            self._compact_hold.setText("Hold")
        _repolish(self._hold_button)
        _repolish(self._compact_hold)

    def _submit_manual(self) -> None:
        text = self._manual_input.text().strip()
        if not text:
            self._manual_input.setFocus()
            return
        self.manual_submitted.emit(text)
        # Keep the text so it can be corrected, but make the next keystroke
        # replace it instead of appending.
        self._manual_input.selectAll()
        self._manual_input.setFocus()

    def _on_clear_clicked(self) -> None:
        if self._clear_button.property("armed"):
            self.clear_history()
            self._reset_clear_button()
            self.show_notice("History cleared.", "info", timeout_ms=2200)
            return
        self._clear_button.setProperty("armed", True)
        self._clear_button.setText("Sure?")
        _repolish(self._clear_button)
        self._clear_reset_timer.start(3200)

    def _reset_clear_button(self) -> None:
        self._clear_button.setProperty("armed", False)
        self._clear_button.setText("Clear")
        _repolish(self._clear_button)

    def _copy_english_text(self) -> None:
        if self._english_value:
            QApplication.clipboard().setText(self._english_value)
            self._flash_copy_button(self._copy_english)

    def _copy_persian_text(self) -> None:
        if self._persian_value:
            QApplication.clipboard().setText(self._persian_value)
            self._flash_copy_button(self._copy_persian)

    def _flash_copy_button(self, button: QToolButton) -> None:
        original = button.text()
        button.setText("Copied")
        QTimer.singleShot(1200, lambda: _set_button_text_safe(button, original))

    @staticmethod
    def _repolish(widget: QWidget) -> None:
        _repolish(widget)

    # ---------------------------------------------------------------------- exit
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        log.info("Overlay closed by user")
        self._collapse_compact_detail()
        self._save_geometry()
        if self._tray is not None:
            self._tray.hide()
        self.quit_requested.emit()
        event.accept()
