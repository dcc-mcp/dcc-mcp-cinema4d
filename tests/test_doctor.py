from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from jsonschema import Draft202012Validator


def _context(tmp_path: Path):
    from dcc_mcp_cinema4d import install

    executable = tmp_path / ("c4dpy.exe" if os.name == "nt" else "c4dpy")
    executable.write_bytes(b"synthetic")
    state = tmp_path / "state"
    receipt = state / "receipt.json"
    return install.InstallContext(
        host=install.HostIdentity(
            executable,
            "Maxon Cinema 4D",
            "2026.1.0",
            "2026.1.0",
            "a" * 64,
            len(b"synthetic"),
            "device:1:file:2",
        ),
        python=install.PythonIdentity(
            Path(sys.executable),
            ".".join(str(item) for item in sys.version_info[:3]),
            Path(sys.prefix),
            install.__version__,
            "0.20.15",
            Path(install.__file__).parent,
            Path(sys.prefix) / "site-packages" / "dcc_mcp_core",
        ),
        state_root=state,
        receipt_path=receipt,
        state="current",
        receipt={"schema_version": 1},
    )


def test_doctor_cli_reports_missing_c4dpy_as_official_stable_json(tmp_path: Path, capsys) -> None:
    from dcc_mcp_core.deployment import load_install_sop_schema

    from dcc_mcp_cinema4d import server

    exit_code = server.main(["doctor", "--c4dpy", str(tmp_path / "missing-c4dpy"), "--json"])

    result = json.loads(capsys.readouterr().out)
    Draft202012Validator(load_install_sop_schema()).validate(result)
    assert exit_code == 10
    assert result["schema_version"] == 1
    assert result["requested_operation"] == "doctor"
    assert result["operation"] == "verify"
    assert result["verify"]["failure_stage"] == "host"
    assert str(tmp_path) not in json.dumps(result)


