from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from dcc_mcp_core.skills_helper import check_dcc_cancelled

from ._process import (
    ProcessCleanupError,
    isolated_environment,
    start_owned_process,
    validate_timeout,
)

_DOCUMENT_SUFFIX = ".c4d"
_IMPORT_SUFFIXES = {
    ".3ds",
    ".abc",
    ".c4d",
    ".dae",
    ".fbx",
    ".glb",
    ".gltf",
    ".obj",
    ".stl",
}
_EXPORT_SUFFIXES = {".abc", ".c4d", ".dae", ".fbx", ".glb", ".gltf", ".obj", ".stl"}
_RENDER_SUFFIXES = {".jpg", ".jpeg", ".png"}
_OBJECT_NAME = re.compile(r"^[^/\\\x00-\x1f]{1,128}$")
_PRIMITIVE_DIMENSIONS = {
    "cube": {"size"},
    "sphere": {"radius"},
    "cylinder": {"radius", "height"},
    "cone": {"bottom_radius", "top_radius", "height"},
    "torus": {"ring_radius", "pipe_radius"},
    "plane": {"width", "height"},
}


class BridgeError(RuntimeError):
    """A bounded Cinema 4D failure safe to return to a local caller."""


class BridgeTimeoutError(BridgeError):
    """c4dpy exceeded the configured deadline."""


def _within(path: Path, roots: Sequence[Path]) -> bool:
    candidate = os.path.normcase(str(path))
    for root in roots:
        root_value = os.path.normcase(str(root))
        try:
            if os.path.commonpath((candidate, root_value)) == root_value:
                return True
        except ValueError:
            continue
    return False


def _split_roots(value: str) -> list[Path]:
    return [
        Path(item.strip()).expanduser().resolve()
        for item in value.split(os.pathsep)
        if item.strip()
    ]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    junction = getattr(path, "is_junction", None)
    if callable(junction) and junction():
        return True
    try:
        attributes = int(getattr(os.lstat(path), "st_file_attributes", 0))
    except OSError:
        return True
    return bool(attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))


def _require_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise BridgeTimeoutError("c4dpy exceeded the configured timeout")


def _read_private_file(path: Path, maximum: int, *, deadline: float) -> bytes:
    while True:
        try:
            _require_deadline(deadline)
            if _is_link_or_reparse(path) or not path.is_file():
                raise BridgeError("Cinema 4D returned an unsafe private artifact")
            _require_deadline(deadline)
            before = os.stat(path, follow_symlinks=False)
            if before.st_size < 0 or before.st_size > maximum:
                raise BridgeError("Cinema 4D private artifact exceeded its size limit")
            _require_deadline(deadline)
            with path.open("rb") as stream:
                _require_deadline(deadline)
                payload = stream.read(maximum + 1)
            _require_deadline(deadline)
            if len(payload) > maximum:
                raise BridgeError("Cinema 4D private artifact exceeded its size limit")
            after = os.stat(path, follow_symlinks=False)
            _require_deadline(deadline)
            break
        except PermissionError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BridgeTimeoutError("c4dpy exceeded the configured timeout") from exc
            time.sleep(min(0.01, remaining))
        except OSError as exc:
            raise BridgeError("Cinema 4D private artifact could not be inspected") from exc
    if (
        int(before.st_dev) != int(after.st_dev)
        or int(before.st_ino) != int(after.st_ino)
        or int(before.st_size) != int(after.st_size)
        or len(payload) != int(before.st_size)
    ):
        raise BridgeError("Cinema 4D private artifact changed during inspection")
    return payload


def _write_ack_exclusive(path: Path, *, deadline: float) -> None:
    try:
        _require_deadline(deadline)
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            _require_deadline(deadline)
            stream.write(b"ok\n")
            stream.flush()
            os.fsync(stream.fileno())
        _require_deadline(deadline)
    except OSError as exc:
        raise BridgeError("Cinema 4D acknowledgement path is unsafe") from exc


def _replace_staged_path(value: Any, staged: Path, final: Path) -> Any:
    if isinstance(value, dict):
        return {key: _replace_staged_path(item, staged, final) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_staged_path(item, staged, final) for item in value]
    if isinstance(value, str):
        return value.replace(str(staged), str(final)).replace(
            str(staged).replace("\\", "/"), str(final).replace("\\", "/")
        )
    return value


