"""Speaker verification: does it actually tell voices apart?

Synthetic voices stand in for different people. That is a weaker test than
real speakers - two TTS voices differ more cleanly than two housemates - so
these assert the *mechanism* (separation exists, thresholds calibrate, the
profile round-trips) rather than a claimed accuracy figure.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from myagent.voice import speaker
from myagent.voice.config import SAMPLE_RATE

# A voice is broadband sound shaped by the resonances of a particular throat
# and mouth. Pure tones are a bad stand-in - they have no energy between
# harmonics, so added noise changes their spectrum more than a different
# speaker would, which is the opposite of the property under test. These
# helpers model the thing that actually distinguishes people: formants.
DAVID = (500.0, 1500.0, 2400.0)
ZIRA = (750.0, 2100.0, 3000.0)


def voice(formants: tuple[float, ...], seed: int = 0, seconds: float = 1.2) -> np.ndarray:
    """Broadband sound shaped by one speaker's formants, with syllables."""
    rng = np.random.default_rng(seed)
    samples = int(SAMPLE_RATE * seconds)
    source = rng.normal(0, 1, samples)
    # Syllabic amplitude modulation, so frames differ over time as speech does.
    t = np.linspace(0, seconds, samples, endpoint=False)
    source *= 0.5 + 0.5 * np.abs(np.sin(2 * np.pi * 3.5 * t))

    spectrum = np.fft.rfft(source)
    frequencies = np.fft.rfftfreq(samples, 1 / SAMPLE_RATE)
    envelope = np.full_like(frequencies, 0.05)
    for peak in formants:
        envelope += np.exp(-(((frequencies - peak) / 120.0) ** 2))
    shaped = np.fft.irfft(spectrum * envelope, samples)
    return (shaped / np.max(np.abs(shaped))).astype(np.float32)


def another_take(formants: tuple[float, ...], seed: int) -> np.ndarray:
    """The same speaker saying it again - same formants, new noise."""
    return voice(formants, seed=seed)


class TestFeatures:
    def test_mfcc_shape_is_frames_by_cepstra(self) -> None:
        assert speaker.mfcc(voice(DAVID)).shape[1] == speaker.CEPSTRA

    def test_embedding_is_unit_length(self) -> None:
        assert np.isclose(np.linalg.norm(speaker.embed(voice(DAVID))), 1.0, atol=1e-5)

    def test_c0_is_excluded(self) -> None:
        """The one detail that made this work.

        C0 is log-energy, around -450 where other coefficients sit within
        ±20; including it swamped the cosine and two obviously different
        voices scored 0.999 against each other.
        """
        assert len(speaker.embed(voice(DAVID))) == (speaker.CEPSTRA - 1) * 2

    def test_too_short_audio_is_refused(self) -> None:
        with pytest.raises(ValueError, match="too short"):
            speaker.embed(np.zeros(100, dtype=np.float32))

    def test_the_same_voice_scores_higher_than_a_different_one(self) -> None:
        mine = speaker.embed(voice(DAVID, seed=1))
        me_again = speaker.embed(voice(DAVID, seed=2))
        someone_else = speaker.embed(voice(ZIRA, seed=3))

        assert speaker.similarity(mine, me_again) > speaker.similarity(mine, someone_else)


class TestEnrolment:
    def test_a_profile_accepts_the_enrolled_voice(self) -> None:
        recordings = [another_take(DAVID, seed) for seed in range(5)]
        profile = speaker.enrol(recordings, phrase="hey buddy")

        accepted, score = profile.matches(another_take(DAVID, 99))
        assert accepted is True, f"the owner was rejected at {score:.3f}"

    def test_a_profile_rejects_a_different_voice(self) -> None:
        profile = speaker.enrol([another_take(DAVID, seed) for seed in range(5)])

        accepted, _score = profile.matches(voice(ZIRA, seed=42))
        assert accepted is False

    def test_too_few_samples_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least"):
            speaker.enrol([voice(DAVID)])

    def test_the_threshold_is_measured_not_guessed(self) -> None:
        """A consistent voice sets a high bar; a variable one sets a lower one."""
        steady = speaker.enrol([another_take(DAVID, seed) for seed in range(5)])
        # Formants wandering between takes: a voice that is not consistent.
        variable = speaker.enrol(
            [voice((500.0 + 90 * seed, 1500.0, 2400.0), seed) for seed in range(5)]
        )

        assert steady.threshold > variable.threshold

    def test_the_threshold_never_goes_below_the_floor(self) -> None:
        """A hopeless enrolment must not accept literally anything."""
        noisy = speaker.enrol(
            [voice((300.0 + 500 * seed, 900.0 + 700 * seed), seed) for seed in range(5)]
        )
        assert noisy.threshold >= speaker.MIN_THRESHOLD

    def test_silence_does_not_match(self) -> None:
        profile = speaker.enrol([another_take(DAVID, seed) for seed in range(5)])
        accepted, _ = profile.matches(np.zeros(SAMPLE_RATE, dtype=np.float32))
        assert accepted is False


