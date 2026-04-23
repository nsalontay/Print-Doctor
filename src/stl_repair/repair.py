"""Core mesh repair pipeline for Print Doctor. Pure Python — no Blender dep.

Handles any mesh format trimesh can load + export (STL, OBJ, PLY, 3MF …).
The output file extension dictates the export format, so the caller controls
round-tripping by choosing the destination filename.

Four-phase escalation:
  1. Basic cleanup (merge verts, drop degenerate/dup faces, fix normals)
  2. MeshFix (robust hole-filling / non-manifold repair via pymeshfix)
  3. Fine voxel remesh (marching cubes at max_dim / 350)
  4. Coarse voxel remesh (marching cubes at max_dim / 150) — best-effort
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
import trimesh
# Use the low-level C extension directly. The high-level `pymeshfix.MeshFix`
# wrapper pulls in pyvista (→ VTK, huge). `_meshfix.PyTMesh` needs only numpy.
from pymeshfix._meshfix import PyTMesh

log = logging.getLogger(__name__)

# Extensions accepted by the pipeline. trimesh handles load/export for all of
# these; the export format is inferred from the output-path extension.
SUPPORTED_EXTENSIONS: tuple[str, ...] = (".stl", ".obj", ".ply", ".3mf")


@dataclass
class RepairResult:
    input_path: str
    output_path: str
    success: bool
    phase: str  # "clean" | "meshfix" | "fine_remesh" | "coarse_remesh" | "failed" | "cancelled"
    message: str
    errors_remaining: int = 0


class _Cancelled(Exception):
    """Raised when the caller's is_cancelled() returns True between phases."""


def _non_manifold_edge_count(mesh: trimesh.Trimesh) -> int:
    """Count edges shared by != 2 faces.

    Uses a single-column structured view of the (E, 2) uint64 edge array so
    `np.unique` runs a 1D merge sort instead of the generic axis-based path.
    On large meshes (millions of edges) this is 10–50× faster — the
    axis-based version was the single biggest CPU sink on big inputs.
    """
    if len(mesh.faces) == 0:
        return 0
    edges = np.ascontiguousarray(mesh.edges_sorted)
    # Reinterpret each 2-element row as a single void scalar.
    view_dtype = np.dtype((np.void, edges.dtype.itemsize * edges.shape[1]))
    _, counts = np.unique(edges.view(view_dtype).ravel(), return_counts=True)
    return int(np.sum(counts != 2))


def _is_clean(mesh: trimesh.Trimesh) -> bool:
    """Watertight implies every edge is shared by exactly 2 faces (trimesh's
    internal check is the same operation as `_non_manifold_edge_count == 0`),
    so we skip the redundant count on the happy path."""
    return bool(mesh.is_watertight)


