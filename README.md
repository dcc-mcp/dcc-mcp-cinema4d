# dcc-mcp-cinema4d

Cinema 4D adapter foundation for the DCC-MCP organization.

This is an experimental, read-only first slice. It is **not** in the released
`dcc-mcp-cli dcc-types` catalog yet.

## Scope

- Discover the headless c4dpy boundary.
- Expose one typed, read-only document inspection tool.
- Keep host API calls outside the MCP HTTP worker.
- Do not expose arbitrary source evaluation.

## Install

```bash
python -m pip install -e ".[test]"
dcc-mcp-cinema4d
```

Configure the bridge environment variables in `src/dcc_mcp_cinema4d/bridge.py`.
A real Cinema 4D live smoke is required before catalog onboarding.

Official API reference: https://developers.maxon.net/docs/py/index.html

