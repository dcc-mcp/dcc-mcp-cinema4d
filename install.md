# Cinema 4D Standalone Install SOP v1

This adapter runs typed operations through Maxon's licensed headless `c4dpy`.
It never modifies the Cinema 4D application. The canonical raw guide is
<https://raw.githubusercontent.com/dcc-mcp/dcc-mcp-cinema4d/main/install.md>.

## Requirements

- Cinema 4D R21 or newer, installed and licensed through Maxon's supported process.
- The matching canonical `c4dpy` executable from that exact Cinema 4D installation.
- External Python 3.9 or newer with `dcc-mcp-core>=0.20.36,<1.0.0` and this adapter installed in the same interpreter.
- Read/write access to every path listed in `DCC_MCP_CINEMA4D_ALLOWED_ROOTS`.

The adapter does not download, scrape, update, or execute a remote Cinema 4D
payload. There is no adapter-managed binary cache. Maxon remains the owner of
the host installation and license workflow.

## Supported versions

| Platform | Canonical executable example | Identity source |
| --- | --- | --- |
| Windows | `C:\Program Files\Maxon Cinema 4D 2026\c4dpy.exe` | Maxon product/file version resource, file identity, size, and SHA-256 |
| macOS | `/Applications/Maxon Cinema 4D 2026/c4dpy.app/Contents/MacOS/c4dpy` | application bundle metadata, file identity, size, and SHA-256 |
| Linux | `/opt/maxon/Cinema 4D 2026/c4dpy` | canonical install-root version, file identity, size, and SHA-256 |

The host floor is Cinema 4D R21. The service floor is Python 3.9 and Core
0.20.36. A selected executable, Python distribution, receipt, or live process
whose identity changes during an operation is rejected.

### Two interpreters

This adapter spans two Python runtimes, and keeping them apart explains most
install problems:

| Side | Interpreter | Floor | What runs there |
| --- | --- | --- | --- |
| Service side | the Python you installed the adapter into | 3.9 | the MCP server, `install`/`doctor`/`verify`, path and timeout policy, the bridge |
| Host side | the Python **inside `c4dpy`** | 3 | the packaged driver, which imports `c4d` and touches the scene |

The service-side floor is enforced by `requires-python`. The host-side floor is
**not** something Maxon's release installer negotiates with you: it is whatever
interpreter that Cinema 4D build ships. Cinema 4D releases whose `c4dpy` is
Python 2.7 (the R21 and R22 lines) cannot run the packaged driver at all,
because the driver uses `os.replace` and text-mode `open(encoding=...)`, neither
of which exists on Python 2.7. In practice that makes R23 the effective floor
even though the build floor is R21.

Both sides are reported together, so you can see the pairing without guessing:

```shell
dcc-mcp-cinema4d doctor --c4dpy <absolute-c4dpy-path> --json
```

look for `checks.runtime.python_version` (the interpreter inside `c4dpy`) next
to `core_version` and `plan.python.version` (the service side).

### Version matrix

Supported releases are declared in machine-readable form in
`src/dcc_mcp_cinema4d/compat_matrix.json`, which ships inside the wheel. The
build number it keys on is the integer from `c4d.GetC4DVersion()` (for example
`26000` for Cinema 4D 2023) — **not** the product version string from the
executable's file metadata, which is formatted differently.

`doctor` and `verify` embed the verdict as `host_matrix`:

```json
{
  "build": 26000,
  "status": "supported",
  "range": { "id": "2023", "c4dpy_python": "3.9", "confidence": "declared_not_executed" },
  "host_python_version": "3.9.7",
  "interpreter_drift": null
}
```

A build outside the matrix fails the run with an explicit `failure_stage` of
`host_version` and a reason naming the declared ranges. It is never silently
downgraded to "probably fine".

**The `c4dpy_python` mapping is declared, not executed.** This adapter has no
real-host CI — a licensed `c4dpy` cannot be provisioned on GitHub-hosted runners
— so no release-to-interpreter mapping here has been observed on a licensed
runtime. Two consequences follow, and both are deliberate:

1. The support decision uses the interpreter version **observed** from the
   runtime, not the declared mapping. A host that reports a usable interpreter
   is accepted even where the matrix guessed wrong.
2. When observed and declared disagree, the verdict sets `interpreter_drift`
   with both values and the warning travels in the report. That is the signal to
   correct the matrix — it is reported, never swallowed.

## Agent quick path

Install the released wheel, then request a non-mutating plan:

```shell
python -m pip install dcc-mcp-cinema4d
dcc-mcp-cinema4d plan --target install --c4dpy <absolute-c4dpy-path> --json
```

Execute only the exact reviewed plan:

```shell
dcc-mcp-cinema4d install --c4dpy <absolute-c4dpy-path> --json --yes
dcc-mcp-cinema4d status --c4dpy <absolute-c4dpy-path> --json
dcc-mcp-cinema4d doctor --json --c4dpy <absolute-c4dpy-path>
```

`doctor` is the compatibility alias for `verify`. Do not call document tools
unless the result exits `0` with `verify.directly_usable=true`.

The execute step writes one atomic receipt below the adapter state root. A
cross-process mutation lock serializes install, upgrade, and uninstall. Receipt
failure restores the exact previous bytes; repeated install is idempotent.

