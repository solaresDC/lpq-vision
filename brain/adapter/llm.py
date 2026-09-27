"""The LLM adapter: ONE LiteLLM library call per job attempt, with a timeout, nothing else.

No internal retries of any kind: a transport error, a timeout or a provider rejection
fails the ATTEMPT and propagates; the queue's run_after is the only retry mechanism in
the system (never-sleeps law). Prompt caching: when config cache is "on" and the call is
Anthropic-bound, the LAST block of the stable prefix [system prompt + menu reference]
carries cache_control ephemeral (5m); the plate photo travels after the mark, never
cached. A discount, never a dependency: below-minimum prefixes are processed at full
price silently, and the counters are logged on every call so the evidence accumulates
alone. The prefix is never padded to chase the discount.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import Any

import litellm

from brain.worker.menu_builder import MenuReference

log = logging.getLogger("lpq.adapter.llm")

# --- named constants (SPEC section 4) -------------------------------------------------
LLM_TIMEOUT_S = 120          # one attempt may take at most this; the worker asserts it < ORPHAN_TIMEOUT
MAX_TOKENS = 1024
TEMPERATURE = 0
CACHE_TTL = "5m"             # the ephemeral mark's TTL (provider default for ephemeral); 1h is on hold
ANTHROPIC_PROVIDER = "anthropic"

# The bounded re-ask (validator contract step): a deliberate SECOND call inside the same
# attempt when the first answer is not parseable. Not a transport retry.
REPAIR_INSTRUCTION = (
    "Tu respuesta anterior no fue un JSON valido. Responde UNICAMENTE con el objeto JSON "
    "del contrato, sin texto antes ni despues, sin bloques de codigo."
)
PLATE_INSTRUCTION = "Analiza este plato (la ultima imagen) y responde con el JSON."

litellm.suppress_debug_info = True


@dataclass(frozen=True)
class LLMResult:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    latency_ms: int

    def usage(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
        }


def provider_for(model: str) -> str | None:
    """Claude models are Anthropic-bound (told to LiteLLM explicitly, so it never needs to
    recognize the tag in advance). Anything else is left to LiteLLM's own routing and gets
    no cache mark (fail-safe)."""
    return ANTHROPIC_PROVIDER if model.startswith("claude") else None


def _image_block(jpeg: bytes) -> dict[str, Any]:
    encoded = base64.b64encode(jpeg).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}}


def build_messages(
    system_prompt: str,
    reference: MenuReference,
    plate_jpeg: bytes,
    cache_on: bool,
    provider: str | None,
) -> list[dict[str, Any]]:
    """[system] + [site reference photos..., reference TEXT (cache mark), plate photo, instruction].

    The reference text is always the last block of the stable prefix, so the mark always
    lands on it regardless of how many reference photos precede it.
    """
    reference_block: dict[str, Any] = {"type": "text", "text": reference.text}
    if cache_on and provider == ANTHROPIC_PROVIDER:
        reference_block["cache_control"] = {"type": "ephemeral"}

    user_content: list[dict[str, Any]] = [_image_block(p.jpeg) for p in reference.photos]
    user_content.append(reference_block)
    user_content.append(_image_block(plate_jpeg))
    user_content.append({"type": "text", "text": PLATE_INSTRUCTION})

    return [
        {"role": "system", "content": [{"type": "text", "text": system_prompt}]},
        {"role": "user", "content": user_content},
    ]


def build_repair_messages(messages: list[dict[str, Any]], bad_text: str) -> list[dict[str, Any]]:
    """The same conversation plus the bad answer and the re-ask; the cached prefix is reused."""
    return messages + [
        {"role": "assistant", "content": bad_text if bad_text.strip() else "(respuesta vacia)"},
        {"role": "user", "content": REPAIR_INSTRUCTION},
    ]


def _usage_int(usage: Any, *paths: str) -> int:
    """Read the first available counter from a LiteLLM usage object, by dotted path; 0 if absent."""
    for path in paths:
        node = usage
        for part in path.split("."):
            if node is None:
                break
            node = node.get(part) if isinstance(node, dict) else getattr(node, part, None)
        if isinstance(node, int) and not isinstance(node, bool):
            return node
    return 0


def complete(model: str, messages: list[dict[str, Any]], provider: str | None) -> LLMResult:
    """ONE call, ONE timeout. Any exception propagates: the caller fails the attempt."""
    started = time.monotonic()
    response = litellm.completion(
        model=model,
        messages=messages,
        max_tokens=MAX_TOKENS,
        temperature=TEMPERATURE,
        timeout=LLM_TIMEOUT_S,
        num_retries=0,                      # the queue is the only retry mechanism
        custom_llm_provider=provider,
    )
    latency_ms = int((time.monotonic() - started) * 1000)

    content = response.choices[0].message.content
    text = content if isinstance(content, str) else ""
    usage = getattr(response, "usage", None)
    result = LLMResult(
        text=text,
        model=model,
        prompt_tokens=_usage_int(usage, "prompt_tokens"),
        completion_tokens=_usage_int(usage, "completion_tokens"),
        cache_creation_input_tokens=_usage_int(usage, "cache_creation_input_tokens"),
        cache_read_input_tokens=_usage_int(
            usage, "cache_read_input_tokens", "prompt_tokens_details.cached_tokens"
        ),
        latency_ms=latency_ms,
    )
    log.info(
        "llm call model=%s provider=%s latency_ms=%d prompt_tokens=%d completion_tokens=%d "
        "cache_creation_input_tokens=%d cache_read_input_tokens=%d",
        model, provider, latency_ms, result.prompt_tokens, result.completion_tokens,
        result.cache_creation_input_tokens, result.cache_read_input_tokens,
    )
    return result
