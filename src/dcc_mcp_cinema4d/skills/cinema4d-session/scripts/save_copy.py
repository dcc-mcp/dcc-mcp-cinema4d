from dcc_mcp_core.skill import run_main

from dcc_mcp_cinema4d.skill_tools import bridge_main

main = bridge_main("save_copy", "Cinema 4D document copy saved.")

if __name__ == "__main__":
    run_main(main)
