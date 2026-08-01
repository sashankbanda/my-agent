"""Security: the permission broker and the machinery it needs.

Architectural invariant: the broker sits *below* cognition, physically in the
execution path (see tools.executor). The model cannot choose to skip it -
there is no code path from a tool call to an effect that bypasses
``broker.authorize``.
"""