class Cinema4dBridge:
    """Run typed, package-owned operations in an isolated c4dpy process."""

    def __init__(
        self,
        executable: Optional[str] = None,
        allowed_roots: Optional[Iterable[Path]] = None,
        max_input_bytes: int = 2 * 1024 * 1024 * 1024,
        max_timeout_secs: float = 1_800,
    ):
        self.executable = self._resolve_executable(executable)
        roots = list(allowed_roots or (Path.cwd(),))
        self.allowed_roots = tuple(Path(root).expanduser().resolve() for root in roots)
        self.max_input_bytes = max(1, int(max_input_bytes))
        maximum = float(max_timeout_secs)
        if not math.isfinite(maximum) or maximum <= 0:
            raise ValueError("max_timeout_secs must be a finite positive number")
        self.max_timeout_secs = maximum
        self.driver_path = Path(__file__).with_name("cinema4d_driver.py").resolve()
        self.executable_sha256 = _sha256_file(Path(self.executable)) if self.executable else None

    @classmethod
    def from_env(cls) -> "Cinema4dBridge":
        roots_value = os.environ.get("DCC_MCP_CINEMA4D_ALLOWED_ROOTS", "")
        roots = _split_roots(roots_value) if roots_value else [Path.cwd().resolve()]
        return cls(
            os.environ.get("DCC_MCP_CINEMA4D_C4DPY") or None,
            allowed_roots=roots,
            max_input_bytes=int(
                os.environ.get("DCC_MCP_CINEMA4D_MAX_INPUT_BYTES", str(2 * 1024**3))
            ),
            max_timeout_secs=float(os.environ.get("DCC_MCP_CINEMA4D_MAX_TIMEOUT_SECS", "1800")),
        )

    @staticmethod
    def _resolve_executable(explicit: Optional[str]) -> Optional[str]:
        candidates: list[Path] = []
        if explicit:
            candidates.append(Path(explicit).expanduser())
        else:
            found = shutil.which("c4dpy") or shutil.which("c4dpy.exe")
            if found:
                candidates.append(Path(found))
            if os.name == "nt":
                program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                maxon_root = program_files / "Maxon"
                if maxon_root.is_dir():
                    candidates.extend(
                        sorted(maxon_root.glob("Cinema 4D */c4dpy.exe"), reverse=True)
                    )
                candidates.extend(program_files.glob("Maxon Cinema 4D */c4dpy.exe"))
            elif sys_platform() == "darwin":
                candidates.extend(
                    sorted(
                        Path("/Applications").glob(
                            "Maxon Cinema 4D */c4dpy.app/Contents/MacOS/c4dpy"
                        ),
                        reverse=True,
                    )
                )
        for candidate in candidates:
            if candidate.is_symlink():
                continue
            candidate = candidate.resolve()
            if candidate.is_dir():
                options = (
                    candidate / "c4dpy.exe",
                    candidate / "c4dpy",
                    candidate / "c4dpy.app" / "Contents" / "MacOS" / "c4dpy",
                )
                candidate = next((item for item in options if item.is_file()), candidate)
            junction = getattr(candidate, "is_junction", None)
            if candidate.is_file() and not (callable(junction) and junction()):
                return str(candidate)
        return None

    def _timeout(self, value: float) -> float:
        try:
            return validate_timeout(value, maximum=self.max_timeout_secs)
        except ValueError as exc:
            raise BridgeError(
                "timeout_secs must be finite, positive, and within the configured maximum"
            ) from exc

    def _path(self, value: str, suffixes: set[str], *, must_exist: bool) -> Path:
        path = Path(value).expanduser().resolve()
        if path.suffix.lower() not in suffixes:
            raise BridgeError("Unsupported file extension: %s" % path.suffix)
        if must_exist and not path.is_file():
            raise BridgeError("Input file does not exist: %s" % path)
        if not must_exist and not path.parent.is_dir():
            raise BridgeError("Output directory does not exist: %s" % path.parent)
        if not _within(path, self.allowed_roots):
            raise BridgeError("Path is outside DCC_MCP_CINEMA4D_ALLOWED_ROOTS")
        if must_exist and path.stat().st_size > self.max_input_bytes:
            raise BridgeError("Input file exceeds the configured size limit")
        return path

    def _document_path(self, value: str) -> Path:
        return self._path(value, {_DOCUMENT_SUFFIX}, must_exist=True)

    def _output_path(self, value: str, suffixes: set[str]) -> Path:
        return self._path(value, suffixes, must_exist=False)

    def _input_path(self, value: str, suffixes: set[str]) -> Path:
        return self._path(value, suffixes, must_exist=True)

    @staticmethod
    def _object_name(value: str) -> str:
        if not _OBJECT_NAME.fullmatch(value):
            raise BridgeError(
                "Object names must contain 1-128 printable characters without path separators"
            )
        return value

    def _invoke(
        self, method: str, params: Mapping[str, Any], timeout_secs: float = 120
    ) -> dict[str, Any]:
        started = time.monotonic()
        timeout = self._timeout(timeout_secs)
        deadline = started + timeout
        if not self.executable:
            raise BridgeError("c4dpy was not found; set DCC_MCP_CINEMA4D_C4DPY")
        if not self.driver_path.is_file():
            raise BridgeError("Packaged Cinema 4D driver is missing")
        _require_deadline(deadline)
        temp_dir: Optional[Path] = None
        owned = None
        primary: Optional[BaseException] = None
        cleanup_error = False
        completed_result: Optional[dict[str, Any]] = None
        try:
            temp_dir = Path(tempfile.mkdtemp(prefix="dcc-mcp-cinema4d-"))
            _require_deadline(deadline)
            request_path = temp_dir / "request.json"
            result_path = temp_dir / "result.json"
            runtime_identity_path = temp_dir / "runtime.json"
            runtime_ready_ack = temp_dir / "runtime-ready.ack"
            runtime_result_ack = temp_dir / "runtime-result.ack"
            stdout_path = temp_dir / "stdout.bin"
            stderr_path = temp_dir / "stderr.bin"
            _require_deadline(deadline)
            request_path.write_text(
                json.dumps({"method": method, "params": dict(params)}, ensure_ascii=False),
                encoding="utf-8",
            )
            _require_deadline(deadline)
            command = [
                self.executable,
                str(self.driver_path),
                str(request_path),
                str(result_path),
                str(runtime_identity_path),
                str(runtime_ready_ack),
                str(runtime_result_ack),
            ]
            with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
                try:
                    owned = start_owned_process(
                        command,
                        cwd=temp_dir,
                        environment=isolated_environment(),
                        stdout=stdout_file,
                        stderr=stderr_file,
                        deadline=deadline,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise BridgeTimeoutError("c4dpy exceeded the configured timeout") from exc
            runtime_identity: Optional[dict[str, Any]] = None
            while not result_path.is_file():
                check_dcc_cancelled()
                if any(
                    path.is_file() and path.stat().st_size > 65_536
                    for path in (stdout_path, stderr_path)
                ):
                    raise BridgeError("Cinema 4D process output exceeded the configured limit")
                if runtime_identity is None and runtime_identity_path.is_file():
                    runtime_identity = self._acknowledge_runtime(
                        runtime_identity_path,
                        runtime_ready_ack,
                        deadline=deadline,
                        owned_process=owned,
                    )
                if time.monotonic() >= deadline:
                    raise BridgeTimeoutError("c4dpy exceeded the configured timeout")
                time.sleep(0.02)
            if runtime_identity is None:
                runtime_identity = self._acknowledge_runtime(
                    runtime_identity_path,
                    runtime_ready_ack,
                    deadline=deadline,
                    owned_process=owned,
                )
            if time.monotonic() >= deadline:
                raise BridgeTimeoutError("c4dpy exceeded the configured timeout")
            self._recapture_runtime(runtime_identity)
            if time.monotonic() >= deadline:
                raise BridgeTimeoutError("c4dpy exceeded the configured timeout")
            _write_ack_exclusive(runtime_result_ack, deadline=deadline)
            self._wait_for_runtime_exit(owned, int(runtime_identity["pid"]), deadline=deadline)
            stdout = _read_private_file(stdout_path, 65_537, deadline=deadline)
            stderr = _read_private_file(stderr_path, 65_537, deadline=deadline)
            _require_deadline(deadline)
            if not result_path.is_file():
                raise BridgeError("c4dpy did not return a result")
            try:
                payload = json.loads(
                    _read_private_file(result_path, 16 * 1024 * 1024, deadline=deadline).decode(
                        "utf-8"
                    )
                )
            except (UnicodeError, ValueError) as exc:
                raise BridgeError("Cinema 4D returned an invalid result") from exc
            if not payload.get("ok"):
                error = payload.get("error") or {}
                error_type = str(error.get("type") or "runtime_error")
                if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{0,127}", error_type):
                    error_type = "runtime_error"
                raise BridgeError("Cinema 4D operation failed (%s)" % error_type)
            result = payload.get("result")
            if not isinstance(result, dict):
                result = {"result": result}
            _require_deadline(deadline)
            project_source = params.get("document_path") or params.get("output_path")
            result.update(
                {
                    "duration_secs": round(time.monotonic() - started, 3),
                    "runtime_identity": runtime_identity,
                    "listener_identity": "not_applicable_standalone",
                    "project_identity": (
                        hashlib.sha256(str(project_source).encode("utf-8")).hexdigest()
                        if project_source
                        else "no_project_for_status_probe"
                    ),
                    "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
                    "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                    "stdout_truncated": len(stdout) > 65_536,
                    "stderr_truncated": len(stderr) > 65_536,
                }
            )
            _require_deadline(deadline)
            completed_result = result
        except BaseException as exc:
            primary = exc
        finally:
            if owned is not None:
                try:
                    owned.close(deadline=deadline)
                except ProcessCleanupError:
                    cleanup_error = True
            while temp_dir is not None and temp_dir.exists():
                try:
                    shutil.rmtree(temp_dir)
                except FileNotFoundError:
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        cleanup_error = True
                        break
                    time.sleep(0.02)
                else:
                    break
        if cleanup_error:
            raise BridgeError("Cinema 4D process-tree cleanup could not be verified") from primary
        if primary is not None:
            raise primary
        _require_deadline(deadline)
        if completed_result is not None:
            return completed_result
        raise BridgeError("Cinema 4D operation did not produce a result")

    def _acknowledge_runtime(
        self,
        identity_path: Path,
        ack_path: Path,
        *,
        deadline: float,
        expected_pid: Optional[int] = None,
        owned_process: Optional[Any] = None,
    ) -> dict[str, Any]:
        identity: Any = None
        while time.monotonic() < deadline:
            try:
                identity = json.loads(
                    _read_private_file(identity_path, 64 * 1024, deadline=deadline).decode("utf-8")
                )
                pid = int(identity["pid"])
                protocol = int(identity["protocol"])
            except (KeyError, TypeError, UnicodeError, ValueError):
                time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
                continue
            is_owned = (
                owned_process.owns_pid(pid)
                if owned_process is not None
                else expected_pid is not None and pid == expected_pid
            )
            if protocol != 1 or not is_owned:
                raise BridgeError("Cinema 4D runtime is not the owned process")
            break
        else:
            raise BridgeTimeoutError("c4dpy exceeded the configured timeout")
        if identity.get("executable") not in {None, self.executable}:
            raise BridgeError("Cinema 4D runtime executable identity does not match")
        from .install import _observe_bound_runtime

        observed = _observe_bound_runtime(pid, self._host_identity_for_runtime())
        _write_ack_exclusive(ack_path, deadline=deadline)
        _require_deadline(deadline)
        return observed

    def _recapture_runtime(self, identity: Mapping[str, Any]) -> None:
        from .install import _recapture_bound_runtime

        _recapture_bound_runtime(identity, self._host_identity_for_runtime())

    @staticmethod
    def _wait_for_runtime_exit(owned_process: Any, pid: int, *, deadline: float) -> None:
        """Allow an acknowledged runtime to finish without granting a second budget."""
        waiter = getattr(owned_process, "wait_pid_exit_until", None)
        if callable(waiter):
            if not waiter(pid, deadline):
                raise BridgeTimeoutError("c4dpy exceeded the configured timeout")
        else:
            while owned_process.owns_pid(pid):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BridgeTimeoutError("c4dpy exceeded the configured timeout")
                time.sleep(min(0.01, remaining))
        _require_deadline(deadline)

    def _host_identity_for_runtime(self):
        from .install import HostIdentity, _file_identity

        if not self.executable or not self.executable_sha256:
            raise BridgeError("Cinema 4D executable identity is unavailable")
        path = Path(self.executable)
        return HostIdentity(
            path=path,
            product="Maxon Cinema 4D",
            product_version="runtime",
            file_version="runtime",
            sha256=self.executable_sha256,
            size=path.stat().st_size,
            file_identity=_file_identity(path),
        )

    def _mutate_document(
        self,
        method: str,
        document_path: str,
        params: Mapping[str, Any],
        timeout_secs: float,
    ) -> dict[str, Any]:
        document = self._document_path(document_path)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".%s." % document.stem,
            suffix=document.suffix,
            dir=str(document.parent),
        )
        os.close(descriptor)
        staged = Path(temp_name)
        try:
            shutil.copy2(str(document), str(staged))
            request = dict(params)
            request["document_path"] = str(staged)
            result = self._invoke(method, request, timeout_secs)
            if not staged.is_file() or staged.stat().st_size <= 0:
                raise BridgeError("Cinema 4D mutation did not produce a durable document")
            os.replace(str(staged), str(document))
            result = _replace_staged_path(result, staged, document)
            durable = self._invoke(
                "document.inspect", {"document_path": str(document)}, timeout_secs
            )
            result["document"] = {
                key: value
                for key, value in durable.items()
                if key
                not in {
                    "duration_secs",
                    "stderr",
                    "stderr_truncated",
                    "stdout",
                    "stdout_truncated",
                }
            }
            result.update(
                {
                    "document_path": str(document),
                    "document_bytes": document.stat().st_size,
                    "document_sha256": _sha256_file(document),
                }
            )
            return result
        finally:
            if staged.exists():
                staged.unlink()

    def _produce_output(
        self,
        method: str,
        output: Path,
        params: Mapping[str, Any],
        overwrite: bool,
        timeout_secs: float,
    ) -> dict[str, Any]:
        replaced_existing = output.exists()
        if replaced_existing and not overwrite:
            raise BridgeError("Output already exists; set overwrite=true to replace it")
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".%s." % output.stem,
            suffix=output.suffix,
            dir=str(output.parent),
        )
        os.close(descriptor)
        staged = Path(temp_name)
        staged.unlink()
        try:
            request = dict(params)
            request["output_path"] = str(staged)
            result = self._invoke(method, request, timeout_secs)
            if not staged.is_file() or staged.stat().st_size <= 0:
                raise BridgeError("Cinema 4D did not create a durable output")
            os.replace(str(staged), str(output))
            result = _replace_staged_path(result, staged, output)
            result.update(
                {
                    "output_path": str(output),
                    "bytes": output.stat().st_size,
                    "sha256": _sha256_file(output),
                    "overwritten": replaced_existing,
                }
            )
            return result
        finally:
            if staged.exists():
                staged.unlink()

    def status(self, timeout_secs: float = 60) -> dict[str, Any]:
        if not self.executable:
            return {
                "ready": False,
                "executable": None,
                "instance_type": "standalone",
                "reason": "c4dpy_not_found",
                "allowed_roots": [str(root) for root in self.allowed_roots],
            }
        result = self._invoke("system.status", {}, timeout_secs)
        result.update(
            {
                "ready": True,
                "executable": self.executable,
                "instance_type": "standalone",
                "allowed_roots": [str(root) for root in self.allowed_roots],
                "max_input_bytes": self.max_input_bytes,
                "max_timeout_secs": self.max_timeout_secs,
            }
        )
        return result

    def capabilities(self) -> dict[str, Any]:
        return {
            "status": self.status(),
            "methods": [
                "create_document",
                "inspect_document",
                "validate_document",
                "save_copy",
                "add_primitive",
                "transform_object",
                "remove_object",
                "import_geometry",
                "export_document",
                "render_document",
            ],
            "primitives": sorted(_PRIMITIVE_DIMENSIONS),
            "import_extensions": sorted(_IMPORT_SUFFIXES),
            "export_extensions": sorted(_EXPORT_SUFFIXES),
            "render_extensions": sorted(_RENDER_SUFFIXES),
            "validated_interchange_baseline": {
                "import_extensions": [".obj"],
                "export_extensions": [".obj"],
            },
            "format_availability": "runtime_dependent",
            "atomic_document_mutations": True,
            "arbitrary_python": False,
            "gui_required": False,
        }

    def create_document(
        self, path: str, overwrite: bool = False, timeout_secs: float = 120
    ) -> dict[str, Any]:
        output = self._output_path(path, {_DOCUMENT_SUFFIX})
        result = self._produce_output("document.create", output, {}, overwrite, timeout_secs)
        durable = self._invoke("document.inspect", {"document_path": str(output)}, timeout_secs)
        result["document"] = {
            key: value
            for key, value in durable.items()
            if key
            not in {
                "duration_secs",
                "stderr",
                "stderr_truncated",
                "stdout",
                "stdout_truncated",
            }
        }
        return result

    def inspect_document(self, path: str, timeout_secs: float = 120) -> dict[str, Any]:
        document = self._document_path(path)
        result = self._invoke("document.inspect", {"document_path": str(document)}, timeout_secs)
        result.update(
            {
                "document_path": str(document),
                "document_bytes": document.stat().st_size,
                "document_sha256": _sha256_file(document),
            }
        )
        return result

    def validate_document(self, path: str, timeout_secs: float = 120) -> dict[str, Any]:
        document = self._document_path(path)
        return self._invoke("document.validate", {"document_path": str(document)}, timeout_secs)

    def save_copy(
        self,
        source_path: str,
        output_path: str,
        overwrite: bool = False,
        timeout_secs: float = 120,
    ) -> dict[str, Any]:
        source = self._document_path(source_path)
        output = self._output_path(output_path, {_DOCUMENT_SUFFIX})
        return self._produce_output(
            "document.save_copy",
            output,
            {"document_path": str(source)},
            overwrite,
            timeout_secs,
        )

    def add_primitive(
        self,
        document_path: str,
        primitive: str,
        name: str,
        dimensions: Mapping[str, Any],
        translation: Sequence[float] = (0, 0, 0),
        rotation_hpb_degrees: Sequence[float] = (0, 0, 0),
        scale: Sequence[float] = (1, 1, 1),
        timeout_secs: float = 120,
    ) -> dict[str, Any]:
        primitive_key = primitive.lower()
        allowed = _PRIMITIVE_DIMENSIONS.get(primitive_key)
        if allowed is None:
            raise BridgeError("Unsupported primitive: %s" % primitive)
        unknown = set(dimensions) - allowed
        if unknown:
            raise BridgeError("Unsupported %s dimensions: %s" % (primitive, ", ".join(unknown)))
        return self._mutate_document(
            "model.add_primitive",
            document_path,
            {
                "primitive": primitive_key,
                "name": self._object_name(name),
                "dimensions": dict(dimensions),
                "translation": list(translation),
                "rotation_hpb_degrees": list(rotation_hpb_degrees),
                "scale": list(scale),
            },
            timeout_secs,
        )

    def transform_object(
        self,
        document_path: str,
        object_name: str,
        translation: Sequence[float],
        rotation_hpb_degrees: Sequence[float] = (0, 0, 0),
        scale: Sequence[float] = (1, 1, 1),
        timeout_secs: float = 120,
    ) -> dict[str, Any]:
        return self._mutate_document(
            "model.transform_object",
            document_path,
            {
                "object_name": self._object_name(object_name),
                "translation": list(translation),
                "rotation_hpb_degrees": list(rotation_hpb_degrees),
                "scale": list(scale),
            },
            timeout_secs,
        )

    def remove_object(
        self,
        document_path: str,
        object_name: str,
        cascade: bool = False,
        timeout_secs: float = 120,
    ) -> dict[str, Any]:
        return self._mutate_document(
            "model.remove_object",
            document_path,
            {"object_name": self._object_name(object_name), "cascade": bool(cascade)},
            timeout_secs,
        )

    def import_geometry(
        self,
        document_path: str,
        input_path: str,
        timeout_secs: float = 600,
    ) -> dict[str, Any]:
        source = self._input_path(input_path, _IMPORT_SUFFIXES)
        return self._mutate_document(
            "model.import_geometry",
            document_path,
            {"input_path": str(source)},
            timeout_secs,
        )

    def export_document(
        self,
        document_path: str,
        output_path: str,
        overwrite: bool = False,
        timeout_secs: float = 600,
    ) -> dict[str, Any]:
        document = self._document_path(document_path)
        output = self._output_path(output_path, _EXPORT_SUFFIXES)
        return self._produce_output(
            "document.export",
            output,
            {"document_path": str(document)},
            overwrite,
            timeout_secs,
        )

    def render_document(
        self,
        document_path: str,
        output_path: str,
        width: int = 640,
        height: int = 480,
        overwrite: bool = False,
        timeout_secs: float = 1_200,
    ) -> dict[str, Any]:
        document = self._document_path(document_path)
        output = self._output_path(output_path, _RENDER_SUFFIXES)
        if not 1 <= int(width) <= 8_192 or not 1 <= int(height) <= 8_192:
            raise BridgeError("Render dimensions must be between 1 and 8192 pixels")
        return self._produce_output(
            "document.render",
            output,
            {
                "document_path": str(document),
                "width": int(width),
                "height": int(height),
            },
            overwrite,
            timeout_secs,
        )


def sys_platform() -> str:
    import sys

    return sys.platform


def get_bridge() -> Cinema4dBridge:
    return Cinema4dBridge.from_env()
