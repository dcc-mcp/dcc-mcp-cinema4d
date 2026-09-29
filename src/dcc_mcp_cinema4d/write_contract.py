"""Post-write read-back contract for mutating Cinema 4D tools.

The failure mode this module exists to eliminate is **"reported success, scene
unchanged"**. In an agent loop that is the most expensive class of bug there is:
the caller receives an affirmative answer, keeps building on it, and the error
only surfaces steps later as an unrelated symptom.

The contract is one sentence:

    A mutating tool returns only after it has read the target back and proven
    that this call's change is actually there.

Specifically, a mutating tool must never:

* return the object payload it just built in memory as if that proved the save;
* report success when the durable document reopened without the change;
* report success when the written file is empty or the wrong shape.

Where the read-back happens
---------------------------

Cinema 4D is driven from a separate ``c4dpy`` process, and the bridge already
re-opens the durable document after every mutation to report ``document``. That
re-open is the read-back, and it is the authority: the in-memory payload only
proves what the driver *built*, not what survived the save. Verification
therefore compares the two on the host side, which has two consequences worth
stating plainly:

* it costs no extra ``c4dpy`` launch for document mutations, and
* it is testable without a licensed host, so the contract itself is covered by
  contract tests rather than only by a manual live run.

That second point is a scope statement, not a brag: this repo has no real-host
CI, so these checks are **contract-level** evidence. They prove the adapter
compares what it was asked for against what it read back; they do not prove
Cinema 4D itself behaved. See :func:`read_back_level`.

Two properties matter more than the exact checks:

* **Expected and actual are always both reported.** A mismatch that only says
  "failed" makes the caller guess; the pair is what makes it actionable.
* **The host build is always attached.** A read-back that disagrees is the
  classic signature of host API drift, and without the build the report is
  unreproducible.
"""

from __future__ import annotations

import math
import os
import struct
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 1

# Read-back evidence levels. Recorded in every mutating result so a caller can
# tell "the change was re-read and compared" from "the file's size was checked".
READ_BACK_COMPARED = "compared"
READ_BACK_SIZE_ONLY = "size_only"

# Floating point read-back tolerance. Cinema 4D stores transforms as doubles and
# round-trips them, so the tolerance absorbs serialisation noise, not a real
# difference. It is deliberately far tighter than any modelling-relevant delta.
DEFAULT_REL_TOLERANCE = 1e-6
DEFAULT_ABS_TOLERANCE = 1e-9

# Methods that change a document or write a file. Every entry owes a read-back.
MUTATING_TOOLS = (
    "document.create",
    "document.save_copy",
    "document.export",
    "document.render",
    "model.add_primitive",
    "model.transform_object",
    "model.remove_object",
    "model.import_geometry",
)

# Methods that observe state and change nothing. Kept here so the classification
# test can prove no method is left unclassified.
READ_ONLY_TOOLS = (
    "system.status",
    "document.inspect",
    "document.validate",
)

TOOL_CLASSIFICATION_ERROR = (
    "every driver method must be listed in write_contract.MUTATING_TOOLS or "
    "write_contract.READ_ONLY_TOOLS; an unclassified method has no answer to "
    "'does this owe a post-write read-back?'"
)

# Exported formats whose leading bytes identify them. A suffix absent from this
# table is verified by size only, and that weaker level is recorded rather than
# being presented as a full read-back.
# Accepted leading bytes per export suffix. Several formats have more than one
# legitimate encoding — Cinema 4D writes binary *or* ASCII FBX, and a Collada
# file may omit the XML declaration — so each entry is a tuple of alternatives.
# The check exists to catch a wrong, empty, or placeholder artifact, not to
# enforce one encoding: a false rejection of a valid export is a worse outcome
# than a weaker check, so an unfamiliar encoding is not treated as a mismatch.
_FILE_SIGNATURES = {
    ".glb": (b"glTF",),
    ".gltf": (b"{",),
    ".fbx": (b"Kaydara FBX Binary", b"; FBX", b"FBXVersion"),
    ".dae": (b"<?xml", b"<COLLADA", b"<collada"),
    ".stl": (b"solid",),
}

_TEXT_VERTEX_PREFIXES = (".obj",)

_UTF8_BOM = b"\xef\xbb\xbf"


