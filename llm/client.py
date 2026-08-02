"""
Thin LLM wrapper. Everything else in the codebase calls LLMClient.generate_json()
and doesn't care which provider is behind it. Default provider is Gemini's free
tier (matches the technical design's cost-control requirement); swap in
OpenAI/Ollama here later without touching any calling code.
"""

import json
import re
import time
from typing import Any

import requests
from json_repair import repair_json

from config.settings import get_settings
from utils.logger import get_logger

logger = get_logger("jobapplypro.llm")

GEMINI_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)


class LLMError(RuntimeError):
    """Raised when the LLM call fails or returns unparseable output after retries."""


class LLMClient:
    def __init__(self, api_key: str | None = None, model: str | None = None):
        settings = get_settings()
        self.api_key = api_key or settings.gemini_api_key
        self.model = model or settings.gemini_model
        self._max_retries = settings.llm_max_retries
        if not self.api_key:
            logger.warning(
                "No GEMINI_API_KEY set. LLMClient will raise on any generate_json call "
                "until one is configured in .env."
            )

    def generate_json(
        self,
        prompt: str,
        max_retries: int | None = None,
    ) -> dict[str, Any]:
        """
        Sends `prompt` to the model and parses the response as JSON.
        Retries with the parse error fed back into the prompt if the model
        returns malformed JSON or wraps it in markdown fences.
        """
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY is not configured. Set it in .env.")

        max_retries = max_retries if max_retries is not None else self._max_retries
        last_error: Exception | None = None
        current_prompt = prompt

        for attempt in range(1, max_retries + 2):  # first try + retries
            try:
                raw_text = self._call_gemini(current_prompt)
                return self._parse_json(raw_text)
            except (LLMError, json.JSONDecodeError, requests.RequestException, ValueError) as exc:
                last_error = exc
                logger.warning("LLM call attempt %d failed: %s", attempt, exc)
                current_prompt = (
                    f"{prompt}\n\n"
                    f"Your previous response could not be parsed as valid JSON "
                    f"(error: {exc}). Respond again with ONLY valid JSON, no markdown "
                    f"fences, no commentary, no trailing commas, and commas between "
                    f"every property/array element."
                )
                time.sleep(min(2 ** attempt, 8))

        raise LLMError(f"LLM call failed after {max_retries + 1} attempts: {last_error}")

    def _call_gemini(self, prompt: str) -> str:
        url = GEMINI_URL_TEMPLATE.format(model=self.model)
        response = requests.post(
            url,
            params={"key": self.api_key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                # Ask Gemini for JSON directly — cuts down on markdown fences / prose.
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "temperature": 0.2,
                },
            },
            timeout=90,
        )
        response.raise_for_status()
        data = response.json()
        try:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"Unexpected Gemini response shape: {data}") from exc

    @staticmethod
    def _parse_json(raw_text: str) -> dict[str, Any]:
        """
        Parse model output as a JSON object, tolerating common LLM slip-ups:
        markdown fences, leading/trailing prose, trailing/missing commas, etc.
        """
        text = raw_text.strip()
        if not text:
            raise json.JSONDecodeError("Empty LLM response", text, 0)

        # Strip ```json ... ``` fences (including mid-response variants).
        fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, flags=re.IGNORECASE)
        if fence:
            text = fence.group(1).strip()

        # Keep the outermost object if the model added commentary around it.
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise json.JSONDecodeError("No JSON object found in LLM response", text, 0)
        text = text[start : end + 1]

        # Trailing commas before } or ] are a frequent Gemini failure mode.
        text = re.sub(r",(\s*[}\]])", r"\1", text)

        # Normalize curly/smart quotes that break json.loads.
        text = (
            text.replace("\u201c", '"')
            .replace("\u201d", '"')
            .replace("\u2018", "'")
            .replace("\u2019", "'")
        )

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            # Gemini often drops commas between properties (e.g. after skills]).
            # json_repair fixes missing/trailing commas and similar LLM artifacts.
            logger.info(
                "Strict JSON parse failed (%s); attempting repair around: %r",
                exc,
                text[max(0, exc.pos - 40) : exc.pos + 40],
            )
            repaired = repair_json(text, return_objects=True)
            if not isinstance(repaired, dict):
                raise
            parsed = repaired

        if not isinstance(parsed, dict):
            raise ValueError(f"Expected a JSON object, got {type(parsed).__name__}")
        return parsed