def _basic_clean(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Merge coincident vertices, drop duplicate/degenerate faces, fix normals.

    `trimesh.repair.fix_normals` needs networkx (soft dep). If it's missing we
    fall back to `fix_inversion` which only needs volume sign — good enough for
    a first pass; MeshFix and voxel remesh fix winding for us later.
    """
    mesh.merge_vertices()
    mesh.update_faces(mesh.unique_faces())
    mesh.update_faces(mesh.nondegenerate_faces())
    mesh.remove_unreferenced_vertices()
    try:
        trimesh.repair.fix_normals(mesh)
    except Exception as e:
        log.warning("fix_normals unavailable (%s); falling back to fix_inversion", e)
        try:
            trimesh.repair.fix_inversion(mesh)
        except Exception as e2:
            log.warning("fix_inversion also failed: %s", e2)
    return mesh


def _drop_sliver_components(mesh: trimesh.Trimesh,
                            min_face_ratio: float = 0.01,
                            min_faces_floor: int = 20) -> Optional[trimesh.Trimesh]:
    """Remove tiny disconnected components before MeshFix.

    CAD boolean exports frequently contain hundreds of 1-3 face slivers along
    symmetry planes. With that much noise, MeshFix's `join_closest_components`
    pairs the real halves to slivers instead of to each other, and the
    subsequent `clean()` drops half the model. Filtering these out first
    exposes the real components to the join step.

    Keeps any component with at least `max(min_faces_floor,
    min_face_ratio * largest_component.face_count)` faces.
    """
    parts = mesh.split(only_watertight=False)
    if len(parts) <= 1:
        return mesh
    max_faces = max(len(p.faces) for p in parts)
    threshold = max(min_faces_floor, int(max_faces * min_face_ratio))
    # Never drop the largest component, and don't filter at all if every
    # component is below the threshold (they're all peers, not slivers).
    if threshold >= max_faces:
        return mesh
    kept = [p for p in parts if len(p.faces) >= threshold]
    if len(kept) == len(parts):
        return mesh
    log.info("Dropped %d sliver component(s), kept %d (face threshold: %d)",
             len(parts) - len(kept), len(kept), threshold)
    return trimesh.util.concatenate(kept)


def _bounds_comparable(src: trimesh.Trimesh, dst: trimesh.Trimesh,
                       min_ratio: float = 0.8) -> bool:
    """True if `dst`'s bounding box matches `src`'s on every axis within
    `min_ratio`. Repair ops fill/weld; they shouldn't shrink bounds. A >20%
    shrink on any axis almost always means geometry was silently dropped
    (e.g. MeshFix removing the "wrong" component)."""
    for a, b in zip(src.extents, dst.extents):
        if a <= 0:
            continue
        if b / a < min_ratio:
            log.warning("MeshFix output shrank from %.2f to %.2f on one axis "
                        "(ratio %.2f < %.2f); treating as failure",
                        a, b, b / a, min_ratio)
            return False
    return True


def _apply_meshfix(mesh: trimesh.Trimesh) -> Optional[trimesh.Trimesh]:
    """Run MeshFix — robust hole-filling + non-manifold repair.

    Uses the low-level PyTMesh C-extension directly (skips the pyvista-heavy
    high-level wrapper). Replicates the steps pymeshfix.MeshFix.repair() does:
    join closest components, remove intersections, remove smallest components,
    fill small boundaries.

    Rejects the result if the output bounding box is substantially smaller
    than the input — that's the fingerprint of `clean()` dropping real
    geometry alongside slivers it couldn't weld.
    """
    filtered = _drop_sliver_components(mesh)
    if filtered is None:
        return None
    try:
        tin = PyTMesh()
        tin.set_quiet(True)
        tin.load_array(np.ascontiguousarray(filtered.vertices, dtype=np.float64),
                       np.ascontiguousarray(filtered.faces, dtype=np.int32))
        tin.join_closest_components()
        tin.fill_small_boundaries(nbe=0)  # 0 = fill all
        tin.clean(max_iters=10, inner_loops=3)
        v, f = tin.return_arrays()
        if len(v) == 0 or len(f) == 0:
            return None
        out = trimesh.Trimesh(vertices=v, faces=f, process=False)
        try:
            trimesh.repair.fix_normals(out)
        except Exception:
            try:
                trimesh.repair.fix_inversion(out)
            except Exception:
                pass
        if not _bounds_comparable(filtered, out):
            return None
        return out
    except Exception as e:
        log.warning("MeshFix failed: %s", e)
        return None


def _voxel_remesh(mesh: trimesh.Trimesh, divider: int) -> Optional[trimesh.Trimesh]:
    """Voxelize → fill → marching cubes. Pitch = max_dim / divider.

    trimesh's `VoxelGrid.marching_cubes` returns vertices in voxel-INDEX space
    (0..N) rather than world coordinates — we apply the grid's transform
    (pitch + origin) manually so the output has the same units and position as
    the input. Otherwise downstream slicers see meshes ~`divider/max_dim`×
    larger than expected.
    """
    try:
        max_dim = float(np.max(mesh.extents))
        if max_dim == 0 or not np.isfinite(max_dim):
            return None
        pitch = max_dim / divider
        vox = mesh.voxelized(pitch=pitch).fill()
        remeshed = vox.marching_cubes
        if remeshed is None or len(remeshed.faces) == 0:
            return None
        remeshed.apply_transform(vox.transform)
        _basic_clean(remeshed)
        return remeshed
    except Exception as e:
        log.warning("Voxel remesh (divider=%d) failed: %s", divider, e)
        return None


def _export(mesh: trimesh.Trimesh, path: str, inverse_scale: float) -> None:
    if inverse_scale != 1.0:
        mesh = mesh.copy()
        mesh.apply_scale(inverse_scale)
    mesh.export(path)


def _simplify_if_huge(mesh: trimesh.Trimesh,
                      threshold: int,
                      target: int,
                      phase: Callable[[str], None]) -> trimesh.Trimesh:
    """Quadric decimation when face count > threshold. Preserves shape
    much better than voxel remesh and dramatically speeds later phases.

    Requires the `fast-simplification` package. If it isn't available
    (e.g. PyInstaller bundle missed it), logs and returns the mesh
    unchanged rather than failing.
    """
    if len(mesh.faces) <= threshold:
        return mesh
    phase(f"simplifying ({len(mesh.faces):,} → {target:,} faces)")
    try:
        t0 = time.monotonic()
        simplified = mesh.simplify_quadric_decimation(target)
        log.info("Simplified %d → %d faces in %.2fs",
                 len(mesh.faces), len(simplified.faces), time.monotonic() - t0)
        return simplified
    except Exception as e:
        log.warning("Simplification failed (%s); continuing with original mesh", e)
        return mesh


def repair_mesh(input_path: str,
                output_path: str,
                scale: float = 1.0,
                simplify_large: bool = False,
                simplify_threshold: int = 1_000_000,
                simplify_target: int = 500_000,
                on_phase: Optional[Callable[[str], None]] = None,
                is_cancelled: Optional[Callable[[], bool]] = None) -> RepairResult:
    """Repair one mesh file. Writes to output_path on success (and coarse fallback).

    Args:
        input_path:  source mesh file (any format trimesh can load).
        output_path: destination mesh file. Format is inferred from the extension.
        scale:       unit conversion factor applied at import (e.g. 0.001 for mm→m).
                     Export applies the inverse so the file's units are preserved.
        simplify_large: if True and input has >simplify_threshold faces, run
                     quadric decimation to simplify_target before repair.
        on_phase:    optional callback(phase_name) invoked as the pipeline
                     progresses. UI uses this for per-file phase display.
        is_cancelled: optional callback() -> bool; checked between phases.
                     When True, bail out with phase="cancelled".
    """
    def phase(name: str) -> None:
        log.info("[%s] %s", os.path.basename(input_path), name)
        if on_phase is not None:
            try:
                on_phase(name)
            except Exception:
                pass

    def check_cancel() -> None:
        if is_cancelled is not None and is_cancelled():
            raise _Cancelled

    t_file = time.monotonic()
    try:
        phase("loading")
        try:
            mesh = trimesh.load(input_path, force="mesh")
        except Exception as e:
            return RepairResult(input_path, output_path, False, "failed",
                                f"Load error: {e}")

        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
            return RepairResult(input_path, output_path, False, "failed",
                                "Empty or unreadable mesh")

        log.info("Loaded %s: %d verts, %d faces, extents=%s",
                 os.path.basename(input_path),
                 len(mesh.vertices), len(mesh.faces), mesh.extents)

        if scale != 1.0:
            mesh.apply_scale(scale)
        inverse_scale = 1.0 / scale if scale != 0 else 1.0

        if simplify_large:
            mesh = _simplify_if_huge(mesh, simplify_threshold,
                                     simplify_target, phase)
            check_cancel()

        # Phase 1 — basic clean
        phase("basic cleanup")
        t0 = time.monotonic()
        _basic_clean(mesh)
        log.info("  Phase 1 (basic cleanup) %.2fs", time.monotonic() - t0)
        if _is_clean(mesh):
            _export(mesh, output_path, inverse_scale)
            return RepairResult(input_path, output_path, True, "clean",
                                "Fixed (basic cleanup)")
        check_cancel()

        # Phase 2 — MeshFix
        phase("MeshFix")
        t0 = time.monotonic()
        fixed = _apply_meshfix(mesh)
        log.info("  Phase 2 (MeshFix) %.2fs", time.monotonic() - t0)
        if fixed is not None and _is_clean(fixed):
            _export(fixed, output_path, inverse_scale)
            return RepairResult(input_path, output_path, True, "meshfix",
                                "Fixed (MeshFix)")
        check_cancel()

        # Phase 3 — fine voxel remesh
        phase("fine voxel remesh")
        source = fixed if fixed is not None else mesh
        t0 = time.monotonic()
        remeshed = _voxel_remesh(source, divider=350)
        log.info("  Phase 3 (fine voxel) %.2fs", time.monotonic() - t0)
        if remeshed is not None and _is_clean(remeshed):
            _export(remeshed, output_path, inverse_scale)
            return RepairResult(input_path, output_path, True, "fine_remesh",
                                "Fixed (fine voxel remesh)")
        check_cancel()

        # Phase 4 — coarse voxel remesh (best-effort; export even if imperfect)
        phase("coarse voxel remesh")
        t0 = time.monotonic()
        remeshed = _voxel_remesh(source, divider=150)
        log.info("  Phase 4 (coarse voxel) %.2fs", time.monotonic() - t0)
        if remeshed is not None:
            _export(remeshed, output_path, inverse_scale)
            errs = _non_manifold_edge_count(remeshed)
            if errs == 0 and remeshed.is_watertight:
                return RepairResult(input_path, output_path, True, "coarse_remesh",
                                    "Fixed (coarse voxel remesh)")
            return RepairResult(input_path, output_path, True, "coarse_remesh",
                                f"Best-effort (coarse remesh, {errs} non-manifold edges)",
                                errors_remaining=errs)

        errs = _non_manifold_edge_count(mesh)
        return RepairResult(input_path, output_path, False, "failed",
                            f"All phases failed ({errs} non-manifold edges)",
                            errors_remaining=errs)
    except _Cancelled:
        log.info("[%s] cancelled after %.2fs",
                 os.path.basename(input_path), time.monotonic() - t_file)
        return RepairResult(input_path, output_path, False, "cancelled",
                            "Cancelled")
    finally:
        log.info("[%s] total %.2fs",
                 os.path.basename(input_path), time.monotonic() - t_file)


def is_already_manifold(input_path: str) -> bool:
    """Quick pre-flight: True if the file loads as a watertight, manifold mesh."""
    try:
        mesh = trimesh.load(input_path, force="mesh")
    except Exception:
        return False
    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
        return False
    # is_watertight already implies 0 non-manifold edges; don't double-count.
    return bool(mesh.is_watertight)


def fast_face_count(path: str) -> Optional[int]:
    """Cheap face-count peek for binary STL — reads 84 bytes, no mesh load.

    Returns None for ASCII STL, other formats, or on any read error. Used by
    the pre-batch scan to flag large meshes without paying the full load
    cost on every file.
    """
    if not path.lower().endswith(".stl"):
        return None
    try:
        size = os.path.getsize(path)
        if size < 84:
            return None
        with open(path, "rb") as f:
            header = f.read(84)
        # ASCII STL starts with "solid " + a readable name. Binary STL also
        # *can* start with "solid" (spec is ambiguous), so verify via the
        # file-size formula: 84 header + 50 bytes/triangle.
        count = int.from_bytes(header[80:84], "little")
        expected = 84 + 50 * count
        if abs(size - expected) < 2:  # allow 1-2 byte trailing padding
            return count
        return None  # ASCII or corrupted header
    except Exception:
        return None


def discover_mesh_files(folder: str) -> list[str]:
    """Return sorted list of supported-mesh filenames (not paths) in folder,
    top-level only. Extensions checked: see SUPPORTED_EXTENSIONS."""
    if not os.path.isdir(folder):
        return []
    return sorted(
        f for f in os.listdir(folder)
        if f.lower().endswith(SUPPORTED_EXTENSIONS)
        and os.path.isfile(os.path.join(folder, f))
    )
