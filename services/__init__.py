"""Service layer: the single home for content operations.

Both the admin API (django-ninja) and the MCP server call these functions, so
business logic lives here exactly once (ARCHITECTURE.md §6/§11).
"""
