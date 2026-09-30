"""Admin MCP server — kitchen/owner tools.

Mounted at /mcp/admin in main.py.
Every request must carry:
  - Header: X-Admin-Key: <ADMIN_API_KEY>

Admin tools are NEVER reachable through /mcp/customer.
Tools are added in M5.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp_admin = FastMCP(
    name="maneki-admin",
    instructions=(
        "Admin tools for kitchen staff and restaurant owners. "
        "Manage orders, update item availability, view sales summaries."
    ),
)

# Tools are registered in M5 via @mcp_admin.tool() decorators.
