"""One-time voice model setup: download every on-device model.

    uv run python scripts/setup_voice.py             # required models
    uv run python scripts/setup_voice.py --kokoro    # + optional Kokoro TTS

Downloads into %LOCALAPPDATA%/MyAgent/models (or voice.yaml's models_dir):
  - Silero VAD        (~2 MB,  MIT)
  - openWakeWord      (~10 MB, Apache-2.0) feature models + the wake model
  - faster-whisper    (~145 MB for base.en, MIT) - prefetched into the cache
  - Kokoro-82M TTS    (--kokoro only: ~310 MB + 27 MB, Apache-2.0; the
                       default TTS engine is the built-in Windows one)

Every download is skipped if the file already exists; re-running is safe.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx

from myagent.voice.config import load_voice_settings

SILERO_URL = "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx"
KOKORO_BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"


def download(url: str, target: Path) -> None:
    """Stream one file to disk with progress; skip when already present."""
    if target.exists() and target.stat().st_size > 0:
        print(f"  [skip] {target.name} (already present)")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    print(f"  [get ] {target.name} <- {url}")
    with httpx.stream("GET", url, follow_redirects=True, timeout=None) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0))
        done = 0
        with partial.open("wb") as handle:
            for chunk in response.iter_bytes(1024 * 512):
                handle.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r         {done / 1e6:8.1f} / {total / 1e6:.1f} MB", end="")
    print()
    partial.replace(target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kokoro", action="store_true", help="also download the optional Kokoro TTS model"
    )
    args = parser.parse_args()

    settings = load_voice_settings()
    models_dir = settings.resolved_models_dir()
    print(f"models dir: {models_dir}")

    print("Silero VAD:")
    download(SILERO_URL, models_dir / "silero_vad.onnx")

    print("openWakeWord:")
    wake_dir = models_dir / "openwakeword"
    wake_dir.mkdir(parents=True, exist_ok=True)
    import openwakeword.utils

    # Several candidate wake models: pronunciation sensitivity varies a lot
    # between voices/accents, so --mic-check scores them all and the user
    # picks the one their voice actually triggers (config wake.model).
    wake_models = sorted({settings.wake.model, "hey_jarvis", "alexa", "hey_mycroft"})
    openwakeword.utils.download_models(model_names=wake_models, target_directory=str(wake_dir))
    print(f"  [ok  ] feature models + {wake_models} in {wake_dir}")

    if args.kokoro:
        print("Kokoro TTS (optional quality engine):")
        download(f"{KOKORO_BASE}/kokoro-v1.0.onnx", models_dir / "kokoro-v1.0.onnx")
        download(f"{KOKORO_BASE}/voices-v1.0.bin", models_dir / "voices-v1.0.bin")

    print(f"faster-whisper ({settings.stt.model}): prefetching...")
    from faster_whisper import WhisperModel

    WhisperModel(
        settings.stt.model,
        device="cpu",
        compute_type=settings.stt.compute_type,
        download_root=str(models_dir / "whisper"),
    )
    print("  [ok  ] whisper model cached")

    print("\nvoice models ready. Start the satellite with:")
    print("  uv run python -m myagent.voice")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
