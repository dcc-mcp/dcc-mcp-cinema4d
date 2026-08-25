from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from dcc_mcp_core import DccServerOptions
from dcc_mcp_core.server_base import DccServerBase

from .__version__ import __version__

_server: Optional["Cinema4dMcpServer"] = None


class _ArgumentFailure(Exception):
    pass


class _LifecycleParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise _ArgumentFailure(message)


class Cinema4dMcpServer(DccServerBase):
    def __init__(self, port: Optional[int] = None):
        options = DccServerOptions.from_env(
            "c4d",
            Path(__file__).parent / "skills",
            port=port,
            server_name="dcc-mcp-cinema4d",
            server_version=__version__,
            adapter_version=__version__,
        )
        super().__init__(options=options)

    def _version_string(self):
        return __version__


def start_server(port: Optional[int] = None):
    global _server
    if _server is None or not _server.is_running:
        _server = Cinema4dMcpServer(port)
        _server.register_builtin_actions()
        _server.start()
    return _server


def stop_server():
    global _server
    if _server is not None:
        _server.stop()
        _server = None


def _build_parser() -> argparse.ArgumentParser:
    parser = _LifecycleParser(description="Run or manage the Cinema 4D adapter lifecycle.")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for operation in ("doctor", "install", "status", "verify", "uninstall", "upgrade"):
        command = subparsers.add_parser(
            operation, help="Plan, inspect, or execute the standalone c4dpy lifecycle."
        )
        command.add_argument("--json", action="store_true", dest="json_output")
        command.add_argument("--c4dpy", type=Path)
        command.add_argument("--python", type=Path, dest="python_executable")
        command.add_argument("--state-root", type=Path)
        command.add_argument("--timeout-secs", type=float, default=60.0)
        command.add_argument("--yes", action="store_true", dest="execute")
    plan = subparsers.add_parser("plan", help="Plan an install, upgrade, or uninstall.")
    plan.add_argument("--target", choices=("install", "upgrade", "uninstall"), required=True)
    plan.add_argument("--json", action="store_true", dest="json_output")
    plan.add_argument("--c4dpy", type=Path)
    plan.add_argument("--python", type=Path, dest="python_executable")
    plan.add_argument("--state-root", type=Path)
    plan.add_argument("--timeout-secs", type=float, default=60.0)
    plan.set_defaults(execute=False)
    return parser


def _print_doctor_result(result: Mapping[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(result, sort_keys=True))
        return
    print("%s: %s" % (result.get("status", "unknown"), result.get("reason", "")))
    for step in result.get("next_steps", []):
        command = step.get("command") if isinstance(step, Mapping) else None
        if isinstance(command, list):
            print("next: %s" % " ".join(str(part) for part in command))


def _run_server() -> None:
    event = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: event.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: event.set())
    start_server()
    try:
        event.wait()
    finally:
        stop_server()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the no-argument service or the official Install SOP lifecycle."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        _run_server()
        return 0
    from dcc_mcp_core.deployment import INSTALL_EXIT_PREFLIGHT

    from .install import LifecycleRequest, failure_outcome, run_lifecycle

    try:
        args = _build_parser().parse_args(arguments)
    except _ArgumentFailure:
        outcome = failure_outcome("arguments", "arguments", INSTALL_EXIT_PREFLIGHT)
        _print_doctor_result(outcome.result, json_output=True)
        return outcome.exit_code
    operation = "verify" if args.command == "doctor" else args.command
    outcome = run_lifecycle(
        LifecycleRequest(
            operation=operation,
            executable=args.c4dpy,
            python_executable=args.python_executable,
            state_root=args.state_root,
            timeout_secs=args.timeout_secs,
            execute=args.execute,
            target=getattr(args, "target", None),
        )
    )
    outcome.result["requested_operation"] = args.command
    _print_doctor_result(outcome.result, json_output=args.json_output)
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
