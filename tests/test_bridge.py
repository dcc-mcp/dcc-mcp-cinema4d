import json
import math
import os
import struct
import sys
from pathlib import Path

import pytest

from dcc_mcp_cinema4d import bridge as bridge_module
from dcc_mcp_cinema4d import install
from dcc_mcp_cinema4d.bridge import BridgeError, BridgeTimeoutError, Cinema4dBridge


def _file_identity(path) -> str:
    """Identity that survives the staged-file rename the bridge performs.

    The bridge writes to a temp file and then ``os.replace``s it onto the final
    path, so a fake keyed on the path would lose its state at exactly the point
    the read-back is taken. ``st_dev``/``st_ino`` follow the rename.
    """
    try:
        info = os.stat(str(path), follow_symlinks=False)
    except OSError:
        return "missing:%s" % path
    return "%s:%s" % (int(info.st_dev), int(info.st_ino))


def _png_bytes(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x06\x00\x00\x00"
        + b"C4D-OUTPUT"
    )


def _jpeg_bytes(width: int, height: int) -> bytes:
    return (
        b"\xff\xd8"
        + b"\xff\xe0"
        + struct.pack(">H", 16)
        + b"JFIF\x00"
        + b"\x01\x01\x00"
        + struct.pack(">HH", 1, 1)
        + b"\x00\x00"
        + b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
        + b"C4D-OUTPUT"
    )


def _synthetic_bytes(suffix: str, width: int = 640, height: int = 480) -> bytes:
    """Bytes that actually claim to be the format the suffix names.

    The write-after-read contract re-reads what was written, so a fake that
    writes ``b"C4D-OUTPUT"`` for every format would either force the contract to
    be weakened or fail for the wrong reason.
    """
    lowered = suffix.lower()
    if lowered == ".png":
        return _png_bytes(width, height)
    if lowered in (".jpg", ".jpeg"):
        return _jpeg_bytes(width, height)
    if lowered == ".glb":
        return b"glTF\x02\x00\x00\x00" + b"C4D-OUTPUT"
    if lowered == ".gltf":
        return b'{"asset":{"version":"2.0"}}'
    if lowered == ".fbx":
        return b"Kaydara FBX Binary  \x00\x1a\x00" + b"C4D-OUTPUT"
    if lowered == ".dae":
        return b'<?xml version="1.0"?><COLLADA/>'
    if lowered == ".stl":
        return b"solid synthetic\nfacet normal 0 0 0\nendsolid synthetic\n"
    if lowered == ".obj":
        return b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n"
    return b"C4D-OUTPUT"


