from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_doctor_cli_reports_missing_c4dpy_as_stable_json(tmp_path: Path, capsys) -> None:
    from dcc_mcp_cinema4d import server

    exit_code = server.main(
        [
            "doctor",
            "--c4dpy",
            str(tmp_path / "missing-c4dpy"),
            "--json",
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 10
    assert result["schema_version"] == "1.0"
    assert result["operation"] == "doctor"
    assert result["exit_code"] == 10
    assert result["dcc_type"] == "c4d"
    assert result["verify"] == {
        "directly_usable": False,
        "failure_stage": "executable_discovery",
        "failure_reason": "c4dpy was not found",
    }
    assert result["discovery"]["c4dpy"]["executable"] is None
    assert result["requirements"]["minimum_core_version"] == "0.19.91"
    assert result["requirements"]["minimum_host_version"] == "R21"
    assert result["auto_provision"] is False
    assert "receipt_path" not in result
    assert isinstance(result["next_steps"][0]["command"], list)


def test_verify_reports_discovered_runtime_versions_and_configuration(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import dcc_mcp_cinema4d.doctor as doctor
    from dcc_mcp_cinema4d import server
    from dcc_mcp_cinema4d.bridge import Cinema4dBridge

    executable = tmp_path / "c4dpy"
    executable.touch()
    monkeypatch.setattr(doctor, "_core_version", lambda: "0.19.91")
    monkeypatch.setattr(
        Cinema4dBridge,
        "status",
        lambda _self, timeout_secs: {
            "ready": True,
            "cinema4d_version": 2026000,
            "api_version": "2026.0.0",
            "python_version": "3.11.9",
            "headless": True,
            "probe_timeout_secs": timeout_secs,
        },
    )

    exit_code = server.main(
        [
            "verify",
            "--c4dpy",
            str(executable),
            "--timeout-secs",
            "7",
            "--json",
        ]
    )

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert result["verify"]["directly_usable"] is True
    assert result["discovery"]["c4dpy"]["executable"] == str(executable.resolve())
    assert result["discovery"]["c4dpy"]["source"] == "--c4dpy"
    assert result["runtime"]["cinema4d_version"] == 2026000
    assert result["runtime"]["api_version"] == "2026.0.0"
    assert result["runtime"]["python_version"] == "3.11.9"
    assert result["configuration"]["probe_timeout_secs"] == 7.0


def test_verify_classifies_license_failure_with_exact_retry_command(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import dcc_mcp_cinema4d.doctor as doctor
    from dcc_mcp_cinema4d import server
    from dcc_mcp_cinema4d.bridge import BridgeError, Cinema4dBridge

    executable = tmp_path / "c4dpy"
    executable.touch()
    monkeypatch.setattr(doctor, "_core_version", lambda: "0.19.91")

    def fail_license(_self, timeout_secs):
        raise BridgeError("No License found for commandline rendering")

    monkeypatch.setattr(Cinema4dBridge, "status", fail_license)

    exit_code = server.main(["verify", "--c4dpy", str(executable), "--json"])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 40
    assert result["verify"]["failure_stage"] == "license"
    assert result["next_steps"][0]["command"] == [
        "dcc-mcp-cinema4d",
        "verify",
        "--c4dpy",
        str(executable.resolve()),
        "--json",
    ]


@pytest.mark.parametrize(
    ("core_version", "host_build", "expected_stage"),
    [("0.19.90", None, "core_version"), ("0.19.91", 20_000, "host_version")],
)
def test_doctor_enforces_core_and_host_version_floors(
    core_version: str,
    host_build: int | None,
    expected_stage: str,
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    import dcc_mcp_cinema4d.doctor as doctor
    from dcc_mcp_cinema4d import server
    from dcc_mcp_cinema4d.bridge import Cinema4dBridge

    executable = tmp_path / "c4dpy"
    executable.touch()
    monkeypatch.setattr(doctor, "_core_version", lambda: core_version)
    if host_build is not None:
        monkeypatch.setattr(
            Cinema4dBridge,
            "status",
            lambda _self, timeout_secs: {
                "ready": True,
                "cinema4d_version": host_build,
                "api_version": host_build,
                "python_version": "3.9.0",
                "headless": True,
            },
        )

    exit_code = server.main(["doctor", "--c4dpy", str(executable), "--json"])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 10
    assert result["verify"]["failure_stage"] == expected_stage


def test_doctor_rejects_missing_allowed_root_before_runtime_launch(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import dcc_mcp_cinema4d.doctor as doctor
    from dcc_mcp_cinema4d import server
    from dcc_mcp_cinema4d.bridge import Cinema4dBridge

    executable = tmp_path / "c4dpy"
    executable.touch()
    monkeypatch.setenv("DCC_MCP_CINEMA4D_ALLOWED_ROOTS", str(tmp_path / "missing-workspace"))
    monkeypatch.setattr(doctor, "_core_version", lambda: "0.19.91")
    monkeypatch.setattr(
        Cinema4dBridge,
        "status",
        lambda _self, timeout_secs: pytest.fail("runtime must not start"),
    )

    exit_code = server.main(["doctor", "--c4dpy", str(executable), "--json"])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 10
    assert result["verify"]["failure_stage"] == "configuration"


def test_verify_does_not_claim_usable_when_runtime_reports_not_ready(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import dcc_mcp_cinema4d.doctor as doctor
    from dcc_mcp_cinema4d import server
    from dcc_mcp_cinema4d.bridge import Cinema4dBridge

    executable = tmp_path / "c4dpy"
    executable.touch()
    monkeypatch.setattr(doctor, "_core_version", lambda: "0.19.91")
    monkeypatch.setattr(
        Cinema4dBridge,
        "status",
        lambda _self, timeout_secs: {
            "ready": False,
            "reason": "runtime_not_ready",
            "cinema4d_version": 2026000,
            "api_version": "2026.0.0",
            "python_version": "3.11.9",
            "headless": True,
        },
    )

    exit_code = server.main(["verify", "--c4dpy", str(executable), "--json"])

    result = json.loads(capsys.readouterr().out)
    assert exit_code == 40
    assert result["verify"]["directly_usable"] is False
    assert result["verify"]["failure_stage"] == "runtime_status"
