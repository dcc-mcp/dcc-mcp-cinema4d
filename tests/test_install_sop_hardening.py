from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from dcc_mcp_cinema4d import _process as process_support
from dcc_mcp_cinema4d import install as installer
from dcc_mcp_cinema4d.bridge import BridgeError, Cinema4dBridge


def _host_identity(path: Path, *, digest: str = "a" * 64) -> installer.HostIdentity:
    return installer.HostIdentity(
        path=path,
        product="Maxon Cinema 4D",
        product_version="2026.1.0",
        file_version="2026.1.0",
        sha256=digest,
        size=128,
        file_identity="device:1:file:2",
    )


def _context(tmp_path: Path) -> installer.InstallContext:
    host = tmp_path / "Maxon" / "Cinema 4D 2026" / ("c4dpy.exe" if os.name == "nt" else "c4dpy")
    host.parent.mkdir(parents=True)
    host.write_bytes(b"synthetic-c4dpy")
    state_root = tmp_path / "state"
    return installer.InstallContext(
        host=_host_identity(host),
        python=installer.PythonIdentity(
            executable=Path(sys.executable),
            version=".".join(str(item) for item in sys.version_info[:3]),
            prefix=Path(sys.prefix),
            adapter_version=installer.__version__,
            core_version="0.20.15",
            adapter_origin=Path(installer.__file__).parent,
            core_origin=Path(sys.prefix) / "site-packages" / "dcc_mcp_core",
        ),
        state_root=state_root,
        receipt_path=state_root / "receipt.json",
        state="fresh",
        receipt=None,
    )


def _request(
    tmp_path: Path, operation: str, *, execute: bool = False
) -> installer.LifecycleRequest:
    return installer.LifecycleRequest(
        operation=operation,
        executable=tmp_path
        / "Maxon"
        / "Cinema 4D 2026"
        / ("c4dpy.exe" if os.name == "nt" else "c4dpy"),
        python_executable=Path(sys.executable),
        state_root=tmp_path / "state",
        timeout_secs=2.0,
        execute=execute,
    )


def test_uses_released_core_contract_and_official_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert installer.MINIMUM_CORE_VERSION == "0.20.14"
    installer._require_official_core_contract()
    context = _context(tmp_path)
    monkeypatch.setattr(installer, "_resolve_context", lambda *_args, **_kwargs: context)

    outcome = installer.run_lifecycle(_request(tmp_path, "install"), environ={})

    from dcc_mcp_core.deployment import load_install_sop_schema

    Draft202012Validator(load_install_sop_schema()).validate(outcome.result)
    assert outcome.result["schema_version"] == 1
    assert outcome.result["status"] == "planned"
    assert outcome.result["receipt_path"] == "<state>/receipt.json"


@pytest.mark.parametrize("operation", ["install", "status", "verify", "uninstall", "upgrade"])
def test_every_lifecycle_operation_returns_schema_valid_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    context = _context(tmp_path)
    monkeypatch.setattr(installer, "_resolve_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(
        installer,
        "_verify_runtime",
        lambda *_args, **_kwargs: {
            "directly_usable": True,
            "failure_stage": None,
            "failure_reason": None,
            "runtime_identity": {"host_pid": 321, "instance_id": "instance-1"},
        },
    )

    outcome = installer.run_lifecycle(_request(tmp_path, operation), environ={})

    from dcc_mcp_core.deployment import load_install_sop_schema

    Draft202012Validator(load_install_sop_schema()).validate(outcome.result)
    assert outcome.result["operation"] == operation
    assert outcome.result["core_version"] == "0.20.15"


