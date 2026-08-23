"""Adapter doctor compatibility layer pending the shared Core #2252/#2320 facade."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any, Mapping, Optional

from .__version__ import __version__
from .bridge import BridgeError, BridgeTimeoutError, Cinema4dBridge

SCHEMA_VERSION = "1.0"
MINIMUM_CORE_VERSION = "0.19.91"
MINIMUM_HOST_VERSION = "R21"
MINIMUM_HOST_BUILD = 21_000

EXIT_OK = 0
EXIT_PREFLIGHT = 10
EXIT_VERIFY = 40


@dataclass(frozen=True)
class DoctorRequest:
    operation: str
    executable: Optional[Path] = None
    timeout_secs: float = 60.0


def _version_tuple(value: object) -> tuple[int, ...]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", str(value))
    return tuple(int(part) for part in match.groups()) if match else ()


def _core_version() -> Optional[str]:
    try:
        return package_version("dcc-mcp-core")
    except PackageNotFoundError:
        return None


def _step(identifier: str, description: str, command: list[str], why: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "description": description,
        "command": command,
        "why": why,
    }


def _result(
    request: DoctorRequest,
    *,
    exit_code: int,
    stage: str,
    reason: str,
    core_version: Optional[str],
    bridge: Optional[Cinema4dBridge] = None,
    runtime: Optional[Mapping[str, Any]] = None,
    next_steps: Optional[list[Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    executable = bridge.executable if bridge is not None else None
    configured = str(request.executable) if request.executable is not None else None
    env_executable = os.environ.get("DCC_MCP_CINEMA4D_C4DPY") or None
    if configured:
        source = "--c4dpy"
    elif env_executable:
        source = "DCC_MCP_CINEMA4D_C4DPY"
    elif executable:
        source = "PATH_or_common_installation"
    else:
        source = None
    usable = exit_code == EXIT_OK
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": request.operation,
        "status": "ready" if usable else "failed",
        "exit_code": exit_code,
        "dcc_type": "c4d",
        "adapter_version": __version__,
        "core_version": core_version,
        "requirements": {
            "minimum_core_version": MINIMUM_CORE_VERSION,
            "minimum_host_version": MINIMUM_HOST_VERSION,
            "minimum_host_build": MINIMUM_HOST_BUILD,
            "licensed_c4dpy_required": True,
        },
        "configuration": {
            "allowed_roots": [str(path) for path in (bridge.allowed_roots if bridge else ())],
            "max_input_bytes": bridge.max_input_bytes if bridge else None,
            "max_timeout_secs": bridge.max_timeout_secs if bridge else None,
            "probe_timeout_secs": request.timeout_secs,
        },
        "discovery": {
            "c4dpy": {
                "requested": configured,
                "environment": env_executable,
                "executable": executable,
                "source": source,
            }
        },
        "runtime": dict(runtime or {}),
        "verify": {
            "directly_usable": usable,
            "failure_stage": None if usable else stage,
            "failure_reason": None if usable else reason,
        },
        "steps": [],
        "next_steps": list(next_steps or []),
        "stage": stage,
        "reason": reason,
        "auto_provision": False,
        "cache": None,
        "binary_source": "maxon_installation",
    }


def _rerun_command(request: DoctorRequest, executable: Optional[str] = None) -> list[str]:
    command = ["dcc-mcp-cinema4d", request.operation]
    if executable:
        command.extend(["--c4dpy", executable])
    command.append("--json")
    return command


def run_doctor(request: DoctorRequest) -> dict[str, Any]:
    if request.operation not in {"doctor", "verify"}:
        raise ValueError("Unsupported Cinema 4D verification operation")
    core_version = _core_version()
    if core_version is None or _version_tuple(core_version) < _version_tuple(MINIMUM_CORE_VERSION):
        return _result(
            request,
            exit_code=EXIT_PREFLIGHT,
            stage="core_version",
            reason="dcc-mcp-core 0.19.91+ is required",
            core_version=core_version,
            next_steps=[
                _step(
                    "upgrade-core",
                    "Install a supported DCC-MCP Core in this Python environment.",
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "--upgrade",
                        "dcc-mcp-core>=0.19.91,<1.0.0",
                    ],
                    "The standalone adapter imports and runs on Core 0.19.91 or newer.",
                )
            ],
        )
    try:
        configured = str(request.executable) if request.executable is not None else None
        if configured is None:
            bridge = Cinema4dBridge.from_env()
        else:
            environment = Cinema4dBridge.from_env()
            bridge = Cinema4dBridge(
                configured,
                allowed_roots=environment.allowed_roots,
                max_input_bytes=environment.max_input_bytes,
                max_timeout_secs=environment.max_timeout_secs,
            )
    except (OSError, TypeError, ValueError) as exc:
        return _result(
            request,
            exit_code=EXIT_PREFLIGHT,
            stage="configuration",
            reason="Cinema 4D runtime configuration is invalid: %s" % str(exc)[:500],
            core_version=core_version,
            next_steps=[
                _step(
                    "fix-configuration",
                    "Correct the Cinema 4D adapter environment and rerun doctor.",
                    _rerun_command(request),
                    "Numeric limits and workspace roots must be valid before runtime launch.",
                )
            ],
        )
    if not bridge.executable:
        return _result(
            request,
            exit_code=EXIT_PREFLIGHT,
            stage="executable_discovery",
            reason="c4dpy was not found",
            core_version=core_version,
            bridge=bridge,
            next_steps=[
                _step(
                    "configure-c4dpy",
                    "Install licensed Cinema 4D or configure its c4dpy executable, then rerun.",
                    _rerun_command(request),
                    "This adapter never downloads or provisions Maxon binaries.",
                )
            ],
        )
    missing_roots = [path for path in bridge.allowed_roots if not path.is_dir()]
    if missing_roots:
        return _result(
            request,
            exit_code=EXIT_PREFLIGHT,
            stage="configuration",
            reason="Configured workspace root does not exist: %s" % missing_roots[0],
            core_version=core_version,
            bridge=bridge,
            next_steps=[
                _step(
                    "fix-workspace-roots",
                    "Create or correct the allowed workspace roots, then rerun doctor.",
                    _rerun_command(request, bridge.executable),
                    "The adapter accepts document paths only inside existing allowed roots.",
                )
            ],
        )
    try:
        runtime = bridge.status(timeout_secs=request.timeout_secs)
    except BridgeTimeoutError:
        reason = "c4dpy timed out; verify the Maxon license and headless configuration"
        return _result(
            request,
            exit_code=EXIT_VERIFY,
            stage="runtime_timeout",
            reason=reason,
            core_version=core_version,
            bridge=bridge,
            next_steps=[
                _step(
                    "activate-license-and-retry",
                    "Activate the Cinema 4D license and rerun verification.",
                    _rerun_command(request, bridge.executable),
                    "A discovered executable is not usable until licensed c4dpy answers.",
                )
            ],
        )
    except BridgeError as exc:
        message = str(exc)[:1000]
        words = message.lower()
        stage = (
            "license"
            if any(word in words for word in ("license", "credential", "authentication"))
            else "runtime_start"
        )
        return _result(
            request,
            exit_code=EXIT_VERIFY,
            stage=stage,
            reason=message or "c4dpy verification failed",
            core_version=core_version,
            bridge=bridge,
            next_steps=[
                _step(
                    "repair-runtime-and-retry",
                    "Repair the licensed c4dpy runtime and rerun verification.",
                    _rerun_command(request, bridge.executable),
                    "The executable was found but the bounded status probe failed.",
                )
            ],
        )
    if runtime.get("ready") is not True:
        return _result(
            request,
            exit_code=EXIT_VERIFY,
            stage="runtime_status",
            reason=str(runtime.get("reason") or "c4dpy reported that it is not ready")[:1000],
            core_version=core_version,
            bridge=bridge,
            runtime=runtime,
            next_steps=[
                _step(
                    "repair-runtime-and-retry",
                    "Repair the licensed c4dpy runtime and rerun verification.",
                    _rerun_command(request, bridge.executable),
                    "Direct usability requires an explicit ready status from c4dpy.",
                )
            ],
        )
    try:
        host_build = int(runtime.get("cinema4d_version", 0))
    except (TypeError, ValueError):
        host_build = 0
    if host_build < MINIMUM_HOST_BUILD:
        return _result(
            request,
            exit_code=EXIT_PREFLIGHT,
            stage="host_version",
            reason="Cinema 4D R21 or newer is required",
            core_version=core_version,
            bridge=bridge,
            runtime=runtime,
            next_steps=[
                _step(
                    "upgrade-cinema4d",
                    "Select a licensed Cinema 4D R21+ c4dpy runtime and rerun verification.",
                    _rerun_command(request, bridge.executable),
                    "The discovered host build is below the supported floor.",
                )
            ],
        )
    return _result(
        request,
        exit_code=EXIT_OK,
        stage="verify",
        reason="Licensed c4dpy is discovered and directly usable",
        core_version=core_version,
        bridge=bridge,
        runtime=runtime,
    )