Set the allowed workspace before starting the long-running adapter service:

```shell
# Windows PowerShell example
$env:DCC_MCP_CINEMA4D_ALLOWED_ROOTS = "D:\projects;D:\exports"
dcc-mcp-cinema4d
```

With no verb, `dcc-mcp-cinema4d` retains the existing service-start behavior.
There is no adapter daemon created by the lifecycle command.

## Manual path

Use these settings when discovery is ambiguous or a separate state root is required:

```text
DCC_MCP_CINEMA4D_C4DPY=<absolute-c4dpy-path>
DCC_MCP_CINEMA4D_STATE_DIR=<adapter-owned-state-root>
DCC_MCP_CINEMA4D_ALLOWED_ROOTS=<path-list-using-the-platform-separator>
DCC_MCP_CINEMA4D_MAX_INPUT_BYTES=2147483648
DCC_MCP_CINEMA4D_MAX_TIMEOUT_SECS=1800
```

On Windows, path lists use `;`; on macOS and Linux, they use `:`. The lifecycle
refuses symlink, junction, reparse, foreign, swapped, or malformed host/state
artifacts. It never copies a plugin or Python package into the Maxon installation.

## Verify

Run the bounded licensed status probe explicitly:

```shell
dcc-mcp-cinema4d verify --c4dpy <absolute-c4dpy-path> --timeout-secs 60 --json
```

The result binds the selected host digest and file identity to the live host
PID/start token, project receipt, and standalone listener state. `c4dpy` runs
under an adapter-owned POSIX session or Windows Job, with an absolute deadline,
bounded output, isolated environment, and full-tree cleanup.

Stable exits are:

| Code | Meaning |
| --- | --- |
| `0` | Plan/status succeeded, or the exact licensed runtime is directly usable |
| `10` | Arguments, Core contract, Python distribution, host, or identity preflight failed |
| `40` | Licensed runtime verification failed or exceeded its deadline |

CI validates the schema and safety paths without pretending to hold a Maxon
license. Real-host acceptance still requires a disposable licensed `c4dpy` run:

```shell
C4D_TEST_EXECUTABLE=<absolute-c4dpy-path> python -m pytest -m cinema4d
```

That opt-in test is the only host-level evidence this adapter produces. CI runs
`pytest -m "not cinema4d"` and is therefore contract-level only.

### What "verified" means here

Every mutating tool reads its change back before reporting success: document
mutations are compared against the document reopened from disk, exports are
checked against their format signature, and renders are checked against the
requested pixel dimensions. A mismatch raises an error naming the tool, the
check, the expected and actual values, and the Cinema 4D build.

Two levels of evidence are in play and they are not interchangeable:

- **Contract level** (what CI proves): the adapter performs the read-back and
  compares correctly. Proven against a model of the Cinema 4D side.
- **Host level** (what a licensed run proves): Cinema 4D actually persisted the
  change on that build.

A green CI run is the first, not the second.

## Upgrade

Plan and execute a receipted adapter upgrade after upgrading the wheel:

```shell
python -m pip install --upgrade dcc-mcp-cinema4d
dcc-mcp-cinema4d plan --target upgrade --c4dpy <absolute-c4dpy-path> --json
dcc-mcp-cinema4d upgrade --c4dpy <absolute-c4dpy-path> --json --yes
```

Failed verification rolls back the exact previous receipt. Cinema 4D and
`c4dpy` upgrades remain Maxon-managed; the adapter never selects a mutable
"latest" host or caches one.

## Uninstall

Plan and remove only the receipt owned by this exact host/Python binding:

```shell
dcc-mcp-cinema4d plan --target uninstall --c4dpy <absolute-c4dpy-path> --json
dcc-mcp-cinema4d uninstall --c4dpy <absolute-c4dpy-path> --json --yes
python -m pip uninstall dcc-mcp-cinema4d
```

There is no adapter daemon, application plugin, or Maxon binary cache to
remove. Uninstall refuses a foreign/mismatched receipt and never deletes the
Maxon installation, license data, operator projects, or unrelated state files.

## Troubleshooting

- `host` or `host_identity`, exit `10`: select the exact non-linked Maxon `c4dpy` binary and retry the generated command.
- `python` or `core_version`, exit `10`: invoke the CLI through the exact Python distribution that owns adapter and Core 0.20.36 or newer.
- `host_version`, exit `40`: the Cinema 4D build is outside the declared matrix, or its `c4dpy` interpreter cannot run the packaged driver. Check `host_matrix` in the JSON report for the declared ranges, the observed interpreter, and any `interpreter_drift`.
- `receipt`, exit `30`: preserve and review the foreign or malformed receipt; do not delete it blindly.
- `busy`, exit `30`: another mutation owns the state root; wait for it to finish.
- `runtime_timeout`, exit `40`: confirm licensing, then retry with a finite bounded timeout.
- `runtime_identity`, exit `40`: the live PID, start token, executable, or digest did not match the selected host.
- A healthy service is not proof of a usable host. Require exit `0` and `verify.directly_usable=true` before document or render tools.

A read-back mismatch is not a bug in the read-back: it means the tool reported
success and the scene disagreed. The error names the Cinema 4D build because
that is usually the first thing worth checking against the matrix.
