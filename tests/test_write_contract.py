"""Contract tests for the post-write read-back contract.

**What these prove, and what they do not.**

These tests prove the adapter compares what it was asked for against what it
read back, and refuses to report success when the two disagree. They run against
an in-memory model of the Cinema 4D side, so they are **contract-level
evidence**: they do not prove that Cinema 4D itself persists a change. That
needs a licensed c4dpy and is covered by the opt-in ``cinema4d`` marker, not by
CI. The distinction is deliberate and is recorded in ``compat_matrix.json``.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest
from test_bridge import FakeBridge, _file_identity, _jpeg_bytes, _png_bytes

from dcc_mcp_cinema4d import write_contract
from dcc_mcp_cinema4d.bridge import WriteVerificationError
from dcc_mcp_cinema4d.cinema4d_driver import _METHODS


def test_every_driver_method_is_classified() -> None:
    """A method that is neither mutating nor read-only has no read-back answer."""
    assert write_contract.unclassified_tools(_METHODS) == []


def test_mutation_and_read_only_sets_do_not_overlap() -> None:
    overlap = set(write_contract.MUTATING_TOOLS) & set(write_contract.READ_ONLY_TOOLS)
    assert overlap == set()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def test_numbers_match_uses_a_tight_tolerance() -> None:
    assert write_contract.numbers_match(1.0, 1.0)
    assert write_contract.numbers_match(1.0, 1.0000001)
    assert not write_contract.numbers_match(1.0, 1.001)
    assert not write_contract.numbers_match(1.0, "x")


def test_sequences_match_rejects_a_length_mismatch() -> None:
    assert write_contract.sequences_match([1, 2, 3], [1.0, 2.0, 3.0])
    assert not write_contract.sequences_match([1, 2, 3], [1.0, 2.0])


def test_expected_transform_applies_the_driver_defaults() -> None:
    assert write_contract.expected_transform({}) == {
        "translation": [0.0, 0.0, 0.0],
        "rotation_hpb_degrees": [0.0, 0.0, 0.0],
        "scale": [1.0, 1.0, 1.0],
    }


def test_transform_matches_reports_a_partial_transform_as_matching() -> None:
    assert write_contract.transform_matches(
        {"translation": [1.0, 2.0, 3.0]},
        {"translation": [1.0, 2.0, 3.0], "rotation_hpb_degrees": [0, 0, 0], "scale": [1, 1, 1]},
    )
    assert not write_contract.transform_matches({"translation": [1.0, 2.0, 3.0]}, None)


@pytest.mark.parametrize(
    ("suffix", "payload", "expected"),
    [
        (".glb", b"glTF\x02\x00\x00\x00rest", True),
        (".glb", b"not-a-gltf-file", False),
        (".fbx", b"Kaydara FBX Binary  \x00rest", True),
        (".fbx", b"; FBX 7.4.0 project file\nFBXVersion: 7400\n", True),
        (".fbx", b"FBXVersion: 7400\n", True),
        (".fbx", b"not-an-fbx-file", False),
        (".dae", b'<?xml version="1.0"?><COLLADA/>', True),
        (".dae", b"<COLLADA/>", True),  # the XML declaration is optional
        (".dae", b"<html/>", False),
        (".obj", b"# comment\nv 0 0 0\nv 1 0 0\n", True),
        (".obj", b"# comment only\n", False),
        (".c4d", b"anything-at-all", True),  # opaque container: size only
    ],
)
def test_file_signature_matches(
    tmp_path: Path, suffix: str, payload: bytes, expected: bool
) -> None:
    target = tmp_path / ("artifact" + suffix)
    target.write_bytes(payload)

    matches, _detail = write_contract.file_signature_matches(str(target), suffix)

    assert matches is expected


def test_signature_check_tolerates_a_leading_preamble(tmp_path: Path) -> None:
    """A BOM or leading whitespace is not a format mismatch."""
    target = tmp_path / "scene.gltf"
    target.write_bytes(b"\xef\xbb\xbf  \n" + b'{"asset":{"version":"2.0"}}')

    assert write_contract.file_signature_matches(str(target), ".gltf")[0] is True


def test_signature_check_accepts_every_encoding_cinema4d_may_write(tmp_path: Path) -> None:
    """Cinema 4D exports binary *or* ASCII FBX; rejecting either would be wrong.

    The check targets a wrong, empty, or placeholder artifact. Enforcing one
    encoding would fail valid exports, which is a worse outcome than a weaker
    check.
    """
    for payload in (b"Kaydara FBX Binary  \x00\x1a\x00", b"; FBX 7.4.0 project file\n"):
        target = tmp_path / "model.fbx"
        target.write_bytes(payload)

        assert write_contract.file_signature_matches(str(target), ".fbx")[0] is True


def test_stl_signatures_cover_ascii_and_binary(tmp_path: Path) -> None:
    ascii_stl = tmp_path / "ascii.stl"
    ascii_stl.write_bytes(b"solid mesh\nendsolid mesh\n")
    assert write_contract.file_signature_matches(str(ascii_stl), ".stl")[0] is True

    binary_stl = tmp_path / "binary.stl"
    binary_stl.write_bytes(b"\x00" * 80 + struct.pack("<I", 2) + b"\x00" * 100)
    assert write_contract.file_signature_matches(str(binary_stl), ".stl")[0] is True

    truncated = tmp_path / "truncated.stl"
    truncated.write_bytes(b"\x00" * 80 + struct.pack("<I", 7) + b"\x00" * 10)
    assert write_contract.file_signature_matches(str(truncated), ".stl")[0] is False


def test_image_dimensions_are_read_back_out_of_the_written_file(tmp_path: Path) -> None:
    png = tmp_path / "frame.png"
    png.write_bytes(_png_bytes(800, 600))
    assert write_contract.image_dimensions(str(png)) == (800, 600)

    jpeg = tmp_path / "frame.jpg"
    jpeg.write_bytes(_jpeg_bytes(1024, 768))
    assert write_contract.image_dimensions(str(jpeg)) == (1024, 768)


def test_read_back_level_distinguishes_compared_from_size_only() -> None:
    assert write_contract.read_back_level(".glb") == write_contract.READ_BACK_COMPARED
    assert write_contract.read_back_level(".obj") == write_contract.READ_BACK_COMPARED
    # An opaque container is verified by size only, and says so.
    assert write_contract.read_back_level(".c4d") == write_contract.READ_BACK_SIZE_ONLY
    assert write_contract.read_back_level(".abc") == write_contract.READ_BACK_SIZE_ONLY


# --------------------------------------------------------------------------
# the contract itself: a change that did not persist must not report success
# --------------------------------------------------------------------------


def _document(tmp_path: Path) -> Path:
    document = tmp_path / "scene.c4d"
    document.write_bytes(b"C4D")
    return document


def test_add_primitive_is_verified_against_the_reopened_document(tmp_path: Path) -> None:
    document = _document(tmp_path)
    bridge = FakeBridge(tmp_path)

    result = bridge.add_primitive(str(document), "cube", "Body", {"size": [10, 20, 30]})

    assert result["read_back"] == write_contract.READ_BACK_COMPARED
    assert result["document"]["object_count"] == 1
    assert result["document"]["objects"][0]["name"] == "Body"


def test_add_primitive_that_did_not_persist_is_rejected(tmp_path: Path) -> None:
    document = _document(tmp_path)
    bridge = FakeBridge(tmp_path, persist=False)

    with pytest.raises(WriteVerificationError) as caught:
        bridge.add_primitive(str(document), "cube", "Body", {"size": [10, 20, 30]})

    payload = caught.value.payload
    assert payload["tool"] == "model.add_primitive"
    assert payload["check"] == "object_present"
    assert payload["expected"] == "Body"
    assert payload["actual"] == []
    # Without the build, a mismatch report cannot be reproduced.
    assert payload["host_build"] == 26000
    assert payload["host_matrix"]["status"] == "supported"
    assert "Body" in str(caught.value)


def test_transform_that_did_not_persist_is_rejected(tmp_path: Path) -> None:
    document = _document(tmp_path)
    seeded = FakeBridge(tmp_path)
    seeded.add_primitive(str(document), "cube", "Body", {"size": [1, 1, 1]})
    bridge = FakeBridge(tmp_path, persist=False, documents=seeded.documents, scene=seeded.scene)

    with pytest.raises(WriteVerificationError) as caught:
        bridge.transform_object(str(document), "Body", translation=[5, 0, 0])

    assert caught.value.payload["check"] == "transform"
    assert caught.value.payload["expected"]["translation"] == [5.0, 0.0, 0.0]
    assert caught.value.payload["actual"]["translation"] == [0.0, 0.0, 0.0]


def test_transform_that_persists_wrong_values_is_rejected(tmp_path: Path) -> None:
    """A transform that 'succeeds' but lands on different values is a failure."""

    class DriftingBridge(FakeBridge):
        def _invoke(self, method, params, timeout_secs=120):
            result = super()._invoke(method, params, timeout_secs)
            if method == "model.transform_object":
                result["updated"]["transform"]["scale"] = [9.0, 9.0, 9.0]
                for entry in self.documents.get(_file_identity(params["document_path"]), ()):
                    entry["transform"]["scale"] = [9.0, 9.0, 9.0]
            return result

    document = _document(tmp_path)
    seeded = DriftingBridge(tmp_path)
    seeded.add_primitive(str(document), "cube", "Body", {"size": [1, 1, 1]})
    bridge = DriftingBridge(tmp_path, documents=seeded.documents, scene=seeded.scene)

    with pytest.raises(WriteVerificationError) as caught:
        bridge.transform_object(str(document), "Body", translation=[5, 0, 0], scale=[1, 1, 1])

    assert caught.value.payload["check"] == "transform"
    assert caught.value.payload["actual"]["scale"] == [9.0, 9.0, 9.0]


def test_remove_that_did_not_persist_is_rejected(tmp_path: Path) -> None:
    document = _document(tmp_path)
    seeded = FakeBridge(tmp_path)
    seeded.add_primitive(str(document), "cube", "Body", {"size": [1, 1, 1]})
    bridge = FakeBridge(tmp_path, persist=False, documents=seeded.documents, scene=seeded.scene)

    with pytest.raises(WriteVerificationError) as caught:
        bridge.remove_object(str(document), "Body")

    assert caught.value.payload["check"] == "object_absent"
    assert caught.value.payload["actual"] == ["Body"]


def test_remove_that_persists_is_verified(tmp_path: Path) -> None:
    document = _document(tmp_path)
    seeded = FakeBridge(tmp_path)
    seeded.add_primitive(str(document), "cube", "Body", {"size": [1, 1, 1]})

    result = FakeBridge(tmp_path, documents=seeded.documents, scene=seeded.scene).remove_object(
        str(document), "Body"
    )

    assert result["read_back"] == write_contract.READ_BACK_COMPARED
    assert result["document"]["object_count"] == 0


def test_import_that_did_not_persist_is_rejected(tmp_path: Path) -> None:
    document = _document(tmp_path)
    source = tmp_path / "mesh.obj"
    source.write_bytes(b"v 0 0 0\nf 1 1 1\n")
    bridge = FakeBridge(tmp_path, persist=False)

    with pytest.raises(WriteVerificationError) as caught:
        bridge.import_geometry(str(document), str(source))

    assert caught.value.payload["check"] == "imported_paths_present"
    assert caught.value.payload["expected"] == ["mesh"]


def test_save_copy_that_lost_objects_is_rejected(tmp_path: Path) -> None:
    """A copy that reports success but holds fewer objects is a data-loss bug."""

    class LosingCopyBridge(FakeBridge):
        def _dispatch(self, method, params, output, document):
            result = super()._dispatch(method, params, output, document)
            if method == "document.save_copy":
                # The copy is written, but its contents do not survive.
                self._store(output, [])
            return result

    document = _document(tmp_path)
    seeded = LosingCopyBridge(tmp_path)
    seeded.add_primitive(str(document), "cube", "Body", {"size": [1, 1, 1]})
    bridge = LosingCopyBridge(tmp_path, documents=seeded.documents, scene=seeded.scene)

    with pytest.raises(WriteVerificationError) as caught:
        bridge.save_copy(str(document), str(tmp_path / "copy.c4d"), overwrite=True)

    assert caught.value.payload["check"] == "object_count"
    assert caught.value.payload["expected"] == 1
    assert caught.value.payload["actual"] == 0


def test_save_copy_that_preserves_objects_is_verified(tmp_path: Path) -> None:
    document = _document(tmp_path)
    seeded = FakeBridge(tmp_path)
    seeded.add_primitive(str(document), "cube", "Body", {"size": [1, 1, 1]})

    result = FakeBridge(tmp_path, documents=seeded.documents, scene=seeded.scene).save_copy(
        str(document), str(tmp_path / "copy.c4d"), overwrite=True
    )

    assert result["read_back"] == write_contract.READ_BACK_COMPARED


def test_export_that_wrote_the_wrong_format_is_rejected(tmp_path: Path) -> None:
    class WrongFormatBridge(FakeBridge):
        def _dispatch(self, method, params, output, document):
            result = super()._dispatch(method, params, output, document)
            if output and method == "document.export":
                Path(output).write_bytes(b"C4D-OUTPUT")
            return result

    document = _document(tmp_path)
    bridge = WrongFormatBridge(tmp_path)

    with pytest.raises(WriteVerificationError) as caught:
        bridge.export_document(str(document), str(tmp_path / "scene.glb"), overwrite=True)

    assert caught.value.payload["check"] == "file_format"
    assert "glb" in str(caught.value)


def test_render_at_the_wrong_resolution_is_rejected(tmp_path: Path) -> None:
    class WrongSizeBridge(FakeBridge):
        def _dispatch(self, method, params, output, document):
            result = super()._dispatch(method, params, output, document)
            if output and method == "document.render":
                Path(output).write_bytes(_png_bytes(320, 240))
            return result

    document = _document(tmp_path)
    bridge = WrongSizeBridge(tmp_path)

    with pytest.raises(WriteVerificationError) as caught:
        bridge.render_document(
            str(document), str(tmp_path / "frame.png"), width=640, height=480, overwrite=True
        )

    assert caught.value.payload["check"] == "image_dimensions"
    assert caught.value.payload["expected"] == [640, 480]
    assert caught.value.payload["actual"] == [320, 240]


def test_render_at_the_requested_resolution_is_verified(tmp_path: Path) -> None:
    document = _document(tmp_path)
    bridge = FakeBridge(tmp_path)

    result = bridge.render_document(
        str(document), str(tmp_path / "frame.png"), width=640, height=480, overwrite=True
    )

    assert result["read_back"] == write_contract.READ_BACK_COMPARED


def test_verification_failure_is_a_bridge_error(tmp_path: Path) -> None:
    """Callers that catch BridgeError must still catch a read-back mismatch."""
    from dcc_mcp_cinema4d.bridge import BridgeError

    document = _document(tmp_path)
    bridge = FakeBridge(tmp_path, persist=False)

    with pytest.raises(BridgeError):
        bridge.add_primitive(str(document), "cube", "Body", {"size": [1, 1, 1]})


def test_module_imports_without_cinema4d() -> None:
    """The contract module must not depend on a licensed host to import."""
    assert sys.modules["dcc_mcp_cinema4d.write_contract"] is write_contract