def test_doctor_alias_runs_verify_without_leaking_host_paths(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from dcc_mcp_cinema4d import install, server

    context = _context(tmp_path)
    monkeypatch.setattr(install, "_resolve_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(
        install,
        "_verify_runtime",
        lambda *_args, **_kwargs: {
            "directly_usable": True,
            "failure_stage": None,
            "failure_reason": None,
            "runtime_identity": {"pid": 123, "start_identity": "start-1"},
            "listener_identity": "not_applicable_standalone",
            "project_identity": "no_project_for_status_probe",
            "receipt_identity": "b" * 64,
        },
    )

    exit_code = server.main(["doctor", "--c4dpy", str(context.host.path), "--json"])
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert result["verify"]["directly_usable"] is True
    assert result["plan"]["host"]["path"].startswith("<cinema4d>")
    assert str(context.host.path) not in json.dumps(result)


def test_verify_reports_not_ready_as_stable_schema_failure(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from dcc_mcp_cinema4d import install, server

    context = _context(tmp_path)
    monkeypatch.setattr(install, "_resolve_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(
        install,
        "_verify_runtime",
        lambda *_args, **_kwargs: {
            "directly_usable": False,
            "failure_stage": "runtime",
            "failure_reason": "Cinema 4D runtime is not ready.",
        },
    )

    exit_code = server.main(["verify", "--c4dpy", str(context.host.path), "--json"])
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 40
    assert result["verify"] == {
        "directly_usable": False,
        "failure_stage": "runtime",
        "failure_reason": "Cinema 4D runtime is not ready.",
    }


def test_host_build_parser_enforces_r21_floor() -> None:
    from dcc_mcp_cinema4d.install import MINIMUM_HOST_BUILD, _host_build

    assert _host_build("R20.0") < MINIMUM_HOST_BUILD
    assert _host_build("R21.0") >= MINIMUM_HOST_BUILD
    assert _host_build("2026.1.0") >= MINIMUM_HOST_BUILD


def _run_doctor_with_runtime(runtime: dict, monkeypatch, tmp_path: Path) -> dict:
    """Run the real `_verify_runtime` against a stubbed c4dpy status probe.

    Only the process boundary is stubbed. The matrix gate under test is the real
    one, so these tests exercise the same classification `doctor` performs on a
    licensed machine rather than a re-implementation of it.
    """
    import io
    from contextlib import redirect_stdout

    from dcc_mcp_cinema4d import install, server

    context = _context(tmp_path)
    context.receipt_path.parent.mkdir(parents=True, exist_ok=True)
    context.receipt_path.write_text(json.dumps(install._build_receipt(context)), encoding="utf-8")

    digest = context.host.sha256

    class StubBridge:
        def __init__(self, *args, **kwargs):  # noqa: ARG002 - mirrors the real signature
            pass

        def status(self, timeout_secs: float) -> dict:
            payload = {
                "ready": True,
                "runtime_identity": {"pid": 1, "executable_sha256": digest},
                "project_identity": "no_project_for_status_probe",
            }
            payload.update(runtime)
            return payload

    monkeypatch.setattr(install, "_resolve_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(install, "_recapture_host", lambda ctx: ctx.host)
    monkeypatch.setattr(install, "_recapture_receipt", lambda ctx: None)
    monkeypatch.setattr(install, "Cinema4dBridge", StubBridge)

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        exit_code = server.main(["doctor", "--c4dpy", str(context.host.path), "--json"])

    result = json.loads(buffer.getvalue())
    result["_exit_code"] = exit_code
    return result


def test_doctor_reports_the_actual_host_version_and_support_verdict(
    tmp_path: Path, monkeypatch
) -> None:
    result = _run_doctor_with_runtime(
        {"cinema4d_version": 26000, "python_version": "3.9.7"}, monkeypatch, tmp_path
    )

    matrix = result["host_matrix"]
    assert matrix["build"] == 26000
    assert matrix["status"] == "supported"
    assert matrix["range"]["id"] == "2023"
    assert matrix["host_python_version"] == "3.9.7"
    assert result["verify"]["directly_usable"] is True
    assert result["_exit_code"] == 0


def test_doctor_rejects_a_host_outside_the_matrix(tmp_path: Path, monkeypatch) -> None:
    result = _run_doctor_with_runtime(
        {"cinema4d_version": 30000, "python_version": "3.11.4"}, monkeypatch, tmp_path
    )

    assert result["verify"]["directly_usable"] is False
    assert result["verify"]["failure_stage"] == "host_version"
    assert result["host_matrix"]["status"] == "too_new"
    assert result["host_matrix"]["supported_ranges"]
    assert result["next_steps"]
    assert result["_exit_code"] == 40


def test_doctor_rejects_a_host_whose_c4dpy_cannot_run_the_driver(
    tmp_path: Path, monkeypatch
) -> None:
    result = _run_doctor_with_runtime(
        {"cinema4d_version": 21000, "python_version": "2.7.18"}, monkeypatch, tmp_path
    )

    assert result["verify"]["directly_usable"] is False
    assert result["host_matrix"]["status"] == "host_python_unsupported"
    assert "Python 3" in result["verify"]["failure_reason"]
    assert result["next_steps"][0]["id"] == "upgrade-cinema4d"


def test_doctor_surfaces_interpreter_drift_without_blocking_a_working_host(
    tmp_path: Path, monkeypatch
) -> None:
    """An unverified mapping must be reported, not enforced.

    The matrix declares 3.11 for Cinema 4D 2024. A host reporting 3.9 still runs
    the driver, so it is accepted — but the disagreement is in the report.
    """
    result = _run_doctor_with_runtime(
        {"cinema4d_version": 27000, "python_version": "3.9.7"}, monkeypatch, tmp_path
    )

    drift = result["host_matrix"]["interpreter_drift"]
    assert drift["declared"] == "3.11"
    assert drift["observed"] == "3.9.7"
    assert result["verify"]["directly_usable"] is True
    assert result["_exit_code"] == 0