class FakeBridge(Cinema4dBridge):
    """An in-memory model of the Cinema 4D side of the bridge.

    It is deliberately stateful: ``document.inspect`` reports what earlier
    mutations actually persisted, which is what makes the write-after-read
    contract testable at all. A stateless fake that always returns one canned
    payload can only ever prove the check did not crash.

    ``persist=False`` models the failure the contract exists to catch: the
    driver reports success, the save does not take, and the reopened document
    disagrees with what was reported.
    """

    def __init__(
        self,
        root: Path,
        *,
        persist: bool = True,
        documents: dict | None = None,
        scene: list | None = None,
    ):
        super().__init__(executable=sys.executable, allowed_roots=[root])
        self.calls = []
        self.persist = persist
        # Shareable so a test can seed a document with one bridge and then model
        # a failing save with another; without that, "remove the object that was
        # never there" is not a meaningful scenario.
        self.documents: dict[str, list[dict]] = {} if documents is None else documents
        self.scene: list[dict] = [] if scene is None else scene
        self._next_type_id = 1000

    # -- model helpers -------------------------------------------------
    #
    # The bridge stages every mutation through a temp copy and then `os.replace`s
    # it onto the document, so a model keyed purely on file identity loses
    # continuity after the first mutation. The fake therefore models what the
    # real thing does: one scene that mutations change in place, plus separate
    # state for each produced file.
    def _objects(self, path):
        produced = self.documents.get(_file_identity(path))
        if produced is not None:
            return [dict(item) for item in produced]
        return [dict(item) for item in self.scene]

    def _store(self, path, objects):
        self.documents[_file_identity(path)] = [dict(item) for item in objects]

    def _reopen(self, document):
        objects = self._objects(document)
        return {
            "document_name": Path(document).name,
            "object_count": len(objects),
            "material_count": 0,
            "objects": objects,
            "materials": [],
        }

    def _allocate_type_id(self):
        self._next_type_id += 1
        return self._next_type_id

    def _invoke(self, method, params, timeout_secs=120):
        self.calls.append((method, dict(params), timeout_secs))
        output = params.get("output_path")
        document = params.get("document_path")
        if method == "system.status":
            return {
                "cinema4d_version": 26000,
                "api_version": 26000,
                "python_version": "3.9.0",
                "headless": True,
            }
        result = self._dispatch(method, params, output, document)
        # The real bridge stamps the build onto every result; read-back failures
        # are unreproducible without it, so the fake has to model that too.
        result.setdefault("cinema4d_version", 26000)
        return result

    def _dispatch(self, method, params, output, document):
        if method == "document.inspect":
            return self._reopen(document)
        if method == "document.create":
            if output:
                Path(output).write_bytes(_synthetic_bytes(Path(output).suffix))
                self._store(output, [])
            return {"method": method}
        if method == "document.save_copy":
            copied = self._objects(document)
            if output:
                Path(output).write_bytes(_synthetic_bytes(Path(output).suffix))
                self._store(output, copied if self.persist else [])
            return {"method": method, "object_count": len(copied)}
        if method == "model.add_primitive":
            objects = self._objects(document)
            objects.append(
                {
                    "name": params["name"],
                    "path": params["name"],
                    "depth": 0,
                    "type_id": self._allocate_type_id(),
                    "type_name": "Cube",
                    "transform": {
                        "translation": [
                            float(item) for item in params.get("translation", (0, 0, 0))
                        ],
                        "rotation_hpb_degrees": [
                            float(item) for item in params.get("rotation_hpb_degrees", (0, 0, 0))
                        ],
                        "scale": [float(item) for item in params.get("scale", (1, 1, 1))],
                    },
                }
            )
            return self._record_mutation(document, objects, params, created=dict(objects[-1]))
        if method == "model.transform_object":
            objects = self._objects(document)
            updated = None
            for entry in objects:
                if entry["name"] == params["object_name"]:
                    entry["transform"] = {
                        "translation": [
                            float(item) for item in params.get("translation", (0, 0, 0))
                        ],
                        "rotation_hpb_degrees": [
                            float(item) for item in params.get("rotation_hpb_degrees", (0, 0, 0))
                        ],
                        "scale": [float(item) for item in params.get("scale", (1, 1, 1))],
                    }
                    updated = entry
            extra = {"updated": dict(updated)} if updated is not None else {}
            return self._record_mutation(document, objects, params, **extra)
        if method == "model.remove_object":
            objects = self._objects(document)
            target = params["object_name"]
            removed = [entry["path"] for entry in objects if entry["name"] == target]
            if params.get("cascade"):
                prefix = "%s/" % target
                removed.extend(
                    entry["path"] for entry in objects if entry["path"].startswith(prefix)
                )
                objects = [
                    entry
                    for entry in objects
                    if entry["name"] != target and not entry["path"].startswith(prefix)
                ]
            else:
                objects = [entry for entry in objects if entry["name"] != target]
            return self._record_mutation(document, objects, params, removed_paths=removed)
        if method == "model.import_geometry":
            objects = self._objects(document)
            name = Path(params["input_path"]).stem
            objects.append(
                {
                    "name": name,
                    "path": name,
                    "depth": 0,
                    "type_id": self._allocate_type_id(),
                    "type_name": "Null",
                    "transform": {
                        "translation": [0.0, 0.0, 0.0],
                        "rotation_hpb_degrees": [0.0, 0.0, 0.0],
                        "scale": [1.0, 1.0, 1.0],
                    },
                }
            )
            return self._record_mutation(document, objects, params, imported_paths=[name])
        if output:
            Path(output).write_bytes(
                _synthetic_bytes(
                    Path(output).suffix,
                    width=int(params.get("width", 640)),
                    height=int(params.get("height", 480)),
                )
            )
            return {"method": method}
        return {"method": method}

    def _record_mutation(self, document, objects, params, **extra):
        """Write the staged bytes and, when persisting, the durable state.

        The returned payload mirrors the driver's: it reports what the mutation
        built in memory. ``persist=False`` models a save that did not take, so
        the payload and the reopened document disagree — which is exactly the
        situation the write-after-read contract must reject.
        """
        with Path(document).open("ab") as stream:
            stream.write(b"-MUTATED")
        if self.persist:
            self.scene = [dict(item) for item in objects]
            # Drop any produced-file state for this identity, otherwise a later
            # reopen would return the document's pre-mutation snapshot.
            self.documents.pop(_file_identity(document), None)
        result: dict = {"method": "model.mutation"}
        result.update(extra)
        return result


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
    # A freshly created document reopens empty; anything else would mean the
    # read-back inspected some other document.
    assert result["document"]["object_count"] == 0
    assert result["read_back"] == "compared"
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
    assert output.read_bytes().startswith(b"glTF")
    assert result["overwritten"] is True
    assert result["read_back"] == "compared"


