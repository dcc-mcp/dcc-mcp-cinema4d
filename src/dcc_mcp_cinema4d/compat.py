"""Machine-readable Cinema 4D host compatibility matrix.

The matrix itself lives in ``compat_matrix.json`` next to this module and ships
inside the wheel, so the service-side preflight and the ``doctor``/``verify``
report read one single source of truth.

Two design points matter, and both come from this adapter having no real-host
CI (a licensed ``c4dpy`` cannot be provisioned on GitHub-hosted runners):

1. **A build outside the declared ranges is rejected explicitly.** It is never
   silently treated as "close enough".

2. **The support decision does not rest on the declared interpreter mapping.**
   That mapping is ``declared_not_executed`` — it records what the adapter
   believes, not what it has run. So :func:`classify_host` gates on the c4dpy
   interpreter version *observed* from ``system.status`` when it is available,
   and reports :func:`interpreter_drift` whenever reality disagrees with the
   matrix. An unverified mapping then gets corrected by the first licensed run
   instead of being quietly trusted.

A version is therefore classified on evidence, and the absence of evidence is
reported as a distinct status rather than being rounded up to "supported".
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

MATRIX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "compat_matrix.json")

SUPPORTED = "supported"
TOO_OLD = "too_old"
TOO_NEW = "too_new"
UNLISTED = "unlisted"
UNKNOWN = "unknown"
HOST_PYTHON_UNSUPPORTED = "host_python_unsupported"

STATUS_MESSAGES = {
    SUPPORTED: "supported",
    TOO_OLD: "below the supported range",
    TOO_NEW: "above the supported range",
    UNLISTED: "inside the covered span but not in any declared range",
    UNKNOWN: "not recognised as a Cinema 4D build",
    HOST_PYTHON_UNSUPPORTED: "declared, but its c4dpy interpreter cannot run the packaged driver",
}

_PYTHON_RELEASE = re.compile(r"^(\d+)(?:\.(\d+))?")


def parse_build(value: Any) -> Optional[int]:
    """Coerce a ``c4d.GetC4DVersion()`` value into an integer build.

    Accepts an int or the string form it is serialised to across the c4dpy
    boundary. Anything else is ``None``, which classifies as unknown rather
    than being coerced into a plausible number.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        # 0 is the sentinel an unprobed runtime reports, not a real build.
        return value if value > 0 else None
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = int(text)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _parse_python(value: Any) -> Optional[Tuple[int, int]]:
    """Parse the leading ``major[.minor]`` of a Python version string."""
    if value is None or isinstance(value, bool):
        return None
    match = _PYTHON_RELEASE.match(str(value).strip())
    if match is None:
        return None
    major, minor = match.groups()
    return int(major), int(minor or 0)


def load_matrix(path: Optional[str] = None) -> Dict[str, Any]:
    with open(path or MATRIX_PATH, "r", encoding="utf-8") as stream:
        return json.load(stream)


