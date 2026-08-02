"""Wake-word detection: openWakeWord over ONNX, always local.

The detector consumes 80 ms chunks (1280 samples @ 16 kHz) and reports when
the configured wake word crosses its threshold. A short refractory period
prevents one spoken wake word from triggering repeatedly.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import cast

import numpy as np

from myagent.logging import get_logger
from myagent.voice.config import SAMPLE_RATE, WakeSettings

log = get_logger(__name__)

WAKE_CHUNK_SAMPLES = 1280  # 80 ms - openWakeWord's expected feed size
REFRACTORY_S = 2.0


# Non-wake helper models that openWakeWord's downloader places alongside the
# wake models; never load these as wake words.
_HELPER_MODEL_MARKERS = ("melspectrogram", "embedding", "silero_vad")


def is_wake_model_file(path: Path) -> bool:
    """True for an actual wake-word model file (not a feature/VAD helper)."""
    return path.suffix == ".onnx" and not any(
        marker in path.name for marker in _HELPER_MODEL_MARKERS
    )


def wake_model_files(wake_dir: Path) -> list[Path]:
    """All downloaded wake-word models in a directory."""
    if not wake_dir.exists():
        return []
    return [path for path in sorted(wake_dir.glob("*.onnx")) if is_wake_model_file(path)]


def available_wake_words(models_dir: Path) -> list[str]:
    """Wake words this machine can actually detect, by spoken name.

    File stems carry a version suffix ("hey_jarvis_v0.1"); the name people put
    in the config file does not.
    """
    names = []
    for path in wake_model_files(models_dir / "openwakeword"):
        names.append(re.sub(r"_v\d+(\.\d+)*$", "", path.stem))
    return sorted(set(names))


class UnknownWakeWordError(ValueError):
    """The configured wake word has no model on this machine.

    Worth its own type because the fix is a specific one-line config edit, and
    the message has to say which words are actually available - a wake word is
    a trained model, not a label, so it cannot be conjured by renaming.
    """


def resolve_wake_model(models_dir: Path, name: str) -> str:
    """Path of a downloaded wake model for ``name``.

    Raises rather than passing an unknown name through to openWakeWord, whose
    own error ("Could not find pretrained model") does not say what *would*
    work - and which crashed the satellite into a restart loop while the HUD
    just showed "voice off".
    """
    for candidate in wake_model_files(models_dir / "openwakeword"):
        if candidate.name.startswith(name):
            return str(candidate)
    available = available_wake_words(models_dir)
    raise UnknownWakeWordError(
        f"there is no wake-word model called {name!r} on this machine. A wake "
        f"word is a trained model, not a label, so renaming one does not "
        f"create it. Available: {', '.join(available) or '(none downloaded)'}. "
        f"Set wake.model in config/voice.yaml to one of those, or set "
        f"wake.phrase to any words you like for a custom one."
    )


class WakeDetector:
    """Streaming wake-word scorer."""

    def __init__(self, settings: WakeSettings, models_dir: Path) -> None:
        from openwakeword.model import Model

        self._threshold = settings.threshold
        wake_dir = models_dir / "openwakeword"
        melspec = wake_dir / "melspectrogram.onnx"
        embedding = wake_dir / "embedding_model.onnx"
        if not melspec.exists() or not embedding.exists():
            raise FileNotFoundError(
                f"openWakeWord feature models missing in {wake_dir} - run: "
                "uv run python scripts/setup_voice.py"
            )
        self._model = Model(
            wakeword_models=[resolve_wake_model(models_dir, settings.model)],
            inference_framework="onnx",
            melspec_model_path=str(melspec),
            embedding_model_path=str(embedding),
        )
        self._refractory_until = 0.0
        self._clock_s = 0.0
        self.last_score = 0.0  # most recent chunk's score (mic-check tuning aid)

    def reset(self) -> None:
        """Clear streaming feature buffers (call after handling a wake-up)."""
        self._model.reset()

    def process(self, chunk: np.ndarray) -> bool:
        """Feed one 80 ms chunk (float32 [-1,1]); True on wake-word detection."""
        if chunk.shape != (WAKE_CHUNK_SAMPLES,):
            raise ValueError(f"expected {WAKE_CHUNK_SAMPLES} samples, got {chunk.shape}")
        self._clock_s += WAKE_CHUNK_SAMPLES / SAMPLE_RATE
        # openWakeWord expects int16-range samples.
        pcm16 = (chunk * 32767.0).astype(np.int16)
        # predict()'s stub type is imprecise; at runtime it returns {name: score}.
        # The key is the model FILE stem (e.g. "hey_jarvis_v0.1"), not the
        # configured name - and exactly one model is loaded, so take the max.
        scores = cast("dict[str, float]", self._model.predict(pcm16))
        score = max((float(value) for value in scores.values()), default=0.0)
        self.last_score = score
        if score >= self._threshold and self._clock_s >= self._refractory_until:
            self._refractory_until = self._clock_s + REFRACTORY_S
            return True
        return False


# -- custom wake phrases ------------------------------------------------------

# Speech-to-text mangles short phrases badly - "hey ev" comes back as "hey f",
# "heyev", "hey Eb", "Hey, EV." - so matching is fuzzy and works on a
# normalized form. Anything stricter rejects the user's own wake word.
# Apostrophes are kept so "what's" stays one word. That matters beyond
# tidiness: the remainder of the utterance becomes the user's request, and
# splitting it into "what s" would hand the model damaged text.
_PUNCTUATION = re.compile(r"[^\w\s'\N{RIGHT SINGLE QUOTATION MARK}]+")

# Words that only carry the phrase; the word *after* them is what identifies it.
_CARRIER_WORDS = frozenset({"hey", "ok", "okay", "hi", "hello", "yo"})
# Measured against real transcription: "ev" (2) scores 0.67 against itself and
# 0.67 against unrelated speech - useless. "eva" (3) transcribes exactly.
MIN_DISTINCTIVE_CHARS = 3


def normalize_phrase(text: str) -> str:
    """Lowercase, drop punctuation between words, collapse whitespace."""
    return " ".join(_PUNCTUATION.sub(" ", text.lower()).split())


class PhraseWake:
    """A wake word of your own, recognized by transcription rather than a model.

    openWakeWord only knows the handful of words it was trained on, and
    training a new one needs hours of GPU time - so a custom wake word has to
    come from somewhere else. Since the pipeline already transcribes speech,
    it can transcribe the short bursts that arrive while idle and check
    whether they were addressed to the assistant.

    The trade is explicit: this costs a local transcription per speech burst
    (roughly 100-300 ms of CPU) where the model-based path costs almost
    nothing. In exchange the wake word is whatever you want, and one useful
    thing falls out for free - because the whole utterance is transcribed,
    "hey ev what's the time" wakes it *and* carries the request, instead of
    needing a pause between the two.
    """

    def __init__(self, phrase: str, similarity: float = 0.72) -> None:
        self.phrase = normalize_phrase(phrase)
        if not self.phrase:
            raise ValueError("wake.phrase is empty")
        self._words = self.phrase.split()
        self._similarity = similarity
        if self.is_risky():
            log.warning(
                "wake_phrase_may_be_unreliable",
                phrase=self.phrase,
                why="the distinctive part is one short syllable",
            )

    def is_risky(self) -> bool:
        """True when this phrase is too short to survive transcription.

        Measured: "hey ev" comes back as "Hey, love" / "Hey of" / "Hey have",
        scoring 0.67 against itself and 0.67 against unrelated speech - no
        separation at all. One extra vowel fixes it: "hey eva" transcribes
        exactly, every time. The distinctive word needs enough sound to be
        heard as itself.
        """
        distinctive = [word for word in self._words if word not in _CARRIER_WORDS]
        if not distinctive:
            return True
        return max(len(word) for word in distinctive) < MIN_DISTINCTIVE_CHARS

    def check(self, text: str) -> tuple[bool, str]:
        """Was this addressed to the assistant, and what remains of it?

        Returns ``(woke, remainder)``. The remainder is whatever followed the
        wake phrase, so it can be handled as the request in the same breath.
        """
        spoken = normalize_phrase(text)
        if not spoken:
            return False, ""
        words = spoken.split()

        # The phrase leads the utterance: the common case, and the only one
        # where the rest is meant as a request.
        window = len(self._words)
        if len(words) >= window:
            head = " ".join(words[:window])
            if self._matches(head):
                # Take the remainder from the ORIGINAL text, so the request
                # keeps its punctuation and capitalisation rather than being
                # handed to the model in normalized form.
                original = text.split()
                remainder = (
                    " ".join(original[window:]).strip(" ,.")
                    if len(original) == len(words)
                    else " ".join(words[window:])
                )
                return True, remainder
        # A bare wake word that STT split oddly ("hey" / "ev" as one blob), or
        # the phrase sitting alone in a longer mishearing.
        if len(words) <= window + 1 and self._matches(spoken):
            return True, ""
        return False, ""

    def similarity(self, candidate: str) -> float:
        """How close a transcription is to the phrase (0-1).

        Exposed so ``--wake-test`` can show the number next to the threshold:
        "heard 0.64, need 0.72" is a tunable answer, "it didn't work" is not.
        """
        return SequenceMatcher(None, normalize_phrase(candidate), self.phrase).ratio()

    def best_similarity(self, text: str) -> float:
        """The score the leading words of ``text`` achieve against the phrase."""
        words = normalize_phrase(text).split()
        window = len(self._words)
        head = " ".join(words[:window]) if len(words) >= window else " ".join(words)
        return max(self.similarity(head), self.similarity(text))

    def _matches(self, candidate: str) -> bool:
        if candidate == self.phrase:
            return True
        return SequenceMatcher(None, candidate, self.phrase).ratio() >= self._similarity
