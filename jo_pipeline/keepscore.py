import logging
from dataclasses import asdict, dataclass, field

from jo_pipeline.reference import ReferenceGrouping
from jo_pipeline.refine import LEAVE_OUT, ImageSignal

LOGGER = logging.getLogger(__name__)

KEEPSCORE_VERSION = "keepscore-1"


@dataclass(frozen=True)
class SignalHit:
    relative_path: str
    keep_signal: str
    reason: str
    confidence: float


@dataclass(frozen=True)
class ExcludedSplit:
    caught: list
    missed: list


@dataclass(frozen=True)
class KeepSignalScore:
    excluded_total: int
    excluded_with_signal: int
    excluded_caught: int
    grouped_total: int
    grouped_with_signal: int
    grouped_false_positives: int
    recall: float
    false_positive_rate: float
    method_version: str
    caught: list = field(default_factory=list)
    missed: list = field(default_factory=list)
    false_positives: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "excluded_total": self.excluded_total,
            "excluded_with_signal": self.excluded_with_signal,
            "excluded_caught": self.excluded_caught,
            "grouped_total": self.grouped_total,
            "grouped_with_signal": self.grouped_with_signal,
            "grouped_false_positives": self.grouped_false_positives,
            "recall": self.recall,
            "false_positive_rate": self.false_positive_rate,
            "method_version": self.method_version,
            "caught": [asdict(hit) for hit in self.caught],
            "missed": [asdict(hit) for hit in self.missed],
            "false_positives": [asdict(hit) for hit in self.false_positives],
        }


class KeepSignalScorer:
    def score(self, reference: ReferenceGrouping, signals: dict) -> KeepSignalScore:
        excluded = sorted(set(reference.excluded_paths))
        grouped = sorted(reference.grouped_paths())

        split = self._split_excluded(excluded, signals)
        caught, missed = split.caught, split.missed
        false_positives = self._false_positives(grouped, signals)

        excluded_with_signal = sum(1 for path in excluded if path in signals)
        grouped_with_signal = sum(1 for path in grouped if path in signals)
        recall = len(caught) / excluded_with_signal if excluded_with_signal else 0.0
        false_positive_rate = len(false_positives) / grouped_with_signal if grouped_with_signal else 0.0

        LOGGER.info(
            f"{reference.dataset_id}: keep signal caught {len(caught)} of {excluded_with_signal} evaluated exclusions "
            f"and marked {len(false_positives)} of {grouped_with_signal} kept photos leave_out"
        )
        return KeepSignalScore(
            excluded_total=len(excluded),
            excluded_with_signal=excluded_with_signal,
            excluded_caught=len(caught),
            grouped_total=len(grouped),
            grouped_with_signal=grouped_with_signal,
            grouped_false_positives=len(false_positives),
            recall=round(recall, 3),
            false_positive_rate=round(false_positive_rate, 3),
            method_version=KEEPSCORE_VERSION,
            caught=caught,
            missed=missed,
            false_positives=false_positives,
        )

    def _split_excluded(self, excluded: list, signals: dict) -> ExcludedSplit:
        caught = []
        missed = []
        for path in excluded:
            signal = signals.get(path)
            if signal is None:
                LOGGER.debug(f"{path}: excluded by the traveler but never evaluated, not scored")
                continue
            if signal.keep_signal == LEAVE_OUT:
                LOGGER.info(f"{path}: model agreed with the traveler and marked it leave_out")
                caught.append(self._hit(signal))
            else:
                LOGGER.info(f"{path}: traveler excluded it but the model said {signal.keep_signal}")
                missed.append(self._hit(signal))
        return ExcludedSplit(caught=caught, missed=missed)

    def _false_positives(self, grouped: list, signals: dict) -> list:
        flagged = []
        for path in grouped:
            signal = signals.get(path)
            if signal is None:
                LOGGER.debug(f"{path}: grouped by the traveler but never evaluated, not scored")
                continue
            if signal.keep_signal == LEAVE_OUT:
                LOGGER.info(f"{path}: traveler kept it in a memory but the model said leave_out")
                flagged.append(self._hit(signal))
        return flagged

    def _hit(self, signal: ImageSignal) -> SignalHit:
        return SignalHit(
            relative_path=signal.relative_path,
            keep_signal=signal.keep_signal,
            reason=signal.keep_reason,
            confidence=signal.keep_confidence,
        )
