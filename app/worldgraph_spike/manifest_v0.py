"""Manifest v0 schema for the WorldGraph technical spike (#204)."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field, field_validator, model_validator

MANIFEST_VERSION = "0"

T = TypeVar("T")


class ProvenanceKind(str, Enum):
    EXTRACTED = "extracted"
    CREATOR_DECLARED = "creator_declared"
    VERIFIED = "verified"
    UNKNOWN = "unknown"


class TrustLevel(str, Enum):
    """Separate trust concepts — never conflate observation with verification."""

    SOURCE_OBSERVATION = "source_observation"
    CREATOR_CLAIM = "creator_claim"
    DOMAIN_CONTROL = "domain_control"
    PLATFORM_OWNERSHIP = "platform_ownership"
    EMAIL_DOMAIN = "email_domain"
    SABERISTIC_REVIEW = "saberistic_review"


class SourceType(str, Enum):
    GITHUB_README = "github_readme"
    GITHUB_REPO = "github_repo"
    AGENT_CARD_JSON = "agent_card_json"
    MCP_REGISTRY_JSON = "mcp_registry_json"
    LANDING_PAGE = "landing_page"
    WELL_KNOWN_MANIFEST = "well_known_manifest"
    DISCORD_BOT_DOCS = "discord_bot_docs"
    HF_SPACE_README = "hf_space_readme"
    ITCH_PAGE = "itch_page"
    NPM_README = "npm_readme"
    CREATOR_FORM = "creator_form"


class EvidenceRecord(BaseModel):
    source_url: str
    source_type: SourceType | str
    excerpt: str = Field(max_length=2000)
    observed_at: datetime
    trust_level: TrustLevel = TrustLevel.SOURCE_OBSERVATION

    @field_validator("source_url")
    @classmethod
    def _non_empty_url(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("source_url must not be empty")
        return stripped


class FieldValue(BaseModel, Generic[T]):
    """Populated factual fields require evidence or creator_declared provenance."""

    value: T | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    provenance: ProvenanceKind = ProvenanceKind.UNKNOWN
    evidence: list[EvidenceRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def _evidence_or_declared(self) -> FieldValue[T]:
        if self.value is None:
            if self.provenance != ProvenanceKind.UNKNOWN:
                raise ValueError("null value requires provenance=unknown")
            return self
        if self.provenance == ProvenanceKind.UNKNOWN:
            raise ValueError("populated value cannot remain unknown")
        if self.provenance == ProvenanceKind.EXTRACTED and not self.evidence:
            raise ValueError("extracted fields require at least one evidence record")
        if self.provenance == ProvenanceKind.VERIFIED and not self.evidence:
            raise ValueError("verified fields require attestation evidence")
        return self


class EntryPoint(BaseModel):
    label: str
    url: str
    kind: str = "web"


class ControlInfo(BaseModel):
    creator_name: FieldValue[str] = Field(default_factory=FieldValue)
    organization: FieldValue[str] = Field(default_factory=FieldValue)
    contact_url: FieldValue[str] = Field(default_factory=FieldValue)
    domains: FieldValue[list[str]] = Field(default_factory=FieldValue)


class AIParticipation(BaseModel):
    modes: FieldValue[list[str]] = Field(default_factory=FieldValue)
    models_disclosed: FieldValue[list[str]] = Field(default_factory=FieldValue)
    human_in_loop: FieldValue[bool] = Field(default_factory=FieldValue)


class RightsInfo(BaseModel):
    license_spdx: FieldValue[str] = Field(default_factory=FieldValue)
    commercial_use: FieldValue[str] = Field(default_factory=FieldValue)
    attribution_required: FieldValue[bool] = Field(default_factory=FieldValue)
    summary: FieldValue[str] = Field(default_factory=FieldValue)


class AccessInfo(BaseModel):
    public: FieldValue[bool] = Field(default_factory=FieldValue)
    authentication: FieldValue[str] = Field(default_factory=FieldValue)
    minimum_age: FieldValue[int] = Field(default_factory=FieldValue)


class WorldManifest(BaseModel):
    manifest_version: str = MANIFEST_VERSION
    world_slug: str
    display_name: FieldValue[str]
    summary: FieldValue[str]
    runtime_types: FieldValue[list[str]] = Field(default_factory=FieldValue)
    entry_points: FieldValue[list[EntryPoint]] = Field(default_factory=FieldValue)
    control: ControlInfo = Field(default_factory=ControlInfo)
    ai_participation: AIParticipation = Field(default_factory=AIParticipation)
    rights: RightsInfo = Field(default_factory=RightsInfo)
    access: AccessInfo = Field(default_factory=AccessInfo)
    tags: FieldValue[list[str]] = Field(default_factory=FieldValue)

    @field_validator("manifest_version")
    @classmethod
    def _version_is_zero(cls, value: str) -> str:
        if value != MANIFEST_VERSION:
            raise ValueError(f"unsupported manifest_version: {value}")
        return value


class ManifestV0(BaseModel):
    """Top-level envelope with extraction metadata."""

    manifest: WorldManifest
    extracted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    extractor_id: str
    source_urls: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def to_search_document(self) -> dict[str, Any]:
        """Flatten manifest for lexical / embedding search benchmarks."""
        m = self.manifest
        parts: list[str] = []
        for field in (
            m.display_name,
            m.summary,
            m.runtime_types,
            m.tags,
            m.ai_participation.modes,
            m.rights.license_spdx,
            m.rights.summary,
        ):
            if field.value:
                if isinstance(field.value, list):
                    parts.extend(str(item) for item in field.value)
                else:
                    parts.append(str(field.value))
        if m.entry_points.value:
            for ep in m.entry_points.value:
                parts.append(ep.label)
        return {
            "world_slug": m.world_slug,
            "text": " ".join(parts).lower(),
            "runtime_types": m.runtime_types.value or [],
            "tags": m.tags.value or [],
            "license_spdx": (
                m.rights.license_spdx.value if m.rights.license_spdx.value else None
            ),
            "public_access": (
                m.access.public.value if m.access.public.value is not None else None
            ),
        }
