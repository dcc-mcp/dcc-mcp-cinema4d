"""Private durable POSIX supervisor for one adapter-owned command tree."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(dict(payload), sort_keys=True), encoding="utf-8")
    os.replace(str(temporary), str(path))


def _watch_parent(parent_pid: int) -> None:
    while True:
        if os.getppid() != parent_pid:
            try:
                os.killpg(os.getpgrp(), signal.SIGKILL)
            finally:
                os._exit(70)
        time.sleep(0.02)


def main(arguments: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if arguments is None else arguments)
    if len(argv) < 4 or argv[2] != "--":
        return 64
    status_path = Path(argv[0])
    ready_path = Path(argv[1])
    command = argv[3:]
    parent_pid = os.getppid()
    watcher = threading.Thread(target=_watch_parent, args=(parent_pid,), daemon=True)
    watcher.start()
    try:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, close_fds=True)
    except OSError as exc:
        _write_json_atomic(
            status_path,
            {"state": "launch_failed", "error_type": exc.__class__.__name__},
        )
        return 127
    _write_json_atomic(
        ready_path,
        {
            "state": "running",
            "pid": child.pid,
            "supervisor_pid": os.getpid(),
        },
    )
    returncode = child.wait()
    _write_json_atomic(
        status_path,
        {
            "state": "completed",
            "pid": child.pid,
            "supervisor_pid": os.getpid(),
            "returncode": int(returncode),
        },
    )
    while True:
        time.sleep(60.0)


if __name__ == "__main__":
    raise SystemExit(main())
