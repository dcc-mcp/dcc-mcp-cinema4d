from dcc_mcp_core.skill import run_main

from dcc_mcp_cinema4d.skill_tools import bridge_main

main = bridge_main("inspect_document", "Cinema 4D document inspected.")

if __name__ == "__main__":
    run_main(main)