def supported_ranges(matrix: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    return list((matrix or load_matrix()).get("supported_ranges") or ())


def supported_range_labels(matrix: Optional[Dict[str, Any]] = None) -> List[str]:
    labels = []
    for entry in supported_ranges(matrix):
        minimum = entry.get("min_build")
        maximum = entry.get("max_build")
        if not isinstance(minimum, int) or not isinstance(maximum, int):
            continue
        if entry.get("driver_compatible") is False:
            continue
        labels.append(str(entry.get("id") or "%d-%d" % (minimum, maximum)))
    return labels


def declared_range_labels(matrix: Optional[Dict[str, Any]] = None) -> List[str]:
    """Every declared range, including ones the packaged driver cannot run."""
    return [str(entry.get("id")) for entry in supported_ranges(matrix)]


def find_range(build: int, matrix: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    for entry in supported_ranges(matrix):
        minimum = entry.get("min_build")
        maximum = entry.get("max_build")
        if not isinstance(minimum, int) or not isinstance(maximum, int):
            continue
        if minimum <= build <= maximum:
            return entry
    return None


def interpreter_drift(
    entry: Optional[Dict[str, Any]], python_version: Optional[str]
) -> Optional[Dict[str, Any]]:
    """Compare the observed c4dpy interpreter against the declared expectation.

    Returns ``None`` when there is nothing to compare — an unknown interpreter
    is reported as unknown, never as agreement.
    """
    if entry is None:
        return None
    declared = str(entry.get("c4dpy_python") or "").strip()
    observed = _parse_python(python_version)
    if not declared or observed is None:
        return None
    expected = _parse_python(declared)
    if expected is None:
        return None
    if expected[0] == observed[0] and expected[1] == observed[1]:
        return None
    return {
        "declared": declared,
        "observed": str(python_version),
        "matches": False,
        "confidence": entry.get("confidence"),
        "why": (
            "compat_matrix.json declares c4dpy Python %s for %s, but the runtime "
            "reported %s. The declared mapping is unverified, so the observed "
            "interpreter was used for the support decision; correct the matrix so "
            "the declaration matches reality." % (declared, entry.get("id"), python_version)
        ),
    }


def classify_host(
    build: Any, python_version: Optional[str] = None, matrix: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Classify a Cinema 4D build against the matrix.

    The result is machine readable and is embedded verbatim in the doctor and
    verify reports, so callers can gate on ``status`` instead of parsing prose.
    """
    matrix = matrix or load_matrix()
    parsed = parse_build(build)
    entry = find_range(parsed, matrix) if parsed is not None else None
    verdict: Dict[str, Any] = {
        "build": parsed,
        "build_raw": build if isinstance(build, (int, str)) else None,
        "status": UNKNOWN,
        "matrix_version": matrix.get("matrix_version"),
        "supported_ranges": supported_range_labels(matrix),
        "declared_ranges": declared_range_labels(matrix),
        "range": None,
        "host_python_version": python_version,
        "driver_requires_python_major": matrix.get("driver_requires_python_major"),
        "interpreter_drift": None,
        "warnings": [],
    }
    if parsed is None:
        return verdict

    if entry is None:
        lows = [
            item["min_build"]
            for item in supported_ranges(matrix)
            if isinstance(item.get("min_build"), int)
        ]
        highs = [
            item["max_build"]
            for item in supported_ranges(matrix)
            if isinstance(item.get("max_build"), int)
        ]
        low = min(lows, default=None)
        high = max(highs, default=None)
        if low is not None and parsed < low:
            verdict["status"] = TOO_OLD
        elif high is not None and parsed > high:
            verdict["status"] = TOO_NEW
        else:
            verdict["status"] = UNLISTED
        return verdict

    verdict["range"] = {
        "id": entry.get("id"),
        "label": entry.get("label"),
        "min_build": entry.get("min_build"),
        "max_build": entry.get("max_build"),
        "c4dpy_python": entry.get("c4dpy_python"),
        "confidence": entry.get("confidence"),
        "driver_compatible": entry.get("driver_compatible"),
    }

    drift = interpreter_drift(entry, python_version)
    if drift is not None:
        verdict["interpreter_drift"] = drift
        verdict["warnings"].append("interpreter_drift")

    # The observed interpreter wins when it is known: it is real evidence, while
    # the declared mapping is only a claim about this release line.
    observed = _parse_python(python_version)
    required = matrix.get("driver_requires_python_major")
    if observed is not None and isinstance(required, int):
        python_major = observed[0]
    else:
        python_major = entry.get("c4dpy_python_major")
    if not isinstance(python_major, int) or isinstance(python_major, bool):
        verdict["warnings"].append("host_python_unknown")
        verdict["status"] = SUPPORTED if entry.get("driver_compatible") else HOST_PYTHON_UNSUPPORTED
        return verdict

    if not isinstance(required, int) or python_major < required:
        verdict["status"] = HOST_PYTHON_UNSUPPORTED
        return verdict

    verdict["status"] = SUPPORTED
    return verdict


def is_supported(
    build: Any, python_version: Optional[str] = None, matrix: Optional[Dict[str, Any]] = None
) -> bool:
    return classify_host(build, python_version, matrix)["status"] == SUPPORTED


def unsupported_reason(verdict: Dict[str, Any]) -> str:
    """Build the human- and agent-readable rejection sentence for a verdict."""
    ranges = verdict.get("supported_ranges") or ()
    covered = ", ".join(ranges) if ranges else "no declared range"
    build = verdict.get("build")
    label = "unknown" if build is None else str(build)
    status = verdict.get("status")
    if status == UNKNOWN:
        return "Cinema 4D reported an unrecognised build %s; supported release lines: %s" % (
            label,
            covered,
        )
    if status == TOO_NEW:
        return (
            "Cinema 4D build %s is newer than the declared compatibility matrix "
            "(declared: %s); the host API may have moved, so the adapter refuses to "
            "run unverified" % (label, covered)
        )
    if status == UNLISTED:
        return (
            "Cinema 4D build %s is not listed in the declared compatibility matrix "
            "(declared: %s); the adapter refuses to run unverified" % (label, covered)
        )
    if status == HOST_PYTHON_UNSUPPORTED:
        entry = verdict.get("range") or {}
        declared = entry.get("c4dpy_python") or "an unsupported interpreter"
        observed = verdict.get("host_python_version")
        observed_text = (
            "the runtime reported Python %s" % observed
            if observed
            else ("the runtime did not report an interpreter version")
        )
        return (
            "%s ships c4dpy with Python %s and %s; the packaged driver "
            "requires Python %s or newer inside c4dpy, so this release cannot run the "
            "adapter. Use a release line whose c4dpy provides Python 3."
            % (
                entry.get("label") or ("Cinema 4D build " + label),
                declared,
                observed_text,
                verdict.get("driver_requires_python_major"),
            )
        )
    if status == TOO_OLD:
        return (
            "Cinema 4D build %s is older than the declared compatibility matrix "
            "(declared: %s); the adapter refuses to run unverified" % (label, covered)
        )
    return "Cinema 4D build %s is unsupported; supported release lines: %s" % (label, covered)


def remediation_steps(verdict: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Concrete next steps for a non-supported verdict."""
    status = verdict.get("status")
    if status == SUPPORTED:
        return []
    ranges = verdict.get("supported_ranges") or ()
    covered = ", ".join(ranges) if ranges else "no declared range"
    if status == HOST_PYTHON_UNSUPPORTED:
        return [
            {
                "id": "upgrade-cinema4d",
                "description": "Use a Cinema 4D release line whose c4dpy provides Python 3 "
                "(declared: %s)" % covered,
                "command": ["dcc-mcp-cinema4d", "doctor", "--json"],
                "why": "The packaged driver runs inside c4dpy and requires Python 3.",
            }
        ]
    if status == UNKNOWN:
        return [
            {
                "id": "recheck-host-matrix",
                "description": "Re-run the compatibility preflight against the pinned c4dpy",
                "command": ["dcc-mcp-cinema4d", "doctor", "--json"],
                "why": "The runtime did not report a build this matrix can classify.",
            }
        ]
    return [
        {
            "id": "pin-supported-cinema4d",
            "description": "Point the adapter at a Cinema 4D release in the declared "
            "compatibility matrix: %s" % covered,
            "command": ["dcc-mcp-cinema4d", "doctor", "--json", "--c4dpy", "<cinema4d>/c4dpy"],
            "why": "A build outside the declared matrix is never treated as compatible.",
        }
    ]
