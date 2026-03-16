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
        selected_step_name = str(page_context.get("selected_step_name") or "").strip()
        selected_chapter_id = str(page_context.get("selected_chapter_id") or "").strip()
        selected_chapter = next((chapter for chapter in chapters if chapter.get("id") == selected_chapter_id), None)
        if not selected_chapter and chapters:
            selected_chapter = chapters[0]

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
                "selected_step_name": selected_step_name or getattr(current_step, "step_name", None),
                "selected_chapter": selected_chapter,
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
