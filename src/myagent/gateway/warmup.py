"""Warming the local model so the first real question is not the slow one.

Ollama loads a model into RAM on first use and unloads it after an idle
period. Measured on the reference laptop: 7.1 s cold versus ~1.5 s warm. A
tiny request at startup pays that cost before the user asks anything.

Deliberately *not* a keep-alive loop: holding ~2 GB resident forever on a
16 GB machine is a worse trade than one slow question after a long idle.
"""

from __future__ import annotations

import httpx

from myagent.gateway.registry import Registry
from myagent.logging import get_logger

log = get_logger(__name__)

WARMUP_TIMEOUT_S = 120.0


async def warm_local_models(registry: Registry) -> None:
    """Load each configured local model into memory, quietly.

    Failure is fine and expected when Ollama is not installed: the local tier
    simply stays unavailable and routing falls through to the cloud.
    """
    for model in registry.all_models:
        if not model.local:
            continue
        provider = registry.provider(model.provider)
        try:
            async with httpx.AsyncClient(timeout=WARMUP_TIMEOUT_S) as client:
                response = await client.post(
                    f"{provider.base_url.rstrip('/')}/chat/completions",
                    json={
                        "model": model.id,
                        "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 1,
                    },
                    headers={"Authorization": "Bearer local"},
                )
                response.raise_for_status()
            log.info("local_model_warm", model=model.key)
        except (TimeoutError, httpx.HTTPError) as exc:
            log.info("local_model_unavailable", model=model.key, error=str(exc)[:120])
