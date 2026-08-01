"""Environment doctor: verify the kernel's prerequisites end to end.

Usage:
    uv run python scripts/doctor.py                 # check config, keys, quotas
    uv run python scripts/doctor.py --ping          # additionally ping every provider
    uv run python scripts/doctor.py --set-key groq  # store an API key in the keyring

Keys are stored in the Windows Credential Manager (service "myagent") and are
never printed, logged, or written to disk.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import keyring

from myagent.config import load_settings
from myagent.db import connection, migrate
from myagent.gateway.client import KEYRING_SERVICE, ProviderClientPool
from myagent.gateway.quota import QuotaGovernor
from myagent.gateway.registry import Registry, load_registry
from myagent.gateway.types import ChatMessage

OK = "  [ok]"
BAD = "  [!!]"


def check_keys(registry: Registry) -> bool:
    """Report which provider credentials are present; True if all are."""
    print("credentials (Windows Credential Manager):")
    all_present = True
    seen: set[str] = set()
    for model in registry.all_models:
        if model.provider in seen:
            continue
        seen.add(model.provider)
        provider = registry.provider(model.provider)
        present = bool(keyring.get_password(KEYRING_SERVICE, provider.api_key_ref))
        marker = OK if present else BAD
        hint = "" if present else f"  -> set with: --set-key {provider.name}"
        print(f"{marker} {provider.name} ({provider.api_key_ref}){hint}")
        all_present = all_present and present
    return all_present


def show_quotas(registry: Registry, governor: QuotaGovernor) -> None:
    """Print configured limits and current consumption per model."""
    print("models and quota state:")
    for model in registry.all_models:
        usage = governor.usage(model)
        rendered = ", ".join(
            f"{window} {count}/{limit}" for window, (count, limit) in usage.items()
        )
        print(f"{OK} {model.key}: {rendered}")


async def ping_providers(registry: Registry) -> bool:
    """One tiny completion per provider; True if all respond."""
    print("provider pings (1-token completion):")
    pool = ProviderClientPool(registry)
    seen: set[str] = set()
    all_ok = True
    for model in registry.all_models:
        if model.provider in seen:
            continue
        seen.add(model.provider)
        try:
            usage: dict[str, int] = {}
            stream = pool.stream(
                model, [ChatMessage(role="user", content="Reply with the word: ok")], usage, 5
            )
            first = ""
            async for delta in stream:
                first += delta
                break
            print(f"{OK} {model.provider} responded via {model.key}")
        except Exception as exc:  # doctor reports, it doesn't crash
            print(f"{BAD} {model.provider}: {exc}")
            all_ok = False
    return all_ok


def set_key(registry: Registry, provider_name: str) -> None:
    """Prompt for a key (hidden input) and store it in the credential manager."""
    provider = registry.provider(provider_name)
    key = getpass.getpass(f"API key for {provider_name} (input hidden): ").strip()
    if not key:
        print("no key entered; nothing stored")
        return
    keyring.set_password(KEYRING_SERVICE, provider.api_key_ref, key)
    print(f"{OK} stored credential '{provider.api_key_ref}' for {provider_name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ping", action="store_true", help="ping each provider")
    parser.add_argument("--set-key", metavar="PROVIDER", help="store an API key")
    args = parser.parse_args()

    registry = load_registry()
    if args.set_key:
        set_key(registry, args.set_key)
        return 0

    settings = load_settings()
    print(f"data dir: {settings.app.resolved_data_dir()}")
    with connection(settings.db_path()) as conn:  # doctor may run before first boot
        migrate(conn)
    keys_ok = check_keys(registry)
    show_quotas(registry, QuotaGovernor(settings.db_path()))
    ping_ok = True
    if args.ping:
        # Ping every provider that has a credential; a missing key fails only
        # that provider's ping, not the whole check.
        ping_ok = asyncio.run(ping_providers(registry))
    return 0 if (keys_ok and ping_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
