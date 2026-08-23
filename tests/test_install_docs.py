from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_install_guide_documents_standalone_wheel_and_verify_contract() -> None:
    guide = (ROOT / "install.md").read_text(encoding="utf-8")

    headings = [
        "## Requirements",
        "## Supported versions",
        "## Agent quick path",
        "## Manual path",
        "## Verify",
        "## Upgrade",
        "## Uninstall",
        "## Troubleshooting",
    ]
    assert all(heading in guide for heading in headings)
    assert all(platform in guide for platform in ("Windows", "macOS", "Linux"))
    assert "python -m pip install dcc-mcp-cinema4d" in guide
    assert "pip install -e" not in guide
    assert "dcc-mcp-cinema4d doctor --json" in guide
    assert "dcc-mcp-cinema4d verify" in guide
    assert all("`%s`" % code in guide for code in (0, 10, 40))
    assert "does not download" in guide
    assert "no adapter-managed binary cache" in guide
    assert "https://raw.githubusercontent.com/dcc-mcp/dcc-mcp-cinema4d/main/install.md" in guide


def test_ci_runs_explicit_doctor_json_smoke() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    names = {step.get("name") for step in workflow["jobs"]["test"]["steps"]}

    assert "Doctor JSON smoke" in names
