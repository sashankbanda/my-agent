"""Speaker verification: is the person saying the wake word actually you?

The muting hotkey solves "do not listen to the room" by hand. This solves the
other half: the assistant hears someone else say the wake word - the TV, a
housemate, a colleague across the desk - and should not answer them.

**Why a simple method is enough here.** General speaker recognition (any
speech, any length) needs a large neural embedding model and, in practice,
PyTorch - gigabytes of dependency for a laptop that already struggles. But
this problem is *text-dependent*: the audio being checked is always the same
short wake phrase, so the comparison is between recordings of the same words.
That is the easiest case in the field, and classical spectral features handle
it well without a single learned parameter.

Features are MFCCs computed here in numpy: mel filterbank, log, DCT - the
standard front end, about forty lines, no dependency beyond what the voice
stack already loads. An utterance becomes the mean and standard deviation of
its MFCCs over time, L2-normalized. Two recordings of the same phrase by the
same person land close together; a different voice does not.

**It is a filter, not a lock.** Voices vary with illness, distance, and
microphone, and the threshold is calibrated from a handful of samples. It is
set to prefer letting the owner through over shutting a stranger out, and it
gates *attention*, never permissions - the broker still decides what may
happen. Anyone with physical access to an unlocked machine has better options
than imitating a wake word.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from myagent.logging import get_logger
from myagent.voice.config import SAMPLE_RATE

log = get_logger(__name__)

# Standard speech-recognition front end: 25 ms windows every 10 ms.
FRAME_LENGTH = int(0.025 * SAMPLE_RATE)
FRAME_STEP = int(0.010 * SAMPLE_RATE)
FFT_SIZE = 512
MEL_BANDS = 26
CEPSTRA = 13  # keep the low quefrencies; the rest is mostly noise
LOW_HZ = 80.0
HIGH_HZ = 7600.0
PREEMPHASIS = 0.97

MIN_ENROLMENT_SAMPLES = 3
MIN_AUDIO_SAMPLES = int(0.25 * SAMPLE_RATE)
# How far below the owner's own consistency a match may fall. Enrolment
# measures how alike the owner's samples are to each other; a stranger has to
# clear that bar minus this slack. Generous on purpose - a false rejection is
# the assistant ignoring you, which is worse than a false accept it cannot act
# on without confirmation anyway.
THRESHOLD_SLACK = 0.06
MIN_THRESHOLD = 0.45


def _hz_to_mel(hz: float) -> float:
    return 2595.0 * np.log10(1.0 + hz / 700.0)


def _mel_to_hz(mel: float) -> float:
    return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)


def _mel_filterbank() -> np.ndarray:
    """Triangular mel filters over the FFT bins (computed once, reused)."""
    low_mel, high_mel = _hz_to_mel(LOW_HZ), _hz_to_mel(HIGH_HZ)
    points = np.linspace(low_mel, high_mel, MEL_BANDS + 2)
    bins = np.floor((FFT_SIZE + 1) * np.array([_mel_to_hz(m) for m in points]) / SAMPLE_RATE)
    filters = np.zeros((MEL_BANDS, FFT_SIZE // 2 + 1), dtype=np.float32)
    for band in range(MEL_BANDS):
        left, centre, right = int(bins[band]), int(bins[band + 1]), int(bins[band + 2])
        for bin_index in range(left, min(centre, filters.shape[1])):
            if centre > left:
                filters[band, bin_index] = (bin_index - left) / (centre - left)
        for bin_index in range(centre, min(right, filters.shape[1])):
            if right > centre:
                filters[band, bin_index] = (right - bin_index) / (right - centre)
    return filters


_FILTERBANK = _mel_filterbank()
# Orthonormal DCT-II matrix: turns the log-mel spectrum into cepstra.
_DCT = np.array(
    [
        [np.cos(np.pi * k * (2 * n + 1) / (2 * MEL_BANDS)) for n in range(MEL_BANDS)]
        for k in range(CEPSTRA)
    ],
    dtype=np.float32,
)


def mfcc(audio: np.ndarray) -> np.ndarray:
    """Mel-frequency cepstral coefficients, shape (frames, CEPSTRA)."""
    signal = np.append(audio[0], audio[1:] - PREEMPHASIS * audio[:-1]).astype(np.float32)
    frame_count = max(1, 1 + (len(signal) - FRAME_LENGTH) // FRAME_STEP)
    padded = np.pad(
        signal, (0, max(0, (frame_count - 1) * FRAME_STEP + FRAME_LENGTH - len(signal)))
    )
    indices = np.arange(FRAME_LENGTH)[None, :] + FRAME_STEP * np.arange(frame_count)[:, None]
    frames = padded[indices] * np.hamming(FRAME_LENGTH).astype(np.float32)
    power = np.abs(np.fft.rfft(frames, FFT_SIZE)) ** 2 / FFT_SIZE
    energies = np.maximum(power @ _FILTERBANK.T, 1e-10)
    return np.log(energies) @ _DCT.T


def embed(audio: np.ndarray) -> np.ndarray:
    """One vector describing the voice in this recording.

    Mean and standard deviation of the cepstra over time: the mean captures
    vocal-tract shape, the deviation how much it moves.

    **C0 is discarded**, and that single detail is the difference between
    working and not. C0 is overall log-energy, around -450 where every other
    coefficient sits within ±20, so it dominates the vector completely -
    measured, two obviously different voices scored 0.999 against each other
    with it included, and 0.85 without. Per-dimension scaling was tried on top
    and made separation *worse*, so it is not done.

    Cepstral mean subtraction is also deliberately skipped: it would remove
    exactly the timbre information that identifies a speaker.
    """
    if len(audio) < MIN_AUDIO_SAMPLES:
        raise ValueError("recording too short to identify a voice")
    coefficients = mfcc(audio)[:, 1:]  # drop C0 (energy)
    vector = np.concatenate([coefficients.mean(axis=0), coefficients.std(axis=0)])
    norm = float(np.linalg.norm(vector))
    return (vector / norm).astype(np.float32) if norm else vector.astype(np.float32)


def similarity(first: np.ndarray, second: np.ndarray) -> float:
    """Cosine similarity of two voice embeddings, clamped to 0-1."""
    return float(max(0.0, min(1.0, float(np.dot(first, second)))))


@dataclass
class VoiceProfile:
    """The enrolled owner: their samples' centroid and an accept threshold."""

    centroid: np.ndarray
    threshold: float
    samples: int
    phrase: str = ""

    def matches(self, audio: np.ndarray) -> tuple[bool, float]:
        """Is this the enrolled voice? Returns (accepted, score)."""
        try:
            score = similarity(embed(audio), self.centroid)
        except ValueError:
            return False, 0.0
        return score >= self.threshold, score

    def save(self, path: Path) -> None:
        """Persist beside the voice models."""
        path.parent.mkdir(parents=True, exist_ok=True)
        np.save(path.with_suffix(".npy"), self.centroid)
        path.with_suffix(".json").write_text(
            json.dumps(
                {"threshold": self.threshold, "samples": self.samples, "phrase": self.phrase},
                indent=2,
            ),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> VoiceProfile | None:
        """Read a saved profile, or None if the owner never enrolled."""
        vectors, meta = path.with_suffix(".npy"), path.with_suffix(".json")
        if not vectors.exists() or not meta.exists():
            return None
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            return cls(
                centroid=np.load(vectors),
                threshold=float(data["threshold"]),
                samples=int(data["samples"]),
                phrase=str(data.get("phrase", "")),
            )
        except (OSError, ValueError, KeyError) as exc:
            log.warning("voice_profile_unreadable", error=str(exc))
            return None


def enrol(recordings: list[np.ndarray], phrase: str = "") -> VoiceProfile:
    """Build a profile from several recordings of the same phrase.

    The threshold is *measured*, not guessed: how alike the owner's own
    samples are sets the bar, minus slack for a day when their voice is
    different. A voice that varies a lot enrols a lower bar automatically,
    which is the right behaviour - it is their voice that is inconsistent.
    """
    if len(recordings) < MIN_ENROLMENT_SAMPLES:
        raise ValueError(f"need at least {MIN_ENROLMENT_SAMPLES} recordings to enrol a voice")
    embeddings = [embed(audio) for audio in recordings]
    centroid = np.mean(embeddings, axis=0)
    norm = float(np.linalg.norm(centroid))
    if norm:
        centroid = centroid / norm

    # Leave-one-out: how well does a sample match a centroid built without it?
    # Using all samples would flatter the score, since each helped build it.
    consistencies = []
    for index, embedding in enumerate(embeddings):
        others = [vector for position, vector in enumerate(embeddings) if position != index]
        other_centroid = np.mean(others, axis=0)
        other_norm = float(np.linalg.norm(other_centroid))
        if other_norm:
            other_centroid = other_centroid / other_norm
        consistencies.append(similarity(embedding, other_centroid))

    threshold = max(MIN_THRESHOLD, min(consistencies) - THRESHOLD_SLACK)
    log.info(
        "voice_enrolled",
        samples=len(recordings),
        threshold=round(threshold, 3),
        consistency=round(float(np.mean(consistencies)), 3),
    )
    return VoiceProfile(
        centroid=centroid.astype(np.float32),
        threshold=threshold,
        samples=len(recordings),
        phrase=phrase,
    )


def profile_path(models_dir: Path) -> Path:
    """Where the owner's voice profile lives (no extension; two files)."""
    return models_dir / "speaker" / "owner"
