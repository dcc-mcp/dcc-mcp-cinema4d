from dcc_mcp_core.skill import run_main

from dcc_mcp_cinema4d.skill_tools import bridge_main

main = bridge_main("remove_object", "Cinema 4D object removed.")

if __name__ == "__main__":
    run_main(main)
