from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from dcc_mcp_core.skills_helper import check_dcc_cancelled

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
        self.max_timeout_secs = max(1.0, float(max_timeout_secs))
        self.driver_path = Path(__file__).with_name("cinema4d_driver.py").resolve()

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
            candidate = candidate.resolve()
            if candidate.is_dir():
                options = (
                    candidate / "c4dpy.exe",
                    candidate / "c4dpy",
                    candidate / "c4dpy.app" / "Contents" / "MacOS" / "c4dpy",
                )
                candidate = next((item for item in options if item.is_file()), candidate)
            if candidate.is_file():
                return str(candidate)
        return None

    def _timeout(self, value: float) -> float:
        timeout = float(value)
        if timeout <= 0 or timeout > self.max_timeout_secs:
            raise BridgeError(
                "timeout_secs must be greater than 0 and no more than %s"
                % int(self.max_timeout_secs)
            )
        return timeout

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
        if not self.executable:
            raise BridgeError("c4dpy was not found; set DCC_MCP_CINEMA4D_C4DPY")
        if not self.driver_path.is_file():
            raise BridgeError("Packaged Cinema 4D driver is missing")
        timeout = self._timeout(timeout_secs)
        with tempfile.TemporaryDirectory(prefix="dcc-mcp-cinema4d-") as temp_dir_value:
            temp_dir = Path(temp_dir_value)
            request_path = temp_dir / "request.json"
            result_path = temp_dir / "result.json"
            runtime_pid_path = temp_dir / "runtime.pid"
            request_path.write_text(
                json.dumps({"method": method, "params": dict(params)}, ensure_ascii=False),
                encoding="utf-8",
            )
            command = [
                self.executable,
                str(self.driver_path),
                str(request_path),
                str(result_path),
                str(runtime_pid_path),
            ]
            started = time.monotonic()
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stdout_file:
                with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr_file:
                    process = subprocess.Popen(
                        command,
                        cwd=str(temp_dir),
                        env=os.environ.copy(),
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_file,
                        stderr=stderr_file,
                        text=True,
                        creationflags=creationflags,
                    )
                    try:
                        deadline = started + timeout
                        while not result_path.is_file():
                            check_dcc_cancelled()
                            if time.monotonic() >= deadline:
                                raise BridgeTimeoutError(
                                    "c4dpy exceeded the %.1f second timeout" % timeout
                                )
                            time.sleep(0.05)
                    except BaseException:
                        self._terminate_runtime(process, runtime_pid_path)
                        raise
                    if process.poll() is None:
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            self._terminate_runtime(process, runtime_pid_path)
                    stdout_file.seek(0)
                    stderr_file.seek(0)
                    stdout = stdout_file.read(65_537)
                    stderr = stderr_file.read(65_537)
            if not result_path.is_file():
                raise BridgeError(
                    "c4dpy did not return a result (exit %s): %s"
                    % (process.returncode, (stderr or stdout).strip()[:1_000])
                )
            if result_path.stat().st_size > 16 * 1024 * 1024:
                raise BridgeError("Cinema 4D result exceeded the 16 MiB response limit")
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            if not payload.get("ok"):
                error = payload.get("error") or {}
                raise BridgeError(str(error.get("message") or "Cinema 4D operation failed"))
            result = payload.get("result")
            if not isinstance(result, dict):
                result = {"result": result}
            result.update(
                {
                    "duration_secs": round(time.monotonic() - started, 3),
                    "stdout": stdout[:65_536],
                    "stderr": stderr[:65_536],
                    "stdout_truncated": len(stdout) > 65_536,
                    "stderr_truncated": len(stderr) > 65_536,
                }
            )
            return result

    @staticmethod
    def _terminate_runtime(process: subprocess.Popen[str], runtime_pid_path: Path) -> None:
        """Stop only the launcher and the runtime that acknowledged this request."""
        runtime_pid = None
        try:
            if runtime_pid_path.is_file():
                runtime_pid = int(runtime_pid_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            runtime_pid = None

        if runtime_pid and runtime_pid != os.getpid() and runtime_pid != process.pid:
            try:
                os.kill(runtime_pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

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

    def status(self) -> dict[str, Any]:
        if not self.executable:
            return {
                "ready": False,
                "executable": None,
                "instance_type": "standalone",
                "reason": "c4dpy_not_found",
                "allowed_roots": [str(root) for root in self.allowed_roots],
            }
        result = self._invoke("system.status", {}, 60)
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
