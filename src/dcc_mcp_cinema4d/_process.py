"""Owned, deadline-bounded subprocess support for the Cinema 4D adapter."""

from __future__ import annotations

import json
import math
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

_MAX_OUTPUT_BYTES = 64 * 1024
_CLEANUP_SECS = 3.0
_SECRET_KEYS = (
    "AWS_",
    "AZURE_",
    "DCC_MCP_TOKEN",
    "GITHUB_",
    "GH_TOKEN",
    "GOOGLE_",
    "OPENAI_",
    "SECRET",
    "TOKEN",
)


class ProcessCleanupError(RuntimeError):
    """The owned process tree could not be proven empty."""


def validate_timeout(value: object, *, maximum: float) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout_secs must be a finite positive number") from exc
    if not math.isfinite(timeout) or timeout <= 0 or timeout > maximum:
        raise ValueError("timeout_secs must be finite, positive, and within the configured maximum")
    return timeout


def isolated_environment(base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """Keep only non-secret process basics and disable Python path injection."""
    source = dict(os.environ if base is None else base)
    keep = {
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    }
    result = {}
    for key, value in source.items():
        upper = key.upper()
        if upper.startswith("PYTHON") or any(token in upper for token in _SECRET_KEYS):
            continue
        if upper in keep or upper.startswith("LC_"):
            result[key] = value
    result["PYTHONNOUSERSITE"] = "1"
    result["PYTHONSAFEPATH"] = "1"
    result["PYTHONDONTWRITEBYTECODE"] = "1"
    return result


class _ProcessTreeOwner:
    def terminate(self) -> None:
        raise NotImplementedError

    def wait_empty(self, timeout: float) -> bool:
        return True

    def close(self) -> None:
        return None

    def contains_pid(self, pid: int) -> bool:
        return False


class _PosixProcessTreeOwner(_ProcessTreeOwner):
    def __init__(self, process: subprocess.Popen[Any]) -> None:
        self.process = process

    def terminate(self) -> None:
        if self.process.poll() is None:
            process_group = os.getpgid(self.process.pid)
            if process_group != self.process.pid:
                raise OSError("owned POSIX session leader identity changed")
            os.killpg(process_group, 9)

    def wait_empty(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            # Reap the supervisor before probing its process group.  A dead but
            # unreaped leader remains visible to killpg(0) on POSIX.
            self.process.poll()
            try:
                os.killpg(self.process.pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
            if sys.platform == "darwin" and not _darwin_group_has_live_members(self.process.pid):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    def contains_pid(self, pid: int) -> bool:
        if self.process.poll() is not None:
            return False
        try:
            return os.getpgid(pid) == self.process.pid
        except (OSError, ProcessLookupError):
            return False


def _darwin_group_has_live_members(process_group: int) -> bool:
    """Distinguish live group members from launchd-owned zombies on macOS."""
    import ctypes
    import errno

    class ProcBsdInfo(ctypes.Structure):
        _fields_ = [
            ("pbi_flags", ctypes.c_uint32),
            ("pbi_status", ctypes.c_uint32),
            ("pbi_xstatus", ctypes.c_uint32),
            ("pbi_pid", ctypes.c_uint32),
            ("pbi_ppid", ctypes.c_uint32),
            ("pbi_uid", ctypes.c_uint32),
            ("pbi_gid", ctypes.c_uint32),
            ("pbi_ruid", ctypes.c_uint32),
            ("pbi_rgid", ctypes.c_uint32),
            ("pbi_svuid", ctypes.c_uint32),
            ("pbi_svgid", ctypes.c_uint32),
            ("pbi_rfu_1", ctypes.c_uint32),
            ("pbi_comm", ctypes.c_char * 16),
            ("pbi_name", ctypes.c_char * 32),
            ("pbi_nfiles", ctypes.c_uint32),
            ("pbi_pgid", ctypes.c_uint32),
            ("pbi_pjobc", ctypes.c_uint32),
            ("e_tdev", ctypes.c_uint32),
            ("e_tpgid", ctypes.c_uint32),
            ("pbi_nice", ctypes.c_int32),
            ("pbi_start_tvsec", ctypes.c_uint64),
            ("pbi_start_tvusec", ctypes.c_uint64),
        ]

    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        libproc.proc_listpids.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        libproc.proc_listpids.restype = ctypes.c_int
        libproc.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        libproc.proc_pidinfo.restype = ctypes.c_int
        pids = (ctypes.c_int * 4096)()
        ctypes.set_errno(0)
        used = libproc.proc_listpids(2, process_group, pids, ctypes.sizeof(pids))
        if used < 0:
            return True
        if used == 0:
            return ctypes.get_errno() not in (0, errno.ESRCH)
        for pid in pids[: used // ctypes.sizeof(ctypes.c_int)]:
            if pid <= 0:
                continue
            info = ProcBsdInfo()
            ctypes.set_errno(0)
            captured = libproc.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
            if captured != ctypes.sizeof(info):
                if captured == 0 and ctypes.get_errno() == errno.ESRCH:
                    continue
                return True
            if int(info.pbi_pid) != pid or int(info.pbi_pgid) != process_group:
                continue
            if int(info.pbi_status) != 5:
                return True
        return False
    except (AttributeError, OSError):
        return True


class _WindowsProcessTreeOwner(_ProcessTreeOwner):
    _BASIC_ACCOUNTING = 1
    _EXTENDED_LIMIT = 9
    _KILL_ON_CLOSE = 0x00002000
    _SNAPTHREAD = 0x00000004
    _THREAD_SUSPEND_RESUME = 0x0002

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimit),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class BasicAccounting(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_int64),
                ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64),
                ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wintypes.DWORD),
                ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD),
                ("TotalTerminatedProcesses", wintypes.DWORD),
            ]

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._accounting_type = BasicAccounting
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        self._kernel32.SetInformationJobObject.restype = wintypes.BOOL
        self._kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self._kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._kernel32.TerminateJobObject.restype = wintypes.BOOL
        self._kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._kernel32.OpenProcess.restype = wintypes.HANDLE
        self._kernel32.IsProcessInJob.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        ]
        self._kernel32.IsProcessInJob.restype = wintypes.BOOL
        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        self._handle = handle
        limits = ExtendedLimit()
        limits.BasicLimitInformation.LimitFlags = self._KILL_ON_CLOSE
        if not self._kernel32.SetInformationJobObject(
            handle,
            self._EXTENDED_LIMIT,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = ctypes.get_last_error()
            self._kernel32.CloseHandle(handle)
            self._handle = None
            raise OSError(error, "SetInformationJobObject failed")

    def assign(self, process: subprocess.Popen[Any]) -> None:
        if not self._kernel32.AssignProcessToJobObject(self._handle, int(process._handle)):
            raise OSError(self._ctypes.get_last_error(), "AssignProcessToJobObject failed")

    def terminate(self) -> None:
        if self._handle and not self._kernel32.TerminateJobObject(self._handle, 1):
            raise OSError(self._ctypes.get_last_error(), "TerminateJobObject failed")

    def wait_empty(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while self._handle:
            accounting = self._accounting_type()
            if not self._kernel32.QueryInformationJobObject(
                self._handle,
                self._BASIC_ACCOUNTING,
                self._ctypes.byref(accounting),
                self._ctypes.sizeof(accounting),
                None,
            ):
                return False
            if accounting.ActiveProcesses == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None

    def contains_pid(self, pid: int) -> bool:
        if not self._handle or pid <= 0:
            return False
        handle = self._kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            present = self._wintypes.BOOL(False)
            if not self._kernel32.IsProcessInJob(handle, self._handle, self._ctypes.byref(present)):
                return False
            return bool(present.value)
        finally:
            self._kernel32.CloseHandle(handle)


def _resume_windows_process(process: subprocess.Popen[Any]) -> None:
    import ctypes
    from ctypes import wintypes

    class ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(_WindowsProcessTreeOwner._SNAPTHREAD, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    resumed = False
    try:
        entry = ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        present = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while present:
            if entry.th32OwnerProcessID == process.pid:
                thread = kernel32.OpenThread(
                    _WindowsProcessTreeOwner._THREAD_SUSPEND_RESUME,
                    False,
                    entry.th32ThreadID,
                )
                if thread:
                    try:
                        if kernel32.ResumeThread(thread) != 0xFFFFFFFF:
                            resumed = True
                    finally:
                        kernel32.CloseHandle(thread)
            present = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    if not resumed:
        raise OSError("No suspended process thread could be resumed")


@dataclass
class OwnedProcess:
    process: Any
    owner: _ProcessTreeOwner

    def owns_pid(self, pid: int) -> bool:
        return self.owner.contains_pid(pid)

    def close(self) -> None:
        cleanup_failed = False
        try:
            try:
                self.owner.terminate()
            except (NotImplementedError, OSError):
                if self.process.poll() is None:
                    try:
                        self.process.kill()
                    except OSError:
                        cleanup_failed = True
            try:
                self.process.wait(timeout=_CLEANUP_SECS)
            except (OSError, subprocess.TimeoutExpired):
                if self.process.poll() is None:
                    try:
                        self.process.kill()
                    except OSError:
                        cleanup_failed = True
                try:
                    self.process.wait(timeout=1.0)
                except (OSError, subprocess.TimeoutExpired):
                    cleanup_failed = True
            if self.process.poll() is None:
                cleanup_failed = True
        finally:
            try:
                if not self.owner.wait_empty(_CLEANUP_SECS):
                    cleanup_failed = True
            except BaseException:
                cleanup_failed = True
            try:
                self.owner.close()
            except BaseException:
                cleanup_failed = True
        if cleanup_failed:
            raise ProcessCleanupError("owned process-tree cleanup could not be verified")


class _SupervisedProcess:
    """Popen-like child view backed by a durable POSIX session leader."""

    def __init__(
        self,
        supervisor: subprocess.Popen[Any],
        status_path: Path,
        pid: int,
        supervisor_pid: int,
    ) -> None:
        self._supervisor = supervisor
        self._status_path = status_path
        self.pid = pid
        self.supervisor_pid = supervisor_pid
        self.returncode: Optional[int] = None

    def poll(self) -> Optional[int]:
        try:
            status = json.loads(self._status_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            status = None
        if isinstance(status, dict) and status.get("state") == "completed":
            self.returncode = int(status.get("returncode", -1))
            return self.returncode
        if isinstance(status, dict) and status.get("state") == "launch_failed":
            self.returncode = 127
            return self.returncode
        supervisor_result = self._supervisor.poll()
        if supervisor_result is not None:
            self.returncode = int(supervisor_result)
        return self.returncode

    def wait(self, timeout: Optional[float] = None) -> int:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            result = self.poll()
            if result is not None:
                return result
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("owned supervised process", timeout)
            time.sleep(0.01)

    def kill(self) -> None:
        if self._supervisor.poll() is None:
            self._supervisor.kill()


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _read_bounded_output(path: Path) -> tuple[bytes, bool, bool]:
    """Read only an unchanged regular output file and never allocate unbounded data."""
    try:
        before = os.lstat(path)
        attributes = int(getattr(before, "st_file_attributes", 0))
        if not stat.S_ISREG(before.st_mode) or attributes & int(
            getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            return b"", False, True
        if before.st_size > _MAX_OUTPUT_BYTES + 1:
            return b"", True, False
        with path.open("rb") as stream:
            payload = stream.read(_MAX_OUTPUT_BYTES + 1)
        after = os.lstat(path)
    except OSError:
        return b"", False, True
    changed = (
        int(before.st_dev) != int(after.st_dev)
        or int(before.st_ino) != int(after.st_ino)
        or int(before.st_size) != int(after.st_size)
        or len(payload) != int(before.st_size)
    )
    return payload, len(payload) > _MAX_OUTPUT_BYTES, changed


def _start_posix_supervised_process(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    stdout: Any,
    stderr: Any,
    deadline: Optional[float],
) -> OwnedProcess:
    status_path = cwd / "process-status.json"
    ready_path = cwd / "process-ready.json"
    supervisor_script = Path(__file__).with_name("_process_supervisor.py").resolve(strict=True)
    supervisor = subprocess.Popen(
        [
            sys.executable,
            str(supervisor_script),
            str(status_path),
            str(ready_path),
            "--",
            *list(command),
        ],
        cwd=os.fspath(cwd),
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        close_fds=True,
        start_new_session=True,
    )
    owner = _PosixProcessTreeOwner(supervisor)
    ready_deadline = min(
        time.monotonic() + 5.0,
        deadline if deadline is not None else time.monotonic() + 5.0,
    )
    try:
        while time.monotonic() < ready_deadline:
            ready = _read_json(ready_path)
            status = _read_json(status_path)
            if isinstance(status, dict) and status.get("state") == "launch_failed":
                raise OSError("owned command launch failed")
            if isinstance(ready, dict):
                pid = ready.get("pid")
                supervisor_pid = ready.get("supervisor_pid")
                if (
                    ready.get("state") == "running"
                    and isinstance(pid, int)
                    and pid > 0
                    and supervisor_pid == supervisor.pid
                ):
                    return OwnedProcess(
                        _SupervisedProcess(supervisor, status_path, pid, supervisor_pid),
                        owner,
                    )
            if supervisor.poll() is not None:
                raise OSError("owned supervisor exited before readiness")
            time.sleep(0.01)
        raise subprocess.TimeoutExpired(
            "owned process readiness", max(0.0, ready_deadline - time.monotonic())
        )
    except BaseException:
        try:
            owner.terminate()
        except OSError:
            if supervisor.poll() is None:
                supervisor.kill()
        try:
            supervisor.wait(timeout=_CLEANUP_SECS)
        except subprocess.TimeoutExpired:
            pass
        owner.close()
        raise


def start_owned_process(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    stdout: Any,
    stderr: Any,
    deadline: Optional[float] = None,
) -> OwnedProcess:
    kwargs: dict[str, Any] = {
        "cwd": os.fspath(cwd),
        "env": dict(environment),
        "stdin": subprocess.DEVNULL,
        "stdout": stdout,
        "stderr": stderr,
        "close_fds": True,
    }
    if os.name == "posix":
        return _start_posix_supervised_process(
            command,
            cwd=cwd,
            environment=environment,
            stdout=stdout,
            stderr=stderr,
            deadline=deadline,
        )
    if os.name == "nt":
        owner = _WindowsProcessTreeOwner()
        process = None
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | 0x00000004
            process = subprocess.Popen(list(command), creationflags=flags, **kwargs)
            owner.assign(process)
            _resume_windows_process(process)
            return OwnedProcess(process, owner)
        except BaseException:
            try:
                owner.terminate()
            except OSError:
                if process is not None and process.poll() is None:
                    process.kill()
            if process is not None:
                try:
                    process.wait(timeout=_CLEANUP_SECS)
                except subprocess.TimeoutExpired:
                    pass
            owner.close()
            raise
    process = subprocess.Popen(list(command), **kwargs)
    return OwnedProcess(process, _ProcessTreeOwner())


def run_owned_command(
    command: Sequence[str],
    *,
    timeout_secs: float,
    environment: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Run one command under an absolute deadline and return bounded diagnostics."""
    timeout = validate_timeout(timeout_secs, maximum=30.0)
    deadline = time.monotonic() + timeout
    root = Path(tempfile.mkdtemp(prefix="dcc-mcp-cinema4d-process-"))
    stdout_path = root / "stdout.bin"
    stderr_path = root / "stderr.bin"
    reason = None
    owned = None
    returncode = None
    cleanup_error = False
    try:
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            owned = start_owned_process(
                command,
                cwd=root,
                environment=isolated_environment(environment),
                stdout=stdout,
                stderr=stderr,
                deadline=deadline,
            )
        while owned.process.poll() is None and time.monotonic() < deadline:
            if any(
                path.is_file() and path.stat().st_size > _MAX_OUTPUT_BYTES
                for path in (stdout_path, stderr_path)
            ):
                reason = "process output exceeded limit"
                break
            time.sleep(0.01)
        if reason == "process output exceeded limit":
            pass
        elif owned.process.poll() is None:
            reason = "process timed out"
        else:
            returncode = int(owned.process.returncode)
            reason = None if returncode == 0 else "process failed"
    except subprocess.TimeoutExpired:
        reason = "process timed out"
    except (OSError, ValueError):
        reason = "process launch failed"
    finally:
        if owned is not None:
            try:
                owned.close()
            except ProcessCleanupError:
                cleanup_error = True
        stdout, stdout_truncated, stdout_unsafe = _read_bounded_output(stdout_path)
        stderr, stderr_truncated, stderr_unsafe = _read_bounded_output(stderr_path)
        if stdout_unsafe or stderr_unsafe:
            cleanup_error = True
        elif (stdout_truncated or stderr_truncated) and reason is None:
            reason = "process output exceeded limit"
        try:
            shutil.rmtree(root)
        except OSError:
            cleanup_error = True
    if cleanup_error:
        return {"success": False, "reason": "process cleanup failed", "returncode": returncode}
    return {
        "success": reason is None,
        "reason": reason,
        "returncode": returncode,
        "stdout_sha256": __import__("hashlib").sha256(stdout).hexdigest(),
        "stderr_sha256": __import__("hashlib").sha256(stderr).hexdigest(),
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }
