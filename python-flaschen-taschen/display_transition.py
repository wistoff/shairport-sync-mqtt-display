"""Cancellable frame transitions for the pixel display."""

from __future__ import annotations

import threading
from collections.abc import Callable

from PIL import Image


class DisplayTransition:
    """Serialize display writes and crossfade between stable RGBA frames."""

    def __init__(
        self,
        send: Callable[[Image.Image], None],
        size: tuple[int, int],
        *,
        duration: float = 0.45,
        fps: float = 20.0,
        wait: Callable[[float], bool] | None = None,
    ):
        self._send = send
        self._size = size
        self._duration = max(0.0, duration)
        self._fps = max(1.0, fps)
        self._wait = wait
        self._lock = threading.RLock()
        self._generation = 0
        self._current = None

    def snapshot(self) -> Image.Image | None:
        with self._lock:
            return self._current.copy() if self._current is not None else None

    def cancel(self):
        with self._lock:
            self._generation += 1

    def show(self, image: Image.Image):
        """Cancel any transition and show a frame immediately."""
        frame = self._prepare(image)
        with self._lock:
            self._generation += 1
            self._current = frame
            self._send(frame)

    def crossfade(
        self,
        image: Image.Image,
        *,
        duration: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        """Crossfade to image; return False if superseded or cancelled."""
        target = self._prepare(image)
        seconds = self._duration if duration is None else max(0.0, duration)
        steps = max(1, round(seconds * self._fps))
        interval = seconds / steps if steps else 0.0

        with self._lock:
            self._generation += 1
            generation = self._generation
            start = self._current.copy() if self._current is not None else Image.new(
                "RGBA", self._size, (0, 0, 0, 255)
            )

        for step in range(1, steps + 1):
            if cancel_event is not None and cancel_event.is_set():
                return False
            frame = Image.blend(start, target, step / steps)
            with self._lock:
                if generation != self._generation:
                    return False
                self._current = frame
                self._send(frame)
            if step < steps and self._wait_for(interval, cancel_event):
                return False
        return True

    def _prepare(self, image: Image.Image) -> Image.Image:
        frame = image.convert("RGBA")
        if frame.size != self._size:
            raise ValueError(f"Frame size {frame.size} does not match {self._size}")
        return frame

    def _wait_for(self, seconds, cancel_event):
        if self._wait is not None:
            return bool(self._wait(seconds))
        if cancel_event is not None:
            return cancel_event.wait(seconds)
        threading.Event().wait(seconds)
        return False
