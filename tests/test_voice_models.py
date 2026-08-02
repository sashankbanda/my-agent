"""Real-model voice tests: the full local audio loop, no microphone needed.

The self-contained trick: TTS *synthesizes* the test speech, which then flows
through the same VAD -> segmentation -> STT path a microphone would feed.
TTS, VAD, and STT verify each other end to end.

The Windows TTS engine is always present, so speech generation never blocks
these tests; the Silero/openWakeWord/whisper stages are gated on the models
downloaded by scripts/setup_voice.py (skipped with a clear reason in a fresh
checkout). Kokoro has its own additionally-gated test.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from myagent.voice.audio import resample_linear
from myagent.voice.config import FRAME_SAMPLES, SAMPLE_RATE, TtsSettings, load_voice_settings
from myagent.voice.tts import KOKORO_MODEL, KOKORO_VOICES, WindowsTts, parse_wav
from myagent.voice.vad import SILERO_FILENAME, SpeechSegmenter

SETTINGS = load_voice_settings()
MODELS_DIR = SETTINGS.resolved_models_dir()

CORE_MODELS_PRESENT = (MODELS_DIR / SILERO_FILENAME).exists() and (
    MODELS_DIR / "openwakeword" / "melspectrogram.onnx"
).exists()
KOKORO_PRESENT = (MODELS_DIR / KOKORO_MODEL).exists() and (MODELS_DIR / KOKORO_VOICES).exists()

needs_models = pytest.mark.skipif(
    not CORE_MODELS_PRESENT,
    reason="voice models not downloaded (run: uv run python scripts/setup_voice.py)",
)


@pytest.fixture(scope="module")
def spoken_16k() -> np.ndarray:
    """The Windows engine saying a test sentence, resampled to the mic rate."""
    tts = WindowsTts(TtsSettings())
    samples, rate = tts.synthesize("Hello world, this is a test of the voice pipeline.")
    return resample_linear(samples, rate, SAMPLE_RATE)


def frames_of(audio: np.ndarray) -> list[np.ndarray]:
    count = len(audio) // FRAME_SAMPLES
    return [audio[i * FRAME_SAMPLES : (i + 1) * FRAME_SAMPLES] for i in range(count)]


def test_windows_tts_synthesizes_speechlike_audio(spoken_16k: np.ndarray) -> None:
    """Engine sanity: non-trivial duration, sane amplitude."""
    assert len(spoken_16k) > SAMPLE_RATE  # more than a second of audio
    peak = float(np.abs(spoken_16k).max())
    assert 0.05 < peak <= 1.0


def test_parse_wav_round_trips_pcm() -> None:
    import io
    import wave

    pcm = (np.sin(np.linspace(0, 200, 8000)) * 20000).astype(np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(22050)
        wav.writeframes(pcm.tobytes())
    samples, rate = parse_wav(buffer.getvalue())
    assert rate == 22050
    assert len(samples) == len(pcm)
    assert float(np.abs(samples).max()) <= 1.0


@needs_models
def test_vad_scores_speech_above_silence(spoken_16k: np.ndarray) -> None:
    from myagent.voice.vad import SileroVad

    vad = SileroVad(MODELS_DIR)
    speech_probabilities = [vad(frame) for frame in frames_of(spoken_16k)]
    vad.reset()
    silence = np.zeros_like(spoken_16k)
    silence_probabilities = [vad(frame) for frame in frames_of(silence)]
    assert max(speech_probabilities) > 0.8
    assert max(silence_probabilities) < 0.3


@needs_models
def test_full_local_loop_tts_vad_stt(spoken_16k: np.ndarray) -> None:
    """TTS speech -> VAD segmentation -> whisper transcription, end to end.

    Pinned to the local engine so this test never depends on the network.
    """
    from myagent.voice.config import SttSettings
    from myagent.voice.stt import Transcriber
    from myagent.voice.vad import SileroVad

    vad = SileroVad(MODELS_DIR)
    segmenter = SpeechSegmenter(SETTINGS.vad)
    padded = np.concatenate(
        [
            np.zeros(SAMPLE_RATE // 2, dtype=np.float32),
            spoken_16k,
            np.zeros(SAMPLE_RATE, dtype=np.float32),
        ]
    )
    utterances = []
    for frame in frames_of(padded):
        result = segmenter.feed(frame, vad(frame))
        if result is not None:
            utterances.append(result)
    assert len(utterances) == 1, f"expected one utterance, got {len(utterances)}"

    stt = Transcriber(SttSettings(engine="local"), MODELS_DIR)
    text = stt.transcribe(utterances[0].audio).lower()
    assert "hello world" in text
    assert "voice" in text


@needs_models
def test_wake_word_scores_on_spoken_phrase() -> None:
    """TTS says the wake phrase; openWakeWord must score it clearly."""
    from myagent.voice.wake import WAKE_CHUNK_SAMPLES, WakeDetector

    tts = WindowsTts(TtsSettings())
    samples, rate = tts.synthesize("Hey Jarvis!")
    audio = resample_linear(samples, rate, SAMPLE_RATE)
    audio = np.concatenate(
        [np.zeros(SAMPLE_RATE, dtype=np.float32), audio, np.zeros(SAMPLE_RATE, dtype=np.float32)]
    )

    detector = WakeDetector(SETTINGS.wake, MODELS_DIR)
    detections = 0
    count = len(audio) // WAKE_CHUNK_SAMPLES
    for index in range(count):
        chunk = audio[index * WAKE_CHUNK_SAMPLES : (index + 1) * WAKE_CHUNK_SAMPLES]
        if detector.process(chunk):
            detections += 1
    assert detections >= 1, "wake word was not detected in synthesized speech"

    # And silence must not trigger it.
    detector.reset()
    silence_detections = 0
    silence = np.zeros(SAMPLE_RATE * 3, dtype=np.float32)
    for index in range(len(silence) // WAKE_CHUNK_SAMPLES):
        chunk = silence[index * WAKE_CHUNK_SAMPLES : (index + 1) * WAKE_CHUNK_SAMPLES]
        if detector.process(chunk):
            silence_detections += 1
    assert silence_detections == 0


@pytest.mark.skipif(not KOKORO_PRESENT, reason="kokoro models not downloaded (--kokoro)")
def test_kokoro_engine_synthesizes() -> None:
    from myagent.voice.tts import KokoroTts

    tts = KokoroTts(TtsSettings(engine="kokoro"), MODELS_DIR)
    samples, rate = tts.synthesize("Quality check.")
    assert rate == 24_000
    assert len(samples) > rate // 4  # at least a quarter second of audio


class TestUnknownWakeWord:
    """A wake word is a trained model, not a label.

    Renaming wake.model to something with no model behind it crashed the
    satellite on startup, and the launcher restarted it every second forever
    while the HUD just showed "voice off".
    """

    def test_an_unknown_name_is_refused_with_the_alternatives(self, tmp_path: Path) -> None:
        from myagent.voice.wake import UnknownWakeWordError, resolve_wake_model

        wake_dir = tmp_path / "openwakeword"
        wake_dir.mkdir(parents=True)
        for name in ("hey_jarvis_v0.1.onnx", "alexa_v0.1.onnx", "melspectrogram.onnx"):
            (wake_dir / name).touch()

        with pytest.raises(UnknownWakeWordError) as caught:
            resolve_wake_model(tmp_path, "hey_ev")

        message = str(caught.value)
        assert "hey_ev" in message
        assert "hey_jarvis" in message and "alexa" in message
        assert "melspectrogram" not in message, "helper models are not wake words"
        assert "wake.phrase" in message, "the custom-phrase route must be offered"

    def test_available_names_drop_the_version_suffix(self, tmp_path: Path) -> None:
        """Files are 'hey_jarvis_v0.1'; the config value is 'hey_jarvis'."""
        from myagent.voice.wake import available_wake_words

        wake_dir = tmp_path / "openwakeword"
        wake_dir.mkdir(parents=True)
        (wake_dir / "hey_jarvis_v0.1.onnx").touch()
        (wake_dir / "hey_mycroft_v0.1.onnx").touch()

        assert available_wake_words(tmp_path) == ["hey_jarvis", "hey_mycroft"]

    def test_a_known_name_still_resolves(self, tmp_path: Path) -> None:
        from myagent.voice.wake import resolve_wake_model

        wake_dir = tmp_path / "openwakeword"
        wake_dir.mkdir(parents=True)
        (wake_dir / "hey_jarvis_v0.1.onnx").touch()

        assert resolve_wake_model(tmp_path, "hey_jarvis").endswith("hey_jarvis_v0.1.onnx")


class TestCustomWakePhrase:
    """A wake word of your own, matched by transcription.

    openWakeWord knows three words and training a fourth needs hours of GPU
    time, so a custom wake word has to come from the transcription the
    pipeline can already produce.
    """

    @pytest.mark.parametrize(
        "heard",
        ["hey ev", "Hey, Ev.", "HEY EV", "hey eb", "hey f", "heyev", "hey  ev  "],
    )
    def test_mishearings_of_the_phrase_still_wake_it(self, heard: str) -> None:
        """STT mangles two-syllable phrases; strict matching rejects the user."""
        from myagent.voice.wake import PhraseWake

        woke, _ = PhraseWake("hey ev").check(heard)
        assert woke is True

    @pytest.mark.parametrize(
        "heard",
        ["hello there", "what is the time", "hey everyone lets go", "", "the end"],
    )
    def test_ordinary_speech_does_not_wake_it(self, heard: str) -> None:
        from myagent.voice.wake import PhraseWake

        woke, _ = PhraseWake("hey ev").check(heard)
        assert woke is False

    def test_the_request_in_the_same_breath_is_kept(self) -> None:
        """The whole utterance is transcribed, so no pause is needed."""
        from myagent.voice.wake import PhraseWake

        woke, remainder = PhraseWake("hey ev").check("Hey EV, what's the time?")
        assert woke is True
        assert remainder == "what's the time?"

    def test_a_bare_wake_word_leaves_no_request(self) -> None:
        from myagent.voice.wake import PhraseWake

        assert PhraseWake("hey ev").check("hey ev") == (True, "")

    def test_a_longer_phrase_works_too(self) -> None:
        from myagent.voice.wake import PhraseWake

        woke, remainder = PhraseWake("okay computer").check("okay computer open chrome")
        assert woke is True
        assert remainder == "open chrome"

    def test_an_empty_phrase_is_refused(self) -> None:
        from myagent.voice.wake import PhraseWake

        with pytest.raises(ValueError, match="empty"):
            PhraseWake("   ")

    def test_sensitivity_is_adjustable(self) -> None:
        """Lower it if your wake word is missed, raise it for false triggers."""
        from myagent.voice.wake import PhraseWake

        assert PhraseWake("hey ev", similarity=0.99).check("hey eb")[0] is False
        assert PhraseWake("hey ev", similarity=0.5).check("hey eb")[0] is True
