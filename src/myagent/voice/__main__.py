"""Voice satellite entrypoint: ``python -m myagent.voice``.

Options:
    --list-devices     print audio devices and exit
    --mic-check [SEC]  capture SEC seconds (default 15) and report what the
                       pipeline hears: input level, VAD probability, wake
                       score - the first thing to run when voice seems deaf
    --wake-test [SEC]  try a custom wake phrase live; --phrase "hey ev" to
                       test one without editing the config first
    --wake-tune        say several candidate phrases and rank them by how
                       reliably YOUR voice triggers each one
    --enrol [N]        record your voice N times (default 5) so the wake
                       word only works when YOU say it
    --config PATH      alternate voice.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import time
from pathlib import Path
from typing import cast

from myagent.logging import configure_logging, get_logger
from myagent.voice.config import load_voice_settings


def _report_wake_config(settings: object, models_dir: Path) -> None:
    """Say what the wake setting is, and whether it can possibly work.

    The first thing to check when voice is deaf is whether the configured wake
    word exists at all - a name with no model behind it crashed the satellite
    on startup, and this is the command people run to find out why.
    """
    from myagent.voice.wake import available_wake_words

    wake = settings.wake  # type: ignore[attr-defined]
    if wake.phrase:
        print(f"wake phrase: {wake.phrase!r} (custom, matched by transcription)")
        return
    available = available_wake_words(models_dir)
    if wake.model in available:
        print(f"wake word: {wake.model} (threshold {wake.threshold})")
        return
    print(
        f"\n  !! wake.model is {wake.model!r}, and there is no such model.\n"
        f"     A wake word is a trained model, not a label.\n"
        f"     Installed: {', '.join(available) or '(none)'}\n"
        f'     Either pick one of those, or set wake.phrase: "{wake.model.replace("_", " ")}"'
        f" for a custom one.\n"
    )


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
    _report_wake_config(settings, models_dir)
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


def wake_test(seconds: int, phrase: str | None) -> None:
    """Live check of a custom wake phrase: say it and see whether it triggers.

    The built-in wake models score 0.00 for some voices no matter how loud and
    clear the speech is - accent, pitch, and microphone all matter, and there
    is nothing to tune. This is the way out, so it needs a way to try a phrase
    and see the number, rather than editing config and hoping.
    """
    import sounddevice as sd

    from myagent.voice.audio import MicStream, probe_live_input
    from myagent.voice.stt import Transcriber
    from myagent.voice.vad import SileroVad, SpeechSegmenter
    from myagent.voice.wake import PhraseWake

    settings = load_voice_settings()
    chosen = phrase or settings.wake.phrase
    if not chosen:
        print(
            "No phrase to test. Either pass one:\n"
            '    uv run python -m myagent.voice --wake-test --phrase "hey ev"\n'
            "or set wake.phrase in config/voice.yaml."
        )
        return

    models_dir = settings.resolved_models_dir()
    matcher = PhraseWake(chosen, settings.wake.phrase_similarity)
    device: str | int | None = settings.input_device
    if device is None:
        device = probe_live_input()
    name = sd.query_devices(device if device is not None else None)["name"]
    print(f"input device: {name}")
    print(f"testing wake phrase: {chosen!r}  (needs {settings.wake.phrase_similarity} similarity)")
    print("Transcribed on this machine - nothing is uploaded.")
    print(f"\nSay it a few times, with pauses. Listening for {seconds}s...\n")

    stt = Transcriber(settings.stt.model_copy(update={"engine": "local"}), models_dir)
    vad = SileroVad(models_dir)
    segmenter = SpeechSegmenter(settings.vad)
    mic = MicStream(device)
    mic.start()
    heard = 0
    woke = 0
    try:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            frame = mic.get(timeout=1.0)
            if frame is None:
                continue
            utterance = segmenter.feed(frame, vad(frame))
            if utterance is None:
                continue
            vad.reset()
            text = stt.transcribe(utterance.audio)
            if not text:
                continue
            heard += 1
            matched, remainder = matcher.check(text)
            score = matcher.best_similarity(text)
            woke += matched
            mark = "WOKE " if matched else "  -  "
            extra = f"   -> request: {remainder!r}" if remainder else ""
            print(f"  {mark} heard {text!r:38} similarity {score:4.2f}{extra}")
    finally:
        mic.stop()

    print(f"\n{woke} of {heard} utterances woke it.")
    if heard and not woke:
        print(
            "  Nothing matched. Lower wake.phrase_similarity (try 0.6), or pick a\n"
            "  phrase that transcribes more reliably - look at what it heard above."
        )
    elif woke:
        print(f'  Working. Put this in config/voice.yaml:\n    wake:\n      phrase: "{chosen}"')


def _record_once(mic: object, vad: object, segmenter: object, timeout: float) -> object:
    """Wait for one complete utterance, or None if the user said nothing."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame = mic.get(timeout=1.0)  # type: ignore[attr-defined]
        if frame is None:
            continue
        utterance = segmenter.feed(frame, vad(frame))  # type: ignore[operator,attr-defined]
        if utterance is not None:
            vad.reset()  # type: ignore[attr-defined]
            return utterance
    return None


