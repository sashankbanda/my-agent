"""Vault: encrypted snapshots of the hot store to a remote (or local) target.

The vault is the kernel's only non-LLM egress, and it transmits ciphertext
exclusively - the remote never sees plaintext or keys (architecture
invariant #2).
"""
