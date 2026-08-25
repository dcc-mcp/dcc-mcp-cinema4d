# Cinema 4D Standalone Install SOP v1

This adapter runs typed operations through Maxon's licensed headless `c4dpy`.
It never modifies the Cinema 4D application. The canonical raw guide is
<https://raw.githubusercontent.com/dcc-mcp/dcc-mcp-cinema4d/main/install.md>.

## Requirements

- Cinema 4D R21 or newer, installed and licensed through Maxon's supported process.
- The matching canonical `c4dpy` executable from that exact Cinema 4D installation.
- External Python 3.9 or newer with `dcc-mcp-core>=0.20.14,<1.0.0` and this adapter installed in the same interpreter.
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
0.20.14. A selected executable, Python distribution, receipt, or live process
whose identity changes during an operation is rejected.

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
license. Real-host acceptance still requires a disposable licensed `c4dpy` run.

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
- `python` or `core_version`, exit `10`: invoke the CLI through the exact Python distribution that owns adapter and Core 0.20.14 or newer.
- `receipt`, exit `30`: preserve and review the foreign or malformed receipt; do not delete it blindly.
- `busy`, exit `30`: another mutation owns the state root; wait for it to finish.
- `runtime_timeout`, exit `40`: confirm licensing, then retry with a finite bounded timeout.
- `runtime_identity`, exit `40`: the live PID, start token, executable, or digest did not match the selected host.
- A healthy service is not proof of a usable host. Require exit `0` and `verify.directly_usable=true` before document or render tools.
