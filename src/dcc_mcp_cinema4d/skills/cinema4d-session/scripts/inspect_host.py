from dcc_mcp_core.skill import run_main, skill_entry, skill_success

from dcc_mcp_cinema4d.bridge import get_bridge


@skill_entry
def main(**_kwargs):
    result = get_bridge().call("cinema4d.inspect_document")
    return skill_success("Cinema 4D document inspected.", document=result)


if __name__ == "__main__":
    run_main(main)