class TestProfileStorage:
    def test_a_profile_round_trips(self, tmp_path: Path) -> None:
        original = speaker.enrol(
            [another_take(DAVID, seed) for seed in range(4)], phrase="hey buddy"
        )
        original.save(tmp_path / "owner")

        loaded = speaker.VoiceProfile.load(tmp_path / "owner")

        assert loaded is not None
        assert loaded.samples == original.samples
        assert loaded.phrase == "hey buddy"
        assert np.allclose(loaded.centroid, original.centroid)
        assert loaded.matches(another_take(DAVID, 7))[0] is True

    def test_no_profile_loads_as_none(self, tmp_path: Path) -> None:
        """Never enrolled is not an error - the feature is simply off."""
        assert speaker.VoiceProfile.load(tmp_path / "nobody") is None

    def test_a_corrupt_profile_loads_as_none(self, tmp_path: Path) -> None:
        """A broken file must not stop voice from starting."""
        np.save(tmp_path / "owner.npy", np.zeros(24, dtype=np.float32))
        (tmp_path / "owner.json").write_text("{not json", encoding="utf-8")

        assert speaker.VoiceProfile.load(tmp_path / "owner") is None


def _stub_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Build a Pipeline without audio hardware, models, or a keyboard hook."""
    from myagent.voice.pipeline import Pipeline

    class _Phrase:
        phrase = "stub"

    monkeypatch.setattr(Pipeline, "_open_audio", lambda self, probe: None)
    monkeypatch.setattr(Pipeline, "_install_mute_hotkey", lambda self: None)
    for attribute in ("SileroVad", "SpeechSegmenter", "Transcriber"):
        monkeypatch.setattr(f"myagent.voice.pipeline.{attribute}", lambda *a, **k: object())
    monkeypatch.setattr("myagent.voice.pipeline.PhraseWake", lambda *a, **k: _Phrase())
    monkeypatch.setattr("myagent.voice.pipeline.create_synthesizer", lambda *a, **k: object())


class TestProfileIsTiedToThePhrase:
    """Verification compares recordings of the SAME words.

    A profile enrolled on "hey jarvis" cannot judge someone saying "hey
    friday" - it would reject the owner. Changing the wake phrase therefore
    invalidates the profile, and the safe response is to stop verifying, not
    to lock the owner out of their own assistant.
    """

    def test_the_profile_records_what_it_was_enrolled_on(self, tmp_path: Path) -> None:
        profile = speaker.enrol([another_take(DAVID, seed) for seed in range(4)], "hey jarvis")
        profile.save(tmp_path / "owner")

        loaded = speaker.VoiceProfile.load(tmp_path / "owner")
        assert loaded is not None
        assert loaded.phrase == "hey jarvis"

    def test_a_stale_profile_is_dropped_not_enforced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The owner changed their wake phrase; they must not be locked out."""
        from myagent.voice.config import VoiceSettings
        from myagent.voice.pipeline import Pipeline

        speaker.enrol([another_take(DAVID, seed) for seed in range(4)], "hey jarvis").save(
            speaker.profile_path(tmp_path)
        )

        _stub_pipeline(monkeypatch)

        settings = VoiceSettings(mode="wake")
        settings.wake.phrase = "hey friday"  # changed since enrolling
        settings.wake.only_my_voice = True
        settings.models_dir = tmp_path

        pipeline = Pipeline(settings)

        assert pipeline.voice_profile is None, "a stale profile must not reject the owner"

    def test_a_matching_profile_is_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from myagent.voice.config import VoiceSettings
        from myagent.voice.pipeline import Pipeline

        speaker.enrol([another_take(DAVID, seed) for seed in range(4)], "hey friday").save(
            speaker.profile_path(tmp_path)
        )

        _stub_pipeline(monkeypatch)

        settings = VoiceSettings(mode="wake")
        settings.wake.phrase = "hey friday"
        settings.wake.only_my_voice = True
        settings.models_dir = tmp_path

        assert Pipeline(settings).voice_profile is not None
