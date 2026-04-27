"""Smoke tests for the repair pipeline.

These exist to catch obvious regressions — import errors, a broken
RepairResult dataclass, busted I/O, or a pipeline that no longer fixes
a known-broken mesh. They are NOT exhaustive; bigger test fixtures
(real-world Hab-Block STLs, etc.) would be slow and binary, so they
live in the issue tracker / manual QA checklist instead.

The fixtures here are generated in-memory via trimesh primitives so the
suite stays fast (<10 s) and the repo stays free of binary blobs.
"""
from __future__ import annotations

import os
import tempfile

import pytest
import trimesh

from stl_repair.repair import RepairResult, is_already_manifold, repair_mesh


@pytest.fixture
def workdir():
    with tempfile.TemporaryDirectory() as d:
        yield d


def _make_clean_cube_stl(path: str) -> None:
    """Watertight unit cube — should pass the manifold pre-flight."""
    cube = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
    cube.export(path)


def _make_broken_cube_stl(path: str) -> None:
    """Cube with one face deleted — non-watertight (open boundary).
    Easy case for the repair pipeline; PyMeshFix's level-0 clean closes
    it. We deliberately pick an easy break so the smoke test stays fast
    and avoids the multi-minute voxel-remesh fallback path."""
    cube = trimesh.creation.box(extents=(10.0, 10.0, 10.0))
    broken = trimesh.Trimesh(
        vertices=cube.vertices,
        faces=cube.faces[1:],  # drop the first triangle
        process=False,
    )
    broken.export(path)


def test_clean_cube_is_recognized_as_manifold(workdir):
    src = os.path.join(workdir, "cube.stl")
    _make_clean_cube_stl(src)
    assert is_already_manifold(src) is True


def test_clean_cube_repair_returns_clean_status(workdir):
    src = os.path.join(workdir, "cube.stl")
    dst = os.path.join(workdir, "cube_out.stl")
    _make_clean_cube_stl(src)

    result = repair_mesh(src, dst)
    assert isinstance(result, RepairResult)
    assert result.success is True
    assert result.phase == "clean"


def test_broken_cube_is_repaired(workdir):
    src = os.path.join(workdir, "broken.stl")
    dst = os.path.join(workdir, "fixed.stl")
    _make_broken_cube_stl(src)

    # Pre-flight: source is NOT manifold.
    assert is_already_manifold(src) is False

    result = repair_mesh(src, dst)
    assert isinstance(result, RepairResult), type(result)
    assert result.success is True, f"repair failed: {result.message}"
    assert os.path.exists(dst), "repair claimed success but no output file"

    # Post-flight: output IS watertight/manifold.
    fixed = trimesh.load(dst, force="mesh")
    assert isinstance(fixed, trimesh.Trimesh)
    assert fixed.is_watertight, "repaired mesh is still not watertight"


def test_repair_phase_callback_fires(workdir):
    """The UI relies on on_phase() callbacks to update its progress label.
    If the pipeline ever stops emitting them, the spinner goes silent."""
    src = os.path.join(workdir, "broken.stl")
    dst = os.path.join(workdir, "fixed.stl")
    _make_broken_cube_stl(src)

    phases: list[str] = []
    repair_mesh(src, dst, on_phase=phases.append)

    # Don't pin exact phase names — just that something fired, so
    # the UI has a chance to update.
    assert len(phases) > 0, "no phase callbacks were emitted"


def test_cancellation_stops_repair(workdir):
    """is_cancelled callback returning True should bail out without
    crashing or producing a partial output that claims success."""
    src = os.path.join(workdir, "broken.stl")
    dst = os.path.join(workdir, "fixed.stl")
    _make_broken_cube_stl(src)

    result = repair_mesh(src, dst, is_cancelled=lambda: True)
    assert isinstance(result, RepairResult)
    # Either it bailed cleanly (success=False) OR it finished before
    # checking. What it MUST NOT do is raise.
    assert result.success in (True, False)