def test_install_is_receipted_idempotent_and_uninstall_removes_only_owned_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(tmp_path)

    def resolve(request, _environ):
        receipt = installer._load_receipt(context.receipt_path)
        return replace(
            context,
            state="current" if receipt else "fresh",
            receipt=receipt,
        )

    monkeypatch.setattr(installer, "_resolve_context", resolve)
    monkeypatch.setattr(installer, "_recapture_host", lambda current: current.host)
    monkeypatch.setattr(
        installer,
        "_verify_runtime",
        lambda *_args, **_kwargs: {
            "directly_usable": True,
            "failure_stage": None,
            "failure_reason": None,
            "runtime_identity": {"host_pid": 321, "instance_id": "instance-1"},
        },
    )
    operator_file = context.state_root / "operator-owned.txt"
    operator_file.parent.mkdir(parents=True)
    operator_file.write_text("keep", encoding="utf-8")

    installed = installer.run_lifecycle(_request(tmp_path, "install", execute=True), environ={})
    first_receipt = context.receipt_path.read_bytes()
    repeated = installer.run_lifecycle(_request(tmp_path, "install", execute=True), environ={})
    assert context.receipt_path.read_bytes() == first_receipt
    uninstalled = installer.run_lifecycle(_request(tmp_path, "uninstall", execute=True), environ={})

    assert installed.exit_code == 0
    assert repeated.exit_code == 0
    assert repeated.result["install_state"] == "current"
    assert uninstalled.exit_code == 0
    assert not context.receipt_path.exists()
    assert operator_file.read_text(encoding="utf-8") == "keep"


def test_mutation_recaptures_context_only_after_acquiring_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = _context(tmp_path)
    fresh_receipt = installer._build_receipt(stale)
    fresh = replace(stale, state="current", receipt=fresh_receipt)
    resolutions = iter((stale, fresh))
    calls = []

    def resolve(*_args, **_kwargs):
        calls.append("resolve")
        resolved = next(resolutions)
        if len(calls) == 1:
            stale.receipt_path.parent.mkdir(parents=True, exist_ok=True)
            stale.receipt_path.write_text(json.dumps(fresh_receipt), encoding="utf-8")
        return resolved

    monkeypatch.setattr(installer, "_resolve_context", resolve)
    monkeypatch.setattr(installer, "_recapture_host", lambda current: current.host)
    monkeypatch.setattr(
        installer,
        "_verify_runtime",
        lambda *_args, **_kwargs: {
            "directly_usable": True,
            "failure_stage": None,
            "failure_reason": None,
        },
    )

    outcome = installer.run_lifecycle(_request(tmp_path, "install", execute=True), environ={})

    assert calls == ["resolve", "resolve"]
    assert outcome.result["steps"][0]["status"] == "already_current"


