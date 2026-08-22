import logging
import time
from dataclasses import dataclass

import httpx

LOGGER = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
CHAT_COMPLETIONS_PATH = "/chat/completions"
REQUEST_TIMEOUT_SECONDS = 180.0
HTTP_REFERER = "https://journeyonward.app"
APP_TITLE = "jo-image-pipeline-llm"
PRICE_INPUT_USD_PER_MTOK = 5.0
PRICE_OUTPUT_USD_PER_MTOK = 30.0


class OpenRouterError(Exception):
    def __init__(self, message: str, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


@dataclass(frozen=True)
class CompletionResult:
    content: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    generation_id: str | None
    latency_ms: int


class OpenRouterClient:
    def __init__(self, api_key: str, model: str, transport: httpx.BaseTransport | None = None):
        self.model = model
        self.client = httpx.Client(
            base_url=OPENROUTER_BASE_URL,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {api_key}", "HTTP-Referer": HTTP_REFERER, "X-Title": APP_TITLE},
            transport=transport,
        )

    def __enter__(self) -> "OpenRouterClient":
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.client.close()

    def complete(self, messages: list[dict], schema_name: str, schema: dict) -> CompletionResult:
        payload = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_schema", "json_schema": {"name": schema_name, "strict": True, "schema": schema}},
            "usage": {"include": True},
            "provider": {"allow_fallbacks": False, "data_collection": "deny"},
            "temperature": 0,
        }
        started = time.monotonic()
        try:
            response = self.client.post(CHAT_COMPLETIONS_PATH, json=payload)
        except httpx.HTTPError as error:
            raise OpenRouterError(f"openrouter request failed with {type(error).__name__}: {error}") from error
        latency_ms = int((time.monotonic() - started) * 1000)

        if response.status_code != 200:
            raise OpenRouterError(f"openrouter returned status {response.status_code}", status_code=response.status_code, body=response.text)
        body = response.json()
        if "error" in body:
            raise OpenRouterError(f"openrouter returned error {body['error']}", status_code=response.status_code, body=response.text)

        usage = body.get("usage", {})
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        cost_usd = usage.get("cost")
        if cost_usd is None:
            cost_usd = (prompt_tokens * PRICE_INPUT_USD_PER_MTOK + completion_tokens * PRICE_OUTPUT_USD_PER_MTOK) / 1_000_000
            LOGGER.debug(f"usage cost missing from response, estimated ${cost_usd:.6f} from token counts")
        else:
            cost_usd = float(cost_usd)
            LOGGER.debug(f"usage cost ${cost_usd:.6f} reported by provider")

        content = body["choices"][0]["message"]["content"] or ""
        return CompletionResult(
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            generation_id=body.get("id"),
            latency_ms=latency_ms,
        )
