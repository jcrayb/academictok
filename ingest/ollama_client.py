"""Thin Ollama client that returns parsed JSON from the local LLM."""
import base64
import json
import logging
import re

import requests

import config

logger = logging.getLogger(__name__)


class LLMDisabledError(RuntimeError):
    """Raised when an LLM call is attempted with config.LLM_ENABLED off."""


# Ollama occasionally hands back ``"done": false`` from a *non-streaming*
# call — the model stopped (an early EOS) before actually finishing, most
# often on longer generations under `format: json` + `think: false` on
# reasoning-tuned models (e.g. qwen3.5). The response is then a truncated,
# unparseable fragment. This is a flaky-generation issue, not a malformed
# prompt, so a plain retry on the same request usually succeeds.
_MAX_GENERATE_ATTEMPTS = 3


def _post_generate_once(payload: dict) -> dict:
    """A single /api/generate call. Raises RuntimeError if Ollama reports the
    response as incomplete (``done: false``) — an early-EOS truncation — so
    callers can retry the same request."""
    resp = requests.post(
        f"{config.OLLAMA_HOST}/api/generate", json=payload, timeout=300
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("done", True) is False:
        raise RuntimeError("Ollama returned an incomplete (done=false) response")
    return data


def generate_text(prompt, system="", images=None, model=None, temperature=0.2):
    """Plain-text completion, optionally multimodal.

    ``images`` is a list of raw image bytes (passed to a vision-capable model).
    ``model`` overrides config.OLLAMA_MODEL (used for VISION_MODEL captioning).
    """
    if not config.LLM_ENABLED:
        raise LLMDisabledError("LLM features are disabled (LLM_ENABLED=false)")
    payload = {
        "model": model or config.OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {"temperature": temperature, "num_ctx": config.OLLAMA_NUM_CTX},
    }
    if system:
        payload["system"] = system
    if images:
        payload["images"] = [base64.b64encode(b).decode("ascii") for b in images]

    last_error = None
    for attempt in range(1, _MAX_GENERATE_ATTEMPTS + 1):
        try:
            data = _post_generate_once(payload)
        except (requests.RequestException, RuntimeError) as e:
            last_error = e
            logger.warning("Ollama generate failed (attempt %d/%d): %s",
                           attempt, _MAX_GENERATE_ATTEMPTS, e)
            continue
        return data.get("response", "").strip()
    raise last_error


def generate_json(prompt: str, system: str = "", temperature: float = 0.2,
                  model: str | None = None) -> dict:
    """Call Ollama with JSON-formatted output and return the parsed object.

    Uses Ollama's `format: json` mode for reliable structured output. Falls back
    to extracting the first {...} block if the response isn't clean JSON.
    ``model`` overrides config.OLLAMA_MODEL (e.g. the fast discovery model).
    """
    if not config.LLM_ENABLED:
        raise LLMDisabledError("LLM features are disabled (LLM_ENABLED=false)")
    payload = {
        "model": model or config.OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        # Disable "thinking" mode: reasoning models (e.g. qwen3) otherwise spend
        # their token budget on hidden reasoning and return an empty response.
        "think": False,
        "options": {"temperature": temperature, "num_ctx": config.OLLAMA_NUM_CTX},
    }
    if system:
        payload["system"] = system

    last_error = None
    for attempt in range(1, _MAX_GENERATE_ATTEMPTS + 1):
        try:
            data = _post_generate_once(payload)
        except (requests.RequestException, RuntimeError) as e:
            last_error = e
            logger.warning("Ollama generate failed (attempt %d/%d): %s",
                           attempt, _MAX_GENERATE_ATTEMPTS, e)
            continue
        text = data.get("response", "").strip()
        try:
            return _parse_json(text)
        except ValueError as e:
            last_error = e
            logger.warning("Unparseable JSON from LLM (attempt %d/%d), retrying",
                           attempt, _MAX_GENERATE_ATTEMPTS)
    raise last_error


def _parse_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        logger.warning("Could not parse JSON from LLM response: %s", text[:200])
        raise ValueError("LLM did not return valid JSON")
