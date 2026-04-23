"""QThread proxy in front of the subprocess repair runners.

The real pipeline lives in `process_worker.worker_loop`, executed in N child
processes. Each worker pulls one file at a time from a shared tasks queue so
CPU load is spread across cores while the UI process stays at 60 fps. This
QThread spawns the workers, drains their shared events queue, and re-emits
events as Qt signals — the UI-side API (`file_started`, `file_phase`,
`file_finished`, `batch_finished`, `batch_error`) is unchanged from v0.1.3.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import queue as queue_mod
import time
import traceback
from typing import Optional

from PySide6.QtCore import QThread, Signal

from .process_worker import (
    EV_BATCH_ERROR,
    EV_FILE_FINISHED,
    EV_FILE_PHASE,
    EV_FILE_STARTED,
    worker_loop,
)
from .repair import RepairResult

# Half the cores, capped at 4. Half because trimesh/PyTMesh C extensions
# likely use OpenMP internally — running one worker per logical core
# over-subscribes and memory grows linearly. Cap at 4 to stay safe on
# 16 GB Macs when a batch contains several dense meshes.
_N_WORKERS = max(1, min((os.cpu_count() or 2) // 2, 4))


class RepairWorker(QThread):
    file_started = Signal(int, int, str)          # (index, total, filename)
    file_phase = Signal(int, int, str, str)       # (index, total, filename, phase_label)
    file_finished = Signal(object)                # RepairResult
    batch_finished = Signal(int, int, list)       # (success_count, total, failed_filenames)
    batch_error = Signal(str)

    # Grace period after cancel before we SIGTERM the children.
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
        self._events_q: "mp.Queue" = self._ctx.Queue()
        self._tasks_q: "mp.Queue" = self._ctx.Queue()
        self._procs: list["mp.Process"] = []

        # Cap workers at the number of files so we don't spawn idle processes.
        self._n_workers = min(_N_WORKERS, max(1, len(self.files)))

        # Batch-completion accounting — computed on the parent side since no
        # single child owns the whole batch now.
        self._success_count = 0
        self._failed_names: list[str] = []

    def cancel(self) -> None:
        """Signal all children to stop between phases. If they don't exit
        within the grace period, `_cleanup_children` terminates them."""
        self._cancel_event.set()

    def run(self) -> None:
        try:
            if not self.files:
                self.batch_error.emit("No mesh files to repair.")
                return

            # Populate the task queue before starting workers so they see work
            # immediately (avoids an initial empty-queue spin).
            total = len(self.files)
            for idx, src in enumerate(self.files, start=1):
                self._tasks_q.put((src, idx, total))
            # Sentinels — one per worker — tell each loop to exit after its
            # current file finishes.
            for _ in range(self._n_workers):
                self._tasks_q.put(None)

            for _ in range(self._n_workers):
                p = self._ctx.Process(
                    target=worker_loop,
                    args=(self._tasks_q, self._events_q, self._cancel_event,
                          self.output_dir, self.scale,
                          self.include_unmodified, self.overwrite,
                          self.simplify_large),
                    daemon=True,
                )
                p.start()
                self._procs.append(p)

            self._drain_events(total)

            # Emit the summary signal. Whatever isn't in _success_count or
            # _failed_names was cancelled before it started — call those
            # failures from the UI's perspective so counts add up.
            unfinished = total - self._success_count - len(self._failed_names)
            if unfinished > 0:
                # Pad with placeholder names so the UI's failure list reflects
                # the right count even if we don't know which files got
                # skipped (cancel cut them off before they emitted events).
                self._failed_names.extend([""] * unfinished)
            self.batch_finished.emit(self._success_count, total, self._failed_names)
        except Exception as e:
            self.batch_error.emit(f"{e}\n{traceback.format_exc()}")
        finally:
            self._cleanup_children()

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _drain_events(self, total: int) -> None:
        """Pull events off the shared queue and re-emit as Qt signals until
        either every file has produced an EV_FILE_FINISHED, or every worker
        has exited, or cancel + grace expired."""
        files_done = 0
        cancel_deadline: Optional[float] = None
        while files_done < total:
            try:
                msg = self._events_q.get(timeout=0.1)
            except queue_mod.Empty:
                # All workers dead? Probably done or all crashed.
                alive = any(p.is_alive() for p in self._procs)
                if not alive:
                    # One last flush attempt.
                    try:
                        msg = self._events_q.get_nowait()
                    except queue_mod.Empty:
                        return
                else:
                    if self._cancel_event.is_set():
                        now = time.monotonic()
                        if cancel_deadline is None:
                            cancel_deadline = now + self._CANCEL_GRACE_SECS
                        elif now > cancel_deadline:
                            for p in self._procs:
                                if p.is_alive():
                                    p.terminate()
                            cancel_deadline = None
                    continue

            tag = msg[0]
            if tag == EV_FILE_STARTED:
                _, idx, total_in_event, name = msg
                self.file_started.emit(idx, total_in_event, name)
            elif tag == EV_FILE_PHASE:
                _, idx, total_in_event, name, phase = msg
                self.file_phase.emit(idx, total_in_event, name, phase)
            elif tag == EV_FILE_FINISHED:
                _, result_dict = msg
                result = RepairResult(**result_dict)
                self.file_finished.emit(result)
                files_done += 1
                if result.success:
                    self._success_count += 1
                else:
                    self._failed_names.append(os.path.basename(result.input_path))
            elif tag == EV_BATCH_ERROR:
                _, err_msg = msg
                # Don't return immediately — other workers may still have
                # in-flight files. But log the error to the UI now so the
                # user sees it before the batch-finished summary lands.
                self.batch_error.emit(err_msg)

    def _cleanup_children(self) -> None:
        for p in self._procs:
            if p.is_alive():
                p.terminate()
                p.join(timeout=2)
                if p.is_alive():
                    p.kill()
                    p.join(timeout=1)
        try:
            self._events_q.close()
            self._events_q.join_thread()
        except Exception:
            pass
        try:
            self._tasks_q.close()
            self._tasks_q.join_thread()
        except Exception:
            pass
