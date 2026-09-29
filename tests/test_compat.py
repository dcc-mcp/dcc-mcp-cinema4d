"""Contract tests for the Cinema 4D host compatibility matrix.

**Scope statement.** These tests prove the matrix is well formed and that the
classifier behaves as documented. They say nothing about whether Cinema 4D
itself is compatible: this adapter has no real-host CI, so every interpreter
mapping in ``compat_matrix.json`` is declared rather than executed. The tests
below guard that honesty rather than dress it up — see
:func:`test_matrix_declares_that_it_is_unverified`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dcc_mcp_cinema4d import compat


@pytest.fixture(scope="module")
def matrix():
    return compat.load_matrix()


# --------------------------------------------------------------------------
# the matrix artifact
# --------------------------------------------------------------------------


def test_matrix_is_well_formed(matrix) -> None:
    assert matrix["schema_version"] == 1
    assert matrix["host"] == "cinema4d"
    assert matrix["supported_ranges"]


def test_every_range_declares_its_claim_and_its_confidence(matrix) -> None:
    for entry in compat.supported_ranges(matrix):
        assert isinstance(entry["min_build"], int), entry
        assert isinstance(entry["max_build"], int), entry
        assert entry["min_build"] <= entry["max_build"], entry
        assert entry["id"], entry
        assert entry["c4dpy_python"], entry
        # A mapping with no confidence marker is a claim with no status attached.
        assert entry["confidence"] in {"declared_not_executed", "verified"}, entry


def test_ranges_do_not_overlap(matrix) -> None:
    spans = sorted(
        (entry["min_build"], entry["max_build"]) for entry in compat.supported_ranges(matrix)
    )
    for (_low, high), (low_next, _high_next) in zip(spans, spans[1:]):
        assert high < low_next, "overlapping build ranges: %s" % spans


def test_matrix_ships_inside_the_wheel() -> None:
    """A matrix that is not packaged cannot be read on a user's machine."""
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")

    assert "compat_matrix.json" in text


def test_matrix_declares_that_it_is_unverified(matrix) -> None:
    """The honesty guard.

    Without real-host CI the interpreter mapping is a claim, not evidence. If
    someone later gets a licensed c4dpy and verifies a range, they must say so
    per range rather than flipping a global "we tested it" switch.
    """
    evidence = matrix.get("evidence_status") or {}

    assert evidence.get("real_host_ci") is False
    assert "declared_not_executed" in json.dumps(matrix)
    for entry in compat.supported_ranges(matrix):
        if entry["confidence"] == "verified":
            pytest.fail(
                "range %s claims verified status; real-host evidence must accompany it"
                % entry["id"]
            )


def test_no_breaking_change_is_declared_without_evidence(matrix) -> None:
    """An empty list is the honest state, not a placeholder to fill in."""
    if matrix.get("breaking_changes"):
        for entry in matrix["breaking_changes"]:
            assert entry.get("evidence"), "a declared host break needs the run that showed it"


def test_driver_python_requirement_is_declared(matrix) -> None:
    """cinema4d_driver.py uses os.replace and open(encoding=...): Python 3 only."""
    assert matrix["driver_requires_python_major"] == 3
    assert "os.replace" in matrix["driver_requires_python_reason"]


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------


def test_supported_release_is_supported() -> None:
    verdict = compat.classify_host(26000, "3.9.7")

    assert verdict["status"] == compat.SUPPORTED
    assert verdict["range"]["id"] == "2023"
    assert verdict["interpreter_drift"] is None


def test_build_below_the_matrix_is_rejected() -> None:
    verdict = compat.classify_host(20000, "3.9.7")

    assert verdict["status"] == compat.TOO_OLD
    assert "older than" in compat.unsupported_reason(verdict)


def test_build_above_the_matrix_is_rejected_not_assumed() -> None:
    verdict = compat.classify_host(30000, "3.11.4")

    assert verdict["status"] == compat.TOO_NEW
    assert "refuses to run unverified" in compat.unsupported_reason(verdict)


@pytest.mark.parametrize("build", [0, None, "", "not-a-build", True])
def test_unusable_build_is_unknown_not_supported(build) -> None:
    """A missing probe is not the same as an old host."""
    verdict = compat.classify_host(build, "3.9.7")

    assert verdict["status"] == compat.UNKNOWN
    assert compat.is_supported(build, "3.9.7") is False


def test_r21_is_rejected_for_its_interpreter_not_just_its_age() -> None:
    """The build floor is R21, but R21's c4dpy is Python 2.7.

    Encoding this separately matters: reporting R21 as merely "too old" would
    send the user looking for a newer build when the actual constraint is the
    interpreter inside c4dpy.
    """
    verdict = compat.classify_host(21000, "2.7.18")

    assert verdict["status"] == compat.HOST_PYTHON_UNSUPPORTED
    reason = compat.unsupported_reason(verdict)
    assert "Python 2.7" in reason
    assert "Python 3" in reason


def test_r21_is_rejected_even_without_an_observed_interpreter() -> None:
    """The declared mapping still applies when the runtime reports nothing."""
    verdict = compat.classify_host(21000, None)

    assert verdict["status"] == compat.HOST_PYTHON_UNSUPPORTED


def test_observed_interpreter_overrides_the_declared_mapping() -> None:
    """Real evidence beats a declared claim.

    This is the property that keeps an unverified matrix from blocking a working
    host: if the runtime reports a usable interpreter, that is what decides.
    """
    # The matrix declares 3.11 for 2024, but the host reports 3.9.
    verdict = compat.classify_host(27000, "3.9.7")

    assert verdict["status"] == compat.SUPPORTED
    drift = verdict["interpreter_drift"]
    assert drift["declared"] == "3.11"
    assert drift["observed"] == "3.9.7"
    assert drift["matches"] is False
    assert "interpreter_drift" in verdict["warnings"]


def test_drift_is_reported_not_swallowed() -> None:
    verdict = compat.classify_host(27000, "3.11.9")

    assert verdict["interpreter_drift"] is None
    assert verdict["warnings"] == []


def test_drift_is_unknown_when_the_runtime_reports_nothing() -> None:
    """No observation is not agreement."""
    verdict = compat.classify_host(27000, None)

    assert verdict["interpreter_drift"] is None
    assert verdict["status"] == compat.SUPPORTED


def test_unsupported_verdict_carries_actionable_next_steps() -> None:
    verdict = compat.classify_host(21000, "2.7.18")

    steps = compat.remediation_steps(verdict)
    assert steps
    assert steps[0]["id"] == "upgrade-cinema4d"
    assert "why" in steps[0]

    assert compat.remediation_steps(compat.classify_host(26000, "3.9.7")) == []


def test_supported_range_labels_exclude_ranges_the_driver_cannot_run() -> None:
    """R21/R22 are declared but cannot run the packaged driver."""
    labels = compat.supported_range_labels()

    assert "R21" not in labels
    assert "R22" not in labels
    assert "R23" in labels
    # The declared list is the honest superset and still names them.
    assert "R21" in compat.declared_range_labels()


def test_verdict_is_json_serialisable() -> None:
    """The verdict is embedded verbatim in doctor/verify output."""
    payload = json.dumps(compat.classify_host(27000, "3.9.7"))

    assert "interpreter_drift" in payload
