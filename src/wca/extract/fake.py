"""A scripted extractor. No network. Used by every test."""

from __future__ import annotations

from typing import Mapping, Sequence

from wca.extract.base import ExtractionResult, RawFactSet, build_facts


class FakeExtractor:
    version = "fake-v0"

    def __init__(self, script: dict[str, RawFactSet]) -> None:
        self._script = script

    def extract(
        self, message_id: str, text: str, thread: Sequence[Mapping[str, str]]
    ) -> ExtractionResult:
        raw = self._script.get(message_id)
        return ExtractionResult(
            message_id=message_id,
            facts=build_facts(raw) if raw else {},
        )