def wake_tune(candidates: list[str], repeats: int) -> None:
    """Say each candidate a few times; rank them by how well YOUR voice lands.

    The built-in models scored 0.00 for this user - accent is the variable
    that matters, and no amount of reasoning about phonetics substitutes for
    measuring it. This is the measurement.
    """
    import sounddevice as sd

    from myagent.voice.audio import MicStream, probe_live_input
    from myagent.voice.stt import Transcriber
    from myagent.voice.tuning import PhraseScore, control_ceiling, rank, recommend
    from myagent.voice.vad import SileroVad, SpeechSegmenter
    from myagent.voice.wake import PhraseWake

    settings = load_voice_settings()
    models_dir = settings.resolved_models_dir()
    device: str | int | None = settings.input_device
    if device is None:
        device = probe_live_input()
    print(f"input device: {sd.query_devices(device if device is not None else None)['name']}")
    print(f"\nTesting {len(candidates)} wake phrases, {repeats} times each.")
    print("Say each one clearly when prompted, then pause. Nothing is uploaded.\n")

    stt = Transcriber(settings.stt.model_copy(update={"engine": "local"}), models_dir)
    vad = SileroVad(models_dir)
    segmenter = SpeechSegmenter(settings.vad)
    mic = MicStream(device)
    mic.start()
    scores: list[PhraseScore] = []
    try:
        for phrase in candidates:
            score = PhraseScore(phrase=phrase, best_control=control_ceiling(phrase))
            matcher = PhraseWake(phrase, settings.wake.phrase_similarity)
            for attempt in range(repeats):
                print(f'  say "{phrase}"  ({attempt + 1}/{repeats}) ... ', end="", flush=True)
                segmenter.reset()
                utterance = _record_once(mic, vad, segmenter, timeout=8.0)
                if utterance is None:
                    print("nothing heard")
                    score.attempts += 1
                    continue
                text = stt.transcribe(utterance.audio)  # type: ignore[attr-defined]
                score.attempts += 1
                score.heard.append(text)
                matched, _ = matcher.check(text)
                score.hits += matched
                mark = "OK " if matched else "no "
                print(f"{mark} heard {text!r} ({matcher.best_similarity(text):.2f})")
            scores.append(score)
            print()
    finally:
        mic.stop()

    print(f"\n{'phrase':18} {'recognised':>11} {'avg score':>10} {'margin':>7}  verdict")
    for score in rank(scores):
        print(
            f"{score.phrase:18} {score.hits:>5}/{score.attempts:<5} "
            f"{score.mean_similarity:>10.2f} {score.margin:>7.2f}  {score.verdict}"
        )
    print(f"\n{recommend(scores)}")


