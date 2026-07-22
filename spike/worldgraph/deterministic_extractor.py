"""Deterministic metadata and README parsing extractor."""

from __future__ import annotations

import json
import re
from typing import Any

from spike.worldgraph.extractor import ExtractionResult, proven, unknown_field
from spike.worldgraph.fetcher import strip_html_to_text
from spike.worldgraph.prompt_injection import detect_injection_phrases, sanitize_model_field


class DeterministicExtractor:
    name = "deterministic"

    _TITLE_RE = re.compile(r"(?is)<title>(.*?)</title>")
    _H1_RE = re.compile(r"(?m)^#\s+(.+)$")
    _OG_DESC_RE = re.compile(r'(?is)<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']')
    _ENTRY_RE = re.compile(
        r"(?i)(?:entry|launch|play|enter|explore|join|start|run)[:\s]+<?(https?://[^\s>]+)>?"
    )
    _AI_ROLE_RE = re.compile(r"(?i)ai role[:\s]+(.+)$")
    _INTERACTION_RE = re.compile(r"(?i)interaction[:\s]+(.+)$")
    _PERSISTENCE_RE = re.compile(r"(?i)persistence[:\s]+(.+)$")
    _CREATOR_RE = re.compile(r"(?i)(?:creator|operator)[:\s]+(.+)$")

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
        warnings: list[str] = []
        rejected: list[str] = []
        text = body
        if "html" in content_type:
            text = strip_html_to_text(body)

        injection_hits = detect_injection_phrases(body)
        if injection_hits:
            rejected.extend(injection_hits)
            warnings.append("prompt_injection_phrases_detected")

        name = self._extract_name(body, text)
        summary = self._extract_summary(body, text)
        entry_points = self._extract_entry_points(body, text, canonical_url)
        interaction = self._first_match(self._INTERACTION_RE, body) or self._infer_interaction(text)
        persistence = self._first_match(self._PERSISTENCE_RE, body) or unknown_field()
        ai_role = self._first_match(self._AI_ROLE_RE, body) or self._infer_ai_role(text)
        creator = self._first_match(self._CREATOR_RE, body) or unknown_field()
        world_type = self._infer_world_type(text, qualification_hint)

        qualification_status = qualification_hint
        if exclusion_reason:
            qualification_status = "excluded"

        manifest: dict[str, Any] = {
            "schema_version": "world-manifest-v0",
            "identity": {
                "name": proven(
                    sanitize_model_field(name),
                    source_kind="source_observation",
                    source_url=canonical_url,
                    evidence_snippet=name[:200],
                    confidence=0.85,
                ),
                "canonical_url": proven(
                    canonical_url,
                    source_kind="creator_declared" if canonical_url.startswith("https://github.com/") else "source_observation",
                    source_url=canonical_url,
                    evidence_snippet=canonical_url,
                    confidence=0.95,
                ),
                "world_type": proven(
                    sanitize_model_field(world_type),
                    source_kind="derived",
                    source_url=canonical_url,
                    evidence_snippet=world_type,
                    confidence=0.7,
                ),
                "status": proven(
                    "published" if qualification_status == "qualifies" else "review",
                    source_kind="derived",
                    source_url=canonical_url,
                    evidence_snippet=qualification_status,
                    confidence=0.6,
                ),
                "summary": summary,
                "creator": creator if isinstance(creator, dict) else proven(
                    sanitize_model_field(str(creator)),
                    source_kind="source_observation",
                    source_url=canonical_url,
                    evidence_snippet=str(creator)[:200],
                    confidence=0.55,
                ),
            },
            "experience": {
                "entry_points": entry_points,
                "interaction_model": proven(
                    sanitize_model_field(interaction),
                    source_kind="source_observation",
                    source_url=canonical_url,
                    evidence_snippet=str(interaction)[:200],
                    confidence=0.65,
                ),
                "persistence_model": persistence if isinstance(persistence, dict) else proven(
                    sanitize_model_field(str(persistence)),
                    source_kind="source_observation",
                    source_url=canonical_url,
                    evidence_snippet=str(persistence)[:200],
                    confidence=0.55,
                ),
            },
            "ai_role": {
                "material_ai_role": proven(
                    sanitize_model_field(ai_role),
                    source_kind="source_observation",
                    source_url=canonical_url,
                    evidence_snippet=str(ai_role)[:200],
                    confidence=0.6,
                ),
                "ai_usage_phase": proven(
                    self._infer_ai_phase(text),
                    source_kind="derived",
                    source_url=canonical_url,
                    evidence_snippet=self._infer_ai_phase(text),
                    confidence=0.5,
                ),
            },
            "trust": {
                "qualification_status": qualification_status,
                "claim_status": "unclaimed",
                "license_status": unknown_field(),
                **(
                    {"exclusion_reason": exclusion_reason}
                    if qualification_status == "excluded" and exclusion_reason
                    else {}
                ),
            },
            "discovery": {
                "tags": [proven(source_id, source_kind="derived", source_url=canonical_url, evidence_snippet=source_id, confidence=1.0)],
                "semantic_description": summary,
            },
        }

        if content_type.endswith("json"):
            manifest = self._merge_json_manifest(manifest, body, canonical_url)

        return ExtractionResult(manifest=manifest, warnings=warnings, rejected_injection_attempts=rejected)

    def _extract_name(self, raw: str, text: str) -> str:
        if m := self._TITLE_RE.search(raw):
            return m.group(1).strip()
        if m := self._H1_RE.search(raw):
            return m.group(1).strip()
        return text.split(".", 1)[0][:80] or "Untitled world"

    def _extract_summary(self, raw: str, text: str) -> dict[str, Any]:
        if m := self._OG_DESC_RE.search(raw):
            snippet = m.group(1).strip()
            return proven(
                sanitize_model_field(snippet),
                source_kind="source_observation",
                source_url=None,
                evidence_snippet=snippet[:200],
                confidence=0.75,
            )
        first_sentence = text[:240].strip()
        if first_sentence:
            return proven(
                sanitize_model_field(first_sentence),
                source_kind="source_observation",
                source_url=None,
                evidence_snippet=first_sentence[:200],
                confidence=0.55,
            )
        return unknown_field()

    def _extract_entry_points(self, raw: str, text: str, canonical_url: str) -> list[dict[str, Any]]:
        urls = []
        for match in self._ENTRY_RE.finditer(raw + "\n" + text):
            urls.append(match.group(1).strip().rstrip(").,"))
        if not urls and "http" in raw:
            for match in re.finditer(r"https?://[^\s<>\"']+", raw):
                urls.append(match.group(0).rstrip(").,"))
        if not urls:
            urls = [canonical_url]
        deduped = []
        seen = set()
        for url in urls:
            if url not in seen:
                deduped.append(
                    proven(
                        url,
                        source_kind="source_observation",
                        source_url=canonical_url,
                        evidence_snippet=url,
                        confidence=0.7,
                    )
                )
                seen.add(url)
        return deduped[:3]

    def _first_match(self, pattern: re.Pattern[str], raw: str) -> str | None:
        if m := pattern.search(raw):
            return m.group(1).strip()
        return None

    def _infer_world_type(self, text: str, qualification_hint: str) -> str:
        lowered = text.lower()
        if "simulation" in lowered or "multi-agent" in lowered:
            return "agent_simulation"
        if "spatial" in lowered or "3d" in lowered or "webxr" in lowered:
            return "ai_spatial"
        if "game" in lowered or "playable" in lowered or "npc" in lowered:
            return "ai_game"
        if "social" in lowered or "economy" in lowered or "multiplayer" in lowered:
            return "persistent_social"
        if "narrative" in lowered or "story" in lowered or "character" in lowered:
            return "interactive_narrative"
        if qualification_hint == "excluded":
            return "excluded_candidate"
        return "interactive_world"

    def _infer_interaction(self, text: str) -> str:
        lowered = text.lower()
        if "multiplayer" in lowered:
            return "multiplayer_social_interaction"
        if "choice" in lowered or "branching" in lowered:
            return "choice_driven_narrative"
        if "explore" in lowered or "walk" in lowered:
            return "spatial_exploration"
        if "simulation" in lowered:
            return "simulation_tick_actions"
        return "interactive_session"

    def _infer_ai_role(self, text: str) -> str:
        lowered = text.lower()
        if "npc" in lowered:
            return "runtime_npc_behavior_and_memory"
        if "agent" in lowered:
            return "runtime_agent_planning"
        if "character" in lowered or "dialogue" in lowered:
            return "runtime_character_dialogue"
        if "moderation" in lowered:
            return "runtime_moderation_and_fill_ins"
        return "material_ai_in_runtime_experience"

    def _infer_ai_phase(self, text: str) -> str:
        lowered = text.lower()
        if "build-time" in lowered or "build time" in lowered:
            return "build_and_runtime"
        if "runtime" in lowered:
            return "runtime"
        return "unknown_phase"

    def _merge_json_manifest(self, manifest: dict[str, Any], body: str, canonical_url: str) -> dict[str, Any]:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return manifest
        if name := payload.get("name"):
            manifest["identity"]["name"] = proven(
                sanitize_model_field(str(name)),
                source_kind="source_observation",
                source_url=canonical_url,
                evidence_snippet=str(name),
                confidence=0.9,
            )
        if entry := payload.get("entry_point"):
            manifest["experience"]["entry_points"] = [
                proven(
                    str(entry),
                    source_kind="source_observation",
                    source_url=canonical_url,
                    evidence_snippet=str(entry),
                    confidence=0.9,
                )
            ]
        return manifest
