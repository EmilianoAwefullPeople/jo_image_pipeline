import json
import logging
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel

from llm_pipeline.client import CompletionResult, OpenRouterClient
from llm_pipeline.schema import RESPONSE_SCHEMA_NAME, ImageEvaluation, response_schema

LOGGER = logging.getLogger(__name__)

VALID = "valid"
INVALID = "invalid"
RETRY_TEMPLATE = "Your previous response failed validation:\n{error}\nReturn a corrected JSON object that satisfies the schema. Output JSON only."


@dataclass(frozen=True)
class EvaluationOutcome:
    validation_status: str
    evaluation: BaseModel | None
    failure_detail: str | None
    attempts: int
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    generation_id: str | None


class StructuredEvaluator:
    def __init__(self, client: OpenRouterClient, schema_name: str, schema: dict, parse: Callable[[str], BaseModel]):
        self.client = client
        self.schema_name = schema_name
        self.schema = schema
        self.parse = parse

    def evaluate(self, label: str, messages: list[dict]) -> EvaluationOutcome:
        first = self.client.complete(messages, self.schema_name, self.schema)
        try:
            payload = self.parse(first.content)
        except ValueError as first_error:
            LOGGER.warning(f"{label}: response failed validation with {type(first_error).__name__}, retrying once with the error")
            return self._retry(label, messages, first, first_error)

        LOGGER.info(f"{label}: {self.schema_name} valid on first attempt")
        return self._outcome(VALID, payload, None, first, None)

    def _retry(self, label: str, messages: list[dict], first: CompletionResult, first_error: Exception) -> EvaluationOutcome:
        retry_messages = messages + [
            {"role": "assistant", "content": first.content},
            {"role": "user", "content": RETRY_TEMPLATE.format(error=first_error)},
        ]
        second = self.client.complete(retry_messages, self.schema_name, self.schema)
        try:
            payload = self.parse(second.content)
        except ValueError as second_error:
            LOGGER.warning(f"{label}: retry failed validation with {type(second_error).__name__}, storing the failure for review")
            detail = f"attempt 1: {first_error}\nattempt 2: {second_error}\nfinal content: {second.content}"
            return self._outcome(INVALID, None, detail, first, second)

        LOGGER.info(f"{label}: {self.schema_name} valid on retry")
        return self._outcome(VALID, payload, None, first, second)

    def _outcome(self, status: str, payload: BaseModel | None, failure_detail: str | None, first: CompletionResult, second: CompletionResult | None) -> EvaluationOutcome:
        results = [first] if second is None else [first, second]
        return EvaluationOutcome(
            validation_status=status,
            evaluation=payload,
            failure_detail=failure_detail,
            attempts=len(results),
            latency_ms=sum(result.latency_ms for result in results),
            prompt_tokens=sum(result.prompt_tokens for result in results),
            completion_tokens=sum(result.completion_tokens for result in results),
            cost_usd=sum(result.cost_usd for result in results),
            generation_id=results[-1].generation_id,
        )


def parse_image_evaluation(content: str) -> ImageEvaluation:
    return ImageEvaluation.model_validate(json.loads(content))


def image_evaluator(client: OpenRouterClient) -> StructuredEvaluator:
    return StructuredEvaluator(client, RESPONSE_SCHEMA_NAME, response_schema(), parse_image_evaluation)
