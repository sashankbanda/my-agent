"""Model Gateway: the kernel's ONLY egress point for LLM inference.

No module outside this package may import a provider SDK or open a connection
to an LLM provider. Everything reasons through ``Gateway.complete``.
"""
