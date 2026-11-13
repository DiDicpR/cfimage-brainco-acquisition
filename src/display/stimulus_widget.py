from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QRect, Qt, Signal, QTimer
from PySide6.QtGui import QColor, QFont, QGuiApplication, QKeyEvent, QPainter, QPixmap, QSurfaceFormat
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QWidget


@dataclass(frozen=True)
class PresentedFrame:
    token: int
    frame_index: int
    timestamp_ns: int


class _StimulusScene:
    """Resizable stimulus renderer with a simple frame-swapped contract."""

    scene_presented = Signal(object)
    key_pressed = Signal(str)
    close_requested = Signal()

    def __init__(
        self,
        *,
        background_rgb: list[int],
        max_image_fraction: float,
        screen_index: int = 0,
        image_cache_size: int = 64,
        protocol_image_mode: bool = False,
    ) -> None:
        super().__init__()
        self._initialize_backend()
        self._background = QColor(*background_rgb)
        self._max_image_fraction = float(max_image_fraction)
        self._screen_index = int(screen_index)
        self._image_cache_size = max(1, int(image_cache_size))
        self._protocol_image_mode = protocol_image_mode
        self._pixmaps: OrderedDict[str, QPixmap] = OrderedDict()
        self._scaled_pixmaps: OrderedDict[tuple[int, int, int], QPixmap] = OrderedDict()
        self._current_image: QPixmap | None = None
        self._current_pair: tuple[QPixmap, QPixmap] | None = None
        self._show_fixation = True
        self._message = ""
        self._instruction: tuple[str, str, str] | None = None
        self._token = 0
        self._painted_token: int | None = None
        self._paint_serial = 0
        self._swapped_paint_serial = 0
        self._frame_index = 0
        self.setWindowTitle("THINGS-EEG2-Extended 刺激窗口（F11 切换全屏）")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def place_on_screen(self) -> None:
        """Bind the window to the configured screen before it is shown.

        Must be called before show/showFullScreen so that fullscreen presentation
        targets the acquisition display instead of the operator's console screen.
        Raises ValueError when the configured index has no matching screen, which
        keeps a mistyped screen_index from silently presenting on the wrong one.
        """
        screens = QGuiApplication.screens()
        if not 0 <= self._screen_index < len(screens):
            names = ", ".join(f"{index}: {screen.name()}" for index, screen in enumerate(screens))
            raise ValueError(
                f"display.screen_index={self._screen_index} is out of range; "
                f"available screens: {names or 'none'}"
            )
        target = screens[self._screen_index]
        self.setScreen(target)
        self.move(target.geometry().topLeft())

    def show_windowed(self, preferred_width: int = 1280, preferred_height: int = 800) -> None:
        """Open as a movable window centred within the target screen."""
        screen = self.screen()
        if screen is None:
            raise RuntimeError("The stimulus window has no target screen")
        available = screen.availableGeometry()
        width = min(preferred_width, max(1, int(available.width() * 0.9)))
        height = min(preferred_height, max(1, int(available.height() * 0.9)))
        self.resize(width, height)
        self.show()
        frame = self.frameGeometry()
        frame.moveCenter(available.center())
        self.move(frame.topLeft())

    def toggle_fullscreen(self) -> None:
        """Toggle true fullscreen on whichever screen currently owns the window."""
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def preload(self, paths: list[str]) -> list[str]:
        missing: list[str] = []
        for value in dict.fromkeys(paths):
            path = str(Path(value))
            pixmap = QPixmap(path)
            if pixmap.isNull():
                missing.append(path)
            else:
                self._cache_image(path, pixmap)
        return missing

    def _cache_image(self, path: str, pixmap: QPixmap) -> None:
        if self._protocol_image_mode and self.screen():
            size = self.screen().size() * self.devicePixelRatioF()
            pixmap = pixmap.scaled(max(1, int(size.width() * self._max_image_fraction)),
                max(1, int(size.height() * self._max_image_fraction)),
                Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self._pixmaps[path] = pixmap
        self._pixmaps.move_to_end(path)
        while len(self._pixmaps) > self._image_cache_size:
            self._pixmaps.popitem(last=False)

    def show_fixation(self, message: str = "") -> int:
        self._current_image = None
        self._show_fixation = True
        self._message = message
        self._instruction = None
        self._current_pair = None
        return self._advance_scene()

    def show_image(self, image_path: str) -> int:
        pixmap = self._pixmaps.get(image_path)
        if pixmap is None:
            pixmap = QPixmap(image_path)
            if pixmap.isNull():
                raise FileNotFoundError(f"Could not load stimulus image: {image_path}")
            self._cache_image(image_path, pixmap)
            pixmap = self._pixmaps[image_path]
        else:
            self._pixmaps.move_to_end(image_path)
        self._current_image = pixmap
        self._show_fixation = not self._protocol_image_mode
        self._message = ""
        self._instruction = None
        self._current_pair = None
        return self._advance_scene()

    def show_question(self, text: str) -> int:
        self._current_image = None
        self._show_fixation = False
        self._message = text
        self._instruction = None
        self._current_pair = None
        return self._advance_scene()

    def show_instruction(self, title: str, body: str, prompt: str) -> int:
        """Show the pre-session page; it is separate from task stimuli."""
        self._current_image = None
        self._show_fixation = False
        self._message = ""
        self._instruction = (title, body, prompt)
        self._current_pair = None
        return self._advance_scene()

    def show_pair(self, left_path: str, right_path: str) -> int:
        """Load both images before requesting one simultaneous presented frame."""
        images = []
        for path in (left_path, right_path):
            pixmap = self._pixmaps.get(path)
            if pixmap is None:
                pixmap = QPixmap(path)
                if pixmap.isNull():
                    raise FileNotFoundError(f"Could not load stimulus image: {path}")
                self._cache_image(path, pixmap)
            else:
                self._pixmaps.move_to_end(path)
            images.append(pixmap)
        self._current_image = None
        self._current_pair = (images[0], images[1])
        self._show_fixation = False
        self._message = ""
        self._instruction = None
        return self._advance_scene()

    def show_blank(self) -> int:
        self._current_image = None
        self._current_pair = None
        self._show_fixation = False
        self._message = ""
        self._instruction = None
        return self._advance_scene()

    def _advance_scene(self) -> int:
        self._token += 1
        self.update()
        return self._token

    def _on_frame_swapped(self) -> None:
        # Window composition can swap without painting this widget again. Never
        # acknowledge a requested scene until paintGL has actually drawn it.
        if self._painted_token is None or self._paint_serial == self._swapped_paint_serial:
            return
        self._swapped_paint_serial = self._paint_serial
        self._frame_index += 1
        self.scene_presented.emit(PresentedFrame(self._painted_token, self._frame_index, time.monotonic_ns()))

    def _scaled_for_display(self, pixmap: QPixmap, width: int, height: int) -> QPixmap:
        width, height = max(1, width), max(1, height)
        size = pixmap.size().scaled(width, height, Qt.AspectRatioMode.KeepAspectRatio)
        if size == pixmap.size():
            return pixmap
        key = (pixmap.cacheKey(), width, height)
        scaled = self._scaled_pixmaps.get(key)
        if scaled is None:
            scaled = pixmap.scaled(size, Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
            self._scaled_pixmaps[key] = scaled
            while len(self._scaled_pixmaps) > self._image_cache_size:
                self._scaled_pixmaps.popitem(last=False)
        else:
            self._scaled_pixmaps.move_to_end(key)
        return scaled

    def _paint_scene(self) -> None:
        painted_token = self._token
        painter = QPainter(self)
        painter.fillRect(self.rect(), self._background)
        if self._current_pair is not None:
            half = self.width() // 2
            maximum = max(1, int(min(half, self.height()) * self._max_image_fraction))
            for index, pixmap in enumerate(self._current_pair):
                scaled = self._scaled_for_display(pixmap, maximum, maximum)
                painter.drawPixmap(index * half + (half - scaled.width()) // 2,
                    (self.height() - scaled.height()) // 2, scaled)
        if self._current_image is not None:
            maximum = int(min(self.width(), self.height()) * self._max_image_fraction)
            scaled = self._scaled_for_display(self._current_image,
                int(self.width() * self._max_image_fraction) if self._protocol_image_mode else maximum,
                int(self.height() * self._max_image_fraction) if self._protocol_image_mode else maximum,
            )
            x = (self.width() - scaled.width()) // 2
            y = (self.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        if self._show_fixation:
            painter.setPen(QColor("white"))
            fixation_font = QFont("Microsoft YaHei")
            fixation_font.setPixelSize(32)
            painter.setFont(fixation_font)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "+")
        if self._instruction:
            title, body, prompt = self._instruction
            centre_y = self.height() // 2
            painter.setPen(QColor("white"))
            title_font = QFont("Microsoft YaHei")
            title_font.setPixelSize(32)
            painter.setFont(title_font)
            painter.drawText(QRect(0, centre_y - 140, self.width(), 48), Qt.AlignmentFlag.AlignCenter, title)
            body_font = QFont("Microsoft YaHei")
            body_font.setPixelSize(22)
            painter.setFont(body_font)
            painter.drawText(QRect(80, centre_y - 70, self.width() - 160, 140), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, body)
            prompt_font = QFont("Microsoft YaHei")
            prompt_font.setPixelSize(18)
            painter.setFont(prompt_font)
            painter.drawText(QRect(0, centre_y + 100, self.width(), 34), Qt.AlignmentFlag.AlignCenter, prompt)
        if self._message:
            painter.setPen(QColor("white"))
            message_font = QFont("Microsoft YaHei")
            message_font.setPixelSize(30)
            painter.setFont(message_font)
            message_rect = self.rect().adjusted(80, 80, -80, -80)
            painter.drawText(message_rect, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self._message)
        painter.end()
        self._painted_token = painted_token
        self._paint_serial += 1

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.isAutoRepeat():
            return
        if event.key() == Qt.Key.Key_F11:
            self.toggle_fullscreen()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.key_pressed.emit("escape")
            return
        if event.key() in {Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter}:
            self.key_pressed.emit("space")
            return
        text = event.text().lower()
        if text:
            self.key_pressed.emit(text)
        else:
            super().keyPressEvent(event)

    def closeEvent(self, event) -> None:
        # Window X must take the same save path as Esc, never bypass it.
        if self.receivers("2close_requested()"):
            event.ignore()
            self.close_requested.emit()
        else:
            super().closeEvent(event)


class StimulusWidget(_StimulusScene, QOpenGLWidget):
    scene_presented = Signal(object)
    key_pressed = Signal(str)
    close_requested = Signal()
    presentation_backend = 'opengl_swap'

    def _initialize_backend(self):
        surface = self.format()
        surface.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
        surface.setSwapInterval(1)
        self.setFormat(surface)
        self.setUpdateBehavior(QOpenGLWidget.UpdateBehavior.NoPartialUpdate)
        self.frameSwapped.connect(self._on_frame_swapped)

    def paintGL(self):
        self._paint_scene()


class RasterStimulusWidget(_StimulusScene, QWidget):
    """Opaque raster backing store; receipts describe paint completion, not vsync."""
    scene_presented = Signal(object)
    key_pressed = Signal(str)
    close_requested = Signal()
    presentation_backend = 'raster_paint_completion'

    def _initialize_backend(self):
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAutoFillBackground(False)

    def paintEvent(self, event):
        self._paint_scene()
        # Run after paintEvent returns so state changes never occur inside a
        # paint operation. Capture the drawn token, not a future scene request.
        token, serial, stamp = self._painted_token, self._paint_serial, time.monotonic_ns()
        def acknowledge():
            if serial <= self._swapped_paint_serial:
                return
            self._swapped_paint_serial = serial
            self._frame_index += 1
            self.scene_presented.emit(PresentedFrame(token, self._frame_index, stamp))
        QTimer.singleShot(0, self, acknowledge)
