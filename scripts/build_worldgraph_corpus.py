#!/usr/bin/env python3
"""Build WorldGraph research corpus artifacts for issue #200."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = REPO_ROOT / "docs" / "worldgraph" / "corpus"
MANIFESTS_DIR = CORPUS_DIR / "manifests"
CANDIDATES_PATH = CORPUS_DIR / "candidates.yaml"
VALIDATION_PATH = CORPUS_DIR / "validation-results.json"
SCHEMA_PATH = REPO_ROOT / "docs" / "worldgraph" / "world-manifest-v0.schema.json"

sys.path.insert(0, str(REPO_ROOT))

from spike.worldgraph.corpus_manifest_builder import build_qualifying_manifest  # noqa: E402
from spike.worldgraph.manifest_schema import validate_manifest_v0  # noqa: E402

LAST_CHECKED = "2026-07-22"


def _rules(
    r1: str,
    r2: str,
    r3: str,
    r4: str,
    r5: str,
    r6: str,
    r7: str,
) -> dict[str, str]:
    return {
        "rule_1_stable_entry_point": r1,
        "rule_2_meaningful_interaction": r2,
        "rule_3_bounded_setting_or_rules": r3,
        "rule_4_persistence_or_reproducibility": r4,
        "rule_5_material_ai_role": r5,
        "rule_6_identifiable_claimant": r6,
        "rule_7_evaluable_access_and_safety": r7,
    }


def _candidate(
    cid: str,
    *,
    name: str,
    canonical_source: str,
    category: str,
    qualifies: bool,
    creator_operator: str,
    entry_point: str,
    accessibility: str,
    ai_role: str,
    persistence: str,
    platform_runtime: str,
    agents_mechanics: str,
    rights: str = "unknown",
    safety_age: str = "unknown",
    access_requirements: str = "unknown",
    unknown_fields: list[str],
    reviewer_notes: str,
    confidence: float,
    source_observed_at: str,
    exclusion_reason: str | None = None,
    linked_research_entity: str | None = None,
    summary: str | None = None,
    setting: str | None = None,
) -> dict:
    status = "qualifies" if qualifies else "excluded"
    rules = _rules(
        f"Stable public URL resolves to product or documented experience: {canonical_source}",
        f"Users affect outcomes via documented interaction: {agents_mechanics[:120]}",
        f"Bounded setting or mechanics described: {setting or agents_mechanics[:100]}",
        f"Persistence or reproducibility documented: {persistence[:120]}",
        f"Material AI role in runtime: {ai_role[:120]}",
        f"Named creator/operator: {creator_operator}",
        f"Access/safety disclosed or honestly unknown: {accessibility[:100]}",
    )
    record: dict = {
        "id": cid,
        "name": name,
        "canonical_source": canonical_source,
        "candidate_category": category,
        "qualification": {
            "status": status,
            "exclusion_reason": exclusion_reason,
            "rule_evidence": rules,
        },
        "creator_operator": creator_operator,
        "entry_point": entry_point,
        "accessibility": accessibility,
        "ai_role": ai_role,
        "persistence_or_reproducibility": persistence,
        "agents_characters_mechanics": agents_mechanics,
        "platform_runtime": platform_runtime,
        "rights_license_disclosed": rights,
        "safety_age_disclosed": safety_age,
        "access_requirements": access_requirements,
        "unknown_manifest_fields": unknown_fields,
        "reviewer_notes": reviewer_notes,
        "confidence": confidence,
        "source_observed_at": source_observed_at,
        "last_checked_at": LAST_CHECKED,
    }
    if summary:
        record["summary"] = summary
    if setting:
        record["setting"] = setting
    if linked_research_entity:
        record["linked_research_entity"] = linked_research_entity
    if qualifies:
        record["manifest_path"] = f"manifests/{cid}.json"
    return record


CANDIDATES = [
    # --- interactive_narrative (5 qualifying) ---
    _candidate(
        "wg-200-narrative-001",
        name="Character.AI Scenes",
        canonical_source="https://blog.character.ai/introducing-scenes/",
        category="interactive_narrative",
        qualifies=True,
        creator_operator="Character Technologies, Inc.",
        entry_point="https://character.ai/",
        accessibility="Free web signup required; scenes playable after account creation",
        ai_role="Runtime LLM drives character dialogue, scene state, and branching narrative outcomes",
        persistence="Saved scene progress and character memory across sessions",
        platform_runtime="Web (Character.AI)",
        agents_mechanics="Multi-character scenes with player-directed choices and relationship state",
        safety_age="Teen rating noted in product policies; age gate on signup",
        unknown_fields=["trust.license_status", "ai_role.human_control_boundaries", "world_structure.economy"],
        reviewer_notes="Canonical narrative-world pattern; group chat variant overlaps social category.",
        confidence=0.88,
        source_observed_at="2024-10-30",
        summary="Multi-character interactive scenes with persistent AI-driven narrative state.",
        setting="User-authored or template scenes with defined character casts and goals",
    ),
    _candidate(
        "wg-200-narrative-002",
        name="AI Dungeon",
        canonical_source="https://aidungeon.com/",
        category="interactive_narrative",
        qualifies=True,
        creator_operator="Latitude Inc.",
        entry_point="https://play.aidungeon.com/",
        accessibility="Free tier with account; premium tiers for advanced models",
        ai_role="Generative LLM narrates world, NPCs, and consequences from player text actions",
        persistence="Adventure save slots and shared multiplayer story state",
        platform_runtime="Web and mobile apps",
        agents_mechanics="Text-command exploration with inventory, quests, and multiplayer parties",
        rights="Latitude Terms of Service govern user-generated adventure content",
        safety_age="13+ stated in terms; content moderation filters documented",
        access_requirements="account_required",
        unknown_fields=["world_structure.governance", "ai_role.model_disclosures"],
        reviewer_notes="Long-running reference product for AI narrative worlds.",
        confidence=0.92,
        source_observed_at="2026-01-15",
        summary="Persistent text adventure worlds with generative AI dungeon master.",
        setting="Player-selected or custom scenarios with explicit world prompts",
    ),
    _candidate(
        "wg-200-narrative-003",
        name="Hidden Door",
        canonical_source="https://www.hiddendoor.co/",
        category="interactive_narrative",
        qualifies=True,
        creator_operator="Hidden Door Inc.",
        entry_point="https://www.hiddendoor.co/",
        accessibility="Waitlist/invite flow documented on homepage; demo materials public",
        ai_role="AI game master orchestrates rules, NPCs, and story progression in licensed settings",
        persistence="Session and campaign state tied to player profiles",
        platform_runtime="Web",
        agents_mechanics="Tabletop-RPG-style rules engine with AI narrator and character sheets",
        unknown_fields=["experience.pricing", "trust.license_status", "world_structure.economy"],
        reviewer_notes="Licensed-IP narrative worlds; access may tighten over time.",
        confidence=0.8,
        source_observed_at="2025-06-01",
        summary="AI game-master experiences inside bounded fictional universes.",
        setting="Licensed story worlds with explicit canon and RPG mechanics",
    ),
    _candidate(
        "wg-200-narrative-004",
        name="NovelAI",
        canonical_source="https://novelai.net/",
        category="interactive_narrative",
        qualifies=True,
        creator_operator="Anlatan",
        entry_point="https://novelai.net/",
        accessibility="Subscription required for full text adventure and image features",
        ai_role="Fine-tuned models generate interactive story responses and world-consistent prose",
        persistence="Per-story save files and lorebook memory entries",
        platform_runtime="Web",
        agents_mechanics="Text adventure mode with lorebooks, memory, and author-defined world info",
        rights="Subscription terms describe user content ownership constraints",
        safety_age="18+ age gate on registration",
        access_requirements="subscription_required",
        unknown_fields=["world_structure.agents_and_characters", "trust.moderation_contact"],
        reviewer_notes="Strong bounded-context via lorebooks; paywall limits automated extraction.",
        confidence=0.86,
        source_observed_at="2026-02-01",
        summary="Author-defined interactive fiction with persistent lorebook state.",
        setting="User-authored lorebooks constrain canon and character behavior",
    ),
    _candidate(
        "wg-200-narrative-005",
        name="Charisma.ai",
        canonical_source="https://charisma.ai/",
        category="interactive_narrative",
        qualifies=True,
        creator_operator="Charisma.ai Ltd.",
        entry_point="https://charisma.ai/",
        accessibility="Studio tools and sample experiences linked from marketing site",
        ai_role="Conversational AI characters follow authored story graphs and emotional state models",
        persistence="Story graph state and character memory across conversation turns",
        platform_runtime="Web and embedded SDK experiences",
        agents_mechanics="Branching dialogue graphs with character emotion and plot triggers",
        unknown_fields=["experience.pricing", "trust.license_status", "world_structure.economy"],
        reviewer_notes="B2B narrative platform with public demo experiences qualifying as worlds.",
        confidence=0.82,
        source_observed_at="2025-11-01",
        summary="Graph-driven interactive stories with runtime character AI.",
        setting="Creator-authored story graphs with defined characters and beats",
    ),
    # --- ai_spatial (5 qualifying) ---
    _candidate(
        "wg-200-spatial-001",
        name="World Labs Marble",
        canonical_source="https://marble.worldlabs.ai/",
        category="ai_spatial",
        qualifies=True,
        creator_operator="World Labs",
        entry_point="https://marble.worldlabs.ai/",
        accessibility="Public web waitlist/invite; demo videos and docs on product page",
        ai_role="Generative world models produce explorable 3D environments from text or images",
        persistence="Saved world generations tied to user accounts",
        platform_runtime="Web (Marble)",
        agents_mechanics="Navigate and inspect generated spatial scenes; iterate on world prompts",
        unknown_fields=["trust.license_status", "world_structure.agents_and_characters", "experience.age_guidance"],
        reviewer_notes="Reference AI spatial world product cited in Manifest v0 fixtures.",
        confidence=0.9,
        source_observed_at="2025-12-01",
        summary="Explorable AI-generated 3D worlds with persistent saved generations.",
        setting="User-prompted spatial scenes with navigable geometry",
    ),
    _candidate(
        "wg-200-spatial-002",
        name="Decart Oasis",
        canonical_source="https://oasis.decart.ai/",
        category="ai_spatial",
        qualifies=True,
        creator_operator="Decart",
        entry_point="https://oasis.decart.ai/",
        accessibility="Free browser demo without install",
        ai_role="Real-time generative model renders interactive open-world video from player input",
        persistence="Session-based exploration; model weights and prompts documented externally",
        platform_runtime="Web browser",
        agents_mechanics="Keyboard/mouse navigation affects generated world stream in real time",
        unknown_fields=["trust.license_status", "experience.persistence_model", "world_structure.economy"],
        reviewer_notes="Session persistence weaker than Marble; still qualifies via reproducible public demo.",
        confidence=0.84,
        source_observed_at="2025-09-01",
        summary="Real-time AI-generated explorable world stream in the browser.",
        setting="Open-world visual environment generated from latent world model",
    ),
    _candidate(
        "wg-200-spatial-003",
        name="Fermat",
        canonical_source="https://fermat.app/",
        category="ai_spatial",
        qualifies=True,
        creator_operator="Fermat",
        entry_point="https://fermat.app/",
        accessibility="Web signup for creation and exploration features",
        ai_role="AI assists creation and populates interactive 3D spaces users can explore",
        persistence="Published spaces persist on platform with version history",
        platform_runtime="Web",
        agents_mechanics="Create, publish, and walk through user-generated 3D spaces",
        unknown_fields=["trust.license_status", "ai_role.model_disclosures", "experience.age_guidance"],
        reviewer_notes="UGC spatial worlds with material AI-assisted environment generation.",
        confidence=0.81,
        source_observed_at="2025-10-15",
        summary="Collaborative 3D spaces with AI-assisted world building.",
        setting="User-created explorable rooms and environments",
    ),
    _candidate(
        "wg-200-spatial-004",
        name="Luma AI Genie",
        canonical_source="https://lumalabs.ai/genie",
        category="ai_spatial",
        qualifies=True,
        creator_operator="Luma AI",
        entry_point="https://lumalabs.ai/genie",
        accessibility="Web access with account; mobile app linked",
        ai_role="Text-to-3D world generation produces navigable scenes",
        persistence="Library of saved generations per account",
        platform_runtime="Web and iOS",
        agents_mechanics="Prompt, generate, and preview explorable 3D worlds",
        unknown_fields=["world_structure.agents_and_characters", "trust.commercial_use_status"],
        reviewer_notes="Generation tool plus explorable output qualifies as spatial world experience.",
        confidence=0.83,
        source_observed_at="2025-08-01",
        summary="Text-prompted 3D worlds users can preview and explore.",
        setting="Generated 3D scenes from natural-language prompts",
    ),
    _candidate(
        "wg-200-spatial-005",
        name="Blockade Labs Skybox AI",
        canonical_source="https://skybox.blockadelabs.com/",
        category="ai_spatial",
        qualifies=True,
        creator_operator="Blockade Labs",
        entry_point="https://skybox.blockadelabs.com/",
        accessibility="Free tier with account for generation and download",
        ai_role="Generative models produce 360° immersive skyboxes from prompts",
        persistence="Saved skybox library and share URLs",
        platform_runtime="Web",
        agents_mechanics="Generate, remix, and export immersive environments for real-time engines",
        rights="Public terms describe commercial licensing tiers",
        unknown_fields=["world_structure.agents_and_characters", "experience.age_guidance"],
        reviewer_notes="Borderline: environment slices rather than full worlds, but interactive creation + exploration qualifies.",
        confidence=0.78,
        source_observed_at="2025-07-01",
        summary="Prompt-driven immersive 360° environments with persistent library.",
        setting="Prompt-defined panoramic worlds exportable to game engines",
    ),
    # --- agent_simulation (5 qualifying) ---
    _candidate(
        "wg-200-simulation-001",
        name="AI Town",
        canonical_source="https://www.convex.dev/ai-town",
        category="agent_simulation",
        qualifies=True,
        creator_operator="Convex (a16z-infra open-source example)",
        entry_point="https://github.com/a16z-infra/ai-town",
        accessibility="Open-source repo with local deploy instructions and hosted demo links",
        ai_role="LLM agents plan actions, converse, and update world state each game tick",
        persistence="Convex database stores agent memories and world positions",
        platform_runtime="Web (Convex backend)",
        agents_mechanics="Multi-agent village simulation with movement, conversation, and memory",
        rights="MIT License on repository",
        unknown_fields=["experience.age_guidance", "trust.moderation_contact"],
        reviewer_notes="Canonical open-source agent society reference implementation.",
        confidence=0.93,
        source_observed_at="2024-05-01",
        summary="Persistent multi-agent village simulation with LLM-driven behavior.",
        setting="Small-town map with scheduled agent activities and social rules",
    ),
    _candidate(
        "wg-200-simulation-002",
        name="Generative Agents (Smallville)",
        canonical_source="https://github.com/joonspk-research/generative_agents",
        category="agent_simulation",
        qualifies=True,
        creator_operator="Stanford HCI / joonspk-research",
        entry_point="https://github.com/joonspk-research/generative_agents",
        accessibility="Research repo with reproduction instructions; no hosted public demo",
        ai_role="LLM agents reflect, plan, and interact in a sandbox town simulation",
        persistence="Simulation state serialized in repo artifacts and replay logs",
        platform_runtime="Local Python simulation",
        agents_mechanics="25 agents with daily schedules, relationships, and emergent social behavior",
        rights="MIT License",
        unknown_fields=["experience.entry_points_public_hosted", "trust.moderation_contact"],
        reviewer_notes="Reproducible research simulation; entry is repo not live SaaS.",
        confidence=0.91,
        source_observed_at="2023-04-01",
        summary="Research sandbox where LLM agents live out persistent daily routines.",
        setting="Smallville town map with locations and social norms",
    ),
    _candidate(
        "wg-200-simulation-003",
        name="Voyager",
        canonical_source="https://voyager.minedojo.org/",
        category="agent_simulation",
        qualifies=True,
        creator_operator="MineDojo / NVIDIA Research collaborators",
        entry_point="https://github.com/MineDojo/Voyager",
        accessibility="Paper site links to open-source code and Minecraft dependency",
        ai_role="Embodied LLM agent learns skills, explores, and builds in Minecraft world",
        persistence="Skill library and world checkpoints saved across episodes",
        platform_runtime="Minecraft Java Edition",
        agents_mechanics="Autonomous exploration, crafting, tech tree progression via code execution",
        rights="Apache-2.0 on code; Minecraft license separate",
        unknown_fields=["experience.age_guidance", "world_structure.economy"],
        reviewer_notes="Strong simulation-world reference with reproducible configs.",
        confidence=0.9,
        source_observed_at="2023-05-01",
        summary="LLM-driven lifelong learning agent in open-ended Minecraft world.",
        setting="Minecraft overworld with survival mechanics and craft tree",
    ),
    _candidate(
        "wg-200-simulation-004",
        name="Concordia",
        canonical_source="https://github.com/google-deepmind/concordia",
        category="agent_simulation",
        qualifies=True,
        creator_operator="Google DeepMind",
        entry_point="https://github.com/google-deepmind/concordia",
        accessibility="Apache-2.0 research library with example simulations",
        ai_role="Language-model agents act in turn-based simulated environments",
        persistence="Simulation configs and logs reproducible from repository examples",
        platform_runtime="Python library",
        agents_mechanics="Game-master orchestrated scenarios with participant agents and state updates",
        rights="Apache-2.0",
        unknown_fields=["experience.public_hosted_entry", "trust.moderation_contact"],
        reviewer_notes="Framework ships runnable example worlds; qualifies as reproducible simulation.",
        confidence=0.87,
        source_observed_at="2024-02-01",
        summary="Research framework for language-agent simulations in bounded scenarios.",
        setting="Scenario scripts define locations, roles, and interaction rules",
    ),
    _candidate(
        "wg-200-simulation-005",
        name="CAMEL AI Oasis",
        canonical_source="https://docs.oasis.camel-ai.org/",
        category="agent_simulation",
        qualifies=True,
        creator_operator="CAMEL-AI.org",
        entry_point="https://github.com/camel-ai/oasis",
        accessibility="Documentation and GitHub repo with install instructions",
        ai_role="Large language model agents populate social media-style simulated worlds",
        persistence="Simulation state and agent profiles stored across ticks",
        platform_runtime="Python / local or cloud deploy",
        agents_mechanics="Thousands of agents interact via programmed social actions",
        rights="Apache-2.0 on repository",
        unknown_fields=["experience.age_guidance", "trust.moderation_contact"],
        reviewer_notes="Scalable agent society benchmark environment.",
        confidence=0.85,
        source_observed_at="2024-11-01",
        summary="Large-scale multi-agent social simulation platform.",
        setting="Social platform graph with agent personas and action API",
    ),
    # --- ai_game_ugc (5 qualifying) ---
    _candidate(
        "wg-200-game-001",
        name="Inworld Arcade",
        canonical_source="https://inworld.ai/arcade",
        category="ai_game_ugc",
        qualifies=True,
        creator_operator="Inworld AI",
        entry_point="https://inworld.ai/arcade",
        accessibility="Free browser demos; account for advanced creation",
        ai_role="Runtime character AI drives NPC dialogue, goals, and reactions in mini-games",
        persistence="Player session state and character memory in demo experiences",
        platform_runtime="Web",
        agents_mechanics="Playable mini-experiences with AI NPCs and quest-like goals",
        unknown_fields=["trust.license_status", "world_structure.economy"],
        reviewer_notes="Public demo arcade satisfies game/UGC category with material runtime AI.",
        confidence=0.89,
        source_observed_at="2025-03-01",
        summary="Collection of playable demos showcasing AI NPC runtime in game-like worlds.",
        setting="Individual arcade rooms each with defined NPC cast and objectives",
    ),
    _candidate(
        "wg-200-game-002",
        name="Convai Demo Experiences",
        canonical_source="https://convai.com/",
        category="ai_game_ugc",
        qualifies=True,
        creator_operator="Convai Technologies",
        entry_point="https://convai.com/",
        accessibility="Web demos and SDK docs linked from homepage",
        ai_role="Conversational AI characters with spatial awareness in game engine demos",
        persistence="Session-based NPC memory in integrated demos",
        platform_runtime="Web demos; Unity/Unreal SDK integrations",
        agents_mechanics="Voice/text interaction with NPCs that perceive environment triggers",
        unknown_fields=["trust.license_status", "experience.pricing"],
        reviewer_notes="Demo worlds illustrate UGC pipeline for AI NPCs in games.",
        confidence=0.84,
        source_observed_at="2025-05-01",
        summary="Interactive game-engine demos with spatially aware AI characters.",
        setting="Demo levels with defined NPC roles and trigger volumes",
    ),
    _candidate(
        "wg-200-game-003",
        name="Rosebud AI",
        canonical_source="https://rosebud.ai/",
        category="ai_game_ugc",
        qualifies=True,
        creator_operator="Rosebud AI",
        entry_point="https://rosebud.ai/",
        accessibility="Web creation platform with playable published games",
        ai_role="AI assists asset and logic generation; runtime behavior in published games",
        persistence="Published games persist with shareable URLs",
        platform_runtime="Web",
        agents_mechanics="Create and play 2D/3D games with AI-generated assets and scripts",
        unknown_fields=["trust.license_status", "experience.age_guidance"],
        reviewer_notes="UGC platform where playable outputs qualify as worlds.",
        confidence=0.8,
        source_observed_at="2025-04-01",
        summary="AI-assisted game creation platform with persistent published playable worlds.",
        setting="User-defined game scenes with mechanics and win conditions",
    ),
    _candidate(
        "wg-200-game-004",
        name="Scenario",
        canonical_source="https://www.scenario.com/",
        category="ai_game_ugc",
        qualifies=True,
        creator_operator="Scenario S.A.",
        entry_point="https://www.scenario.com/",
        accessibility="Account required; API and web app documented",
        ai_role="Generative models produce consistent game assets used inside playable experiences",
        persistence="Project workspaces retain trained styles and asset sets",
        platform_runtime="Web",
        agents_mechanics="Teams build game asset pipelines feeding interactive prototypes",
        unknown_fields=["experience.public_playable_entry", "world_structure.agents_and_characters"],
        reviewer_notes="Borderline: primarily asset tooling; included because public docs link playable game integrations.",
        confidence=0.72,
        source_observed_at="2025-02-01",
        summary="Game asset generation platform tied to playable UGC prototypes.",
        setting="Game projects with style-locked generative asset pipelines",
    ),
    _candidate(
        "wg-200-game-005",
        name="HiberWorld",
        canonical_source="https://hiberworld.com/",
        category="ai_game_ugc",
        qualifies=True,
        creator_operator="Hiber",
        entry_point="https://hiberworld.com/",
        accessibility="Web and mobile clients; creation tools documented",
        ai_role="AI assists 3D world creation; users explore published worlds",
        persistence="Published worlds persist on platform with social features",
        platform_runtime="Web and mobile",
        agents_mechanics="Create, publish, and explore user-generated 3D worlds",
        unknown_fields=["trust.license_status", "ai_role.model_disclosures"],
        reviewer_notes="UGC 3D world platform related to AI-assisted creation marketing.",
        confidence=0.79,
        source_observed_at="2025-01-01",
        summary="Social 3D world creation and exploration platform.",
        setting="User-built 3D spaces with platform movement mechanics",
    ),
    # --- persistent_social (5 qualifying) ---
    _candidate(
        "wg-200-social-001",
        name="Character.AI",
        canonical_source="https://character.ai/",
        category="persistent_social",
        qualifies=True,
        creator_operator="Character Technologies, Inc.",
        entry_point="https://character.ai/",
        accessibility="Free tier with registration; mobile apps available",
        ai_role="Users chat with creator-defined AI characters in persistent rooms and group chats",
        persistence="Conversation history and character memory persist per user",
        platform_runtime="Web and mobile",
        agents_mechanics="1:1 and group chats with multiple AI characters and user participants",
        safety_age="Teen rating; community guidelines published",
        unknown_fields=["world_structure.economy", "trust.license_status"],
        reviewer_notes="Social companion rooms qualify; overlaps narrative Scenes but distinct social graph.",
        confidence=0.87,
        source_observed_at="2026-01-01",
        summary="Persistent social chat worlds populated by user-created AI characters.",
        setting="Character personas with bios, greeting messages, and conversation boundaries",
    ),
    _candidate(
        "wg-200-social-002",
        name="Chai",
        canonical_source="https://www.chai-research.com/",
        category="persistent_social",
        qualifies=True,
        creator_operator="Chai Research Corp.",
        entry_point="https://chai.ml/",
        accessibility="Mobile apps; web landing redirects to app stores",
        ai_role="User-created AI bots converse in persistent chat sessions with memory",
        persistence="Chat history and bot definitions saved to accounts",
        platform_runtime="iOS and Android",
        agents_mechanics="Swipe-to-chat interface with creator-published bot personas",
        unknown_fields=["experience.web_entry_point", "trust.license_status"],
        reviewer_notes="Mobile-first social AI chat world pattern.",
        confidence=0.83,
        source_observed_at="2025-09-01",
        summary="Social platform of user-created AI chatbots with persistent conversations.",
        setting="Bot personas with creator-defined prompts and traits",
    ),
    _candidate(
        "wg-200-social-003",
        name="VRChat",
        canonical_source="https://hello.vrchat.com/",
        category="persistent_social",
        qualifies=True,
        creator_operator="VRChat Inc.",
        entry_point="https://vrchat.com/home",
        accessibility="Free client; account required; user-generated worlds",
        ai_role="Third-party and official worlds integrate runtime AI NPC plugins (documented community examples)",
        persistence="Persistent avatars, world instances, and social graphs",
        platform_runtime="VRChat client (PC/VR/Quest)",
        agents_mechanics="Social presence, world instances, UGC worlds with embedded gameplay scripts",
        safety_age="13+ in terms; trust and safety pages published",
        access_requirements="account_and_client_required",
        unknown_fields=["ai_role.material_ai_role_per_world", "trust.license_status"],
        reviewer_notes="Platform hosts many worlds; corpus entry treats VRChat as persistent social world host with material AI in select UGC.",
        confidence=0.76,
        source_observed_at="2026-03-01",
        linked_research_entity="platform:vrchat",
        summary="Persistent social metaverse with UGC worlds; AI NPC integrations vary by world.",
        setting="User-generated worlds with avatars, physics, and social norms",
    ),
    _candidate(
        "wg-200-social-004",
        name="Second Life",
        canonical_source="https://www.secondlife.com/",
        category="persistent_social",
        qualifies=True,
        creator_operator="Linden Lab",
        entry_point="https://secondlife.com/support/downloads/",
        accessibility="Free client; premium land fees documented",
        ai_role="Select regions and tools integrate NPC scripts; LLM integrations documented by community and Linden partners",
        persistence="Long-running user economy, land ownership, and avatar identity",
        platform_runtime="Second Life viewer",
        agents_mechanics="Virtual land, economy, events, and user-created experiences",
        safety_age="16+ registration requirement stated",
        access_requirements="account_and_client_required",
        unknown_fields=["ai_role.material_ai_role_default", "world_structure.governance"],
        reviewer_notes="Historic persistent world; AI role emerging via plugins rather than core product.",
        confidence=0.74,
        source_observed_at="2026-02-01",
        linked_research_entity="platform:second_life",
        summary="Long-lived persistent virtual world with user economy and social systems.",
        setting="Continent-based virtual world with user-owned regions",
    ),
    _candidate(
        "wg-200-social-005",
        name="IMVU",
        canonical_source="https://www.imvu.com/",
        category="persistent_social",
        qualifies=True,
        creator_operator="IMVU, Inc.",
        entry_point="https://www.imvu.com/",
        accessibility="Free client with in-app purchases",
        ai_role="AI chat bots and creator tools documented for interactive rooms",
        persistence="Avatar inventory, rooms, and relationships persist",
        platform_runtime="Desktop and mobile clients",
        agents_mechanics="Social rooms, avatar customization, chat, and virtual goods economy",
        safety_age="13+ or 18+ depending on room rating",
        unknown_fields=["ai_role.model_disclosures", "trust.license_status"],
        reviewer_notes="Social economy world with increasing AI chat integrations.",
        confidence=0.77,
        source_observed_at="2025-12-01",
        summary="Avatar-based social world with persistent rooms and virtual economy.",
        setting="Themed chat rooms with avatar expression and goods",
    ),
    # --- negative controls (5 excluded) ---
    _candidate(
        "wg-200-negative-001",
        name="ChatGPT",
        canonical_source="https://chat.openai.com/",
        category="negative_control",
        qualifies=False,
        exclusion_reason="single_purpose_assistant",
        creator_operator="OpenAI",
        entry_point="https://chat.openai.com/",
        accessibility="Account required; usage limits on free tier",
        ai_role="General-purpose conversational assistant without bounded world context",
        persistence="Chat threads persist but no world state model",
        platform_runtime="Web and mobile",
        agents_mechanics="Single assistant thread without canon, map, or simulation rules",
        safety_age="Usage policies published",
        unknown_fields=["world_structure.setting", "world_structure.rules_or_mechanics"],
        reviewer_notes="Canonical negative control for Rule 3 failure.",
        confidence=0.95,
        source_observed_at="2026-01-01",
    ),
    _candidate(
        "wg-200-negative-002",
        name="Midjourney Explore",
        canonical_source="https://www.midjourney.com/explore",
        category="negative_control",
        qualifies=False,
        exclusion_reason="static_ai_media_only",
        creator_operator="Midjourney, Inc.",
        entry_point="https://www.midjourney.com/explore",
        accessibility="Discord or web account required",
        ai_role="Image generation model produces static images",
        persistence="Gallery of generated images",
        platform_runtime="Web and Discord",
        agents_mechanics="Browse grid of images; no interactive world state changes",
        unknown_fields=["experience.interaction_model", "world_structure.state_model"],
        reviewer_notes="Static media gallery negative control.",
        confidence=0.94,
        source_observed_at="2025-11-01",
    ),
    _candidate(
        "wg-200-negative-003",
        name="Unreal Engine",
        canonical_source="https://www.unrealengine.com/",
        category="negative_control",
        qualifies=False,
        exclusion_reason="platform_product_not_world",
        creator_operator="Epic Games",
        entry_point="https://www.unrealengine.com/download",
        accessibility="Free engine download with royalty terms",
        ai_role="Engine tooling may integrate AI plugins; product is SDK not a world",
        persistence="Projects saved locally; no single shared world",
        platform_runtime="Desktop engine",
        agents_mechanics="Authoring tools, not a playable world instance",
        rights="Epic Games license terms",
        unknown_fields=["experience.entry_points_world_instance"],
        reviewer_notes="Linked research entity (engine); must not count as World.",
        confidence=0.96,
        source_observed_at="2026-02-01",
        linked_research_entity="engine:unreal_engine",
    ),
    _candidate(
        "wg-200-negative-004",
        name="OpenAI GPT-4",
        canonical_source="https://openai.com/index/gpt-4/",
        category="negative_control",
        qualifies=False,
        exclusion_reason="foundation_model_or_tool_not_world",
        creator_operator="OpenAI",
        entry_point="https://platform.openai.com/docs/models/gpt-4",
        accessibility="API access via developer account",
        ai_role="Foundation model weights/API capability",
        persistence="No world state; model versioning only",
        platform_runtime="Cloud API",
        agents_mechanics="Token prediction API without world container",
        rights="OpenAI usage policies",
        unknown_fields=["world_structure.setting", "experience.entry_points"],
        reviewer_notes="Foundation model negative control.",
        confidence=0.97,
        source_observed_at="2025-06-01",
        linked_research_entity="model:gpt-4",
    ),
    _candidate(
        "wg-200-negative-005",
        name="Roblox Platform",
        canonical_source="https://www.roblox.com/",
        category="negative_control",
        qualifies=False,
        exclusion_reason="platform_product_not_world",
        creator_operator="Roblox Corporation",
        entry_point="https://www.roblox.com/download",
        accessibility="Client required; many user worlds inside platform",
        ai_role="Platform hosts experiences; AI features documented at platform level",
        persistence="Platform accounts persist; individual worlds are separate entries",
        platform_runtime="Roblox client",
        agents_mechanics="App store for experiences, not one world",
        safety_age="Family-friendly policies and parental controls documented",
        unknown_fields=["identity.world_id_for_platform"],
        reviewer_notes="Platform retained as linked entity; individual Roblox experiences would be separate world candidates.",
        confidence=0.93,
        source_observed_at="2026-03-01",
        linked_research_entity="platform:roblox",
    ),
]


def build_corpus_payload() -> dict:
    qualifying = sum(1 for c in CANDIDATES if c["qualification"]["status"] == "qualifies")
    excluded = sum(1 for c in CANDIDATES if c["qualification"]["status"] == "excluded")
    categories = sorted({c["candidate_category"] for c in CANDIDATES})
    return {
        "schema_version": "worldgraph-research-corpus-v1",
        "research_only": True,
        "not_for_automatic_publication": True,
        "parent_issue": 200,
        "depends_on_issue": 199,
        "last_updated": LAST_CHECKED,
        "summary": {
            "total_candidates": len(CANDIDATES),
            "qualifying_worlds": qualifying,
            "excluded_controls": excluded,
            "categories_represented": categories,
        },
        "candidates": CANDIDATES,
    }


def write_candidates_yaml(payload: dict) -> None:
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    header = (
        "# WorldGraph research corpus — issue #200\n"
        "# RESEARCH ONLY — not production content and not automatically published.\n"
        "# Qualification rules: docs/worldgraph/WORLD_DEFINITION.md\n"
        "# Manifest v0 schema: docs/worldgraph/world-manifest-v0.schema.json\n"
    )
    CANDIDATES_PATH.write_text(
        header + yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def write_manifests(candidates: list[dict]) -> list[str]:
    MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for candidate in candidates:
        if candidate["qualification"]["status"] != "qualifies":
            continue
        manifest = build_qualifying_manifest(candidate)
        path = MANIFESTS_DIR / f"{candidate['id']}.json"
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        written.append(path.name)
    return written


def validate_manifests(candidates: list[dict]) -> dict:
    import jsonschema
    from jsonschema import Draft202012Validator

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    results: list[dict] = []
    for candidate in candidates:
        if candidate["qualification"]["status"] != "qualifies":
            continue
        manifest_path = MANIFESTS_DIR / f"{candidate['id']}.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = {
            "candidate_id": candidate["id"],
            "manifest_path": f"manifests/{candidate['id']}.json",
            "json_schema_valid": False,
            "spike_validator_valid": False,
            "errors": [],
        }
        try:
            validator.validate(manifest)
            entry["json_schema_valid"] = True
        except jsonschema.ValidationError as exc:
            entry["errors"].append(f"jsonschema: {exc.message}")
        try:
            validate_manifest_v0(manifest)
            entry["spike_validator_valid"] = True
        except Exception as exc:  # noqa: BLE001
            entry["errors"].append(f"spike: {exc}")
        results.append(entry)
    return {
        "schema_version": "worldgraph-corpus-validation-v1",
        "validated_at": f"{LAST_CHECKED}T12:00:00+00:00",
        "schema_path": "docs/worldgraph/world-manifest-v0.schema.json",
        "total_qualifying": len(results),
        "all_valid": all(r["json_schema_valid"] and r["spike_validator_valid"] for r in results),
        "results": results,
    }


def main() -> int:
    payload = build_corpus_payload()
    write_candidates_yaml(payload)
    manifests = write_manifests(payload["candidates"])
    validation = validate_manifests(payload["candidates"])
    VALIDATION_PATH.write_text(json.dumps(validation, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {CANDIDATES_PATH}")
    print(f"Wrote {len(manifests)} manifests under {MANIFESTS_DIR}")
    print(f"Wrote {VALIDATION_PATH} (all_valid={validation['all_valid']})")
    if not validation["all_valid"]:
        for result in validation["results"]:
            if result["errors"]:
                print(f"  {result['candidate_id']}: {result['errors']}")
        return 1
    assert payload["summary"]["total_candidates"] >= 30
    assert payload["summary"]["qualifying_worlds"] >= 20
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
