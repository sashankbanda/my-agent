"""Set up the on-device model tier (Ollama).

    uv run python scripts/setup_local_model.py            # install check + pull
    uv run python scripts/setup_local_model.py --bench     # also measure speed

What this gives you:
  * easy turns answered on this machine: no tokens, no network, no rate limits
  * secrets stay private: local_only prompts are served here or not at all
  * an offline fallback when every cloud provider is exhausted or unreachable

Ollama itself is installed with: winget install Ollama.Ollama
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx
import keyring

from myagent.gateway.client import KEYRING_SERVICE
from myagent.gateway.registry import load_registry

OLLAMA_URL = "http://127.0.0.1:11434"
BENCH_PROMPT = "In one short sentence, what is a good reason to go for a walk?"


def local_models_from_registry() -> list[str]:
    """Model ids the registry expects to find on the local provider."""
    registry = load_registry()
    return [model.id for model in registry.all_models if model.local]


def server_running() -> bool:
    try:
        httpx.get(f"{OLLAMA_URL}/api/tags", timeout=3).raise_for_status()
        return True
    except httpx.HTTPError:
        return False


def installed_models() -> list[str]:
    response = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=10)
    response.raise_for_status()
    return [entry["name"] for entry in response.json().get("models", [])]


def pull(model: str) -> bool:
    """Download a model, streaming progress to the console."""
    print(f"pulling {model} (a few hundred MB; one time)...")
    process = subprocess.run(["ollama", "pull", model], check=False)
    return process.returncode == 0


def benchmark(model: str) -> None:
    """Measure first-token and total latency the way a turn would feel."""
    print(f"\nbenchmarking {model}...")
    started = time.perf_counter()
    first_token = None
    text = ""
    with httpx.stream(
        "POST",
        f"{OLLAMA_URL}/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": BENCH_PROMPT}],
            "stream": True,
            "max_tokens": 60,
        },
        timeout=120,
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if not line.startswith("data: ") or line.endswith("[DONE]"):
                continue
            import json

            chunk = json.loads(line[6:])
            delta = chunk["choices"][0]["delta"].get("content", "")
            if delta and first_token is None:
                first_token = time.perf_counter() - started
            text += delta
    total = time.perf_counter() - started
    words = max(1, len(text.split()))
    print(f"  first token: {(first_token or total) * 1000:.0f} ms")
    print(f"  full answer: {total * 1000:.0f} ms for ~{words} words")
    print(f"  answer: {text.strip()[:160]}")
    if total > 12:
        print("  NOTE: slow on this machine. Consider a 1.5b model:")
        print("        ollama pull qwen2.5:1.5b-instruct-q4_K_M")
        print("        then update config/providers.yaml to match.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bench", action="store_true", help="measure speed after setup")
    args = parser.parse_args()

    if not server_running():
        print("Ollama is not responding on 127.0.0.1:11434.")
        print("Install it with:  winget install Ollama.Ollama")
        print("It normally starts automatically; otherwise run:  ollama serve")
        return 1
    print("ollama: running")

    # The OpenAI client requires *some* credential; Ollama ignores its value.
    if not keyring.get_password(KEYRING_SERVICE, "ollama_api_key"):
        keyring.set_password(KEYRING_SERVICE, "ollama_api_key", "local")
        print("stored placeholder credential for the local provider")

    wanted = local_models_from_registry()
    if not wanted:
        print("no local models are declared in config/providers.yaml")
        return 1

    have = installed_models()
    for model in wanted:
        if any(name.startswith(model.split(":")[0]) and model in name for name in have):
            print(f"  [ok  ] {model} already present")
        elif not pull(model):
            print(f"  [!!] failed to pull {model}")
            return 1
        if args.bench:
            benchmark(model)

    print("\nlocal tier ready. Easy questions will now run on this machine.")
    print("Disable with tools.local_tier: false in config/default.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