def test_upgrade_verify_failure_restores_exact_previous_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = replace(_context(tmp_path), state="upgrade")
    context.receipt_path.parent.mkdir(parents=True)
    previous = b'{"owned":"previous"}\n'
    context.receipt_path.write_bytes(previous)
    context = replace(context, receipt=json.loads(previous))
    monkeypatch.setattr(installer, "_resolve_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(installer, "_recapture_host", lambda current: current.host)
    monkeypatch.setattr(
        installer,
        "_verify_runtime",
        lambda *_args, **_kwargs: {
            "directly_usable": False,
            "failure_stage": "runtime_identity",
            "failure_reason": "Runtime identity was not stable.",
        },
    )

    outcome = installer.run_lifecycle(_request(tmp_path, "upgrade", execute=True), environ={})

    assert outcome.exit_code == 40
    assert outcome.result["previous_restored"] is True
    assert context.receipt_path.read_bytes() == previous


def test_mutation_lock_rejects_concurrent_install_and_cleanup_failure_is_visible(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    with installer._mutation_lock(state_root, deadline=time.monotonic() + 2.0):
        with pytest.raises(installer.LifecycleFailure, match="already in progress"):
            with installer._mutation_lock(state_root, deadline=time.monotonic() + 2.0):
                pass


def test_host_requires_exact_maxon_product_version_regular_file_and_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    impostor = tmp_path / "Maxon" / "Cinema 4D 2026" / ("c4dpy.exe" if os.name == "nt" else "c4dpy")
    impostor.parent.mkdir(parents=True)
    impostor.write_bytes(b"renamed arbitrary executable")
    monkeypatch.setattr(
        installer,
        "_platform_file_metadata",
        lambda _path: {
            "company": "Foreign Company",
            "product": "Foreign Product",
            "product_version": "2026.1.0",
            "file_version": "2026.1.0",
        },
    )

    with pytest.raises(installer.LifecycleFailure, match="Maxon Cinema 4D product identity"):
        installer._inspect_host_executable(impostor)

    monkeypatch.setattr(
        installer,
        "_platform_file_metadata",
        lambda _path: {
            "company": "Maxon Computer GmbH",
            "product": "Maxon Cinema 4D",
            "product_version": "2026.1.0",
            "file_version": "2026.1.0",
        },
    )
    identity = installer._inspect_host_executable(impostor)
    assert identity.sha256 == installer._hash_file(impostor)
    assert identity.size == len(b"renamed arbitrary executable")
    assert identity.product_version == "2026.1.0"


def test_host_recapture_rejects_file_swap_and_pid_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(tmp_path)
    changed = _host_identity(context.host.path, digest="b" * 64)
    monkeypatch.setattr(installer, "_inspect_host_executable", lambda _path: changed)

    with pytest.raises(installer.LifecycleFailure, match="changed during the operation"):
        installer._recapture_host(context)

    observations = iter(
        [
            {
                "pid": 123,
                "executable_sha256": context.host.sha256,
                "executable_file_identity": context.host.file_identity,
                "start_identity": "start-1",
            },
            {
                "pid": 123,
                "executable_sha256": context.host.sha256,
                "executable_file_identity": context.host.file_identity,
                "start_identity": "reused-1",
            },
        ]
    )
    monkeypatch.setattr(installer, "_observe_process_identity", lambda _pid: next(observations))
    first = installer._observe_bound_runtime(123, context.host)
    with pytest.raises(installer.LifecycleFailure, match="PID identity changed"):
        installer._recapture_bound_runtime(first, context.host)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS libproc identity contract")
def test_macos_runtime_identity_uses_kernel_microseconds() -> None:
    identity = installer._observe_process_identity(os.getpid())

    prefix, seconds, microseconds = identity["start_identity"].split(":")
    assert prefix == "darwin"
    assert int(seconds) > 0
    assert 0 <= int(microseconds) < 1_000_000
    assert identity["executable_sha256"]
    assert identity["executable_file_identity"]


def _fake_darwin_ps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str,
    returncode: int = 0,
):
    fake_ps = tmp_path / "ps"
    fake_ps.write_bytes(b"")
    observed: list[tuple[list[str], float]] = []

    def fake_run(command, **kwargs):
        observed.append((list(command), float(kwargs["timeout"])))
        return subprocess.CompletedProcess(command, returncode, stdout, "")

    monkeypatch.setattr(process_support, "_DARWIN_PS_PATHS", (fake_ps,))
    monkeypatch.setattr(process_support.subprocess, "run", fake_run)
    return fake_ps, observed


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected"),
    [
        ("", 0, False),
        ("24680 12345 12345 R\n", 0, True),
        ("24680 54321 12345 R\n", 0, True),
        ("24680 54321 54321 R\n", 0, False),
        ("24680 12345 12345 Z\n", 0, False),
        ("malformed\n", 0, True),
        ("", 1, True),
    ],
)
def test_macos_process_group_accounting_is_exact_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    returncode: int,
    expected: bool,
) -> None:
    _fake_darwin_ps(
        tmp_path,
        monkeypatch,
        stdout=stdout,
        returncode=returncode,
    )

    assert process_support._darwin_group_has_live_members(12345) is expected