def jsonable(value):
    """Coerce ``value`` into something ``json.dump`` accepts.

    Read-back evidence crosses a process boundary, so anything that cannot be
    represented in JSON is rendered as text rather than dropped: a dropped
    field is how a report ends up saying "expected something, got something".
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # NaN/Infinity are not valid JSON; keep them visible as text.
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    return repr(value)


def _describe(value):
    """Render one side of an expected/actual pair compactly."""
    if isinstance(value, (list, tuple)):
        return "[%s]" % ", ".join(_describe(item) for item in value)
    if isinstance(value, dict):
        return "{%s}" % ", ".join("%s: %s" % (key, _describe(item)) for key, item in value.items())
    if isinstance(value, float):
        return repr(value)
    return str(value)


def numbers_match(expected, actual, rel_tolerance=None, abs_tolerance=None):
    """Compare two scalars with the contract's default tolerance."""
    try:
        return math.isclose(
            float(expected),
            float(actual),
            rel_tol=DEFAULT_REL_TOLERANCE if rel_tolerance is None else rel_tolerance,
            abs_tol=DEFAULT_ABS_TOLERANCE if abs_tolerance is None else abs_tolerance,
        )
    except (TypeError, ValueError):
        return False


def sequences_match(expected, actual, rel_tolerance=None, abs_tolerance=None):
    """Compare two numeric sequences element by element.

    A length mismatch is a mismatch, not a truncated comparison: reporting
    three coordinates against two would hide the difference.
    """
    try:
        expected = [float(item) for item in expected]
        actual = [float(item) for item in actual]
    except (TypeError, ValueError):
        return False
    if len(expected) != len(actual):
        return False
    return all(
        numbers_match(item, other, rel_tolerance, abs_tolerance)
        for item, other in zip(expected, actual)
    )


def expected_transform(params: Dict[str, Any]) -> Dict[str, List[float]]:
    """The transform a request asked for, with the driver's own defaults.

    Mirrors ``cinema4d_driver._set_transform`` so the comparison is against the
    values actually applied, not against whichever keys the caller happened to
    send.
    """
    return {
        "translation": [float(item) for item in params.get("translation") or (0.0, 0.0, 0.0)],
        "rotation_hpb_degrees": [
            float(item) for item in params.get("rotation_hpb_degrees") or (0.0, 0.0, 0.0)
        ],
        "scale": [float(item) for item in params.get("scale") or (1.0, 1.0, 1.0)],
    }


def transform_matches(expected: Dict[str, Any], actual: Any) -> bool:
    """Compare a requested transform against a read-back ``transform`` block."""
    if not isinstance(actual, dict):
        return False
    for key, wanted in expected.items():
        got = actual.get(key)
        if not isinstance(got, (list, tuple)) or not sequences_match(wanted, got):
            return False
    return True


def find_object(document: Any, name: str) -> Optional[Dict[str, Any]]:
    """Find a top-level-named object in a ``document.inspect`` payload."""
    if not isinstance(document, dict):
        return None
    for entry in document.get("objects") or ():
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return None


def object_paths(document: Any) -> List[str]:
    if not isinstance(document, dict):
        return []
    return [
        str(entry.get("path"))
        for entry in document.get("objects") or ()
        if isinstance(entry, dict) and entry.get("path") is not None
    ]


def file_signature_matches(path: str, suffix: str) -> Tuple[bool, Optional[str]]:
    """Check a written file really is in the format its suffix claims.

    Returns ``(matches, detail)``. ``matches`` is True when no signature is
    known for the suffix — an unknown container is not evidence of a wrong one,
    and the caller records the weaker level instead of failing the write.
    """
    lowered = suffix.lower()
    if lowered in _TEXT_VERTEX_PREFIXES:
        return _text_geometry_matches(path)
    signatures = _FILE_SIGNATURES.get(lowered)
    if signatures is None:
        return True, None
    try:
        if lowered == ".stl":
            return _stl_matches(path, signatures)
        with open(path, "rb") as stream:
            header = stream.read(64)
    except OSError as exc:
        return False, "the written file could not be re-read: %s" % exc
    if not header:
        return False, "the written file is empty"
    # Text-based containers may carry an XML declaration, a BOM, or leading
    # whitespace; the shape being checked for is the format, not the encoding's
    # preamble.
    header = header.lstrip()
    if header.startswith(_UTF8_BOM):
        header = header[len(_UTF8_BOM) :].lstrip()
    if not header.startswith(signatures):
        return False, "the file does not start with a %s signature" % lowered
    return True, None


def _text_geometry_matches(path: str) -> Tuple[bool, Optional[str]]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.split(" ", 1)[0].lower() in {"v", "vn", "vt", "f"}:
                    return True, None
                break
    except OSError as exc:
        return False, "the written file could not be re-read: %s" % exc
    return False, "the file contains no geometry keywords"


def _stl_matches(path: str, signatures: Tuple[bytes, ...]) -> Tuple[bool, Optional[str]]:
    try:
        with open(path, "rb") as stream:
            header = stream.read(84)
    except OSError as exc:
        return False, "the written file could not be re-read: %s" % exc
    if header.startswith(signatures):
        return True, None
    # Binary STL: 80-byte header followed by a little-endian triangle count that
    # must account for the whole remaining file.
    if len(header) < 84:
        return False, "the file is shorter than a binary STL header"
    try:
        count = struct.unpack("<I", header[80:84])[0]
    except struct.error:
        return False, "the binary STL triangle count could not be read"
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return False, "the written file could not be re-read: %s" % exc
    if size != 84 + count * 50:
        return False, "the binary STL triangle count does not match the file size"
    return True, None


