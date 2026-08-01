"""Always-on-top overlay: a small orb that shows what the assistant is doing.

Deliberately tkinter (Python standard library) rather than a native shell:
an always-on-top borderless window with transparency is all this needs, and
tkinter provides that on Windows with zero new dependencies and a ~15 MB
footprint. The rich dashboard lives in the browser HUD; this is the glance.

The playbook puts a Tauri desktop shell in M8 as polish. This does not replace
that - it makes the everyday experience good now, and it is ~250 lines that
can be deleted the day the Tauri shell lands.
"""