def test_macos_process_group_timeout_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_ps = tmp_path / "ps"
    fake_ps.write_bytes(b"")

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(process_support, "_DARWIN_PS_PATHS", (fake_ps,))
    monkeypatch.setattr(process_support.subprocess, "run", fake_run)

    assert process_support._darwin_group_has_live_members(12345, 0.05) is True


def test_macos_process_group_falls_back_to_the_sess_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_ps = tmp_path / "ps"
    fake_ps.write_bytes(b"")
    observed: list[list[str]] = []

    def fake_run(command, **_kwargs):
        observed.append(list(command))
        returncode = 1 if "pid=,pgid=,sid=,state=" in command else 0
        return subprocess.CompletedProcess(command, returncode, "", "")

    monkeypatch.setattr(process_support, "_DARWIN_PS_PATHS", (fake_ps,))
    monkeypatch.setattr(process_support.subprocess, "run", fake_run)

    assert process_support._darwin_group_has_live_members(12345) is False
    assert observed == [
        [str(fake_ps), "-axo", "pid=,pgid=,sid=,state="],
        [str(fake_ps), "-axo", "pid=,pgid=,sess=,state="],
    ]


def test_macos_wait_empty_retains_the_leader_until_group_accounting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeProcess:
        pid = 12345

        def poll(self):
            events.append("reap")
            return -9

    owner = process_support._PosixProcessTreeOwner(FakeProcess())
    monkeypatch.setattr(process_support.sys, "platform", "darwin")
    monkeypatch.setattr(
        process_support.os,
        "killpg",
        lambda _group, _signal: (_ for _ in ()).throw(OSError("signal-zero unavailable")),
        raising=False,
    )
    monkeypatch.setattr(
        process_support,
        "_darwin_group_has_live_members",
        lambda _group, _timeout: events.append("account") or False,
    )

    assert owner.wait_empty(0.1) is True
    assert events == ["account", "reap"]


def test_macos_process_group_ignores_zombies_from_bounded_ps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_ps, observed = _fake_darwin_ps(
        tmp_path,
        monkeypatch,
        stdout="24680 12345 12345 Z\n",
    )

    assert process_support._darwin_group_has_live_members(12345) is False
    assert observed
    assert observed[0][0] == [str(fake_ps), "-axo", "pid=,pgid=,sid=,state="]
    assert 0 < observed[0][1] <= 3.0


def test_runtime_ready_receipt_rejects_a_foreign_or_forged_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = Cinema4dBridge(executable=sys.executable, allowed_roots=[Path.cwd()])
    identity = tmp_path / "runtime.json"
    acknowledgement = tmp_path / "ready.ack"
    identity.write_text(json.dumps({"pid": os.getpid(), "protocol": 1}), encoding="utf-8")
    observations = []
    monkeypatch.setattr(
        installer,
        "_observe_process_identity",
        lambda pid: observations.append(pid) or {},
    )

    with pytest.raises(BridgeError, match="owned process"):
        bridge._acknowledge_runtime(
            identity,
            acknowledgement,
            deadline=time.monotonic() + 1.0,
            expected_pid=os.getpid() + 1,
        )

    assert observations == []
    assert not acknowledgement.exists()


@pytest.mark.parametrize("timeout", [math.nan, math.inf, -math.inf, 0.0, -1.0])
def test_timeout_rejects_non_finite_and_non_positive_values(timeout: float) -> None:
    bridge = Cinema4dBridge(executable=sys.executable, allowed_roots=[Path.cwd()])

    with pytest.raises(BridgeError, match="finite"):
        bridge._timeout(timeout)


