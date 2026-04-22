# Print Doctor

Drag-and-drop batch repair for non-manifold meshes. Built for 3D-printing workflows — drop a folder (or a single file), click one button, get printable meshes back.

Supports `.stl`, `.obj`, `.ply`, and `.3mf`. The output file keeps the input format (determined by the source filename's extension).

No Blender, MeshLab, or other dependencies to install. Just download the app.

## Install (macOS)

1. Download the latest `Print-Doctor-macOS-*.zip` from the [Releases page](../../releases).
   - Apple Silicon Macs (M1/M2/M3/M4): pick the `arm64` zip
   - Intel Macs: pick the `x86_64` zip
2. Unzip and drag `Print Doctor.app` into your Applications folder.
3. **First launch:** right-click the app → **Open** → **Open** in the dialog.
   (Double-clicking shows "unidentified developer" — this is macOS's warning for apps not signed with a paid Apple Developer ID. Right-click → Open bypasses it. You only need to do this once.)

## Use

1. Drag a folder of mesh files — or a single mesh — onto the window.
2. Pick the source units if your mesh isn't already in millimeters (most slicers expect mm). "Keep original units" is usually correct for files from Cura / PrusaSlicer / Fusion360.
3. Click **Repair**. Progress appears per-file in the list.

Repaired files are written to a `Repaired/` subfolder next to your source. Originals are never modified.

## How it repairs

Four-phase escalation — each file is tried with the cheapest fix first, falling back only if it doesn't produce a watertight manifold mesh:

1. **Basic cleanup** — merge coincident vertices, drop degenerate/duplicate faces, recompute normals.
2. **MeshFix** ([pymeshfix](https://github.com/pyvista/pymeshfix) / [MeshFix](https://github.com/MarcoAttene/MeshFix-V2.1)) — robust hole-filling and non-manifold repair.
3. **Fine voxel remesh** — voxelize at `max_dim / 350`, fill interior, marching cubes. Preserves surface detail.
4. **Coarse voxel remesh** — same at `max_dim / 150`. Last resort; may lose fine features but produces a printable solid.

The input mesh's units and position are preserved in the output, including through voxel remeshing (output fits to within one voxel pitch of the input's bounding box).

## Build from source

```bash
git clone <this repo>
cd "Print Doctor"
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[build]"

# Run the app
python -m stl_repair

# Or build the .app
./build_macos.sh
```

Requires Python 3.10+ (3.12 or 3.14 from [python.org](https://www.python.org/downloads/macos/) recommended on macOS; CommandLineTools' Python 3.9 does not support PySide6's cocoa plugin).

## License

GPL-3.0-or-later. The bundled [MeshFix](https://github.com/MarcoAttene/MeshFix-V2.1) algorithm (via [pymeshfix](https://github.com/pyvista/pymeshfix)) is GPL, which makes this whole tool GPL. Full text in [LICENSE](LICENSE).

## Credits

- [trimesh](https://trimsh.org) — mesh I/O and voxel remeshing
- [pymeshfix](https://github.com/pyvista/pymeshfix) / [MeshFix](https://github.com/MarcoAttene/MeshFix-V2.1) by Marco Attene — non-manifold repair
- [PySide6](https://doc.qt.io/qtforpython-6/) — UI framework
