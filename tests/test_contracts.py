from pathlib import Path
from unittest.mock import patch

import yaml

from dcc_mcp_cinema4d.server import Cinema4dMcpServer

ROOT = Path(__file__).parents[1]
SKILLS = ROOT / "src" / "dcc_mcp_cinema4d" / "skills"


def test_server_uses_core_canonical_c4d_type():
    with (
        patch("dcc_mcp_cinema4d.server.DccServerOptions.from_env") as from_env,
        patch("dcc_mcp_cinema4d.server.DccServerBase.__init__", return_value=None),
    ):
        Cinema4dMcpServer()

    assert from_env.call_args.args[0] == "c4d"


def test_skill_contracts_are_complete_and_bounded():
    names = set()
    for tools_path in SKILLS.glob("*/tools.yaml"):
        payload = yaml.safe_load(tools_path.read_text(encoding="utf-8"))
        for tool in payload["tools"]:
            assert tool["name"] not in names
            names.add(tool["name"])
            assert (tools_path.parent / tool["source_file"]).is_file()
            assert tool["input_schema"]["additionalProperties"] is False
            assert tool["output_schema"]["type"] == "object"
            assert "timeout_hint_secs" in tool
            assert tool["annotations"]["open_world_hint"] is False

    assert names == {
        "add_primitive",
        "create_document",
        "export_document",
        "get_capabilities",
        "get_status",
        "import_geometry",
        "inspect_document",
        "remove_object",
        "render_document",
        "save_copy",
        "transform_object",
        "validate_document",
    }


def test_public_sources_do_not_expose_arbitrary_python():
    text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "src" / "dcc_mcp_cinema4d").rglob("*")
        if path.is_file() and path.suffix in {".md", ".py", ".yaml"}
    ).lower()
    assert "exec(" not in text
    assert "eval(" not in text
