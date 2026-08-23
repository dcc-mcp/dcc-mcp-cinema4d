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
    parser = argparse.ArgumentParser(description="Run or verify the Cinema 4D adapter.")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for operation in ("doctor", "verify"):
        command = subparsers.add_parser(
            operation, help="Verify the standalone licensed c4dpy runtime."
        )
        command.add_argument("--json", action="store_true", dest="json_output")
        command.add_argument("--c4dpy", type=Path)
        command.add_argument("--timeout-secs", type=float, default=60.0)
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
    """Run the legacy no-argument service or a standalone verification command."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        _run_server()
        return 0
    args = _build_parser().parse_args(arguments)
    from .doctor import DoctorRequest, run_doctor

    result = run_doctor(
        DoctorRequest(
            operation=args.command,
            executable=args.c4dpy,
            timeout_secs=args.timeout_secs,
        )
    )
    _print_doctor_result(result, json_output=args.json_output)
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
