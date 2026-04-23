"""QThread proxy in front of the subprocess repair runner.

The real pipeline lives in `process_worker.run_batch`, executed in a child
process so its CPU load (voxel remesh, MeshFix, numpy sort) cannot starve the
UI thread of the GIL. This QThread just drains a multiprocessing.Queue and
re-emits events as Qt signals, preserving the API `app.py` depended on.
"""

from __future__ import annotations

import multiprocessing as mp
import queue as queue_mod
import time
import traceback
from typing import Optional

from PySide6.QtCore import QThread, Signal

from .process_worker import (
    EV_BATCH_ERROR,
    EV_BATCH_FINISHED,
    EV_DONE,
    EV_FILE_FINISHED,
    EV_FILE_PHASE,
    EV_FILE_STARTED,
    run_batch,
)
from .repair import RepairResult


class RepairWorker(QThread):
    file_started = Signal(int, int, str)          # (index, total, filename)
    file_phase = Signal(int, int, str, str)       # (index, total, filename, phase_label)
    file_finished = Signal(object)                # RepairResult
    batch_finished = Signal(int, int, list)       # (success_count, total, failed_filenames)
    batch_error = Signal(str)

    # Grace period after cancel before we SIGTERM the child.
    _CANCEL_GRACE_SECS = 5.0

    def __init__(self, files: list[str], output_dir: str, scale: float,
                 include_unmodified: bool = False, overwrite: bool = False,
                 simplify_large: bool = False):
        """
        Args:
            files: absolute paths to mesh files to repair.
            output_dir: directory to write repaired files into (created if missing).
            scale: unit conversion factor applied at import.
            include_unmodified: copy already-manifold files into the output folder.
            overwrite: if False, suffix duplicates ("name (1).obj", "name (2).obj", ...).
            simplify_large: run quadric decimation on meshes with >1M faces
                before repair. Much faster; loses some surface detail.
        """
        super().__init__()
        self.files = list(files)
        self.output_dir = output_dir
        self.scale = scale
        self.include_unmodified = include_unmodified
        self.overwrite = overwrite
        self.simplify_large = simplify_large

        # Explicit spawn context — default on macOS/Windows; fork() after Qt
        # init crashes on Darwin, so never rely on the platform default.
        self._ctx = mp.get_context("spawn")
        self._cancel_event = self._ctx.Event()
        self._event_q: "mp.Queue" = self._ctx.Queue()
        self._proc: Optional["mp.Process"] = None

    def cancel(self) -> None:
        """Signal the child to stop between phases. If it doesn't exit within
        the grace period, the drain loop terminates it."""
        self._cancel_event.set()

    def run(self) -> None:
        try:
            self._proc = self._ctx.Process(
                target=run_batch,
                args=(self.files, self.output_dir, self.scale,
                      self.include_unmodified, self.overwrite,
                      self.simplify_large,
                      self._event_q, self._cancel_event),
                daemon=True,
            )
            self._proc.start()
            self._drain_queue()
        except Exception as e:
            self.batch_error.emit(f"{e}\n{traceback.format_exc()}")
        finally:
            self._cleanup_child()

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _drain_queue(self) -> None:
        """Pull events off the queue and re-emit as Qt signals until the child
        signals EV_DONE or dies. Short timeout keeps cancel latency bounded."""
        cancel_deadline: Optional[float] = None
        while True:
            try:
                msg = self._event_q.get(timeout=0.1)
            except queue_mod.Empty:
                # Child died without sending EV_DONE?
                if self._proc is not None and not self._proc.is_alive():
                    # Give the queue one last chance to flush.
                    try:
                        msg = self._event_q.get_nowait()
                    except queue_mod.Empty:
                        self.batch_error.emit(
                            "Repair process exited unexpectedly "
                            f"(exit code {self._proc.exitcode})."
                        )
                        return
                else:
                    # Enforce cancel grace period.
                    if self._cancel_event.is_set():
                        now = time.monotonic()
                        if cancel_deadline is None:
                            cancel_deadline = now + self._CANCEL_GRACE_SECS
                        elif (now > cancel_deadline and self._proc is not None
                              and self._proc.is_alive()):
                            self._proc.terminate()
                            cancel_deadline = None
                    continue

            tag = msg[0]
            if tag == EV_DONE:
                return
            if tag == EV_FILE_STARTED:
                _, idx, total, name = msg
                self.file_started.emit(idx, total, name)
            elif tag == EV_FILE_PHASE:
                _, idx, total, name, phase = msg
                self.file_phase.emit(idx, total, name, phase)
            elif tag == EV_FILE_FINISHED:
                _, result_dict = msg
                self.file_finished.emit(RepairResult(**result_dict))
            elif tag == EV_BATCH_FINISHED:
                _, success, total, failed = msg
                self.batch_finished.emit(success, total, failed)
            elif tag == EV_BATCH_ERROR:
                _, err_msg = msg
                self.batch_error.emit(err_msg)

    def _cleanup_child(self) -> None:
        if self._proc is None:
            return
        if self._proc.is_alive():
            self._proc.terminate()
            self._proc.join(timeout=2)
            if self._proc.is_alive():
                self._proc.kill()
                self._proc.join(timeout=1)
        try:
            self._event_q.close()
            self._event_q.join_thread()
        except Exception:
            pass
