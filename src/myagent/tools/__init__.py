"""Tools: the assistant's capabilities.

A built-in tool is a plain Python function with a ``@tool`` decorator (v3
review F3 - no MCP ceremony for first-party code; MCP arrives in M8 for
third-party plugins, mounting into this same registry).

Every tool declares its permission tier, and execution always routes through
``tools.executor.execute`` - the physical chokepoint that consults the broker.
"""

from myagent.tools.registry import ToolContext, ToolError, tool

__all__ = ["ToolContext", "ToolError", "tool"]
