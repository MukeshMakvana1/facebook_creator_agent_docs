"""Stage helpers: script prompts, storyboard construction, publish payload.

These functions are pure (no IO) so they can be unit tested exhaustively;
the orchestrator wires them to providers and the project store.
"""

from __future__ import annotations

from typing import Any, Optional

from ..core.errors import CreatorError, ErrorCode
from ..core.ids import iso, utcnow
from ..core.models import AssetRecord, Project, Scene, ScriptDraft, Storyboard

SCRIPT_SYSTEM_PROMPT = """You are a professional short-form video scriptwriter for Facebook.
You write narration for stock-footage videos edited with captions.
Hard rules:
- Respond with a single JSON object matching the requested schema. No markdown.
- Narration is spoken text only: no stage directions, no camera notes, no emoji.
- Each scene needs a short stock-media search query in English (2-5 plain words).
- on_screen_text is at most 6 words, or an empty string.
- Never invent statistics, dates, quotes, or studies. Prefer general, evergreen
  statements. If a claim would need verification, write it as an observation,
  not a fact.
- Caption: at most 2000 characters. Hashtags: 3-6 items, lowercase, no '#'.
- Match the requested language for narration, caption, and hook.
"""


def scene_budget(max_scenes: int, target_duration_seconds: int) -> int:
    """Scene count target: ~6s per scene, clamped to the configured limit."""
    return max(3, min(max_scenes, max(1, round(target_duration_seconds / 6))))


def build_script_prompts(
    project: Project,
    *,
    max_scenes: int = 60,
    revision_instructions: Optional[str] = None,
    research_note: Optional[str] = None,
    previous_script: Optional[str] = None,
) -> tuple[str, str]:
    count = scene_budget(max_scenes, project.target_duration_seconds)
    user_lines = [
        f"Topic: {project.topic}",
        f"Language (BCP-47): {project.language}",
        f"Target total duration: {project.target_duration_seconds} seconds (about {count} scenes of 4-8s)",
        f"Aspect ratio: {project.aspect_ratio}",
        f"Tone: {project.tone or 'engaging, informative'}",
        f"Audience: {project.audience or 'general audience'}",
    ]
    if project.brand_instructions:
        user_lines.append(f"Brand instructions (must follow): {project.brand_instructions}")
    if research_note:
        user_lines.append(f"Research: {research_note}")
    else:
        user_lines.append(
            "Research: not available. Do not cite sources and avoid verifiable-specific claims."
        )
    if revision_instructions:
        user_lines.append(f"Revision instructions (apply exactly): {revision_instructions}")
        if previous_script:
            user_lines.append(f"Previous script to revise:\n{previous_script[:4000]}")
    user_lines.append(
        "Return JSON with keys: title, locale, hook, scenes (list of "
        "{scene_id, narration, on_screen_text, visual_description, media_query, target_seconds}), "
        "cta, caption, hashtags."
    )
    return SCRIPT_SYSTEM_PROMPT, "\n".join(user_lines)


def draft_to_storyboard(
    project: Project,
    draft: ScriptDraft,
    *,
    research_status: str,
    max_scenes: int = 60,
    unverified_claims: Optional[list[str]] = None,
) -> Storyboard:
    """Convert a validated LLM draft into a deterministic storyboard.

    Duration allocation: scenes with a positive ``target_seconds`` keep their
    relative weight; weights are rescaled so the total matches the project's
    target duration exactly.
    """
    if not draft.scenes:
        raise CreatorError(
            ErrorCode.MODEL_OUTPUT_INVALID,
            "Model returned a script with no scenes.",
            stage="script",
            next_action="Re-run creator_generate_script, optionally with revision instructions.",
        )
    if len(draft.scenes) > max_scenes:
        raise CreatorError(
            ErrorCode.MODEL_OUTPUT_INVALID,
            f"Model returned {len(draft.scenes)} scenes; the configured limit is {max_scenes}.",
            stage="script",
            next_action="Ask for fewer scenes or raise the limit.",
        )

    raw_scenes: list[dict[str, Any]] = []
    for index, item in enumerate(draft.scenes):
        if not isinstance(item, dict):
            raise CreatorError(
                ErrorCode.MODEL_OUTPUT_INVALID,
                f"Scene {index + 1} is not an object.",
                stage="script",
                next_action="Re-run creator_generate_script.",
            )
        narration = str(item.get("narration") or "").strip()
        if not narration:
            raise CreatorError(
                ErrorCode.MODEL_OUTPUT_INVALID,
                f"Scene {index + 1} has no narration text.",
                stage="script",
                next_action="Re-run creator_generate_script with more explicit instructions.",
            )
        raw_scenes.append(item)

    weights: list[float] = []
    for item in raw_scenes:
        try:
            weight = float(item.get("target_seconds") or 0.0)
        except (TypeError, ValueError):
            weight = 0.0
        weights.append(weight if weight > 0 else project.target_duration_seconds / len(raw_scenes))
    total_weight = sum(weights) or float(project.target_duration_seconds)
    target_total = float(project.target_duration_seconds)

    scenes: list[Scene] = []
    for index, (item, weight) in enumerate(zip(raw_scenes, weights)):
        narration = str(item.get("narration")).strip()
        scene_id = str(item.get("scene_id") or f"s{index + 1:02d}")
        scene_id = "".join(ch for ch in scene_id if ch.isalnum() or ch in ("-", "_"))[:24] or f"s{index + 1:02d}"
        scenes.append(
            Scene(
                index=index,
                scene_id=scene_id,
                narration=narration,
                on_screen_text=(str(item.get("on_screen_text") or "").strip() or None),
                visual_description=(str(item.get("visual_description") or "").strip() or None),
                media_query=(str(item.get("media_query") or "").strip() or project.topic),
                target_seconds=round(max(0.5, target_total * weight / total_weight), 3),
            )
        )

    return Storyboard(
        project_id=project.project_id,
        script_version=project.script_version + 1,
        title=(draft.title or project.topic).strip(),
        locale=draft.locale or project.language,
        hook=draft.hook,
        cta=draft.cta,
        caption=(draft.caption or "").strip(),
        hashtags=[h.lstrip("#").strip().lower() for h in draft.hashtags if h and h.strip()][:10],
        total_target_seconds=target_total,
        scenes=scenes,
        research_status="available" if research_status == "available" else "unavailable",
        unverified_claims=unverified_claims or [],
        generated_at=utcnow(),
    )


