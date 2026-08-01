"""Voice satellite: the CPU-only, always-local audio edge.

Runs as a separate process (``python -m myagent.voice``) and talks to the
kernel over WebSocket ONLY - it never imports kernel internals (gateway,
memory, loop). A crash here can never take down the kernel, and no audio ever
leaves this machine: wake word, VAD, STT, and TTS are all on-device.
"""
