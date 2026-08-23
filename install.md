# Cinema 4D Standalone Install SOP v1

This adapter runs typed operations through Maxon's licensed headless `c4dpy`.
Nothing is installed into the Cinema 4D application. The canonical raw guide is
<https://raw.githubusercontent.com/dcc-mcp/dcc-mcp-cinema4d/main/install.md>.

## Requirements

- Cinema 4D R21 or newer, installed and licensed through Maxon's supported
  process.
- The matching `c4dpy` executable shipped with that Cinema 4D installation.
- External Python 3.9 or newer with `dcc-mcp-core>=0.19.91,<1.0.0`.
- Read/write access to every path listed in
  `DCC_MCP_CINEMA4D_ALLOWED_ROOTS`.

The adapter does not download, scrape, update, or execute a remote Cinema 4D
payload. There is no adapter-managed binary cache. `c4dpy` remains owned by the
Maxon installation and license manager.

## Supported versions

| Platform | Automatic discovery | Explicit example |
| --- | --- | --- |
| Windows | `PATH`, `%ProgramFiles%\Maxon\Cinema 4D *`, `%ProgramFiles%\Maxon Cinema 4D *` | `C:\Program Files\Maxon Cinema 4D 2026\c4dpy.exe` |
| macOS | `PATH`, `/Applications/Maxon Cinema 4D */c4dpy.app` | `/Applications/Maxon Cinema 4D 2026/c4dpy.app/Contents/MacOS/c4dpy` |
| Linux | `PATH`; Maxon install roots vary, so explicit configuration is recommended | `/opt/maxon/cinema4d/c4dpy` |

The runtime floor is Cinema 4D R21. The service floor is Python 3.9 and Core
0.19.91. `doctor` checks the installed Core version before starting `c4dpy` and
the host build returned by the bounded runtime probe.

## Agent quick path

Install the released wheel into the service Python environment:

```shell
python -m pip install dcc-mcp-cinema4d
dcc-mcp-cinema4d doctor --json
```

If discovery succeeds and a license is available, the JSON result has exit
code `0` and `verify.directly_usable=true`. Otherwise follow the structured
`next_steps` command; do not proceed to document tools on a failed doctor.

Set the allowed workspace before starting the long-running adapter service:

```shell
# Windows PowerShell example
$env:DCC_MCP_CINEMA4D_ALLOWED_ROOTS = "D:\projects;D:\exports"
dcc-mcp-cinema4d
```

With no verb, `dcc-mcp-cinema4d` retains the existing service-start behavior.

## Manual path

When automatic discovery is ambiguous or unavailable, provide the absolute
Maxon-owned executable:

```shell
dcc-mcp-cinema4d doctor --c4dpy <absolute-c4dpy-path> --json
```

The persistent environment equivalent is:

```text
DCC_MCP_CINEMA4D_C4DPY=<absolute-c4dpy-path>
DCC_MCP_CINEMA4D_ALLOWED_ROOTS=<path-list-using-the-platform-separator>
DCC_MCP_CINEMA4D_MAX_INPUT_BYTES=2147483648
DCC_MCP_CINEMA4D_MAX_TIMEOUT_SECS=1800
```

On Windows, path lists use `;`; on macOS and Linux, they use `:`. Invalid
numeric settings fail preflight instead of silently falling back. This is a
standalone runtime: there is no plugin copy, host receipt, registration step,
or host-side Python package install.

## Verify

Run the same bounded licensed status probe explicitly:

```shell
dcc-mcp-cinema4d verify --c4dpy <absolute-c4dpy-path> --timeout-secs 60 --json
```

The result reports executable source, allowed roots, configured limits, Core
and adapter versions, and the runtime's Cinema 4D, API, and Python versions.
It uses schema `1.0` with `verify.directly_usable`, `failure_stage`,
`failure_reason`, and machine-executable `next_steps`.

Stable exits are:

| Code | Meaning |
| --- | --- |
| `0` | Core, executable, licensed runtime, and host version are usable |
| `10` | Discovery, configuration, Core floor, or host floor failed preflight |
| `40` | The executable was found but license/runtime verification failed |

CI verifies this contract without pretending to hold a Maxon license. A real
licensed `c4dpy` response is required before claiming live-host acceptance.

## Upgrade

Stop the user-started adapter service, upgrade the wheel, then rerun doctor:

```shell
python -m pip install --upgrade dcc-mcp-cinema4d
dcc-mcp-cinema4d doctor --json
```

Cinema 4D and `c4dpy` upgrades remain Maxon-managed. The adapter neither
selects a mutable "latest" payload nor caches one. Python package caching is
owned by pip; inspect it with `python -m pip cache info` and, when deliberately
required, clean it with `python -m pip cache purge`.

## Uninstall

Stop the foreground adapter with its normal interrupt, then remove only the
Python wheel:

```shell
python -m pip uninstall dcc-mcp-cinema4d
```

There is no adapter daemon, application plugin, install receipt, or binary
cache to remove. Do not delete the Maxon installation or license data as part
of adapter uninstall.

## Troubleshooting

- `executable_discovery`, exit `10`: set `DCC_MCP_CINEMA4D_C4DPY` or pass an
  absolute `--c4dpy` path matching the installed Cinema 4D release.
- `configuration`, exit `10`: correct numeric timeout/input limits and ensure
  every allowed root uses the platform path separator and exists.
- `core_version`, exit `10`: upgrade Core in the same external Python that
  owns the adapter wheel.
- `host_version`, exit `10`: select a licensed Cinema 4D R21 or newer runtime.
- `license`, exit `40`: open the Maxon licensing workflow under the owning
  account, activate the installed product, then rerun the exact `next_steps`
  command. The adapter does not automate credentials.
- `runtime_start` or `runtime_timeout`, exit `40`: run `c4dpy` under the same
  user, confirm its headless license/configuration, increase `--timeout-secs`
  only within the configured maximum, and retry.
- A healthy Python service is not proof of a usable host. Require exit `0` and
  `verify.directly_usable=true` before calling document or render tools.
