"""Research corpus loader for the WorldGraph spike."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_PATH = REPO_ROOT / "docs" / "worldgraph" / "research-corpus.json"
FIXTURES_ROOT = REPO_ROOT / "tests" / "fixtures" / "worldgraph"


class CorpusEntry(BaseModel):
    id: str
    label: str
    source_type: str
    url: str
    fixture: str | None = None
    expected_qualifies: bool
    notes: str = ""
    negative_control: bool = False
    expected_block_reason: str | None = None


class ResearchCorpus(BaseModel):
    version: str
    description: str
    entries: list[CorpusEntry] = Field(default_factory=list)

    @property
    def qualifying_entries(self) -> list[CorpusEntry]:
        return [entry for entry in self.entries if not entry.negative_control]

    @property
    def negative_controls(self) -> list[CorpusEntry]:
        return [entry for entry in self.entries if entry.negative_control]


def load_research_corpus(path: Path | None = None) -> ResearchCorpus:
    target = path or CORPUS_PATH
    payload = json.loads(target.read_text(encoding="utf-8"))
    return ResearchCorpus.model_validate(payload)


def load_fixture_bytes(entry: CorpusEntry) -> bytes:
    if not entry.fixture:
        raise FileNotFoundError(f"entry {entry.id} has no fixture path")
    fixture_path = REPO_ROOT / entry.fixture
    return fixture_path.read_bytes()


def load_fixture_text(entry: CorpusEntry) -> str:
    return load_fixture_bytes(entry).decode("utf-8")


def corpus_summary(corpus: ResearchCorpus) -> dict[str, Any]:
    return {
        "version": corpus.version,
        "total_entries": len(corpus.entries),
        "qualifying": len(corpus.qualifying_entries),
        "negative_controls": len(corpus.negative_controls),
        "source_types": sorted({entry.source_type for entry in corpus.entries}),
    }
