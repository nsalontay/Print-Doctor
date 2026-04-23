"""Child-process repair runner. Decouples the CPU-bound repair pipeline from
the Qt UI thread — repair runs in a separate OS process with its own GIL, so
the UI stays responsive even during heavy work (voxelize, marching cubes).

`worker.py` spawns N of these children and feeds them files via a shared
`tasks_q`; they post results to a shared `events_q`. One file is owned by a
single worker start-to-finish, so per-file event ordering is preserved even
though files interleave across workers.

Events are (tag, *payload) tuples. All payload fields are picklable primitives
(or a `RepairResult` dataclass, which pickles cleanly).
"""

from __future__ import annotations

import logging
import os
import queue as queue_mod
import shutil
import traceback
from dataclasses import asdict
from multiprocessing import Queue
from multiprocessing.synchronize import Event as EventType
from typing import Any

from .repair import RepairResult, is_already_manifold, repair_mesh

# Event tags. Kept as short strings so the queue protocol is greppable.
EV_FILE_STARTED = "file_started"   # (idx, total, name)
EV_FILE_PHASE = "file_phase"       # (idx, total, name, phase)
EV_FILE_FINISHED = "file_finished" # (result_dict,)
EV_BATCH_ERROR = "batch_error"     # (msg,)


def _setup_child_logging() -> None:
    """Attach a FileHandler in the child so per-phase timings still land in
    ~/Library/Logs/Print Doctor/repair.log. Separate from the parent handler —
    all processes write to the same file (OS handles append atomicity for
    single-line writes, which is what FileHandler emits)."""
    log_dir = os.path.expanduser("~/Library/Logs/Print Doctor")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        return
    log_path = os.path.join(log_dir, "repair.log")
    root = logging.getLogger()
    # Avoid duplicate handlers if this module gets reloaded.
    for h in root.handlers:
        if isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", None) == log_path:
            break
    else:
        h = logging.FileHandler(log_path, encoding="utf-8")
        h.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s [child %(process)d]: %(message)s"
        ))
        root.addHandler(h)
    root.setLevel(logging.INFO)


def _repair_one(src: str, idx: int, total: int,
                output_dir: str, scale: float,
                include_unmodified: bool, overwrite: bool,
                simplify_large: bool,
                events_q: "Queue[Any]",
                cancel_event: EventType) -> RepairResult:
    """Repair a single file and return a RepairResult. Posts EV_FILE_STARTED /
    EV_FILE_PHASE to `events_q` while working. Never raises — any exception
    becomes a failed RepairResult."""
    name = os.path.basename(src)
    events_q.put((EV_FILE_STARTED, idx, total, name))

    # Resolve destination with the overwrite policy.
    dst = os.path.join(output_dir, name)
    if not overwrite and os.path.exists(dst):
        stem, ext = os.path.splitext(name)
        i = 1
        while True:
            candidate = os.path.join(output_dir, f"{stem} ({i}){ext}")
            if not os.path.exists(candidate):
                dst = candidate
                break
            i += 1

    if is_already_manifold(src):
        if include_unmodified:
            try:
                shutil.copy2(src, dst)
                return RepairResult(src, dst, True, "clean",
                                    "Already manifold (copied)")
            except Exception as e:
                return RepairResult(src, dst, False, "failed",
                                    f"Copy error: {e}")
        return RepairResult(src, dst, True, "clean",
                            "Already manifold (skipped)")

    def _on_phase(phase_name: str) -> None:
        try:
            events_q.put((EV_FILE_PHASE, idx, total, name, phase_name))
        except Exception:
            pass

    def _cancelled() -> bool:
        return cancel_event.is_set()

    try:
        return repair_mesh(
            src, dst,
            scale=scale,
            simplify_large=simplify_large,
            on_phase=_on_phase,
            is_cancelled=_cancelled,
        )
    except Exception as e:
        return RepairResult(
            src, dst, False, "failed",
            f"Unhandled error: {e}\n{traceback.format_exc()}"
        )


def worker_loop(tasks_q: "Queue[Any]", events_q: "Queue[Any]",
                cancel_event: EventType,
                output_dir: str, scale: float,
                include_unmodified: bool, overwrite: bool,
                simplify_large: bool) -> None:
    """Pool-worker entrypoint — must be top-level (picklable by name) for
    `spawn`. Pulls one task at a time from `tasks_q` until it sees a None
    sentinel, then exits cleanly. Also exits early if `cancel_event` is set.

    Tasks are `(src_path, idx, total)` tuples; `idx` is the 1-based position
    in the original submission order. `total` is the full batch size (used
    for UI labels; workers don't need it for anything else)."""
    _setup_child_logging()
    log = logging.getLogger(__name__)
    log.info("worker started (pid %d)", os.getpid())

    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as e:
        events_q.put((EV_BATCH_ERROR, f"Could not create output dir: {e}"))
        return

    try:
        while True:
            if cancel_event.is_set():
                break
            try:
                task = tasks_q.get(timeout=0.2)
            except queue_mod.Empty:
                continue
            if task is None:
                break
            src, idx, total = task
            result = _repair_one(
                src, idx, total, output_dir, scale,
                include_unmodified, overwrite, simplify_large,
                events_q, cancel_event,
            )
            events_q.put((EV_FILE_FINISHED, asdict(result)))
    except Exception as e:
        events_q.put((EV_BATCH_ERROR,
                      f"Worker crashed: {e}\n{traceback.format_exc()}"))
    finally:
        log.info("worker exiting (pid %d)", os.getpid())
