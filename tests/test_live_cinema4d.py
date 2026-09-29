"""Real-host verification behind the opt-in ``cinema4d`` marker.

Why this file exists
--------------------

``pyproject.toml`` declared ``markers=["cinema4d: requires a real licensed
c4dpy runtime"]`` and CI ran ``pytest -m "not cinema4d"``, but **no test used
the marker**. That is worse than not having it: the repo looked like it had
real-host capability while producing no host evidence at all.

The marker now has a test behind it. A licensed machine opts in with::

    C4D_TEST_EXECUTABLE=/path/to/c4dpy pytest -m cinema4d

and gets the end-to-end path: create, add, transform, remove, copy, export,
render, inspect, validate.

What this does and does not give you
------------------------------------

CI cannot run this: a licensed c4dpy cannot be provisioned on GitHub-hosted
runners, and that is a real constraint rather than an oversight. So the
``cinema4d`` marker stays excluded from CI, and CI evidence remains
**contract-level only**. When this test does run on a licensed machine it is the
only **host-level** evidence the adapter has — which is exactly why it must
exist rather than being deleted.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from live_cinema4d_smoke import main as smoke_main

C4D_TEST_EXECUTABLE = "C4D_TEST_EXECUTABLE"


def _real_cinema4d() -> bool:
    """Opt-in gate: unset means "no licensed c4dpy here", so skip."""
    return bool(os.environ.get(C4D_TEST_EXECUTABLE, "").strip())


@pytest.mark.cinema4d
@pytest.mark.skipif(not _real_cinema4d(), reason="%s is not set" % C4D_TEST_EXECUTABLE)
def test_real_cinema4d_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = os.environ[C4D_TEST_EXECUTABLE]
    # The bridge resolves its runtime from DCC_MCP_CINEMA4D_C4DPY; accept either
    # name so the documented gate and the existing variable both work.
    monkeypatch.setenv("DCC_MCP_CINEMA4D_C4DPY", executable)
    monkeypatch.setenv("DCC_MCP_CINEMA4D_ALLOWED_ROOTS", str(tmp_path))

    assert smoke_main(["--evidence-dir", str(tmp_path / "evidence")]) == 0
