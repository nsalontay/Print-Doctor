"""Child-process repair runner. Decouples the CPU-bound repair pipeline from
the Qt UI thread — repair runs in a separate OS process with its own GIL, so
the UI stays responsive even during heavy work (voxelize, marching cubes).

The parent spawns this via `multiprocessing.Process` and drains events off a
`multiprocessing.Queue`. See `worker.py` for the QThread-side proxy.

Events are (tag, *payload) tuples. All payload fields are picklable primitives
(or a `RepairResult` dataclass, which pickles cleanly).
"""

from __future__ import annotations

import logging
import os
import shutil
import traceback
from dataclasses import asdict
from multiprocessing import Event, Queue
from multiprocessing.synchronize import Event as EventType
from typing import Any

from .repair import RepairResult, is_already_manifold, repair_mesh

# Event tags. Kept as short strings so the queue protocol is greppable.
EV_FILE_STARTED = "file_started"   # (idx, total, name)
EV_FILE_PHASE = "file_phase"       # (idx, total, name, phase)
EV_FILE_FINISHED = "file_finished" # (result_dict,)
EV_BATCH_FINISHED = "batch_finished"  # (success, total, failed_list)
EV_BATCH_ERROR = "batch_error"     # (msg,)
EV_DONE = "__done__"               # sentinel so the parent can stop draining


def _setup_child_logging() -> None:
    """Attach a FileHandler in the child so per-phase timings still land in
    ~/Library/Logs/Print Doctor/repair.log. Separate from the parent handler —
    both processes write to the same file (OS handles append atomicity for
    single-line writes)."""
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


def run_batch(files: list[str], output_dir: str, scale: float,
              include_unmodified: bool, overwrite: bool,
              simplify_large: bool,
              event_q: "Queue[Any]", cancel_event: EventType) -> None:
    """Child entrypoint — must be top-level (picklable by name) for `spawn`.

    Mirrors the old `RepairWorker.run()` logic but writes events to `event_q`
    instead of emitting Qt signals. `cancel_event` is polled between phases."""
    _setup_child_logging()
    log = logging.getLogger(__name__)

    def _resolve_dst(name: str) -> str:
        dst = os.path.join(output_dir, name)
        if overwrite or not os.path.exists(dst):
            return dst
        stem, ext = os.path.splitext(name)
        i = 1
        while True:
            candidate = os.path.join(output_dir, f"{stem} ({i}){ext}")
            if not os.path.exists(candidate):
                return candidate
            i += 1

    try:
        if not files:
            event_q.put((EV_BATCH_ERROR, "No mesh files to repair."))
            return

        os.makedirs(output_dir, exist_ok=True)

        success = 0
        failed: list[str] = []
        total = len(files)
        for i, src in enumerate(files):
            if cancel_event.is_set():
                break
            name = os.path.basename(src)
            idx = i + 1
            event_q.put((EV_FILE_STARTED, idx, total, name))
            dst = _resolve_dst(name)

            if is_already_manifold(src):
                if include_unmodified:
                    try:
                        shutil.copy2(src, dst)
                        result = RepairResult(src, dst, True, "clean",
                                              "Already manifold (copied)")
                    except Exception as e:
                        result = RepairResult(src, dst, False, "failed",
                                              f"Copy error: {e}")
                else:
                    result = RepairResult(src, dst, True, "clean",
                                          "Already manifold (skipped)")
            else:
                def _on_phase(phase_name: str, _name=name, _idx=idx) -> None:
                    try:
                        event_q.put((EV_FILE_PHASE, _idx, total, _name, phase_name))
                    except Exception:
                        pass

                def _cancelled() -> bool:
                    return cancel_event.is_set()

                try:
                    result = repair_mesh(
                        src, dst,
                        scale=scale,
                        simplify_large=simplify_large,
                        on_phase=_on_phase,
                        is_cancelled=_cancelled,
                    )
                except Exception as e:
                    result = RepairResult(
                        src, dst, False, "failed",
                        f"Unhandled error: {e}\n{traceback.format_exc()}"
                    )

            # Send the RepairResult as a plain dict — picklable without importing
            # the dataclass on the parent side (though it does anyway).
            event_q.put((EV_FILE_FINISHED, asdict(result)))
            if result.success:
                success += 1
            else:
                failed.append(name)

        event_q.put((EV_BATCH_FINISHED, success, total, failed))
    except Exception as e:
        event_q.put((EV_BATCH_ERROR, f"{e}\n{traceback.format_exc()}"))
    finally:
        # Sentinel so the parent's drain loop exits cleanly even if the child
        # crashed mid-batch.
        try:
            event_q.put((EV_DONE,))
        except Exception:
            pass
