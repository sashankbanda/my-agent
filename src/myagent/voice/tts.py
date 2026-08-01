"""Text-to-speech engines behind one interface.

Two engines, selected by ``tts.engine`` in config/voice.yaml:

- ``windows`` (default): the OS speech synthesizer via WinRT. Synthesis is
  effectively instant (~10 ms/sentence measured), needs no downloads, and
  exists on every Windows 11 machine. Voice quality is classic-synthetic.
- ``kokoro``: Kokoro-82M over ONNX - near-human quality, but sub-real-time
  on slower CPUs (measured 7 s for 3 s of audio on the reference laptop), so
  it is opt-in for hardware that can afford it.

Deviation from the playbook's "Piper fallback", recorded here: piper-tts has
no Python 3.14 Windows wheels and sherpa-onnx (which runs Piper voices) ships
a broken ORT pairing on this platform as of 2026-08. The Windows engine fills
the low-latency slot instead. Revisit-trigger: piper/sherpa publish working
cp314 Windows wheels.

Sentence-sized inputs come from the kernel (voice_ws splits them), so each
``synthesize`` call is short - that is what keeps time-to-first-audio low.
"""

from __future__ import annotations

import asyncio
import struct
from pathlib import Path
from typing import Protocol

import numpy as np

from myagent.voice.config import TtsSettings

KOKORO_MODEL = "kokoro-v1.0.onnx"
KOKORO_VOICES = "voices-v1.0.bin"
KOKORO_SAMPLE_RATE = 24_000


class TtsEngine(Protocol):
    """What the pipeline needs from a synthesis engine."""

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        """(float32 mono samples, sample_rate) for one sentence. Blocking."""
        ...


def parse_wav(raw: bytes) -> tuple[np.ndarray, int]:
    """Minimal RIFF/WAVE parser for 16-bit mono PCM (what WinRT emits)."""
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE stream")
    offset = 12
    sample_rate: int | None = None
    pcm: bytes | None = None
    while offset + 8 <= len(raw):
        chunk_id = raw[offset : offset + 4]
        (chunk_size,) = struct.unpack_from("<I", raw, offset + 4)
        body = raw[offset + 8 : offset + 8 + chunk_size]
        if chunk_id == b"fmt ":
            (sample_rate,) = struct.unpack_from("<I", body, 4)
        elif chunk_id == b"data":
            pcm = body
        offset += 8 + chunk_size + (chunk_size % 2)  # chunks are word-aligned
    if sample_rate is None or pcm is None:
        raise ValueError("WAVE stream missing fmt or data chunk")
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    return samples, sample_rate


class WindowsTts:
    """OS-native synthesis via Windows.Media.SpeechSynthesis."""

    def __init__(self, settings: TtsSettings) -> None:
        # Compatibility: onnxruntime's DLL must initialize before WinRT flips
        # the process COM/apartment state, or importing it later fails with
        # "DLL initialization routine failed" (observed on Windows 11). Every
        # voice-process configuration also uses onnxruntime (VAD), so preload.
        import onnxruntime  # noqa: F401
        from winrt.windows.media.speechsynthesis import SpeechSynthesizer

        self._synth = SpeechSynthesizer()
        if settings.windows_voice:
            wanted = settings.windows_voice.lower()
            for voice in SpeechSynthesizer.all_voices:
                if wanted in voice.display_name.lower():
                    self._synth.voice = voice
                    break
            else:
                raise ValueError(
                    f"no Windows voice matches '{settings.windows_voice}'; available: "
                    + ", ".join(v.display_name for v in SpeechSynthesizer.all_voices)
                )

    async def _synthesize_async(self, text: str) -> bytes:
        from winrt.windows.storage.streams import DataReader

        stream = await self._synth.synthesize_text_to_stream_async(text)
        size = int(stream.size)
        reader = DataReader(stream.get_input_stream_at(0))
        await reader.load_async(size)
        buffer = bytearray(size)
        reader.read_bytes(buffer)
        return bytes(buffer)

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        # Blocking by contract (TtsEngine): call from a worker thread
        # (asyncio.to_thread), never directly from a running event loop.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            raw = asyncio.run(self._synthesize_async(text))
            return parse_wav(raw)
        raise RuntimeError(
            "synthesize() is blocking; call it via asyncio.to_thread from async code"
        )


class KokoroTts:
    """Kokoro-82M over ONNX (quality engine for capable CPUs)."""

    def __init__(self, settings: TtsSettings, models_dir: Path) -> None:
        from kokoro_onnx import Kokoro

        model_path = models_dir / KOKORO_MODEL
        voices_path = models_dir / KOKORO_VOICES
        if not model_path.exists() or not voices_path.exists():
            raise FileNotFoundError(
                f"Kokoro model files missing in {models_dir} - run: "
                "uv run python scripts/setup_voice.py --kokoro"
            )
        self._kokoro = Kokoro(str(model_path), str(voices_path))
        self._voice = settings.voice
        self._speed = settings.speed

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        samples, sample_rate = self._kokoro.create(
            text, voice=self._voice, speed=self._speed, lang="en-us"
        )
        return samples.astype(np.float32), int(sample_rate)


def create_synthesizer(settings: TtsSettings, models_dir: Path) -> TtsEngine:
    """Build the configured engine."""
    if settings.engine == "kokoro":
        return KokoroTts(settings, models_dir)
    return WindowsTts(settings)
