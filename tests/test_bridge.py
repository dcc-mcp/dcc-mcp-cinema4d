import math
import sys
from pathlib import Path

import pytest

from dcc_mcp_cinema4d.bridge import BridgeError, Cinema4dBridge


class FakeBridge(Cinema4dBridge):
    def __init__(self, root: Path):
        super().__init__(executable=sys.executable, allowed_roots=[root])
        self.calls = []

    def _invoke(self, method, params, timeout_secs=120):
        self.calls.append((method, dict(params), timeout_secs))
        output = params.get("output_path")
        document = params.get("document_path")
        if method == "system.status":
            return {
                "cinema4d_version": 2023200,
                "api_version": 2023200,
                "python_version": "3.9.0",
                "headless": True,
            }
        if method == "document.inspect":
            return {
                "document_name": Path(document).name,
                "object_count": 1,
                "material_count": 0,
                "objects": [],
                "materials": [],
            }
        if output:
            Path(output).write_bytes(b"C4D-OUTPUT")
        elif document and method.startswith("model."):
            with Path(document).open("ab") as stream:
                stream.write(b"-MUTATED")
        return {"method": method}


def test_status_without_c4dpy_is_actionable(tmp_path):
    bridge = Cinema4dBridge(executable="missing-c4dpy", allowed_roots=[tmp_path])
    status = bridge.status()
    assert status["ready"] is False
    assert status["reason"] == "c4dpy_not_found"


def test_create_document_is_atomic_and_durable(tmp_path):
    bridge = FakeBridge(tmp_path)
    output = tmp_path / "scene.c4d"

    result = bridge.create_document(str(output))

    assert output.read_bytes() == b"C4D-OUTPUT"
    assert result["bytes"] == len(b"C4D-OUTPUT")
    assert len(result["sha256"]) == 64
    assert result["document"]["object_count"] == 1
    assert not list(tmp_path.glob(".scene.*.c4d"))


def test_mutation_reopens_durable_document(tmp_path):
    document = tmp_path / "scene.c4d"
    document.write_bytes(b"C4D")
    bridge = FakeBridge(tmp_path)

    result = bridge.add_primitive(
        str(document),
        "cube",
        "Body",
        {"size": [10, 20, 30]},
    )

    assert document.read_bytes() == b"C4D-MUTATED"
    assert result["document"]["object_count"] == 1
    assert [call[0] for call in bridge.calls] == [
        "model.add_primitive",
        "document.inspect",
    ]


def test_paths_are_restricted_to_allowed_roots(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.c4d"
    outside.write_bytes(b"C4D")
    bridge = FakeBridge(allowed)

    with pytest.raises(BridgeError, match="outside"):
        bridge.inspect_document(str(outside))


def test_primitive_dimensions_are_typed(tmp_path):
    document = tmp_path / "scene.c4d"
    document.write_bytes(b"C4D")
    bridge = FakeBridge(tmp_path)

    with pytest.raises(BridgeError, match="Unsupported cube dimensions"):
        bridge.add_primitive(str(document), "cube", "Body", {"radius": 5})


def test_output_overwrite_requires_opt_in(tmp_path):
    document = tmp_path / "scene.c4d"
    output = tmp_path / "scene.glb"
    document.write_bytes(b"C4D")
    output.write_bytes(b"OLD")
    bridge = FakeBridge(tmp_path)

    with pytest.raises(BridgeError, match="overwrite=true"):
        bridge.export_document(str(document), str(output))

    result = bridge.export_document(str(document), str(output), overwrite=True)
    assert output.read_bytes() == b"C4D-OUTPUT"
    assert result["overwritten"] is True


def test_packaged_driver_rejects_unknown_methods(tmp_path):
    executable = getattr(sys, "_base_executable", sys.executable)
    bridge = Cinema4dBridge(executable=executable, allowed_roots=[tmp_path])

    with pytest.raises(BridgeError, match="Cinema 4D operation failed"):
        bridge._invoke("unsafe.eval", {}, 10)


def test_bridge_waits_when_executable_launches_worker_and_exits(tmp_path):
    launcher = tmp_path / "launcher.py"
    driver = Path(__file__).parents[1] / "src" / "dcc_mcp_cinema4d" / "cinema4d_driver.py"
    launcher.write_text(
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, %r, *sys.argv[1:]])\n" % str(driver),
        encoding="utf-8",
    )
    executable = getattr(sys, "_base_executable", sys.executable)
    bridge = Cinema4dBridge(executable=executable, allowed_roots=[tmp_path])
    bridge.driver_path = launcher

    with pytest.raises(BridgeError, match="Cinema 4D operation failed"):
        bridge._invoke("unsafe.eval", {}, 10)


@pytest.mark.parametrize("timeout", [math.nan, math.inf, -math.inf])
def test_bridge_rejects_non_finite_timeout(timeout, tmp_path):
    bridge = Cinema4dBridge(executable=sys.executable, allowed_roots=[tmp_path])

    with pytest.raises(BridgeError, match="finite"):
        bridge._timeout(timeout)
