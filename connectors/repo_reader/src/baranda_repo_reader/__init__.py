"""baranda connector: read-only exploration of a git repository over MCP."""

from .server import create_server

__all__ = ["create_server"]