def test_lifecycle_uses_one_absolute_deadline_from_caller_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(tmp_path)
    observed = []
    monkeypatch.setattr(installer.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(installer, "_resolve_context", lambda *_args, **_kwargs: context)

    def verify(_context, deadline):
        observed.append(deadline)
        return {
            "directly_usable": False,
            "failure_stage": "runtime_timeout",
            "failure_reason": "Cinema 4D verification timed out.",
        }

    monkeypatch.setattr(installer, "_verify_runtime", verify)

    outcome = installer.run_lifecycle(_request(tmp_path, "verify"), environ={})

    assert observed == [102.0]
    assert outcome.exit_code == 40


@pytest.mark.parametrize("operation", ["status", "install"])
def test_lifecycle_rejects_success_after_slow_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    context = _context(tmp_path)
    clock = [100.0]
    request = replace(_request(tmp_path, operation), timeout_secs=0.05)
    monkeypatch.setattr(installer.time, "monotonic", lambda: clock[0])

    def slow_resolve(*_args, **_kwargs):
        clock[0] = 100.06
        return context

    monkeypatch.setattr(installer, "_resolve_context", slow_resolve)

    outcome = installer.run_lifecycle(request, environ={})

    assert outcome.exit_code == installer.INSTALL_EXIT_VERIFY
    assert outcome.result["status"] == "failed"
    assert outcome.result["verify"]["failure_stage"] == "runtime_timeout"


@pytest.mark.parametrize("operation", ["install", "uninstall"])
def test_lifecycle_expiry_after_lock_cannot_write_or_unlink_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    context = _context(tmp_path)
    if operation == "uninstall":
        receipt = installer._build_receipt(context)
        context.receipt_path.parent.mkdir(parents=True, exist_ok=True)
        context.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        context = replace(context, state="current", receipt=receipt)
    before = context.receipt_path.read_bytes() if context.receipt_path.exists() else None
    clock = [100.0]
    request = replace(_request(tmp_path, operation, execute=True), timeout_secs=0.05)
    monkeypatch.setattr(installer.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(installer, "_resolve_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(installer, "_recapture_receipt", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(installer, "_recapture_host", lambda current: current.host)
    monkeypatch.setattr(
        installer,
        "_verify_runtime",
        lambda *_args, **_kwargs: {
            "directly_usable": True,
            "failure_stage": None,
            "failure_reason": None,
        },
    )

    @contextmanager
    def slow_lock(_state_root, *, deadline):
        assert deadline == 100.05
        clock[0] = 100.06
        yield

    monkeypatch.setattr(installer, "_mutation_lock", slow_lock)

    outcome = installer.run_lifecycle(request, environ={})

    after = context.receipt_path.read_bytes() if context.receipt_path.exists() else None
    assert outcome.exit_code == installer.INSTALL_EXIT_VERIFY
    assert outcome.result["verify"]["failure_stage"] == "runtime_timeout"
    assert after == before


def test_lifecycle_rolls_back_when_lock_cleanup_consumes_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(tmp_path)
    receipt = installer._build_receipt(context)
    context.receipt_path.parent.mkdir(parents=True, exist_ok=True)
    context.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    previous = context.receipt_path.read_bytes()
    context = replace(context, state="current", receipt=receipt)
    clock = [100.0]
    request = replace(_request(tmp_path, "uninstall", execute=True), timeout_secs=0.05)
    original_unlink = Path.unlink
    monkeypatch.setattr(installer.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(installer, "_resolve_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(installer, "_recapture_receipt", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(installer, "_recapture_host", lambda current: current.host)

    def delayed_lock_unlink(path, *args, **kwargs):
        result = original_unlink(path, *args, **kwargs)
        if path.name == "lifecycle.lock":
            clock[0] = 100.06
        return result

    monkeypatch.setattr(Path, "unlink", delayed_lock_unlink)

    outcome = installer.run_lifecycle(request, environ={})

    assert outcome.exit_code == installer.INSTALL_EXIT_VERIFY
    assert outcome.result["verify"]["failure_stage"] == "runtime_timeout"
    assert context.receipt_path.read_bytes() == previous


def test_lifecycle_rolls_back_when_atomic_replace_crosses_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(tmp_path)
    clock = [100.0]
    request = replace(_request(tmp_path, "install", execute=True), timeout_secs=0.05)
    original_replace = os.replace
    monkeypatch.setattr(installer.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(installer, "_resolve_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr(installer, "_recapture_receipt", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(installer, "_recapture_host", lambda current: current.host)

    def delayed_receipt_replace(source, target):
        result = original_replace(source, target)
        if Path(target) == context.receipt_path:
            clock[0] = 100.06
        return result

    monkeypatch.setattr(installer.os, "replace", delayed_receipt_replace)

    outcome = installer.run_lifecycle(request, environ={})

    assert outcome.exit_code == installer.INSTALL_EXIT_VERIFY
    assert outcome.result["verify"]["failure_stage"] == "runtime_timeout"
    assert not context.receipt_path.exists()


def test_owned_command_cannot_return_success_after_output_or_cleanup_expires_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [100.0]
    close_deadlines = []

    class FakeProcess:
        returncode = 0

        @staticmethod
        def poll():
            return 0

    class FakeOwned:
        process = FakeProcess()

        @staticmethod
        def close(*, deadline=None):
            close_deadlines.append(deadline)

    monkeypatch.setattr(process_support.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(process_support.tempfile, "mkdtemp", lambda **_kwargs: str(tmp_path))
    monkeypatch.setattr(
        process_support, "start_owned_process", lambda *_args, **_kwargs: FakeOwned()
    )

    def delayed_read(_path, *, deadline):
        assert deadline == 100.05
        clock[0] = 100.06
        return b"", False, False

    monkeypatch.setattr(process_support, "_read_bounded_output", delayed_read)

    result = process_support.run_owned_command(["synthetic"], timeout_secs=0.05)

    assert close_deadlines == [100.05]
    assert result["success"] is False
    assert result["reason"] == "process timed out"


def test_subprocess_environment_isolated_from_python_and_secret_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONPATH", "shadow")
    monkeypatch.setenv("PYTHONHOME", "shadow-home")
    monkeypatch.setenv("DCC_MCP_TOKEN", "secret-token")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret-key")

    environment = installer._isolated_environment({"PATH": os.environ.get("PATH", "")})

    assert "PYTHONPATH" not in environment
    assert "PYTHONHOME" not in environment
    assert "DCC_MCP_TOKEN" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert environment["PYTHONNOUSERSITE"] == "1"


def test_public_failures_are_schema_valid_and_redact_paths_secrets_and_raw_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "token-123"
    private = tmp_path / "private" / "operator.c4d"
    monkeypatch.setattr(
        installer,
        "_resolve_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(f"{secret} at {private} stdout=private-output stderr=private-error")
        ),
    )

    outcome = installer.run_lifecycle(_request(tmp_path, "verify"), environ={})
    serialized = json.dumps(outcome.result)

    from dcc_mcp_core.deployment import load_install_sop_schema

    Draft202012Validator(load_install_sop_schema()).validate(outcome.result)
    assert outcome.exit_code == 40
    assert secret not in serialized
    assert str(private) not in serialized
    assert "private-output" not in serialized
    assert "private-error" not in serialized
    assert outcome.result["verify"]["failure_reason"] == "Cinema 4D verification failed."


def _pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x00000102
    finally:
        kernel32.CloseHandle(handle)


def test_owned_timeout_terminates_root_descendant_and_inherited_handles(tmp_path: Path) -> None:
    identity_path = tmp_path / "owned-pids.json"
    child_script = "import sys,time; print('ready', flush=True); time.sleep(60)"
    root_script = (
        "import json,os,pathlib,subprocess,sys,time; "
        f"child=subprocess.Popen([sys.executable,'-c',{child_script!r}]); "
        f"pathlib.Path({str(identity_path)!r}).write_text(json.dumps("
        "{'root':os.getpid(),'child':child.pid})); "
        "time.sleep(60)"
    )

    result = installer._run_owned_command([sys.executable, "-c", root_script], timeout_secs=2.0)

    assert result["success"] is False
    assert result["reason"] == "process cleanup failed"
    identities = json.loads(identity_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and any(_pid_alive(pid) for pid in identities.values()):
        time.sleep(0.05)
    assert not _pid_alive(identities["root"])
    assert not _pid_alive(identities["child"])


def test_owned_command_bounds_process_output_without_returning_raw_bytes() -> None:
    script = "import sys; sys.stdout.write('x' * 70000); sys.stdout.flush()"

    result = installer._run_owned_command([sys.executable, "-c", script], timeout_secs=5.0)

    assert result["success"] is False
    assert result["reason"] == "process output exceeded limit"
    assert result["stdout_truncated"] is True
    assert "stdout" not in result
    assert "stderr" not in result


@pytest.mark.skipif(os.name != "posix", reason="POSIX session parent-death contract")
def test_posix_supervisor_kills_the_session_when_the_caller_dies(tmp_path: Path) -> None:
    info_path = tmp_path / "supervisor-info.json"
    controller_script = (
        "import json,os,pathlib,sys,time; "
        "from dcc_mcp_cinema4d._process import isolated_environment,start_owned_process; "
        "root=pathlib.Path(sys.argv[1]); info=pathlib.Path(sys.argv[2]); "
        "sink=open(os.devnull,'wb'); deadline=time.monotonic()+10; "
        "owned=start_owned_process([sys.executable,'-c','import time; time.sleep(60)'],"
        "cwd=root,environment=isolated_environment(),stdout=sink,stderr=sink,deadline=deadline); "
        "data={'root':owned.process.pid,'supervisor':owned.process.supervisor_pid}; "
        "info.write_text(json.dumps(data)); "
        "time.sleep(60)"
    )
    controller = subprocess.Popen(
        [sys.executable, "-c", controller_script, str(tmp_path), str(info_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    identities = None
    try:
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and not info_path.is_file():
            if controller.poll() is not None:
                raise AssertionError("controller exited before supervised root readiness")
            time.sleep(0.02)
        identities = json.loads(info_path.read_text(encoding="utf-8"))
        os.kill(controller.pid, 9)
        controller.wait(timeout=3.0)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and any(_pid_alive(pid) for pid in identities.values()):
            time.sleep(0.02)
        assert not _pid_alive(identities["root"])
        assert not _pid_alive(identities["supervisor"])
    finally:
        if controller.poll() is None:
            controller.kill()
            controller.wait(timeout=3.0)
        if identities is not None and _pid_alive(identities["supervisor"]):
            if os.getpgid(identities["supervisor"]) == identities["supervisor"]:
                os.killpg(identities["supervisor"], 9)


def test_cli_argument_errors_return_only_schema_valid_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from dcc_mcp_cinema4d.server import main

    exit_code = main(["install", "--json", "--unknown-option", "token-123"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    from dcc_mcp_core.deployment import load_install_sop_schema

    Draft202012Validator(load_install_sop_schema()).validate(payload)
    assert exit_code == 10
    assert captured.err == ""
    assert "token-123" not in json.dumps(payload)
    assert payload["verify"]["failure_stage"] == "arguments"


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-1"])
def test_cli_timeout_errors_return_stable_schema_json(
    value: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from dcc_mcp_core.deployment import load_install_sop_schema

    from dcc_mcp_cinema4d.server import main

    exit_code = main(
        [
            "verify",
            "--json",
            "--timeout-secs",
            value,
            "--c4dpy",
            str(tmp_path / "private-c4dpy"),
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    Draft202012Validator(load_install_sop_schema()).validate(payload)
    assert exit_code == 10
    assert captured.err == ""
    assert str(tmp_path) not in json.dumps(payload)
    assert payload["verify"]["failure_stage"] == "arguments"
