"""Model-assisted structured extraction prototype (offline, no external API)."""

from __future__ import annotations

import json
import re
from typing import Any

from spike.worldgraph.deterministic_extractor import DeterministicExtractor
from spike.worldgraph.extractor import ExtractionResult
from spike.worldgraph.prompt_injection import block_trust_escalation, detect_injection_phrases


class ModelAssistedExtractor:
    """Simulates LLM structured output using deterministic base + guarded enrichment."""

    name = "model_assisted"

    _STRUCTURED_BLOCK = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)

    def __init__(self) -> None:
        self._deterministic = DeterministicExtractor()

    def extract(
        self,
        *,
        source_id: str,
        canonical_url: str,
        content_type: str,
        body: str,
        qualification_hint: str,
        exclusion_reason: str | None = None,
    ) -> ExtractionResult:
        base = self._deterministic.extract(
            source_id=source_id,
            canonical_url=canonical_url,
            content_type=content_type,
            body=body,
            qualification_hint=qualification_hint,
            exclusion_reason=exclusion_reason,
        )
        warnings = list(base.warnings)
        rejected = list(base.rejected_injection_attempts)
        manifest = json.loads(json.dumps(base.manifest))

        simulated = self._simulate_model_json(body)
        if simulated:
            manifest, sim_warnings, sim_rejected = self._merge_simulated(
                manifest,
                simulated,
                canonical_url=canonical_url,
            )
            warnings.extend(sim_warnings)
            rejected.extend(sim_rejected)

        injection_hits = detect_injection_phrases(body)
        if injection_hits:
            manifest["trust"]["claim_status"] = "unclaimed"
            warnings.append("model_output_not_used_for_trust_escalation")

        return ExtractionResult(
            manifest=manifest,
            warnings=warnings,
            rejected_injection_attempts=rejected,
        )

    def _simulate_model_json(self, body: str) -> dict[str, Any] | None:
        """Heuristic stand-in for model JSON — spike runs without API keys."""
        if block := self._STRUCTURED_BLOCK.search(body):
            try:
                return json.loads(block.group(1))
            except json.JSONDecodeError:
                return None
        if body.strip().startswith("{"):
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return None
        return {
            "semantic_description": body[:180],
            "tags": ["ai_world", "spike_fixture"],
        }

    def _merge_simulated(
        self,
        manifest: dict[str, Any],
        simulated: dict[str, Any],
        *,
        canonical_url: str,
    ) -> tuple[dict[str, Any], list[str], list[str]]:
        warnings: list[str] = []
        rejected: list[str] = []

        if desc := simulated.get("semantic_description"):
            manifest.setdefault("discovery", {})["semantic_description"] = {
                "value": str(desc)[:500],
                "provenance": {
                    "source_kind": "derived",
                    "source_url": canonical_url,
                    "evidence_snippet": str(desc)[:200],
                    "confidence": 0.45,
                    "observed_at": manifest["identity"]["name"]["provenance"]["observed_at"],
                    "verification_status": "unverified",
                },
            }
            warnings.append("model_derived_semantic_description")

        if tags := simulated.get("tags"):
            if isinstance(tags, list):
                manifest.setdefault("discovery", {})["tags"] = [
                    {
                        "value": str(tag),
                        "provenance": {
                            "source_kind": "derived",
                            "source_url": canonical_url,
                            "evidence_snippet": str(tag),
                            "confidence": 0.4,
                            "observed_at": manifest["identity"]["name"]["provenance"]["observed_at"],
                            "verification_status": "unverified",
                        },
                    }
                    for tag in tags[:5]
                ]

        if simulated.get("claim_status"):
            rejected.append("model_attempted_claim_status_override")
            warnings.append("ignored_model_claim_status")

        if simulated.get("verification_status"):
            allowed = frozenset({"unverified"})
            if block_trust_escalation(str(simulated["verification_status"]), allowed=allowed) is None:
                rejected.append("model_attempted_verification_escalation")

        return manifest, warnings, rejected
