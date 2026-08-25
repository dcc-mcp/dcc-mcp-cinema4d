"""Cinema 4D-owned Install SOP lifecycle using the released Core contract."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from importlib import import_module
from importlib.metadata import PackageNotFoundError, distribution
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional
from urllib.parse import unquote, urlsplit

from dcc_mcp_core.deployment import (
    INSTALL_EXIT_INSTALL,
    INSTALL_EXIT_OK,
    INSTALL_EXIT_PREFLIGHT,
    INSTALL_EXIT_VERIFY,
    INSTALL_SOP_SCHEMA_VERSION,
    load_install_sop_schema,
)

from .__version__ import __version__
from ._process import isolated_environment, run_owned_command, validate_timeout
from .bridge import BridgeError, BridgeTimeoutError, Cinema4dBridge

DCC_TYPE = "c4d"
COMMAND = "dcc-mcp-cinema4d"
MINIMUM_CORE_VERSION = "0.20.14"
MINIMUM_HOST_BUILD = 21_000
MAX_TIMEOUT_SECS = 1_800.0

_STATE_ENV = "DCC_MCP_CINEMA4D_STATE_DIR"
_LOCK_GUARD = threading.Lock()
_LOCKED_ROOTS: set[str] = set()
_VERSION_RE = re.compile(
    r"(?:R(?P<release>\d{2,3})|(?P<year>20\d{2}))(?:[ ._-](?P<minor>\d+))?", re.I
)
_CANONICAL_VERSION_RE = re.compile(r"(?:0|[1-9]\d{0,5})(?:\.(?:0|[1-9]\d{0,5})){2}")


@dataclass(frozen=True)
class HostIdentity:
    path: Path
    product: str
    product_version: str
    file_version: str
    sha256: str
    size: int
    file_identity: str


@dataclass(frozen=True)
class PythonIdentity:
    executable: Path
    version: str
    prefix: Path
    adapter_version: str
    core_version: str
    adapter_origin: Path
    core_origin: Path
    adapter_sha256: Optional[str] = None
    core_sha256: Optional[str] = None
    server_version: Optional[str] = None
    server_origin: Optional[Path] = None
    server_sha256: Optional[str] = None
    server_binary: Optional[Path] = None
    server_binary_sha256: Optional[str] = None


@dataclass(frozen=True)
class InstallContext:
    host: HostIdentity
    python: PythonIdentity
    state_root: Path
    receipt_path: Path
    state: str
    receipt: Optional[dict[str, Any]]
    receipt_identity: Optional[str] = None


@dataclass(frozen=True)
class LifecycleRequest:
    operation: str
    executable: Optional[Path] = None
    python_executable: Optional[Path] = None
    state_root: Optional[Path] = None
    timeout_secs: float = 60.0
    execute: bool = False
    target: Optional[str] = None


@dataclass(frozen=True)
class LifecycleOutcome:
    result: dict[str, Any]
    exit_code: int


class LifecycleFailure(RuntimeError):
    def __init__(self, stage: str, message: str, exit_code: int = INSTALL_EXIT_PREFLIGHT) -> None:
        super().__init__(message)
        self.stage = stage
        self.exit_code = exit_code


def _require_official_core_contract() -> None:
    try:
        schema = load_install_sop_schema()
        version = schema["properties"]["schema_version"]["const"]
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise LifecycleFailure(
            "core_contract", "The official Core Install SOP schema is unavailable."
        ) from exc
    if (
        schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema"
        or version != INSTALL_SOP_SCHEMA_VERSION
    ):
        raise LifecycleFailure(
            "core_contract", "The official Core Install SOP schema is incompatible."
        )


def _version_tuple(value: object) -> Optional[tuple[int, int, int]]:
    text = str(value or "").strip()
    if not _CANONICAL_VERSION_RE.fullmatch(text):
        return None
    parts = tuple(int(item) for item in text.split("."))
    return parts if len(parts) == 3 else None


def _core_version() -> str:
    try:
        return package_version("dcc-mcp-core")
    except PackageNotFoundError:
        return "unavailable"


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_identity(path: Path) -> str:
    info = os.stat(path, follow_symlinks=False)
    return "device:%s:file:%s" % (int(info.st_dev), int(info.st_ino))


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


def _require_regular_file(path: Path, *, stage: str, label: str) -> Path:
    try:
        if _is_link_or_reparse(path) or not path.is_file() or path.stat().st_size <= 0:
            raise LifecycleFailure(stage, f"{label} is not a non-empty regular file.")
    except OSError as exc:
        raise LifecycleFailure(stage, f"{label} could not be inspected.") from exc
    return path.resolve()


def _windows_file_metadata(path: Path) -> dict[str, str]:
    import ctypes
    from ctypes import wintypes

    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, wintypes.LPDWORD]
    version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    version.GetFileVersionInfoW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
    ]
    version.GetFileVersionInfoW.restype = wintypes.BOOL
    version.VerQueryValueW.argtypes = [
        wintypes.LPCVOID,
        wintypes.LPCWSTR,
        ctypes.POINTER(wintypes.LPVOID),
        wintypes.PUINT,
    ]
    version.VerQueryValueW.restype = wintypes.BOOL
    unused = wintypes.DWORD(0)
    size = version.GetFileVersionInfoSizeW(str(path), ctypes.byref(unused))
    if not size:
        return {}
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        return {}
    translations_ptr = wintypes.LPVOID()
    translations_len = wintypes.UINT(0)
    if not version.VerQueryValueW(
        buffer,
        r"\VarFileInfo\Translation",
        ctypes.byref(translations_ptr),
        ctypes.byref(translations_len),
    ):
        return {}
    values = ctypes.cast(translations_ptr, ctypes.POINTER(ctypes.c_ushort))
    language, codepage = int(values[0]), int(values[1])
    result = {}
    for source, target in (
        ("CompanyName", "company"),
        ("ProductName", "product"),
        ("ProductVersion", "product_version"),
        ("FileVersion", "file_version"),
    ):
        pointer = wintypes.LPVOID()
        length = wintypes.UINT(0)
        query = rf"\StringFileInfo\{language:04x}{codepage:04x}\{source}"
        if (
            version.VerQueryValueW(buffer, query, ctypes.byref(pointer), ctypes.byref(length))
            and length.value
        ):
            result[target] = ctypes.wstring_at(pointer, length.value).rstrip("\x00")
    return result


def _platform_file_metadata(path: Path) -> dict[str, str]:
    if os.name == "nt":
        return _windows_file_metadata(path)
    if sys.platform == "darwin":
        import plistlib

        for parent in path.parents:
            if parent.suffix.lower() == ".app":
                plist_path = parent / "Contents" / "Info.plist"
                try:
                    payload = plistlib.loads(plist_path.read_bytes())
                except (OSError, ValueError):
                    return {}
                bundle_name = str(payload.get("CFBundleName") or "")
                bundle_identifier = str(payload.get("CFBundleIdentifier") or "")
                bundle_identity = re.sub(
                    r"[^a-z0-9]+",
                    " ",
                    "%s %s %s" % (bundle_name, bundle_identifier, parent.parent.name).lower(),
                )
                if "maxon" not in bundle_identity or not any(
                    token in bundle_identity for token in ("cinema 4d", "cinema4d", "c4dpy")
                ):
                    return {}
                return {
                    "company": "Maxon",
                    "product": "Maxon Cinema 4D",
                    "product_version": str(payload.get("CFBundleShortVersionString") or ""),
                    "file_version": str(payload.get("CFBundleVersion") or ""),
                }
        return {}
    parent_text = " ".join(parent.name for parent in tuple(path.parents)[:4])
    normalized_parent = re.sub(r"[^a-z0-9]+", " ", parent_text.lower()).strip()
    if "maxon" not in normalized_parent or "cinema 4d" not in normalized_parent:
        return {}
    match = _VERSION_RE.search(parent_text)
    if not match:
        return {}
    value = match.group(0).replace(" ", ".").replace("_", ".").replace("-", ".")
    return {
        "company": "Maxon",
        "product": "Maxon Cinema 4D",
        "product_version": value,
        "file_version": value,
    }


def _host_build(value: str) -> int:
    match = _VERSION_RE.search(str(value))
    if not match:
        canonical = _version_tuple(str(value))
        return canonical[0] * 1000 + canonical[1] if canonical else 0
    if match.group("release"):
        return int(match.group("release")) * 1000 + int(match.group("minor") or 0)
    return int(match.group("year")) * 1000 + int(match.group("minor") or 0)


def _inspect_host_executable(path: Path) -> HostIdentity:
    selected = _require_regular_file(path.expanduser(), stage="host", label="Cinema 4D executable")
    if selected.name.lower() not in {"c4dpy", "c4dpy.exe"}:
        raise LifecycleFailure("host", "The selected executable is not the canonical c4dpy binary.")
    before = _file_identity(selected)
    metadata = _platform_file_metadata(selected)
    product = str(metadata.get("product") or "").strip()
    company = str(metadata.get("company") or "").strip()
    normalized_product = re.sub(r"[^a-z0-9]+", " ", product.lower()).strip()
    normalized_company = re.sub(r"[^a-z0-9]+", " ", company.lower()).strip()
    if "maxon" not in normalized_company or "cinema 4d" not in normalized_product:
        raise LifecycleFailure(
            "host", "The executable does not expose Maxon Cinema 4D product identity."
        )
    product_version = str(metadata.get("product_version") or "").strip()
    file_version = str(metadata.get("file_version") or "").strip()
    if _host_build(product_version) < MINIMUM_HOST_BUILD or not file_version:
        raise LifecycleFailure(
            "host_version", "Cinema 4D R21 or newer with exact file version metadata is required."
        )
    size = selected.stat().st_size
    digest = _hash_file(selected)
    after = _file_identity(selected)
    if before != after or size != selected.stat().st_size:
        raise LifecycleFailure(
            "host_identity", "The Cinema 4D executable changed during inspection."
        )
    return HostIdentity(selected, product, product_version, file_version, digest, size, after)


def _module_file(name: str) -> Path:
    spec = importlib.util.find_spec(name)
    if spec is None or not spec.origin:
        raise LifecycleFailure("python", "The target interpreter distribution is incomplete.")
    return _require_regular_file(
        Path(spec.origin),
        stage="python",
        label="Imported %s module" % name,
    )


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _editable_root(metadata: Any) -> Optional[Path]:
    try:
        raw = metadata.read_text("direct_url.json")
        payload = json.loads(raw) if raw else None
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("dir_info"), dict):
        return None
    if payload["dir_info"].get("editable") is not True:
        return None
    parsed = urlsplit(str(payload.get("url") or ""))
    if (
        parsed.scheme != "file"
        or parsed.query
        or parsed.fragment
        or parsed.netloc not in {"", "localhost"}
    ):
        return None
    raw_path = unquote(parsed.path)
    if re.fullmatch(r"/[A-Za-z]:/.*", raw_path):
        raw_path = raw_path[1:]
    root = Path(raw_path).resolve()
    return root if root.is_dir() and not _is_link_or_reparse(root) else None


def _require_distribution_file(
    distribution_name: str,
    module_file: Path,
    *,
    allow_editable: bool,
) -> tuple[str, str]:
    try:
        metadata = distribution(distribution_name)
    except PackageNotFoundError as exc:
        raise LifecycleFailure(
            "python", "%s distribution is unavailable." % distribution_name
        ) from exc
    version = str(metadata.version)
    matches = [
        item
        for item in tuple(metadata.files or ())
        if Path(metadata.locate_file(item)).resolve() == module_file
    ]
    if len(matches) == 1:
        record = matches[0]
        digest = record.hash
        size = record.size
        if (
            digest is None
            or digest.mode != "sha256"
            or size is None
            or int(size) != module_file.stat().st_size
        ):
            raise LifecycleFailure(
                "python", "%s module RECORD integrity is invalid." % distribution_name
            )
        actual = (
            base64.urlsafe_b64encode(hashlib.sha256(module_file.read_bytes()).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        if actual != digest.value:
            raise LifecycleFailure(
                "python", "%s module RECORD integrity failed." % distribution_name
            )
    else:
        editable = _editable_root(metadata) if allow_editable else None
        if editable is None or not _within(module_file, editable):
            raise LifecycleFailure(
                "python", "%s module has no exact distribution ownership." % distribution_name
            )
    return version, _hash_file(module_file)


def _require_server_binary() -> tuple[Path, str]:
    try:
        metadata = distribution("dcc-mcp-server")
    except PackageNotFoundError as exc:
        raise LifecycleFailure("python", "The Core server distribution is unavailable.") from exc
    expected = "dcc-mcp-server.exe" if os.name == "nt" else "dcc-mcp-server"
    entries = [item for item in tuple(metadata.files or ()) if Path(str(item)).name == expected]
    if len(entries) != 1:
        raise LifecycleFailure("python", "The Core server binary ownership is ambiguous.")
    entry = entries[0]
    binary = _require_regular_file(
        Path(metadata.locate_file(entry)),
        stage="python",
        label="Core server binary",
    )
    if not _within(binary, Path(sys.prefix)):
        raise LifecycleFailure(
            "python", "The Core server binary is outside the selected interpreter."
        )
    digest = entry.hash
    size = entry.size
    if (
        digest is None
        or digest.mode != "sha256"
        or size is None
        or int(size) != binary.stat().st_size
    ):
        raise LifecycleFailure("python", "The Core server binary RECORD integrity is invalid.")
    actual = (
        base64.urlsafe_b64encode(hashlib.sha256(binary.read_bytes()).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    if actual != digest.value:
        raise LifecycleFailure("python", "The Core server binary RECORD integrity failed.")
    return binary, _hash_file(binary)


def _inspect_python(selected: Optional[Path]) -> PythonIdentity:
    current = _require_regular_file(
        Path(sys.executable), stage="python", label="Current Python interpreter"
    )
    if selected is not None:
        requested = _require_regular_file(
            selected.expanduser(), stage="python", label="Target Python interpreter"
        )
        try:
            same = os.path.samefile(requested, current)
        except OSError:
            same = False
        if not same:
            raise LifecycleFailure(
                "python",
                "Invoke the CLI with the exact Python interpreter that owns "
                "the adapter distribution.",
            )
    core = _core_version()
    parsed = _version_tuple(core)
    floor = _version_tuple(MINIMUM_CORE_VERSION)
    if parsed is None or floor is None or parsed < floor:
        raise LifecycleFailure("core_version", f"dcc-mcp-core>={MINIMUM_CORE_VERSION} is required.")
    adapter_module = _module_file("dcc_mcp_cinema4d")
    core_module = _module_file("dcc_mcp_core")
    server_module = _module_file("dcc_mcp_server")
    adapter, adapter_digest = _require_distribution_file(
        "dcc-mcp-cinema4d", adapter_module, allow_editable=True
    )
    if adapter != __version__:
        raise LifecycleFailure(
            "python", "The adapter distribution and imported package versions differ."
        )
    core_distribution_version, core_digest = _require_distribution_file(
        "dcc-mcp-core", core_module, allow_editable=False
    )
    reported_core = str(getattr(import_module("dcc_mcp_core"), "__version__", ""))
    if core_distribution_version != core or reported_core != core:
        raise LifecycleFailure("python", "Core package and distribution versions differ.")
    server_version, server_digest = _require_distribution_file(
        "dcc-mcp-server", server_module, allow_editable=False
    )
    reported_server = str(getattr(import_module("dcc_mcp_server"), "__version__", ""))
    if reported_server != server_version:
        raise LifecycleFailure("python", "Core server package and distribution versions differ.")
    if _version_tuple(server_version) is None or _version_tuple(server_version) < floor:
        raise LifecycleFailure(
            "core_version", f"dcc-mcp-server>={MINIMUM_CORE_VERSION} is required."
        )
    server_binary, server_binary_digest = _require_server_binary()
    return PythonIdentity(
        executable=current,
        version=".".join(str(item) for item in sys.version_info[:3]),
        prefix=Path(sys.prefix).resolve(),
        adapter_version=adapter,
        core_version=core,
        adapter_origin=adapter_module,
        core_origin=core_module,
        adapter_sha256=adapter_digest,
        core_sha256=core_digest,
        server_version=server_version,
        server_origin=server_module,
        server_sha256=server_digest,
        server_binary=server_binary,
        server_binary_sha256=server_binary_digest,
    )


def _default_state_root(environ: Mapping[str, str]) -> Path:
    configured = environ.get(_STATE_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().absolute()
    if os.name == "nt" and environ.get("LOCALAPPDATA"):
        return Path(environ["LOCALAPPDATA"]) / "dcc-mcp" / "cinema4d"
    return Path.home() / ".dcc-mcp" / "cinema4d"


def _load_receipt(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    _require_regular_file(path, stage="receipt", label="Install receipt")
    before = _file_identity(path)
    size = path.stat().st_size
    if size > 256 * 1024:
        raise LifecycleFailure(
            "receipt", "The install receipt exceeds the size limit.", INSTALL_EXIT_INSTALL
        )
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as exc:
        raise LifecycleFailure(
            "receipt", "The install receipt is unreadable.", INSTALL_EXIT_INSTALL
        ) from exc
    if not isinstance(payload, dict):
        raise LifecycleFailure(
            "receipt", "The install receipt root must be an object.", INSTALL_EXIT_INSTALL
        )
    if _file_identity(path) != before or path.stat().st_size != size or len(raw) != size:
        raise LifecycleFailure(
            "receipt", "The install receipt changed during inspection.", INSTALL_EXIT_INSTALL
        )
    return payload


def _receipt_identity(path: Path) -> str:
    before = _file_identity(path)
    size = path.stat().st_size
    digest = _hash_file(path)
    after = _file_identity(path)
    if before != after or size != path.stat().st_size:
        raise LifecycleFailure(
            "receipt", "The install receipt changed during inspection.", INSTALL_EXIT_INSTALL
        )
    return "%s:size:%d:sha256:%s" % (after, size, digest)


def _recapture_receipt(context: InstallContext) -> Optional[str]:
    if context.receipt is None:
        if context.receipt_path.exists():
            raise LifecycleFailure(
                "receipt", "An unexpected install receipt appeared.", INSTALL_EXIT_INSTALL
            )
        return None
    current_payload = _load_receipt(context.receipt_path)
    current_identity = _receipt_identity(context.receipt_path)
    if current_payload != context.receipt or (
        context.receipt_identity is not None and current_identity != context.receipt_identity
    ):
        raise LifecycleFailure(
            "receipt", "The install receipt changed during the operation.", INSTALL_EXIT_INSTALL
        )
    return current_identity


def _receipt_matches(context: InstallContext, receipt: Mapping[str, Any]) -> bool:
    host = receipt.get("host") if isinstance(receipt.get("host"), dict) else {}
    python = receipt.get("python") if isinstance(receipt.get("python"), dict) else {}
    expected = {
        "schema_version": 1,
        "dcc_type": DCC_TYPE,
        "adapter_version": __version__,
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        return False
    return (
        host.get("path") == str(context.host.path)
        and host.get("sha256") == context.host.sha256
        and host.get("file_identity") == context.host.file_identity
        and host.get("product_version") == context.host.product_version
        and python.get("executable") == str(context.python.executable)
        and python.get("adapter_version") == context.python.adapter_version
        and python.get("core_version") == context.python.core_version
        and python.get("adapter_origin") == str(context.python.adapter_origin)
        and python.get("core_origin") == str(context.python.core_origin)
        and python.get("adapter_sha256") == context.python.adapter_sha256
        and python.get("core_sha256") == context.python.core_sha256
        and python.get("server_version") == context.python.server_version
        and python.get("server_origin")
        == (None if context.python.server_origin is None else str(context.python.server_origin))
        and python.get("server_sha256") == context.python.server_sha256
        and python.get("server_binary")
        == (None if context.python.server_binary is None else str(context.python.server_binary))
        and python.get("server_binary_sha256") == context.python.server_binary_sha256
    )


def _resolve_context(request: LifecycleRequest, environ: Mapping[str, str]) -> InstallContext:
    _require_official_core_contract()
    if request.executable is None:
        configured = environ.get("DCC_MCP_CINEMA4D_C4DPY", "").strip()
        if not configured:
            raise LifecycleFailure("host", "Select the exact Maxon c4dpy executable.")
        executable = Path(configured)
    else:
        executable = request.executable
    host = _inspect_host_executable(executable)
    python = _inspect_python(request.python_executable)
    state_root = (request.state_root or _default_state_root(environ)).expanduser().absolute()
    if state_root.exists() and _is_link_or_reparse(state_root):
        raise LifecycleFailure(
            "state", "The lifecycle state root cannot be a link or reparse point."
        )
    receipt_path = state_root / "receipt.json"
    receipt = _load_receipt(receipt_path)
    if receipt is None:
        state = "fresh"
    elif _receipt_matches(
        InstallContext(host, python, state_root, receipt_path, "current", receipt), receipt
    ):
        state = "current"
    elif receipt.get("dcc_type") == DCC_TYPE and receipt.get("schema_version") == 1:
        state = "upgrade" if receipt.get("adapter_version") != __version__ else "repair"
    else:
        state = "partial"
    receipt_identity = _receipt_identity(receipt_path) if receipt is not None else None
    return InstallContext(host, python, state_root, receipt_path, state, receipt, receipt_identity)


@contextmanager
def _mutation_lock(state_root: Path) -> Iterator[None]:
    key = os.path.normcase(str(state_root.absolute()))
    lock_path = state_root / ".locks" / "lifecycle.lock"
    with _LOCK_GUARD:
        if key in _LOCKED_ROOTS:
            raise LifecycleFailure(
                "busy",
                "A Cinema 4D lifecycle mutation is already in progress.",
                INSTALL_EXIT_INSTALL,
            )
        _LOCKED_ROOTS.add(key)
    descriptor = None
    identity = None
    cleanup_failure = None
    primary = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if _is_link_or_reparse(lock_path.parent):
            raise LifecycleFailure(
                "state", "The lifecycle lock directory cannot be a link or reparse point."
            )
        try:
            descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise LifecycleFailure(
                "busy",
                "A Cinema 4D lifecycle mutation is already in progress.",
                INSTALL_EXIT_INSTALL,
            ) from exc
        info = os.fstat(descriptor)
        identity = (int(info.st_dev), int(info.st_ino))
        os.write(descriptor, ("pid=%d\n" % os.getpid()).encode("ascii"))
        os.fsync(descriptor)
        yield
    except BaseException as exc:
        primary = exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as exc:
                cleanup_failure = LifecycleFailure(
                    "cleanup",
                    "The lifecycle lock handle could not be closed.",
                    INSTALL_EXIT_INSTALL,
                )
                cleanup_failure.__cause__ = exc
        if identity is not None:
            try:
                current = os.stat(lock_path, follow_symlinks=False)
                if (int(current.st_dev), int(current.st_ino)) == identity:
                    lock_path.unlink()
                else:
                    cleanup_failure = LifecycleFailure(
                        "cleanup", "The lifecycle lock identity changed.", INSTALL_EXIT_INSTALL
                    )
            except FileNotFoundError:
                cleanup_failure = LifecycleFailure(
                    "cleanup", "The lifecycle lock disappeared unexpectedly.", INSTALL_EXIT_INSTALL
                )
            except OSError as exc:
                cleanup_failure = LifecycleFailure(
                    "cleanup", "The lifecycle lock could not be removed.", INSTALL_EXIT_INSTALL
                )
                cleanup_failure.__cause__ = exc
        with _LOCK_GUARD:
            _LOCKED_ROOTS.discard(key)
    if cleanup_failure is not None:
        if primary is not None:
            raise cleanup_failure from primary
        raise cleanup_failure
    if primary is not None:
        raise primary


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if _is_link_or_reparse(path.parent):
        raise LifecycleFailure(
            "receipt",
            "The receipt directory cannot be a link or reparse point.",
            INSTALL_EXIT_INSTALL,
        )
    temporary = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    except BaseException as primary:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError as cleanup:
                raise LifecycleFailure(
                    "receipt_cleanup", "Receipt staging cleanup failed.", INSTALL_EXIT_INSTALL
                ) from cleanup
        raise primary


def _build_receipt(context: InstallContext) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "dcc_type": DCC_TYPE,
        "adapter_version": __version__,
        "core_version": context.python.core_version,
        "host": {
            "path": str(context.host.path),
            "product": context.host.product,
            "product_version": context.host.product_version,
            "file_version": context.host.file_version,
            "sha256": context.host.sha256,
            "size": context.host.size,
            "file_identity": context.host.file_identity,
        },
        "python": {
            "executable": str(context.python.executable),
            "version": context.python.version,
            "prefix": str(context.python.prefix),
            "adapter_version": context.python.adapter_version,
            "core_version": context.python.core_version,
            "adapter_origin": str(context.python.adapter_origin),
            "core_origin": str(context.python.core_origin),
            "adapter_sha256": context.python.adapter_sha256,
            "core_sha256": context.python.core_sha256,
            "server_version": context.python.server_version,
            "server_origin": (
                None if context.python.server_origin is None else str(context.python.server_origin)
            ),
            "server_sha256": context.python.server_sha256,
            "server_binary": (
                None if context.python.server_binary is None else str(context.python.server_binary)
            ),
            "server_binary_sha256": context.python.server_binary_sha256,
        },
    }


def _recapture_host(context: InstallContext) -> HostIdentity:
    current = _inspect_host_executable(context.host.path)
    if current != context.host:
        raise LifecycleFailure(
            "host_identity",
            "The Cinema 4D executable changed during the operation.",
            INSTALL_EXIT_VERIFY,
        )
    return current


def _observe_process_identity(pid: int) -> dict[str, Any]:
    if pid <= 0:
        raise LifecycleFailure(
            "runtime_identity", "The runtime PID is invalid.", INSTALL_EXIT_VERIFY
        )
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            wintypes.PDWORD,
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            wintypes.LPFILETIME,
            wintypes.LPFILETIME,
            wintypes.LPFILETIME,
            wintypes.LPFILETIME,
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            raise LifecycleFailure(
                "runtime_identity",
                "The runtime process could not be observed.",
                INSTALL_EXIT_VERIFY,
            )
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(buffer))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                raise LifecycleFailure(
                    "runtime_identity",
                    "The runtime executable could not be observed.",
                    INSTALL_EXIT_VERIFY,
                )
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel_time = wintypes.FILETIME()
            user_time = wintypes.FILETIME()
            if not kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                raise LifecycleFailure(
                    "runtime_identity",
                    "The runtime start identity could not be observed.",
                    INSTALL_EXIT_VERIFY,
                )
            start = (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)
            executable = Path(buffer.value).resolve()
        finally:
            kernel32.CloseHandle(handle)
    elif sys.platform == "darwin":
        import ctypes

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
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
            libproc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            libproc.proc_pidpath.restype = ctypes.c_int
            libproc.proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            libproc.proc_pidinfo.restype = ctypes.c_int
            path_buffer = ctypes.create_string_buffer(4096)
            if libproc.proc_pidpath(pid, path_buffer, len(path_buffer)) <= 0:
                raise OSError("proc_pidpath failed")
            info = ProcBsdInfo()
            if libproc.proc_pidinfo(
                pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)
            ) != ctypes.sizeof(info):
                raise OSError("proc_pidinfo failed")
            if int(info.pbi_pid) != pid or int(info.pbi_start_tvsec) <= 0:
                raise OSError("proc_pidinfo identity mismatch")
            executable = Path(path_buffer.value.decode("utf-8", errors="strict")).resolve()
            start = "darwin:%d:%d" % (
                int(info.pbi_start_tvsec),
                int(info.pbi_start_tvusec),
            )
        except (AttributeError, OSError, UnicodeError) as exc:
            raise LifecycleFailure(
                "runtime_identity",
                "The runtime process identity could not be observed.",
                INSTALL_EXIT_VERIFY,
            ) from exc
    elif Path("/proc").is_dir():
        try:
            executable = Path(os.readlink("/proc/%d/exe" % pid)).resolve()
            process_stat = Path("/proc/%d/stat" % pid).read_text(encoding="ascii")
            closing = process_stat.rfind(") ")
            fields = process_stat[closing + 2 :].split()
            start = fields[19]
        except (OSError, IndexError) as exc:
            raise LifecycleFailure(
                "runtime_identity",
                "The runtime process identity could not be observed.",
                INSTALL_EXIT_VERIFY,
            ) from exc
    else:
        raise LifecycleFailure(
            "runtime_identity",
            "This platform cannot safely bind the runtime executable.",
            INSTALL_EXIT_VERIFY,
        )
    _require_regular_file(executable, stage="runtime_identity", label="Runtime executable")
    return {
        "pid": pid,
        "executable_sha256": _hash_file(executable),
        "executable_file_identity": _file_identity(executable),
        "start_identity": str(start),
    }


def _observe_bound_runtime(pid: int, host: HostIdentity) -> dict[str, Any]:
    identity = _observe_process_identity(pid)
    if (
        identity.get("executable_sha256") != host.sha256
        or identity.get("executable_file_identity") != host.file_identity
    ):
        raise LifecycleFailure(
            "runtime_identity",
            "The runtime executable does not match the selected Cinema 4D binary.",
            INSTALL_EXIT_VERIFY,
        )
    return identity


def _recapture_bound_runtime(previous: Mapping[str, Any], host: HostIdentity) -> dict[str, Any]:
    current = _observe_bound_runtime(int(previous.get("pid", 0)), host)
    if current != dict(previous):
        raise LifecycleFailure(
            "runtime_identity",
            "The runtime PID identity changed during verification.",
            INSTALL_EXIT_VERIFY,
        )
    return current


def _verify_runtime(context: InstallContext, deadline: float) -> dict[str, Any]:
    _recapture_host(context)
    _recapture_receipt(context)
    receipt = _load_receipt(context.receipt_path)
    if receipt is None or not _receipt_matches(context, receipt):
        return {
            "directly_usable": False,
            "failure_stage": "receipt",
            "failure_reason": "The exact Cinema 4D install receipt is missing or stale.",
        }
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return {
            "directly_usable": False,
            "failure_stage": "runtime_timeout",
            "failure_reason": "Cinema 4D verification timed out.",
        }
    try:
        bridge = Cinema4dBridge(
            str(context.host.path),
            allowed_roots=[Path.cwd()],
            max_timeout_secs=MAX_TIMEOUT_SECS,
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BridgeTimeoutError("Cinema 4D verification timed out")
        runtime = bridge.status(timeout_secs=remaining)
    except BridgeTimeoutError:
        return {
            "directly_usable": False,
            "failure_stage": "runtime_timeout",
            "failure_reason": "Cinema 4D verification timed out.",
        }
    except BridgeError:
        return {
            "directly_usable": False,
            "failure_stage": "runtime",
            "failure_reason": "Cinema 4D runtime verification failed.",
        }
    if runtime.get("ready") is not True:
        return {
            "directly_usable": False,
            "failure_stage": "runtime",
            "failure_reason": "Cinema 4D runtime is not ready.",
        }
    if time.monotonic() >= deadline:
        return {
            "directly_usable": False,
            "failure_stage": "runtime_timeout",
            "failure_reason": "Cinema 4D verification timed out.",
        }
    identity = runtime.get("runtime_identity")
    if not isinstance(identity, dict) or identity.get("executable_sha256") != context.host.sha256:
        return {
            "directly_usable": False,
            "failure_stage": "runtime_identity",
            "failure_reason": "Cinema 4D runtime identity was not verified.",
        }
    try:
        host_build = int(runtime.get("cinema4d_version", 0))
    except (TypeError, ValueError):
        host_build = 0
    if host_build < MINIMUM_HOST_BUILD:
        return {
            "directly_usable": False,
            "failure_stage": "host_version",
            "failure_reason": "Cinema 4D R21 or newer is required.",
        }
    _recapture_host(context)
    receipt_identity = _recapture_receipt(context)
    if time.monotonic() >= deadline:
        return {
            "directly_usable": False,
            "failure_stage": "runtime_timeout",
            "failure_reason": "Cinema 4D verification timed out.",
        }
    return {
        "directly_usable": True,
        "failure_stage": None,
        "failure_reason": None,
        "runtime_identity": identity,
        "listener_identity": "not_applicable_standalone",
        "project_identity": runtime.get("project_identity", "no_project_for_status_probe"),
        "receipt_identity": (
            None if receipt_identity is None else receipt_identity.rsplit("sha256:", 1)[-1]
        ),
    }


def _public_result(
    operation: str,
    context: Optional[InstallContext],
    *,
    status: str,
    verify: Mapping[str, Any],
    steps: Optional[list[Mapping[str, Any]]] = None,
    next_steps: Optional[list[Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    core = context.python.core_version if context else _core_version()
    result = {
        "schema_version": INSTALL_SOP_SCHEMA_VERSION,
        "status": status,
        "dcc_type": DCC_TYPE,
        "adapter_version": __version__,
        "core_version": core if core else "unavailable",
        "steps": list(steps or []),
        "next_steps": list(next_steps or []),
        "receipt_path": "<state>/receipt.json" if context else None,
        "verify": {
            "directly_usable": bool(verify.get("directly_usable")),
            "failure_stage": verify.get("failure_stage"),
            "failure_reason": verify.get("failure_reason"),
        },
        "operation": operation,
    }
    if context:
        result.update(
            {
                "install_state": context.state,
                "plan": {
                    "host": {
                        "product": context.host.product,
                        "product_version": context.host.product_version,
                        "file_version": context.host.file_version,
                        "sha256": context.host.sha256,
                        "size": context.host.size,
                        "path": "<cinema4d>/c4dpy" + (".exe" if os.name == "nt" else ""),
                    },
                    "python": {
                        "version": context.python.version,
                        "adapter_version": context.python.adapter_version,
                        "core_version": context.python.core_version,
                        "executable": "<python>/python" + (".exe" if os.name == "nt" else ""),
                    },
                    "mutates_host": False,
                    "managed_artifacts": ["<state>/receipt.json"],
                },
            }
        )
        for key in (
            "runtime_identity",
            "listener_identity",
            "project_identity",
            "receipt_identity",
        ):
            if key in verify:
                result[key] = verify[key]
    return result


def _command(context: InstallContext, operation: str, *, execute: bool) -> list[str]:
    command = [COMMAND, operation, "--json", "--c4dpy", "<cinema4d>/c4dpy"]
    if execute:
        command.append("--yes")
    return command


def _planned(context: InstallContext, operation: str) -> LifecycleOutcome:
    target = operation
    steps = [
        {"id": "preflight", "status": "ok"},
        {"id": "identity", "status": "ok"},
        {"id": target, "status": "planned"},
        {"id": "verify", "status": "planned" if target != "uninstall" else "not_applicable"},
    ]
    next_steps = [
        {
            "id": "execute",
            "description": "Execute the validated Cinema 4D lifecycle plan.",
            "command": _command(context, target, execute=True),
            "why": "Planning never changes the receipt or launches Cinema 4D.",
        }
    ]
    result = _public_result(
        operation,
        context,
        status="planned",
        verify={"directly_usable": False, "failure_stage": None, "failure_reason": None},
        steps=steps,
        next_steps=next_steps,
    )
    return LifecycleOutcome(result, INSTALL_EXIT_OK)


def _restore_receipt(path: Path, previous: Optional[bytes]) -> None:
    if previous is None:
        if path.exists():
            path.unlink()
        return
    temporary = path.with_name(".%s.restore.%s.tmp" % (path.name, uuid.uuid4().hex))
    try:
        with temporary.open("xb") as stream:
            stream.write(previous)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError as exc:
            raise LifecycleFailure(
                "receipt_cleanup", "Receipt rollback staging cleanup failed.", INSTALL_EXIT_INSTALL
            ) from exc
        raise


def _mutate(
    context: InstallContext,
    request: LifecycleRequest,
    environ: Mapping[str, str],
    deadline: float,
) -> LifecycleOutcome:
    with _mutation_lock(context.state_root):
        current = _resolve_context(request, environ)
        if os.path.normcase(str(current.state_root.absolute())) != os.path.normcase(
            str(context.state_root.absolute())
        ):
            raise LifecycleFailure(
                "state",
                "The lifecycle state root changed before mutation.",
                INSTALL_EXIT_INSTALL,
            )
        context = current
        _recapture_receipt(context)
        previous = context.receipt_path.read_bytes() if context.receipt_path.is_file() else None
        _recapture_host(context)
        if request.operation == "uninstall":
            if context.receipt is None:
                verify = {"directly_usable": False, "failure_stage": None, "failure_reason": None}
                result = _public_result(
                    request.operation,
                    context,
                    status="ok",
                    verify=verify,
                    steps=[{"id": "uninstall", "status": "already_absent"}],
                )
                result["install_state"] = "fresh"
                return LifecycleOutcome(result, INSTALL_EXIT_OK)
            if not _receipt_matches(context, context.receipt):
                raise LifecycleFailure(
                    "receipt",
                    "The receipt does not own the selected Cinema 4D binding.",
                    INSTALL_EXIT_INSTALL,
                )
            _recapture_receipt(context)
            try:
                context.receipt_path.unlink()
            except OSError as exc:
                raise LifecycleFailure(
                    "uninstall",
                    "The owned install receipt could not be removed.",
                    INSTALL_EXIT_INSTALL,
                ) from exc
            verify = {"directly_usable": False, "failure_stage": None, "failure_reason": None}
            result = _public_result(
                request.operation,
                context,
                status="ok",
                verify=verify,
                steps=[{"id": "uninstall", "status": "removed"}],
            )
            result["install_state"] = "fresh"
            return LifecycleOutcome(result, INSTALL_EXIT_OK)
        if context.state == "partial":
            raise LifecycleFailure(
                "receipt",
                "A foreign or malformed receipt must be reviewed before mutation.",
                INSTALL_EXIT_INSTALL,
            )
        if request.operation == "install" and context.state == "current":
            verify = _verify_runtime(context, deadline)
            status = "ok" if verify["directly_usable"] else "failed"
            result = _public_result(
                request.operation,
                context,
                status=status,
                verify=verify,
                steps=[
                    {"id": "install", "status": "already_current"},
                    {"id": "verify", "status": "ok" if status == "ok" else "failed"},
                ],
            )
            return LifecycleOutcome(
                result, INSTALL_EXIT_OK if status == "ok" else INSTALL_EXIT_VERIFY
            )
        _write_json_atomic(context.receipt_path, _build_receipt(context))
        current = replace(context, state="current", receipt=_load_receipt(context.receipt_path))
        verify = _verify_runtime(current, deadline)
        if not verify["directly_usable"]:
            try:
                _restore_receipt(context.receipt_path, previous)
            except OSError as exc:
                raise LifecycleFailure(
                    "rollback",
                    "The previous install receipt could not be restored.",
                    INSTALL_EXIT_INSTALL,
                ) from exc
            result = _public_result(
                request.operation,
                context,
                status="failed",
                verify=verify,
                steps=[
                    {"id": request.operation, "status": "rolled_back"},
                    {"id": "verify", "status": "failed"},
                ],
            )
            result["previous_restored"] = True
            return LifecycleOutcome(result, INSTALL_EXIT_VERIFY)
        _recapture_host(current)
        result = _public_result(
            request.operation,
            current,
            status="ok",
            verify=verify,
            steps=[
                {"id": request.operation, "status": "ok"},
                {"id": "receipt", "status": "ok"},
                {"id": "verify", "status": "ok"},
            ],
        )
        return LifecycleOutcome(result, INSTALL_EXIT_OK)


_PUBLIC_FAILURES = {
    "arguments": "Lifecycle arguments are invalid.",
    "busy": "Another lifecycle mutation is already in progress.",
    "cleanup": "Lifecycle cleanup could not be verified.",
    "core_contract": "The official Core Install SOP contract is unavailable.",
    "core_version": f"dcc-mcp-core>={MINIMUM_CORE_VERSION} is required.",
    "host": "The exact Maxon Cinema 4D executable was not verified.",
    "host_identity": "Cinema 4D executable identity was not stable.",
    "host_version": "Cinema 4D R21 or newer is required.",
    "install": "Cinema 4D installation failed.",
    "python": "The target Python distribution was not verified.",
    "receipt": "The Cinema 4D install receipt is invalid.",
    "receipt_cleanup": "Receipt staging cleanup failed.",
    "rollback": "The previous Cinema 4D receipt could not be restored.",
    "runtime": "Cinema 4D verification failed.",
    "runtime_identity": "Cinema 4D runtime identity was not verified.",
    "runtime_timeout": "Cinema 4D verification timed out.",
    "state": "The lifecycle state root is unsafe.",
    "uninstall": "Cinema 4D uninstall failed.",
}


def failure_outcome(operation: str, stage: str, exit_code: int) -> LifecycleOutcome:
    reason = _PUBLIC_FAILURES.get(stage, "Cinema 4D verification failed.")
    result = _public_result(
        operation,
        None,
        status="failed",
        verify={"directly_usable": False, "failure_stage": stage, "failure_reason": reason},
        steps=[{"id": stage, "status": "failed"}],
    )
    return LifecycleOutcome(result, exit_code)


def run_lifecycle(
    request: LifecycleRequest, *, environ: Optional[Mapping[str, str]] = None
) -> LifecycleOutcome:
    operation = request.target if request.operation == "plan" else request.operation
    if operation not in {"install", "status", "verify", "uninstall", "upgrade"}:
        return failure_outcome(request.operation, "arguments", INSTALL_EXIT_PREFLIGHT)
    try:
        started = time.monotonic()
        timeout = validate_timeout(request.timeout_secs, maximum=MAX_TIMEOUT_SECS)
        deadline = started + timeout
        request = replace(request, operation=operation, timeout_secs=timeout)
        context = _resolve_context(request, dict(os.environ if environ is None else environ))
        if request.operation in {"install", "uninstall", "upgrade"} and not request.execute:
            return _planned(context, request.operation)
        if request.operation == "status":
            verify = {"directly_usable": False, "failure_stage": None, "failure_reason": None}
            result = _public_result(
                "status",
                context,
                status="ok",
                verify=verify,
                steps=[{"id": "status", "status": context.state}],
            )
            return LifecycleOutcome(result, INSTALL_EXIT_OK)
        if request.operation == "verify":
            verify = _verify_runtime(context, deadline)
            status = "ok" if verify["directly_usable"] else "failed"
            result = _public_result(
                "verify",
                context,
                status=status,
                verify=verify,
                steps=[{"id": "verify", "status": status}],
            )
            return LifecycleOutcome(
                result, INSTALL_EXIT_OK if status == "ok" else INSTALL_EXIT_VERIFY
            )
        return _mutate(
            context,
            request,
            dict(os.environ if environ is None else environ),
            deadline,
        )
    except LifecycleFailure as exc:
        return failure_outcome(operation, exc.stage, exc.exit_code)
    except ValueError:
        return failure_outcome(operation, "arguments", INSTALL_EXIT_PREFLIGHT)
    except Exception:
        exit_code = INSTALL_EXIT_VERIFY if operation == "verify" else INSTALL_EXIT_INSTALL
        stage = "runtime" if operation == "verify" else "install"
        return failure_outcome(operation, stage, exit_code)


def _isolated_environment(base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    return isolated_environment(base)


def _run_owned_command(command: list[str], timeout_secs: float) -> dict[str, Any]:
    return run_owned_command(command, timeout_secs=timeout_secs)
