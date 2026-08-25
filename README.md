# dcc-mcp-cinema4d

<p align="center">
  <img src="docs/assets/dcc-mcp-cinema4d.svg" alt="DCC-MCP · CINEMA 4D" width="600">
</p>

Typed Cinema 4D document automation for DCC-MCP, executed through Maxon's licensed
headless `c4dpy` runtime.

![Typed primitives assembled, validated, rendered, and prepared for interchange through Cinema 4D](docs/images/dcc-mcp-cinema4d-showcase.webp)

_Illustrative workflow limited to the adapter's implemented primitive, transform, validation, render, and interchange operations; generated source is retained in `docs/images/dcc-mcp-cinema4d-showcase-source.png`._

## Capabilities

- Discover and report the real Cinema 4D, Python, and API versions.
- Create, inspect, validate, and atomically copy `.c4d` documents.
- Add typed cube, sphere, cylinder, cone, torus, and plane primitives.
- Set absolute translation, HPB rotation, and scale; remove named objects safely.
- Import C4D, OBJ, FBX, glTF/GLB, STL, Alembic, Collada, and 3DS geometry when
  the installed Cinema 4D runtime provides the corresponding importer.
- Export full documents to C4D, OBJ, FBX, glTF/GLB, STL, Alembic, or Collada
  when the installed runtime provides the corresponding exporter.
- Render bounded PNG and JPEG images without a GUI.

The adapter never accepts arbitrary Python source. Every host call executes a packaged,
allowlisted driver in a fresh `c4dpy` process with bounded paths, timeouts, output streams,
and response size. Document mutations use a staged file and atomically replace the durable
document only after a successful host operation.

OBJ import and export form the production-validated interchange baseline. Other formats
are runtime-dependent; `get_capabilities` reports this boundary and unavailable optional
importers or exporters fail with a format-specific error.

## Requirements

- Cinema 4D R21 or newer with a valid license.
- The matching `c4dpy` executable shipped with that Cinema 4D installation.
- Python 3.9 or newer for the DCC-MCP service.
- `dcc-mcp-core>=0.20.14,<1.0.0` in the same Python distribution.

Maxon documents that `c4dpy` is a headless Cinema 4D instance capable of loading,
constructing, saving, and rendering scenes, but it requires a real Cinema 4D installation
and license.

## Install

See [`install.md`](install.md) for the official Core Install SOP contract,
identity-bound planning, atomic receipt, verification, upgrade, uninstall, and troubleshooting.

```bash
python -m pip install dcc-mcp-cinema4d
dcc-mcp-cinema4d plan --target install --c4dpy <absolute-c4dpy-path> --json
dcc-mcp-cinema4d install --c4dpy <absolute-c4dpy-path> --json --yes
dcc-mcp-cinema4d doctor --c4dpy <absolute-c4dpy-path> --json
```

Set the licensed runtime and allowed workspace before starting the adapter:

```powershell
$env:DCC_MCP_CINEMA4D_C4DPY = "C:\Program Files\Maxon Cinema 4D 2026\c4dpy.exe"
$env:DCC_MCP_CINEMA4D_ALLOWED_ROOTS = "D:\projects;D:\exports"
dcc-mcp-cinema4d
```

For an explicit standalone readiness check:

```powershell
dcc-mcp-cinema4d verify --c4dpy "C:\Program Files\Maxon Cinema 4D 2026\c4dpy.exe" --json
```

Lifecycle execution binds the exact Maxon product/file identity, digest, selected Python
distribution, live PID/start token, and adapter-owned receipt. The default allowed root is
the adapter's current working directory.

## Agent workflow

```bash
dcc-mcp-cli list
dcc-mcp-cli search Cinema 4D status --dcc-type c4d
dcc-mcp-cli load-skill cinema4d-session --dcc-type c4d --instance-id <id>
dcc-mcp-cli describe c4d.<id>.get_status
dcc-mcp-cli call c4d.<id>.get_status --json '{}'
```

Load `cinema4d-modeling` only when modeling, interchange, or rendering tools are needed.
Use `--wait` for document operations because they run as bounded asynchronous jobs.

## Development

```bash
python -m pip install -e ".[dev]"
python -m ruff check src tests
python -m ruff format --check src tests
python -m pytest -m "not cinema4d"
python -m build
python -m twine check dist/*
```

Real-host acceptance requires a licensed `c4dpy`; CI intentionally does not mock a Maxon
license.

Official references: [c4dpy manual](https://developers.maxon.net/docs/py/2025_1_0/manuals/manual_py_c4dpy.html),
[Cinema 4D Python SDK](https://developers.maxon.net/docs/py/index.html).
