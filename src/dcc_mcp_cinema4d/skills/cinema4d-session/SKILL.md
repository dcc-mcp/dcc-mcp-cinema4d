---
name: cinema4d-session
description: >-
  Inspect a connected Cinema 4D session through the DCC-MCP headless c4dpy boundary.
  This first slice is read-only and does not execute arbitrary source.
license: MIT
compatibility: "Cinema 4D; dcc-mcp-core 0.19+"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: cinema4d
    layer: domain
    version: "0.1.0"
    tags: "cinema4d,mcp,dcc,automation"
    tools: tools.yaml
    depends: "dcc-diagnostics"
---

# Cinema 4D

Experimental first slice. Live host validation, version matrices, catalog
onboarding, and mutation tools are separate follow-up gates.