def _accept_synthetic_runtime_identity(monkeypatch):
    def observe(pid, _host):
        return {
            "pid": pid,
            "executable_sha256": "0" * 64,
            "executable_file_identity": "synthetic",
            "start_identity": "synthetic:%d" % pid,
        }

    monkeypatch.setattr(install, "_observe_bound_runtime", observe)
    monkeypatch.setattr(install, "_recapture_bound_runtime", lambda identity, _host: identity)


def test_packaged_driver_rejects_unknown_methods(tmp_path, monkeypatch):
    _accept_synthetic_runtime_identity(monkeypatch)
    executable = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    bridge = Cinema4dBridge(executable=executable, allowed_roots=[tmp_path])

    with pytest.raises(BridgeError, match="Cinema 4D operation failed"):
        bridge._invoke("unsafe.eval", {}, 10)


def test_bridge_waits_when_executable_launches_worker_and_exits(tmp_path, monkeypatch):
    _accept_synthetic_runtime_identity(monkeypatch)
    launcher = tmp_path / "launcher.py"
    driver = Path(__file__).parents[1] / "src" / "dcc_mcp_cinema4d" / "cinema4d_driver.py"
    launcher.write_text(
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, %r, *sys.argv[1:]])\n" % str(driver),
        encoding="utf-8",
    )
    executable = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    bridge = Cinema4dBridge(executable=executable, allowed_roots=[tmp_path])
    bridge.driver_path = launcher

    with pytest.raises(BridgeError, match="Cinema 4D operation failed"):
        bridge._invoke("unsafe.eval", {}, 10)


