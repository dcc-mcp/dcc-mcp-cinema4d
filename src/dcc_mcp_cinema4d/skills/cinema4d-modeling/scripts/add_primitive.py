from dcc_mcp_core.skill import run_main

from dcc_mcp_cinema4d.skill_tools import bridge_main

main = bridge_main("add_primitive", "Cinema 4D primitive added.")

if __name__ == "__main__":
    run_main(main)
