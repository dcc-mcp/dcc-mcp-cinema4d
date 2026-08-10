"""Run a destructive smoke test against an explicitly configured c4dpy runtime."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dcc_mcp_cinema4d.bridge import get_bridge


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--import-path")
    args = parser.parse_args()

    evidence_dir = Path(args.evidence_dir).expanduser().resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    document = evidence_dir / "production-smoke.c4d"
    copy = evidence_dir / "production-smoke-copy.c4d"
    exported = evidence_dir / "production-smoke.obj"
    rendered = evidence_dir / "production-smoke.png"

    bridge = get_bridge()
    bridge.create_document(str(document), overwrite=True)
    bridge.add_primitive(
        str(document),
        "cube",
        "Body",
        {"size": [80, 50, 24]},
    )
    bridge.add_primitive(
        str(document),
        "sphere",
        "Detail",
        {"radius": 12},
        translation=[30, 0, 20],
    )
    bridge.transform_object(
        str(document),
        "Detail",
        translation=[32, 0, 18],
        rotation_hpb_degrees=[0, 25, 0],
        scale=[1.1, 1.1, 1.1],
    )
    bridge.add_primitive(
        str(document),
        "cone",
        "Disposable",
        {"bottom_radius": 5, "top_radius": 0, "height": 10},
    )
    bridge.remove_object(str(document), "Disposable")
    bridge.save_copy(str(document), str(copy), overwrite=True)

    if args.import_path:
        bridge.import_geometry(str(document), str(Path(args.import_path).resolve()))

    inspection = bridge.inspect_document(str(document))
    validation = bridge.validate_document(str(document))
    export_result = bridge.export_document(str(document), str(exported), overwrite=True)
    render_result = bridge.render_document(
        str(document), str(rendered), width=640, height=480, overwrite=True
    )

    assert validation["valid"] is True
    assert inspection["object_count"] >= 2
    assert exported.stat().st_size > 0
    assert rendered.stat().st_size > 0
    print(
        "CINEMA4D_LIVE_SMOKE_OK "
        + json.dumps(
            {
                "document_bytes": document.stat().st_size,
                "object_count": inspection["object_count"],
                "export_bytes": export_result["bytes"],
                "render_bytes": render_result["bytes"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
