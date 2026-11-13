"""MCP server over stdio (2026-07-28, and legacy 2025-11-25 and 2025-06-18), standard library only.

Start it with ``zlib-cli mcp``; see docs/mcp.md. ``serve_stdio`` serves an
already-built tool set on two binary streams, or on the diverted process
descriptors when none are given.
"""

from .protocol import McpServer, divert_stdio


def serve_stdio(tools, *, server_info, instructions=None, logger=None, reader=None, writer=None):
    """Serve until EOF and return the McpServer; the caller closes the tools' backend."""
    if reader is None or writer is None:
        reader, writer = divert_stdio()
    server = McpServer(tools, server_info=server_info, instructions=instructions, logger=logger)
    server.serve(reader, writer)
    return server


__all__ = ["McpServer", "divert_stdio", "serve_stdio"]
