"""Provider-neutral extraction for Manifest v0."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.worldgraph_spike.manifest_v0 import (
    EntryPoint,
    EvidenceRecord,
    FieldValue,
    ManifestV0,
    ProvenanceKind,
    SourceType,
    TrustLevel,
    WorldManifest,
)
from app.worldgraph_spike.security import sanitize_html_for_storage, strip_prompt_injection_markers

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_DESC_RE = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
    re.IGNORECASE | re.DOTALL,
)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_README_TITLE_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_README_SUMMARY_RE = re.compile(r"^##\s+Summary\s*\n+(.+?)(?:\n#|\Z)", re.DOTALL | re.MULTILINE)
_README_RUNTIME_RE = re.compile(r"^-\s*Runtime:\s*(.+)$", re.MULTILINE)
_README_ENTRY_RE = re.compile(r"^-\s*Entry:\s*(\S+)\s*—\s*(.+)$", re.MULTILINE)
_README_LICENSE_RE = re.compile(r"^License:\s*(.+)$", re.MULTILINE)


@dataclass
class ExtractionContext:
    source_url: str
    source_type: SourceType | str
    content: str
    content_kind: str = "text"
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ExtractionResult:
    manifest: ManifestV0 | None
    qualifies: bool
    warnings: list[str] = field(default_factory=list)
    block_reason: str | None = None


class Extractor(ABC):
    """Provider-neutral extractor interface."""

    extractor_id: str

    @abstractmethod
    def extract(self, context: ExtractionContext) -> ExtractionResult:
        raise NotImplementedError


def _strip_tags(text: str) -> str:
    return _TAG_RE.sub("", text).strip()


def _evidence(
    context: ExtractionContext,
    excerpt: str,
    *,
    trust_level: TrustLevel = TrustLevel.SOURCE_OBSERVATION,
) -> EvidenceRecord:
    return EvidenceRecord(
        source_url=context.source_url,
        source_type=context.source_type,
        excerpt=sanitize_html_for_storage(excerpt)[:500],
        observed_at=context.observed_at,
        trust_level=trust_level,
    )


def _field_from_match(
    context: ExtractionContext,
    value: str | None,
    *,
    confidence: float,
) -> FieldValue[str]:
    if not value:
        return FieldValue(value=None, confidence=0.0, provenance=ProvenanceKind.UNKNOWN)
    return FieldValue(
        value=value.strip(),
        confidence=confidence,
        provenance=ProvenanceKind.EXTRACTED,
        evidence=[_evidence(context, value)],
    )


class DeterministicExtractor(Extractor):
    """Parse structured hints without model calls."""

    extractor_id = "deterministic-v0"

    def extract(self, context: ExtractionContext) -> ExtractionResult:
        source_type = str(context.source_type)
        warnings: list[str] = []

        if source_type in {"github_readme", "hf_space_readme", "npm_readme"}:
            return self._extract_readme(context, context.content)
        if source_type in {"agent_card_json", "mcp_registry_json", "well_known_manifest"}:
            return self._extract_json(context, context.content)
        if source_type in {"landing_page", "itch_page", "discord_bot_docs"}:
            return self._extract_html(context, context.content)
        if source_type == "github_repo":
            return self._extract_readme(context, context.content)
        warnings.append(f"unsupported_source_type:{source_type}")
        return ExtractionResult(manifest=None, qualifies=False, warnings=warnings)

    def _extract_readme(self, context: ExtractionContext, text: str) -> ExtractionResult:
        title_match = _README_TITLE_RE.search(text)
        summary_match = _README_SUMMARY_RE.search(text)
        if not title_match:
            return ExtractionResult(
                manifest=None,
                qualifies=False,
                block_reason="insufficient_readme_structure",
            )
        slug = _slugify(title_match.group(1))
        runtime_values = [m.group(1).strip() for m in _README_RUNTIME_RE.finditer(text)]
        entry_points = [
            EntryPoint(label=m.group(2).strip(), url=m.group(1).strip())
            for m in _README_ENTRY_RE.finditer(text)
        ]
        license_match = _README_LICENSE_RE.search(text)
        manifest = WorldManifest(
            world_slug=slug,
            display_name=_field_from_match(context, title_match.group(1), confidence=0.85),
            summary=_field_from_match(
                context,
                summary_match.group(1).strip() if summary_match else None,
                confidence=0.75 if summary_match else 0.0,
            ),
            runtime_types=FieldValue(
                value=runtime_values or None,
                confidence=0.7 if runtime_values else 0.0,
                provenance=ProvenanceKind.EXTRACTED if runtime_values else ProvenanceKind.UNKNOWN,
                evidence=[_evidence(context, ", ".join(runtime_values))] if runtime_values else [],
            ),
            entry_points=FieldValue(
                value=entry_points or None,
                confidence=0.8 if entry_points else 0.0,
                provenance=ProvenanceKind.EXTRACTED if entry_points else ProvenanceKind.UNKNOWN,
                evidence=[_evidence(context, entry_points[0].url)] if entry_points else [],
            ),
            rights=_rights_from_license(context, license_match.group(1) if license_match else None),
        )
        return ExtractionResult(
            manifest=ManifestV0(
                manifest=manifest,
                extractor_id=self.extractor_id,
                source_urls=[context.source_url],
            ),
            qualifies=True,
        )

    def _extract_json(self, context: ExtractionContext, text: str) -> ExtractionResult:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return ExtractionResult(
                manifest=None,
                qualifies=False,
                block_reason="invalid_json",
            )
        name = payload.get("name") or payload.get("display_name") or payload.get("title")
        summary = payload.get("description") or payload.get("summary")
        if not name:
            return ExtractionResult(
                manifest=None,
                qualifies=False,
                block_reason="missing_required_name",
            )
        slug = _slugify(str(name))
        entry_url = payload.get("url") or payload.get("homepage")
        entry_points = (
            [EntryPoint(label="primary", url=str(entry_url))]
            if entry_url
            else []
        )
        runtime = payload.get("runtime_types") or payload.get("capabilities") or []
        if isinstance(runtime, str):
            runtime = [runtime]
        manifest = WorldManifest(
            world_slug=slug,
            display_name=_field_from_match(context, str(name), confidence=0.9),
            summary=_field_from_match(
                context,
                str(summary) if summary else None,
                confidence=0.85 if summary else 0.0,
            ),
            runtime_types=FieldValue(
                value=[str(item) for item in runtime] or None,
                confidence=0.8 if runtime else 0.0,
                provenance=ProvenanceKind.EXTRACTED if runtime else ProvenanceKind.UNKNOWN,
                evidence=[_evidence(context, json.dumps(runtime)[:200])] if runtime else [],
            ),
            entry_points=FieldValue(
                value=entry_points or None,
                confidence=0.85 if entry_points else 0.0,
                provenance=ProvenanceKind.EXTRACTED if entry_points else ProvenanceKind.UNKNOWN,
                evidence=[_evidence(context, entry_points[0].url)] if entry_points else [],
            ),
        )
        return ExtractionResult(
            manifest=ManifestV0(
                manifest=manifest,
                extractor_id=self.extractor_id,
                source_urls=[context.source_url],
            ),
            qualifies=True,
        )

    def _extract_html(self, context: ExtractionContext, text: str) -> ExtractionResult:
        title = _first_group(_TITLE_RE, text) or _first_group(_H1_RE, text)
        description = _first_group(_META_DESC_RE, text)
        json_ld = self._parse_json_ld(text)
        if json_ld:
            name = json_ld.get("name")
            summary = json_ld.get("description")
            title = title or (str(name) if name else None)
            description = description or (str(summary) if summary else None)
        if not title:
            return ExtractionResult(
                manifest=None,
                qualifies=False,
                block_reason="insufficient_html_metadata",
            )
        slug = _slugify(title)
        manifest = WorldManifest(
            world_slug=slug,
            display_name=_field_from_match(context, title, confidence=0.7),
            summary=_field_from_match(
                context,
                description,
                confidence=0.65 if description else 0.0,
            ),
        )
        return ExtractionResult(
            manifest=ManifestV0(
                manifest=manifest,
                extractor_id=self.extractor_id,
                source_urls=[context.source_url],
            ),
            qualifies=True,
        )

    def _parse_json_ld(self, text: str) -> dict[str, Any] | None:
        match = _JSON_LD_RE.search(text)
        if not match:
            return None
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            return None
        if isinstance(payload, list) and payload:
            payload = payload[0]
        return payload if isinstance(payload, dict) else None


class ModelAssistedExtractor(Extractor):
    """Simulated model extraction with injection defenses (no live LLM in spike)."""

    extractor_id = "model-assisted-v0-stub"

    def __init__(self, *, fallback: DeterministicExtractor | None = None) -> None:
        self._fallback = fallback or DeterministicExtractor()

    def extract(self, context: ExtractionContext) -> ExtractionResult:
        cleaned, injection_warnings = strip_prompt_injection_markers(context.content)
        if injection_warnings:
            cleaned_context = ExtractionContext(
                source_url=context.source_url,
                source_type=context.source_type,
                content=cleaned,
                content_kind=context.content_kind,
                observed_at=context.observed_at,
            )
            result = self._fallback.extract(cleaned_context)
            result.warnings.extend(injection_warnings)
            if result.manifest is not None:
                result.manifest.extractor_id = self.extractor_id
                result.manifest.warnings.extend(injection_warnings)
            return result
        result = self._fallback.extract(context)
        if result.manifest is not None:
            result.manifest.extractor_id = self.extractor_id
        return result


def _first_group(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if not match:
        return None
    return _strip_tags(match.group(1))


def _slugify(value: str) -> str:
    lowered = value.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return slug or "world"


def _rights_from_license(
    context: ExtractionContext,
    license_value: str | None,
) -> Any:
    from app.worldgraph_spike.manifest_v0 import RightsInfo

    if not license_value:
        return RightsInfo()
    return RightsInfo(
        license_spdx=FieldValue(
            value=license_value.strip(),
            confidence=0.6,
            provenance=ProvenanceKind.EXTRACTED,
            evidence=[_evidence(context, license_value)],
        ),
    )
