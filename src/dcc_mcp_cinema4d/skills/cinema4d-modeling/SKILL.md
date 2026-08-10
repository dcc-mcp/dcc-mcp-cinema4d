---
name: cinema4d-modeling
description: >-
  Create and transform typed Cinema 4D primitives, import geometry, export documents,
  and render images through an isolated c4dpy process. No arbitrary Python execution.
license: MIT
compatibility: "Cinema 4D R21+ with licensed c4dpy; dcc-mcp-core 0.19.91+"
allowed-tools: "python"
metadata:
  dcc-mcp:
    dcc: cinema4d
    layer: domain
    version: "0.1.1"  # x-release-please-version
    tags: "cinema4d,modeling,import,export,render"
    tools: tools.yaml
    depends: "cinema4d-session,dcc-diagnostics"
---

# Cinema 4D modeling

Inspect the document before changing it. Use unique object names, save mutations to the
same `.c4d` document, and inspect or validate the durable result. Paths are restricted to
`DCC_MCP_CINEMA4D_ALLOWED_ROOTS`; set `DCC_MCP_CINEMA4D_C4DPY` when discovery cannot
locate the licensed `c4dpy` executable.
