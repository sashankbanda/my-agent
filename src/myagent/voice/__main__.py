"""Voice satellite entrypoint: ``python -m myagent.voice``.

Options:
    --list-devices     print audio devices and exit
    --mic-check [SEC]  capture SEC seconds (default 15) and report what the
                       pipeline hears: input level, VAD probability, wake
                       score - the first thing to run when voice seems deaf
    --config PATH      alternate voice.yaml
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import cast

from myagent.logging import configure_logging, get_logger
from myagent.voice.config import load_voice_settings


def mic_check(seconds: int) -> None:
    """Live per-second report of level / VAD / and EVERY wake model's score.

    All downloaded wake models are scored simultaneously, so the user can try
    "Hey Jarvis", "Alexa", "Hey Mycroft", ... in one session and see which
    phrase their voice actually triggers - then set it as wake.model.
    """
    import numpy as np
    import sounddevice as sd
    from openwakeword.model import Model

    from myagent.voice.audio import MicStream, probe_live_input
    from myagent.voice.config import FRAME_SAMPLES, SAMPLE_RATE
    from myagent.voice.vad import SileroVad
    from myagent.voice.wake import WAKE_CHUNK_SAMPLES, wake_model_files

    settings = load_voice_settings()
    models_dir = settings.resolved_models_dir()
    device: str | int | None = settings.input_device
    if device is not None:
        print(f"configured input device: {sd.query_devices(device)['name']}")
    else:
        device = probe_live_input()  # same auto policy as the running pipeline
        if device is not None:
            print(f"auto-probed input device: {sd.query_devices(device)['name']}")
        else:
            print(f"system default input device: {sd.query_devices(kind='input')['name']}")

    wake_dir = models_dir / "openwakeword"
    wake_files = [str(path) for path in wake_model_files(wake_dir)]
    if not wake_files:
        print("no wake models found - run: uv run python scripts/setup_voice.py")
        return
    wake_model = Model(
        wakeword_models=wake_files,
        inference_framework="onnx",
        melspec_model_path=str(wake_dir / "melspectrogram.onnx"),
        embedding_model_path=str(wake_dir / "embedding_model.onnx"),
    )
    phrases = ", ".join(sorted(name.split("_v0")[0] for name in wake_model.models))
    print(f"scoring wake models: {phrases}")
    print(f"speak, and try each wake phrase - reporting once per second for {seconds}s:\n")

    vad = SileroVad(models_dir)
    mic = MicStream(device)
    mic.start()
    try:
        frames_per_second = SAMPLE_RATE // FRAME_SAMPLES
        wake_buffer = np.zeros(0, dtype=np.float32)
        for second in range(seconds):
            peak = 0.0
            vad_max = 0.0
            best_scores: dict[str, float] = {}
            for _ in range(frames_per_second):
                frame = mic.get(timeout=2.0)
                if frame is None:
                    print("!! no audio frames arriving - mic is not delivering data")
                    return
                peak = max(peak, float(np.abs(frame).max()))
                vad_max = max(vad_max, vad(frame))
                wake_buffer = np.concatenate([wake_buffer, frame])
                while len(wake_buffer) >= WAKE_CHUNK_SAMPLES:
                    pcm16 = (wake_buffer[:WAKE_CHUNK_SAMPLES] * 32767.0).astype(np.int16)
                    # predict()'s stub type is imprecise; runtime returns {name: score}
                    scores_by_model = cast("dict[str, float]", wake_model.predict(pcm16))
                    for name, score in scores_by_model.items():
                        short = name.split("_v0")[0]
                        best_scores[short] = max(best_scores.get(short, 0.0), float(score))
                    wake_buffer = wake_buffer[WAKE_CHUNK_SAMPLES:]
            bar = "#" * int(min(peak, 1.0) * 20)
            scores = "  ".join(
                f"{name} {score:4.2f}" for name, score in sorted(best_scores.items())
            )
            print(f"  {second + 1:2d}s  peak {peak:5.3f} |{bar:<20}| vad {vad_max:4.2f}  {scores}")
        print(f"\n(threshold is {settings.wake.threshold}; whichever phrase scores above it")
        print(" for your voice, set it as wake.model in config/voice.yaml)")
    finally:
        mic.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-devices", action="store_true", help="print audio devices")
    parser.add_argument(
        "--mic-check",
        nargs="?",
        const=15,
        type=int,
        default=None,
        metavar="SEC",
        help="capture SEC seconds and report level/VAD/wake",
    )
    parser.add_argument("--config", type=Path, default=None, help="alternate voice.yaml")
    args = parser.parse_args()

    if args.list_devices:
        from myagent.voice.audio import list_devices

        print(list_devices())
        return

    if args.mic_check is not None:
        mic_check(args.mic_check)
        return

    settings = load_voice_settings(args.config)
    from myagent.config import load_settings

    configure_logging(load_settings().logging)
    log = get_logger(__name__)

    from myagent.voice.pipeline import Pipeline

    log.info("voice_starting", mode=settings.mode, kernel=settings.kernel_url)
    try:
        asyncio.run(Pipeline(settings).run())
    except KeyboardInterrupt:
        log.info("voice_stopped")


if __name__ == "__main__":
    main()
