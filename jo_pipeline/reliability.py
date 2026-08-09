import logging
from dataclasses import dataclass

from jo_pipeline.normalize import MetadataObservation

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FieldReliability:
    field: str
    category: str
    present: int
    total: int
    unknown_reasons: dict

    @property
    def presence_rate(self) -> float:
        return self.present / self.total


class ReliabilityReport:
    def __init__(self):
        self.assets = 0
        self.failures = {}
        self._categories = {}
        self._present = {}
        self._total = {}
        self._reasons = {}

    def add_asset(self, relative_path: str, observations: list[MetadataObservation]):
        self.assets += 1
        for observation in observations:
            self._categories[observation.field] = observation.category
            self._total[observation.field] = self._total.get(observation.field, 0) + 1
            if observation.value is None:
                reasons = self._reasons.setdefault(observation.field, {})
                reasons[observation.unknown_reason] = reasons.get(observation.unknown_reason, 0) + 1
            else:
                self._present[observation.field] = self._present.get(observation.field, 0) + 1

    def add_failure(self, relative_path: str, detail: str):
        self.failures[relative_path] = detail
        LOGGER.warning(f"{relative_path}: recorded extraction failure {detail}")

    def rows(self) -> list[FieldReliability]:
        return [
            FieldReliability(
                field=field,
                category=self._categories[field],
                present=self._present.get(field, 0),
                total=self._total[field],
                unknown_reasons=self._reasons.get(field, {}),
            )
            for field in sorted(self._total, key=lambda name: (self._categories[name], name))
        ]
