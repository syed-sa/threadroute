import sys

from threadroute.config import load_env
from threadroute.mcp_server.server import build_server

if __name__ == "__main__":
    load_env()
    # stdout belongs to the MCP protocol - anything human-readable must go to stderr.
    print("threadroute-wms MCP server starting (stdio)", file=sys.stderr)
    build_server().run()