def _png_dimensions(header: bytes) -> Optional[Tuple[int, int]]:
    if not header.startswith(b"\x89PNG\r\n\x1a\n") or len(header) < 24:
        return None
    if header[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", header[16:24])
    return int(width), int(height)


def _jpeg_dimensions(header: bytes) -> Optional[Tuple[int, int]]:
    if not header.startswith(b"\xff\xd8"):
        return None
    offset = 2
    size = len(header)
    while offset + 9 < size:
        if header[offset] != 0xFF:
            offset += 1
            continue
        marker = header[offset + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            offset += 2
            continue
        if marker in (0xDD,):
            return None
        try:
            (length,) = struct.unpack(">H", header[offset + 2 : offset + 4])
        except struct.error:
            return None
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            try:
                height, width = struct.unpack(">HH", header[offset + 5 : offset + 9])
            except struct.error:
                return None
            return int(width), int(height)
        if length <= 0:
            return None
        offset += 2 + length
    return None


def image_dimensions(path: str) -> Optional[Tuple[int, int]]:
    """Read the pixel dimensions back out of a written PNG or JPEG."""
    try:
        with open(path, "rb") as stream:
            header = stream.read(1 << 16)
    except OSError:
        return None
    return _png_dimensions(header) or _jpeg_dimensions(header)


def read_back_level(suffix: str) -> str:
    """How strong the artifact read-back is for an output suffix."""
    lowered = suffix.lower()
    if lowered in _FILE_SIGNATURES or lowered in _TEXT_VERTEX_PREFIXES:
        return READ_BACK_COMPARED
    return READ_BACK_SIZE_ONLY


def format_message(payload):
    """Render the human- and agent-readable sentence for a mismatch.

    Deliberately states the tool, the check, both values, and the host build in
    that order: the reader should never have to re-run the call to find out
    what differed.
    """
    tool = payload.get("tool") or "unknown tool"
    check = payload.get("check") or "unknown check"
    message = (
        "%s did not take effect: the post-write read-back disagreed on %s "
        "(expected %s, read back %s)"
        % (
            tool,
            check,
            _describe(payload.get("expected")),
            _describe(payload.get("actual")),
        )
    )
    build = payload.get("host_build")
    if build:
        message += "; host Cinema 4D build %s" % build
    matrix = payload.get("host_matrix") or {}
    if matrix.get("status"):
        message += " (matrix status: %s)" % matrix["status"]
    remediation = payload.get("remediation")
    if remediation:
        message += ". %s" % remediation
    return message


class WriteVerificationError(RuntimeError):
    """A mutating tool reported success but the read-back disagreed.

    The structured :attr:`payload` travels with the exception so the boundary
    that serialises the error can forward it verbatim and the caller can branch
    on ``check``/``expected``/``actual`` instead of parsing prose.
    """

    def __init__(
        self,
        tool,
        check,
        expected=None,
        actual=None,
        host_build=None,
        host_matrix=None,
        params=None,
        remediation=None,
        message=None,
    ):
        self.payload = {
            "schema_version": SCHEMA_VERSION,
            "tool": tool,
            "check": check,
            "expected": jsonable(expected),
            "actual": jsonable(actual),
            "host_build": host_build,
            "host_matrix": jsonable(host_matrix),
            "params": jsonable(params),
            "remediation": remediation,
        }
        super().__init__(message or format_message(self.payload))

    @property
    def tool(self):
        return self.payload.get("tool")

    @property
    def check(self):
        return self.payload.get("check")

    @property
    def expected(self):
        return self.payload.get("expected")

    @property
    def actual(self):
        return self.payload.get("actual")

    @property
    def host_build(self):
        return self.payload.get("host_build")

    @classmethod
    def from_payload(cls, payload):
        """Rebuild the error on the caller's side of a process boundary."""
        return cls(
            tool=payload.get("tool"),
            check=payload.get("check"),
            expected=payload.get("expected"),
            actual=payload.get("actual"),
            host_build=payload.get("host_build"),
            host_matrix=payload.get("host_matrix"),
            params=payload.get("params"),
            remediation=payload.get("remediation"),
            message=format_message(payload),
        )


def unclassified_tools(methods: Sequence[str]) -> List[str]:
    """Driver methods with no answer to 'does this owe a read-back?'."""
    known = set(MUTATING_TOOLS) | set(READ_ONLY_TOOLS)
    return sorted(set(methods) - known)