def script_markdown(storyboard: Storyboard, *, sources: Optional[list[dict]] = None) -> str:
    lines = [
        f"# {storyboard.title}",
        "",
        f"- Version: {storyboard.script_version}",
        f"- Locale: {storyboard.locale}",
        f"- Target: {storyboard.total_target_seconds:.0f}s",
        f"- Research: {storyboard.research_status}",
        "",
    ]
    if storyboard.hook:
        lines += ["## Hook", "", storyboard.hook, ""]
    lines += ["## Scenes", ""]
    for scene in storyboard.scenes:
        lines.append(f"### {scene.index + 1}. {scene.scene_id} ({scene.target_seconds:.0f}s)")
        lines.append("")
        lines.append(f"- Narration: {scene.narration}")
        if scene.on_screen_text:
            lines.append(f"- On screen: {scene.on_screen_text}")
        if scene.visual_description:
            lines.append(f"- Visual: {scene.visual_description}")
        lines.append(f"- Media query: {scene.media_query}")
        lines.append("")
    if storyboard.cta:
        lines += ["## Call to action", "", storyboard.cta, ""]
    lines += ["## Caption", "", storyboard.caption or "(none)", ""]
    if storyboard.hashtags:
        lines += ["## Hashtags", "", " ".join(f"#{h}" for h in storyboard.hashtags), ""]
    if sources:
        lines += ["## Sources", ""]
        for source in sources[:20]:
            lines.append(f"- {source.get('title') or source.get('url')}: {source.get('url')}")
        lines.append("")
    if storyboard.research_status != "available":
        lines += [
            "> Research was unavailable: factual claims in this script are unverified.",
            "",
        ]
    return "\n".join(lines)


def attribution_block(assets: list[AssetRecord]) -> list[str]:
    """Human-readable attribution lines for assets that carry attribution."""
    lines: list[str] = []
    for asset in assets:
        text = (asset.attribution or "").strip()
        if not text and not asset.license_name:
            continue
        parts = []
        if asset.title:
            parts.append(asset.title)
        if text:
            parts.append(text)
        if asset.license_name:
            lic = asset.license_name
            if asset.license_url:
                lic = f"{lic} ({asset.license_url})"
            parts.append(lic)
        if parts:
            lines.append(" — ".join(parts))
    return lines


def build_publish_payload(
    *,
    project: Project,
    storyboard: Storyboard,
    page_id: str,
    media_rel: str,
    media_sha256: str,
    attribution: list[str],
) -> dict[str, Any]:
    """The exact object a user approves and the publisher revalidates."""
    caption = (storyboard.caption or "").strip()
    hashtags = ["#" + h for h in storyboard.hashtags]
    if hashtags:
        caption = (caption + "\n\n" + " ".join(hashtags)).strip()
    return {
        "action": "PUBLISH_VIDEO",
        "project_id": project.project_id,
        "page_id": str(page_id),
        "title": (storyboard.title or project.topic).strip()[:255],
        "caption": caption[:5000],
        "hashtags": hashtags,
        "video": media_rel,
        "media_sha256": media_sha256,
        "attribution": attribution,
        "generated_at": iso(utcnow()),
    }
