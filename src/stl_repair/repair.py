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
from dataclasses import dataclass
from typing import Optional

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
    phase: str  # "clean" | "meshfix" | "fine_remesh" | "coarse_remesh" | "failed"
    message: str
    errors_remaining: int = 0


def _non_manifold_edge_count(mesh: trimesh.Trimesh) -> int:
    """Count edges shared by != 2 faces (Blender's definition of non-manifold)."""
    if len(mesh.faces) == 0:
        return 0
    edges = mesh.edges_sorted
    # group identical edges; non-manifold = any group size != 2
    _, counts = np.unique(edges, axis=0, return_counts=True)
    return int(np.sum(counts != 2))


def _is_clean(mesh: trimesh.Trimesh) -> bool:
    return mesh.is_watertight and _non_manifold_edge_count(mesh) == 0


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


def _apply_meshfix(mesh: trimesh.Trimesh) -> Optional[trimesh.Trimesh]:
    """Run MeshFix — robust hole-filling + non-manifold repair.

    Uses the low-level PyTMesh C-extension directly (skips the pyvista-heavy
    high-level wrapper). Replicates the steps pymeshfix.MeshFix.repair() does:
    join closest components, remove intersections, remove smallest components,
    fill small boundaries.
    """
    try:
        tin = PyTMesh()
        tin.set_quiet(True)
        tin.load_array(np.ascontiguousarray(mesh.vertices, dtype=np.float64),
                       np.ascontiguousarray(mesh.faces, dtype=np.int32))
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


def repair_mesh(input_path: str,
                output_path: str,
                scale: float = 1.0) -> RepairResult:
    """Repair one mesh file. Writes to output_path on success (and coarse fallback).

    Args:
        input_path:  source mesh file (any format trimesh can load).
        output_path: destination mesh file. Format is inferred from the extension.
        scale:       unit conversion factor applied at import (e.g. 0.001 for mm→m).
                     Export applies the inverse so the file's units are preserved.
    """
    try:
        mesh = trimesh.load(input_path, force="mesh")
    except Exception as e:
        return RepairResult(input_path, output_path, False, "failed",
                            f"Load error: {e}")

    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
        return RepairResult(input_path, output_path, False, "failed",
                            "Empty or unreadable mesh")

    if scale != 1.0:
        mesh.apply_scale(scale)
    inverse_scale = 1.0 / scale if scale != 0 else 1.0

    # Phase 1 — basic clean
    _basic_clean(mesh)
    if _is_clean(mesh):
        _export(mesh, output_path, inverse_scale)
        return RepairResult(input_path, output_path, True, "clean",
                            "Fixed (basic cleanup)")

    # Phase 2 — MeshFix
    fixed = _apply_meshfix(mesh)
    if fixed is not None and _is_clean(fixed):
        _export(fixed, output_path, inverse_scale)
        return RepairResult(input_path, output_path, True, "meshfix",
                            "Fixed (MeshFix)")

    # Phase 3 — fine voxel remesh
    source = fixed if fixed is not None else mesh
    remeshed = _voxel_remesh(source, divider=350)
    if remeshed is not None and _is_clean(remeshed):
        _export(remeshed, output_path, inverse_scale)
        return RepairResult(input_path, output_path, True, "fine_remesh",
                            "Fixed (fine voxel remesh)")

    # Phase 4 — coarse voxel remesh (best-effort; export even if imperfect)
    remeshed = _voxel_remesh(source, divider=150)
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


def is_already_manifold(input_path: str) -> bool:
    """Quick pre-flight: True if the file loads as a watertight, manifold mesh."""
    try:
        mesh = trimesh.load(input_path, force="mesh")
    except Exception:
        return False
    if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
        return False
    return bool(mesh.is_watertight) and _non_manifold_edge_count(mesh) == 0


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