def test_bridge_allows_acknowledged_runtime_to_exit_before_cleanup(tmp_path, monkeypatch):
    _accept_synthetic_runtime_identity(monkeypatch)
    exited = tmp_path / "runtime-exited"
    driver = tmp_path / "delayed-exit-driver.py"
    driver.write_text(
        "import json, os, pathlib, sys, time\n"
        "request, result, runtime, ready_ack, result_ack = sys.argv[1:]\n"
        "runtime_tmp=pathlib.Path(runtime + '.tmp')\n"
        "runtime_tmp.write_text(json.dumps({'pid': os.getpid(), 'protocol': 1}))\n"
        "runtime_tmp.replace(runtime)\n"
        "while not pathlib.Path(ready_ack).is_file(): time.sleep(0.01)\n"
        "result_tmp=pathlib.Path(result + '.tmp')\n"
        "result_tmp.write_text(json.dumps({'ok': False, 'error': {'type': 'ValueError'}}))\n"
        "result_tmp.replace(result)\n"
        "while not pathlib.Path(result_ack).is_file(): time.sleep(0.01)\n"
        "time.sleep(0.2)\n"
        "pathlib.Path(%r).write_text('exited')\n" % str(exited),
        encoding="utf-8",
    )
    executable = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    bridge = Cinema4dBridge(executable=executable, allowed_roots=[tmp_path])
    bridge.driver_path = driver

    with pytest.raises(BridgeError, match="Cinema 4D operation failed"):
        bridge._invoke("unsafe.eval", {}, 10)

    assert exited.read_text(encoding="utf-8") == "exited"


def test_runtime_exit_rechecks_the_original_deadline_after_ownership_turns_false(
    monkeypatch,
):
    class SlowOwnershipProbe:
        def owns_pid(self, _pid):
            monkeypatch.setattr(bridge_module.time, "monotonic", lambda: 10.1)
            return False

    monkeypatch.setattr(bridge_module.time, "monotonic", lambda: 9.9)

    with pytest.raises(BridgeTimeoutError, match="configured timeout"):
        Cinema4dBridge._wait_for_runtime_exit(SlowOwnershipProbe(), 1234, deadline=10.0)


def test_bridge_cleanup_cannot_turn_an_expired_operation_into_success(tmp_path, monkeypatch):
    clock = [100.0]

    class FakeProcess:
        returncode = 0

        @staticmethod
        def poll():
            return 0

    class FakeOwned:
        process = FakeProcess()

        @staticmethod
        def wait_pid_exit_until(_pid, _deadline):
            return True

        @staticmethod
        def close(*, deadline):
            assert deadline == 100.05
            clock[0] = 100.06

    def fake_start(_command, *, cwd, **_kwargs):
        (cwd / "result.json").write_text(
            json.dumps({"ok": True, "result": {"ready": True}}), encoding="utf-8"
        )
        return FakeOwned()

    executable = str(Path(getattr(sys, "_base_executable", sys.executable)).resolve())
    bridge = Cinema4dBridge(executable=executable, allowed_roots=[tmp_path])
    monkeypatch.setattr(bridge_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(bridge_module, "start_owned_process", fake_start)
    monkeypatch.setattr(
        bridge,
        "_acknowledge_runtime",
        lambda *_args, **_kwargs: {"pid": 1234, "start_identity": "synthetic"},
    )
    monkeypatch.setattr(bridge, "_recapture_runtime", lambda _identity: None)

    with pytest.raises(BridgeTimeoutError, match="configured timeout"):
        bridge._invoke("system.status", {}, timeout_secs=0.05)


def test_private_artifact_retries_transient_windows_sharing_denial(tmp_path, monkeypatch):
    artifact = tmp_path / "result.json"
    artifact.write_bytes(b"{}")
    original_open = Path.open
    attempts = []

    def transient_open(path, *args, **kwargs):
        if path == artifact and not attempts:
            attempts.append("denied")
            raise PermissionError(13, "synthetic sharing denial")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", transient_open)

    payload = bridge_module._read_private_file(
        artifact, 64, deadline=bridge_module.time.monotonic() + 1.0
    )

    assert payload == b"{}"
    assert attempts == ["denied"]


@pytest.mark.parametrize("timeout", [math.nan, math.inf, -math.inf])
def test_bridge_rejects_non_finite_timeout(timeout, tmp_path):
    bridge = Cinema4dBridge(executable=sys.executable, allowed_roots=[tmp_path])

    with pytest.raises(BridgeError, match="finite"):
        bridge._timeout(timeout)
