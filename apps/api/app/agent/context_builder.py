from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session
from workflow_engine import StepStatus

from app.agent.retrieval_service import AgentRetrievalService
from app.db.models import Project
from app.services.pipeline_service import PipelineService
from app.services.style_service import normalize_style_profile


class AgentContextBuilder:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.pipeline = PipelineService(db)
        self.retrieval = AgentRetrievalService(db)

    def build(self, project: Project, user_text: str, page_context: dict[str, Any] | None = None) -> dict[str, Any]:
        page_context = page_context or {}
        steps = self.pipeline.list_steps(project.id)
        step_by_name = {step.step_name: step for step in steps}
        chapters = self.pipeline.list_chapters(project.id)
        timeline = self.pipeline.project_timeline(project)
        story_bible = normalize_style_profile(project.style_profile).get("story_bible", {})
        step_counts = self._count_statuses(item.status for item in steps)
        chapter_stage_counts = self._count_statuses(
            status
            for chapter in chapters
            for status in (chapter.get("stage_map", {}) or {}).values()
        )
        failed_chapters = [chapter for chapter in chapters if self._chapter_has_status(chapter, {StepStatus.FAILED.value})]
        rework_chapters = [
            chapter for chapter in chapters if self._chapter_has_status(chapter, {StepStatus.REWORK_REQUESTED.value})
        ]
        review_required_chapters = [
            chapter for chapter in chapters if self._chapter_has_status(chapter, {StepStatus.REVIEW_REQUIRED.value})
        ]
        current_step = next((step for step in steps if step.status != StepStatus.APPROVED.value), steps[-1] if steps else None)
        selected_step_key = str(page_context.get("selected_step_key") or "").strip() or getattr(current_step, "step_name", None)
        selected_step_name = str(page_context.get("selected_step_name") or "").strip()
        selected_chapter_id = str(page_context.get("selected_chapter_id") or "").strip()
        selected_chapter = next((chapter for chapter in chapters if chapter.get("id") == selected_chapter_id), None)
        if not selected_chapter and chapters:
            selected_chapter = chapters[0]
        selected_step = step_by_name.get(str(selected_step_key or "").strip()) if selected_step_key else None
        prompt_snapshot = (
            self.pipeline.get_active_prompt_snapshot(project.id, selected_step.step_name)
            if selected_step is not None
            else None
        )
        selected_stage_summary = self._selected_stage_summary(selected_chapter, getattr(selected_step, "step_name", None))

        retrieval_hits = self.retrieval.search_project_knowledge(project, user_text, limit=6)
        story_bible_characters = [
            str(item.get("name") or "").strip()
            for item in story_bible.get("characters", []) or []
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        story_bible_scenes = [
            str(item.get("name") or "").strip()
            for item in story_bible.get("scenes", []) or []
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]

        sources: list[dict[str, Any]] = [
            {
                "kind": "project_overview",
                "label": "项目总览",
                "snippet": f"项目状态 {project.status}，章节 {len(chapters)}，当前步骤 {getattr(current_step, 'step_display_name', '-')}",
            }
        ]
        if selected_step_name:
            sources.append(
                {
                    "kind": "page_context",
                    "label": "页面当前步骤",
                    "snippet": selected_step_name,
                }
            )
        if selected_chapter:
            sources.append(
                {
                    "kind": "page_context",
                    "label": "页面当前章节",
                    "snippet": str(selected_chapter.get("title") or f"章节 {selected_chapter.get('chapter_index', 0) + 1}"),
                }
            )
        if selected_stage_summary:
            sources.append(
                {
                    "kind": "focus_summary",
                    "label": "当前焦点内容",
                    "snippet": selected_stage_summary,
                }
            )
        if prompt_snapshot:
            sources.append(
                {
                    "kind": "prompt_version",
                    "label": "当前生效提示词",
                    "snippet": str(prompt_snapshot.get("task_prompt") or "")[:220],
                }
            )
        for hit in retrieval_hits[:4]:
            sources.append(
                {
                    "kind": hit["kind"],
                    "label": hit["title"],
                    "snippet": hit["snippet"],
                }
            )

        return {
            "overview": {
                "project_name": project.name,
                "project_status": project.status,
                "target_duration_sec": project.target_duration_sec,
                "chapter_count": len(chapters),
                "step_count": len(steps),
                "step_status_counts": step_counts,
                "chapter_stage_status_counts": chapter_stage_counts,
                "failed_chapter_count": len(failed_chapters),
                "rework_chapter_count": len(rework_chapters),
                "review_required_chapter_count": len(review_required_chapters),
                "current_step": {
                    "step_name": getattr(current_step, "step_name", None),
                    "step_display_name": getattr(current_step, "step_display_name", None),
                    "status": getattr(current_step, "status", None),
                },
                "timeline": timeline,
            },
            "page_context": {
                "selected_step_key": getattr(selected_step, "step_name", None) or getattr(current_step, "step_name", None),
                "selected_step_name": selected_step_name or getattr(current_step, "step_name", None),
                "selected_chapter": selected_chapter,
            },
            "focus": {
                "selected_step_key": getattr(selected_step, "step_name", None),
                "selected_step_display_name": getattr(selected_step, "step_display_name", None),
                "selected_chapter_id": selected_chapter.get("id") if selected_chapter else None,
                "selected_chapter_title": selected_chapter.get("title") if selected_chapter else None,
                "selected_chapter_summary": selected_chapter.get("summary") if selected_chapter else None,
                "selected_stage_summary": selected_stage_summary,
                "active_prompt": prompt_snapshot,
            },
            "story_bible_summary": {
                "character_count": len(story_bible_characters),
                "scene_count": len(story_bible_scenes),
                "characters": story_bible_characters[:8],
                "scenes": story_bible_scenes[:8],
            },
            "chapter_buckets": {
                "failed": failed_chapters[:6],
                "rework_requested": rework_chapters[:6],
                "review_required": review_required_chapters[:6],
            },
            "retrieval_hits": retrieval_hits,
            "sources": sources,
        }

    def _count_statuses(self, values: Any) -> dict[str, int]:
        counts: dict[str, int] = {}
        for value in values:
            key = str(value or "UNKNOWN")
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _chapter_has_status(self, chapter: dict[str, Any], targets: set[str]) -> bool:
        stage_map = chapter.get("stage_map", {}) or {}
        return any(str(status) in targets for status in stage_map.values())

    def _selected_stage_summary(self, chapter: dict[str, Any] | None, step_name: str | None) -> str | None:
        if not chapter or not step_name:
            return None
        meta = chapter.get("meta", {}) or {}
        stages = meta.get("stages", {}) if isinstance(meta, dict) else {}
        stage = stages.get(step_name, {}) if isinstance(stages, dict) else {}
        if not isinstance(stage, dict):
            return None
        output = stage.get("output", {}) if isinstance(stage.get("output"), dict) else stage
        artifact = output.get("artifact", {}) if isinstance(output.get("artifact"), dict) else {}
        if isinstance(artifact.get("summary"), str) and artifact.get("summary"):
            return str(artifact["summary"])[:220]
        if isinstance(output.get("summary"), str) and output.get("summary"):
            return str(output["summary"])[:220]
        if step_name == "story_scripting":
            beats = artifact.get("beats") if isinstance(artifact.get("beats"), list) else []
            if beats:
                return " / ".join(str((item or {}).get("summary") or "") for item in beats[:3] if isinstance(item, dict))[:220]
        if step_name == "shot_detailing":
            shots = artifact.get("shots") if isinstance(artifact.get("shots"), list) else []
            if shots:
                return " / ".join(str((item or {}).get("visual") or "") for item in shots[:3] if isinstance(item, dict))[:220]
        return None