def enrol_voice(samples: int) -> None:
    """Record the wake phrase a few times and remember whose voice it is.

    Enrolment is text-dependent on purpose: it records the same phrase the
    assistant will later hear, which is the easiest case for verification and
    the reason a small classical method is enough here.
    """
    import sounddevice as sd

    from myagent.voice import speaker
    from myagent.voice.audio import MicStream, probe_live_input
    from myagent.voice.vad import SileroVad, SpeechSegmenter

    settings = load_voice_settings()
    models_dir = settings.resolved_models_dir()
    phrase = settings.wake.phrase or settings.wake.model.replace("_", " ")
    # Verification compares recordings of the same words, so the profile is
    # tied to the phrase. Enrolling first and changing the wake phrase after
    # would quietly invalidate it, so say the order out loud.
    print(f'Enrolling for the wake phrase currently configured: "{phrase}"')
    print("If you plan to change it, change it FIRST - a profile only judges")
    print("the phrase it was recorded on.\n")
    device: str | int | None = settings.input_device
    if device is None:
        device = probe_live_input()
    print(f"input device: {sd.query_devices(device if device is not None else None)['name']}")
    print(f'\nSay "{phrase}" {samples} times, pausing between each.')
    print("Speak normally - how you will actually say it, from where you usually sit.\n")

    vad = SileroVad(models_dir)
    segmenter = SpeechSegmenter(settings.vad)
    mic = MicStream(device)
    mic.start()
    recordings = []
    try:
        while len(recordings) < samples:
            print(f'  say "{phrase}"  ({len(recordings) + 1}/{samples}) ... ', end="", flush=True)
            segmenter.reset()
            utterance = _record_once(mic, vad, segmenter, timeout=10.0)
            if utterance is None:
                print("nothing heard, try again")
                continue
            recordings.append(utterance.audio)  # type: ignore[attr-defined]
            print(f"got it ({utterance.duration_s:.1f}s)")  # type: ignore[attr-defined]
    except KeyboardInterrupt:
        print("\ncancelled")
        return
    finally:
        mic.stop()

    try:
        profile = speaker.enrol(recordings, phrase=phrase)
    except ValueError as exc:
        print(f"\nCould not enrol: {exc}")
        return
    profile.save(speaker.profile_path(models_dir))

    # Say how well it separated the samples, so the number is not a mystery.
    scores = [profile.matches(audio)[1] for audio in recordings]
    print(f"\nEnrolled from {profile.samples} recordings.")
    print(f"  your samples scored {min(scores):.2f}-{max(scores):.2f} against each other")
    print(f"  accept threshold set to {profile.threshold:.2f}")
    print("\nTurn it on in config/voice.yaml:")
    print("  wake:\n    only_my_voice: true")
    print(
        "\nThis is a filter, not a lock: it gates attention only, and every "
        "action\nstill goes through the permission broker."
    )


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
    parser.add_argument(
        "--wake-test",
        nargs="?",
        const=30,
        type=int,
        default=None,
        metavar="SEC",
        help="say a custom wake phrase and see whether it triggers",
    )
    parser.add_argument("--phrase", default=None, help='wake phrase to test, e.g. "hey ev"')
    parser.add_argument(
        "--wake-tune",
        action="store_true",
        help="try several wake phrases and rank them for your voice",
    )
    parser.add_argument("--repeats", type=int, default=3, help="attempts per phrase when tuning")
    parser.add_argument(
        "--enrol",
        "--enroll",
        dest="enrol",
        nargs="?",
        const=5,
        type=int,
        default=None,
        metavar="N",
        help="record your voice N times so it can tell you from other people",
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

    if args.wake_test is not None:
        wake_test(args.wake_test, args.phrase)
        return

    if args.enrol is not None:
        enrol_voice(args.enrol)
        return

    if args.wake_tune:
        from myagent.voice.tuning import DEFAULT_CANDIDATES

        chosen = [args.phrase] if args.phrase else list(DEFAULT_CANDIDATES)
        wake_tune(chosen, args.repeats)
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
