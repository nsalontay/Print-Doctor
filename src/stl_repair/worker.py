"""QThread worker that runs the repair pipeline off the UI thread."""

from __future__ import annotations

import os
import shutil
import traceback

from PySide6.QtCore import QThread, Signal

from .repair import RepairResult, is_already_manifold, repair_mesh


class RepairWorker(QThread):
    file_started = Signal(int, int, str)          # (index, total, filename)
    file_phase = Signal(int, int, str, str)       # (index, total, filename, phase_label)
    file_finished = Signal(object)                # RepairResult
    batch_finished = Signal(int, int, list)       # (success_count, total, failed_filenames)
    batch_error = Signal(str)

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
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def _resolve_dst(self, name: str) -> str:
        """Apply overwrite policy: when overwriting, return the direct path;
        otherwise append a numeric suffix if the file already exists."""
        dst = os.path.join(self.output_dir, name)
        if self.overwrite or not os.path.exists(dst):
            return dst
        stem, ext = os.path.splitext(name)
        i = 1
        while True:
            candidate = os.path.join(self.output_dir, f"{stem} ({i}){ext}")
            if not os.path.exists(candidate):
                return candidate
            i += 1

    def run(self) -> None:
        try:
            if not self.files:
                self.batch_error.emit("No mesh files to repair.")
                return

            os.makedirs(self.output_dir, exist_ok=True)

            success = 0
            failed: list[str] = []
            total = len(self.files)
            for i, src in enumerate(self.files):
                if self._cancel:
                    break
                name = os.path.basename(src)
                idx = i + 1
                self.file_started.emit(idx, total, name)
                dst = self._resolve_dst(name)

                # Pre-flight: already-manifold files bypass the repair pipeline.
                if is_already_manifold(src):
                    if self.include_unmodified:
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
                        self.file_phase.emit(_idx, total, _name, phase_name)

                    def _cancelled() -> bool:
                        return self._cancel

                    try:
                        result = repair_mesh(
                            src, dst,
                            scale=self.scale,
                            simplify_large=self.simplify_large,
                            on_phase=_on_phase,
                            is_cancelled=_cancelled,
                        )
                    except Exception as e:
                        result = RepairResult(
                            src, dst, False, "failed",
                            f"Unhandled error: {e}\n{traceback.format_exc()}"
                        )

                self.file_finished.emit(result)
                if result.success:
                    success += 1
                else:
                    failed.append(name)

            self.batch_finished.emit(success, total, failed)
        except Exception as e:
            self.batch_error.emit(f"{e}\n{traceback.format_exc()}")
