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
    for operation in ("plan", "install", "status", "verify", "uninstall", "upgrade"):
        assert "dcc-mcp-cinema4d %s" % operation in guide
    assert all("`%s`" % code in guide for code in (0, 10, 40))
    assert "does not download" in guide
    assert "no adapter-managed binary cache" in guide
    assert "atomic receipt" in guide
    assert "mutation lock" in guide
    assert "dcc-mcp-core>=0.20.36" in guide
    assert "There is no adapter daemon" in guide
    assert "https://raw.githubusercontent.com/dcc-mcp/dcc-mcp-cinema4d/main/install.md" in guide


def test_install_guide_documents_the_two_interpreter_boundary() -> None:
    guide = (ROOT / "install.md").read_text(encoding="utf-8")

    # The adapter spans two runtimes; the docs have to say so explicitly and
    # name the constraint that makes R23 the effective floor.
    assert "Two interpreters" in guide
    assert "c4dpy" in guide
    assert "Python 2.7" in guide
    # The read-back contract, and the honest split between evidence levels.
    assert "Contract level" in guide
    assert "Host level" in guide
    assert "C4D_TEST_EXECUTABLE" in guide


def test_readme_documents_the_two_interpreter_boundary() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "Two interpreters" in readme
    assert "dcc-mcp-core>=0.20.36" in readme


def test_ci_runs_explicit_doctor_json_smoke() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    names = {step.get("name") for step in workflow["jobs"]["test"]["steps"]}

    assert "Doctor JSON smoke" in names
