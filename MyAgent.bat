@echo off
rem Double-click to start MyAgent (kernel + voice + overlay + HUD).
rem Close this window or press Ctrl+C to stop everything.
cd /d "%~dp0"
title MyAgent
uv run python -m myagent.start %*
if errorlevel 1 pause
