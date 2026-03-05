from __future__ import annotations

import asyncio
import base64
import html
import json
import math
import re
import subprocess
import textwrap
import zipfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from consistency_engine import score_consistency
from provider_adapters import ProviderRegistry, ProviderRequest, ProviderResponse
from sqlalchemy import Select, select
from sqlalchemy.orm import Session
from workflow_engine import PIPELINE_STEPS, ProjectStatus, StepStatus
from workflow_engine.pipeline import next_step_name

from app.core.config import settings
from app.db.models import (
    Asset,
    ChapterChunk,
    ExportJob,
    ModelRun,
    PipelineStep,
    Project,
    PromptVersion,
    ReviewAction,
    SourceDocument,
    StoryboardVersion,
)
from app.services.prompt_service import get_baseline_prompts
from app.services.storage_service import project_category_dir, project_root, sanitize_component, step_category, storage_root
from app.services.style_service import build_style_prompt, normalize_style_profile

SOURCE_EXCERPT_LIMIT = 2000
SOURCE_CONTENT_LIMIT = 2_000_000
STORY_BIBLE_CONTEXT_CHARS = 2200
STORY_BIBLE_MAX_CHAPTERS = 28
GENERATED_DIR = storage_root()
LOCAL_ONLY_STEPS = {"ingest_parse", "chapter_chunking"}
LOCAL_STEP_MODELS = {
    "ingest_parse": ("local", "builtin-parser"),
    "chapter_chunking": ("local", "builtin-chunker"),
}
LOCAL_CHAPTER_MAX_CHARS = 12_000
TEXT_EDITABLE_STEPS = {"ingest_parse", "chapter_chunking", "story_scripting", "shot_detailing"}
CHAPTER_SCOPED_STEPS = {
    "story_scripting",
    "shot_detailing",
    "storyboard_image",
    "consistency_check",
    "segment_video",
}
CHAPTER_STEP_SEQUENCE = [
    "story_scripting",
    "shot_detailing",
    "storyboard_image",
    "consistency_check",
    "segment_video",
]
CHAPTER_DEPENDENCIES = {
    "story_scripting": "chapter_chunking",
    "shot_detailing": "story_scripting",
    "storyboard_image": "shot_detailing",
    "consistency_check": "storyboard_image",
    "segment_video": "consistency_check",
}


class PipelineService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.registry = ProviderRegistry()
        self.step_def_map = {step.step_name: step for step in PIPELINE_STEPS}

    def list_provider_catalog(self) -> list[dict[str, Any]]:
        return [
            {"provider": item.provider, "step": item.step, "models": item.models, "model_pricing": item.model_pricing or {}}
            for item in self.registry.list_catalog()
        ]

    def apply_model_bindings(self, project: Project, incoming_bindings: dict[str, Any]) -> Project:
        merged = dict(project.model_bindings or {})
        merged.update(incoming_bindings)
        project.model_bindings = merged
        self.db.add(project)
        self.db.flush()

        steps = self._list_steps(project.id)
        for step in steps:
            if step.step_name not in merged:
                continue
            value = merged[step.step_name]
            if not isinstance(value, list) or not value:
                continue
            first = value[0]
            provider = first.get("provider")
            model = first.get("model")
            if not provider or not model:
                continue
            if step.status == StepStatus.GENERATING.value:
                continue
            step.model_provider = provider
            step.model_name = model
            self.db.add(step)
        self.db.commit()
        self.db.refresh(project)
        return project

    def ensure_pipeline_steps(self, project: Project) -> list[PipelineStep]:
        existing = {
            step.step_name: step
            for step in self.db.scalars(
                select(PipelineStep).where(PipelineStep.project_id == project.id).order_by(PipelineStep.step_order.asc())
            ).all()
        }
        for definition in PIPELINE_STEPS:
            if definition.step_name in existing:
                continue
            provider, model = self._resolve_binding(project, definition.step_name, definition.step_type)
            step = PipelineStep(
                project_id=project.id,
                step_name=definition.step_name,
                step_order=definition.order,
                status=StepStatus.PENDING.value,
                model_provider=provider,
                model_name=model,
            )
            self.db.add(step)
        self.db.commit()
        return self._list_steps(project.id)

    async def run_project(self, project: Project) -> PipelineStep | None:
        self.ensure_pipeline_steps(project)
        if project.status == ProjectStatus.COMPLETED.value:
            return None
        project.status = ProjectStatus.RUNNING.value
        self.db.add(project)
        self.db.commit()
        return await self._run_next_eligible_step(project.id)

    async def run_specific_step(
        self,
        project: Project,
        step_name: str,
        force: bool = False,
        params: dict[str, Any] | None = None,
    ) -> PipelineStep:
        params = params or {}
        steps = {item.step_name: item for item in self._list_steps(project.id)}
        step = steps.get(step_name)
        if not step:
            raise ValueError(f"step not found: {step_name}")
        runnable_statuses = {StepStatus.PENDING.value, StepStatus.REWORK_REQUESTED.value, StepStatus.FAILED.value}
        if step.step_name in CHAPTER_SCOPED_STEPS:
            runnable_statuses.update({StepStatus.APPROVED.value, StepStatus.REVIEW_REQUIRED.value})
        if not force and step.status not in runnable_statuses:
            raise ValueError(f"step {step_name} is not runnable in status {step.status}")
        if step_name in CHAPTER_SCOPED_STEPS:
            chapter = self._resolve_target_chapter(project.id, step_name, params.get("chapter_id"), force=force)
            params["chapter_id"] = chapter.id
        return await self._execute_step(project, step, params=params)

    async def run_step_for_all_chapters(
        self,
        project: Project,
        step_name: str,
        *,
        force: bool = True,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if step_name not in CHAPTER_SCOPED_STEPS:
            raise ValueError("run-all-chapters is only allowed on chapter-scoped steps")
        params = params or {}
        chapters = self._list_project_chapters(project.id)
        if not chapters:
            raise ValueError("no chapters available")

        results: list[dict[str, Any]] = []
        succeeded = 0
        failed = 0
        skipped = 0
        last_step: PipelineStep | None = None

        for chapter in chapters:
            title = str((chapter.meta or {}).get("title") or f"章节 {chapter.chapter_index + 1}")
            chapter_status = self._chapter_step_status(chapter, step_name)
            if not self._chapter_dependency_satisfied(project.id, chapter, step_name):
                skipped += 1
                results.append(
                    {
                        "chapter_id": chapter.id,
                        "chapter_title": title,
                        "status": "SKIPPED",
                        "detail": "前置阶段尚未通过，已跳过。",
                    }
                )
                continue
            if not force and chapter_status == StepStatus.APPROVED.value:
                skipped += 1
                results.append(
                    {
                        "chapter_id": chapter.id,
                        "chapter_title": title,
                        "status": "SKIPPED",
                        "detail": "当前章节该阶段已通过，已跳过。",
                    }
                )
                continue
            try:
                last_step = await self.run_specific_step(
                    project,
                    step_name,
                    force=force,
                    params={**params, "chapter_id": chapter.id},
                )
                succeeded += 1
                results.append(
                    {
                        "chapter_id": chapter.id,
                        "chapter_title": title,
                        "status": last_step.status,
                        "detail": "当前章节已运行完成。",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                results.append(
                    {
                        "chapter_id": chapter.id,
                        "chapter_title": title,
                        "status": "FAILED",
                        "detail": str(exc),
                    }
                )

        return {
            "project_id": project.id,
            "step_name": step_name,
            "total": len(chapters),
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
            "chapter_results": results,
            "current_step": last_step or self.db.scalar(
                select(PipelineStep).where(PipelineStep.project_id == project.id, PipelineStep.step_name == step_name)
            ),
        }

    def _sync_global_chapter_scoped_step(self, project: Project, step: PipelineStep) -> PipelineStep:
        chapters = self._list_project_chapters(project.id)
        chapter_statuses = [self._chapter_step_status(chapter, step.step_name) for chapter in chapters]
        if chapter_statuses and all(status == StepStatus.APPROVED.value for status in chapter_statuses):
            step.status = StepStatus.APPROVED.value
            step.output_ref = self._build_step_queue_output(step.step_name, None, chapters[-1] if chapters else None)
            project.status = ProjectStatus.REVIEW_REQUIRED.value
        elif any(status == StepStatus.REVIEW_REQUIRED.value for status in chapter_statuses):
            step.status = StepStatus.REVIEW_REQUIRED.value
            step.output_ref = self._build_step_queue_output(step.step_name, self._next_pending_chapter(project.id, step.step_name), None)
            project.status = ProjectStatus.REVIEW_REQUIRED.value
        elif any(status == StepStatus.REWORK_REQUESTED.value for status in chapter_statuses):
            step.status = StepStatus.REWORK_REQUESTED.value
            project.status = ProjectStatus.REVIEW_REQUIRED.value
        elif any(status == StepStatus.FAILED.value for status in chapter_statuses):
            step.status = StepStatus.FAILED.value
            project.status = ProjectStatus.FAILED.value
        else:
            step.status = StepStatus.PENDING.value
            step.output_ref = self._build_step_queue_output(step.step_name, self._next_pending_chapter(project.id, step.step_name), None)
            project.status = ProjectStatus.RUNNING.value
        self.db.add(step)
        self.db.add(project)
        self.db.commit()
        self.db.refresh(step)
        return step

    def _batch_action_response(
        self,
        project_id: str,
        step_name: str,
        results: list[dict[str, Any]],
        current_step: PipelineStep | None,
    ) -> dict[str, Any]:
        succeeded = sum(1 for item in results if item["status"] == StepStatus.APPROVED.value or item["status"] == StepStatus.REVIEW_REQUIRED.value)
        failed = sum(1 for item in results if item["status"] == "FAILED")
        skipped = sum(1 for item in results if item["status"] == "SKIPPED")
        return {
            "project_id": project_id,
            "step_name": step_name,
            "total": len(results),
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
            "chapter_results": results,
            "current_step": current_step,
        }

    async def approve_step(self, project: Project, step_id: str, payload: dict[str, Any]) -> PipelineStep | None:
        step = self._get_step(project.id, step_id)
        if step.status != StepStatus.REVIEW_REQUIRED.value:
            raise ValueError(f"approve not allowed in status {step.status}")
        if step.step_name in CHAPTER_SCOPED_STEPS:
            current_chapter = self._get_current_step_chapter(project.id, step)
            self._set_chapter_stage_state(
                current_chapter,
                step.step_name,
                status=StepStatus.APPROVED.value,
                output=self._build_chapter_stage_output(step.output_ref, payload.get("comment")),
                attempt=step.attempt,
                provider=step.model_provider,
                model=step.model_name,
            )
            next_chapter = self._next_pending_chapter(project.id, step.step_name)
            if next_chapter:
                step.status = StepStatus.PENDING.value
                step.output_ref = self._build_step_queue_output(step.step_name, next_chapter, current_chapter)
            else:
                step.status = StepStatus.APPROVED.value
                step.finished_at = datetime.now(timezone.utc)
        else:
            step.status = StepStatus.APPROVED.value
            step.finished_at = datetime.now(timezone.utc)
        self.db.add(step)
        self._record_review(project.id, step.id, payload.get("scope_type", "step"), "approve", payload, payload["created_by"])
        self.db.commit()
        if step.step_name in CHAPTER_SCOPED_STEPS and step.status != StepStatus.APPROVED.value:
            self.db.refresh(step)
            return step
        return self._advance_gate_only(project.id, step.step_name)

    async def edit_continue(self, project: Project, step_id: str, payload: dict[str, Any]) -> PipelineStep | None:
        step = self._get_step(project.id, step_id)
        if step.step_name not in TEXT_EDITABLE_STEPS:
            raise ValueError("edit-continue is only allowed on text editing steps")
        if step.status != StepStatus.REVIEW_REQUIRED.value:
            raise ValueError(f"edit-continue not allowed in status {step.status}")
        merged = dict(step.output_ref or {})
        merged["human_edit"] = payload.get("editor_payload", {})
        step.output_ref = merged
        if step.step_name in CHAPTER_SCOPED_STEPS:
            current_chapter = self._get_current_step_chapter(project.id, step)
            self._set_chapter_stage_state(
                current_chapter,
                step.step_name,
                status=StepStatus.APPROVED.value,
                output=self._build_chapter_stage_output(merged, payload.get("comment")),
                attempt=step.attempt,
                provider=step.model_provider,
                model=step.model_name,
            )
            next_chapter = self._next_pending_chapter(project.id, step.step_name)
            if next_chapter:
                step.status = StepStatus.PENDING.value
                step.output_ref = self._build_step_queue_output(step.step_name, next_chapter, current_chapter)
            else:
                step.status = StepStatus.APPROVED.value
                step.finished_at = datetime.now(timezone.utc)
        else:
            step.status = StepStatus.APPROVED.value
            step.finished_at = datetime.now(timezone.utc)
        self.db.add(step)
        self._record_review(
            project.id, step.id, payload.get("scope_type", "step"), "edit_continue", payload, payload["created_by"]
        )
        self.db.commit()
        if step.step_name in CHAPTER_SCOPED_STEPS and step.status != StepStatus.APPROVED.value:
            self.db.refresh(step)
            return step
        return self._advance_gate_only(project.id, step.step_name)

    async def edit_prompt_regenerate(self, project: Project, step_id: str, payload: dict[str, Any]) -> PipelineStep:
        step = self._get_step(project.id, step_id)
        if step.status not in {
            StepStatus.REVIEW_REQUIRED.value,
            StepStatus.REWORK_REQUESTED.value,
            StepStatus.FAILED.value,
        }:
            raise ValueError(f"edit-prompt-regenerate not allowed in status {step.status}")
        self._upsert_prompt_version(
            project_id=project.id,
            step_name=step.step_name,
            task_prompt=payload["task_prompt"],
            system_prompt=payload.get("system_prompt"),
        )
        self._record_review(
            project.id,
            step.id,
            payload.get("scope_type", "step"),
            "edit_prompt_regen",
            payload,
            payload["created_by"],
        )
        self.db.commit()
        return await self._execute_step(project, step, params=payload.get("params", {}))

    async def switch_model_rerun(self, project: Project, step_id: str, payload: dict[str, Any]) -> PipelineStep:
        step = self._get_step(project.id, step_id)
        step.model_provider = payload["provider"]
        step.model_name = payload["model_name"]
        self._record_review(
            project.id,
            step.id,
            payload.get("scope_type", "step"),
            "switch_model_rerun",
            payload,
            payload["created_by"],
        )
        self.db.commit()
        return await self._execute_step(project, step, params=payload.get("params", {}))

    async def approve_step_for_all_chapters(self, project: Project, step_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        step = self._get_step(project.id, step_id)
        if step.step_name not in CHAPTER_SCOPED_STEPS:
            raise ValueError("approve-all-chapters is only allowed on chapter-scoped steps")
        results: list[dict[str, Any]] = []
        for chapter in self._list_project_chapters(project.id):
            title = str((chapter.meta or {}).get("title") or f"章节 {chapter.chapter_index + 1}")
            chapter_status = self._chapter_step_status(chapter, step.step_name)
            if chapter_status != StepStatus.REVIEW_REQUIRED.value:
                results.append({"chapter_id": chapter.id, "chapter_title": title, "status": "SKIPPED", "detail": f"当前状态为 {chapter_status}，已跳过。"})
                continue
            stage_output = self._chapter_stage_chain(chapter).get(step.step_name, {})
            self._set_chapter_stage_state(
                chapter,
                step.step_name,
                status=StepStatus.APPROVED.value,
                output=self._build_chapter_stage_output(stage_output, payload.get("comment")),
                attempt=step.attempt,
                provider=step.model_provider,
                model=step.model_name,
            )
            self._record_review(project.id, step.id, "chapter", "approve", {**payload, "chapter_id": chapter.id}, payload["created_by"])
            results.append({"chapter_id": chapter.id, "chapter_title": title, "status": StepStatus.APPROVED.value, "detail": "当前章节已批量通过。"})
        synced = self._sync_global_chapter_scoped_step(project, step)
        return self._batch_action_response(project.id, step.step_name, results, synced)

    async def edit_continue_for_all_chapters(self, project: Project, step_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        step = self._get_step(project.id, step_id)
        if step.step_name not in TEXT_EDITABLE_STEPS or step.step_name not in CHAPTER_SCOPED_STEPS:
            raise ValueError("edit-continue-all-chapters is only allowed on chapter-scoped text steps")
        results: list[dict[str, Any]] = []
        for chapter in self._list_project_chapters(project.id):
            title = str((chapter.meta or {}).get("title") or f"章节 {chapter.chapter_index + 1}")
            chapter_status = self._chapter_step_status(chapter, step.step_name)
            if chapter_status != StepStatus.REVIEW_REQUIRED.value:
                results.append({"chapter_id": chapter.id, "chapter_title": title, "status": "SKIPPED", "detail": f"当前状态为 {chapter_status}，已跳过。"})
                continue
            stage_output = deepcopy(self._chapter_stage_chain(chapter).get(step.step_name, {}))
            stage_output["human_edit"] = payload.get("editor_payload", {})
            self._set_chapter_stage_state(
                chapter,
                step.step_name,
                status=StepStatus.APPROVED.value,
                output=self._build_chapter_stage_output(stage_output, payload.get("comment")),
                attempt=step.attempt,
                provider=step.model_provider,
                model=step.model_name,
            )
            self._record_review(project.id, step.id, "chapter", "edit_continue", {**payload, "chapter_id": chapter.id}, payload["created_by"])
            results.append({"chapter_id": chapter.id, "chapter_title": title, "status": StepStatus.APPROVED.value, "detail": "当前章节已保存人工编辑并通过。"})
        synced = self._sync_global_chapter_scoped_step(project, step)
        return self._batch_action_response(project.id, step.step_name, results, synced)

    async def edit_prompt_regenerate_for_all_chapters(self, project: Project, step_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        step = self._get_step(project.id, step_id)
        if step.step_name not in CHAPTER_SCOPED_STEPS:
            raise ValueError("edit-prompt-regenerate-all-chapters is only allowed on chapter-scoped steps")
        self._upsert_prompt_version(
            project_id=project.id,
            step_name=step.step_name,
            task_prompt=payload["task_prompt"],
            system_prompt=payload.get("system_prompt"),
        )
        self.db.commit()
        result = await self.run_step_for_all_chapters(project, step.step_name, force=True, params=payload.get("params", {}))
        for item in result["chapter_results"]:
            if item["status"] != "SKIPPED":
                self._record_review(project.id, step.id, "chapter", "edit_prompt_regen", {**payload, "chapter_id": item["chapter_id"]}, payload["created_by"])
        self.db.commit()
        return result

    async def switch_model_rerun_for_all_chapters(self, project: Project, step_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        step = self._get_step(project.id, step_id)
        if step.step_name not in CHAPTER_SCOPED_STEPS:
            raise ValueError("switch-model-rerun-all-chapters is only allowed on chapter-scoped steps")
        step.model_provider = payload["provider"]
        step.model_name = payload["model_name"]
        self.db.add(step)
        self.db.commit()
        result = await self.run_step_for_all_chapters(project, step.step_name, force=True, params=payload.get("params", {}))
        for item in result["chapter_results"]:
            if item["status"] != "SKIPPED":
                self._record_review(project.id, step.id, "chapter", "switch_model_rerun", {**payload, "chapter_id": item["chapter_id"]}, payload["created_by"])
        self.db.commit()
        return result

    def list_steps(self, project_id: str) -> list[PipelineStep]:
        return self._list_steps(project_id)

    def list_assets(self, project_id: str) -> list[Asset]:
        return list(
            self.db.scalars(select(Asset).where(Asset.project_id == project_id).order_by(Asset.created_at.desc())).all()
        )

    async def rebuild_story_bible_references(self, project: Project) -> Project:
        chapter_step = self.db.scalar(
            select(PipelineStep).where(PipelineStep.project_id == project.id, PipelineStep.step_name == "chapter_chunking")
        )
        if not chapter_step:
            raise ValueError("chapter_chunking step not found")
        chapters = self._list_project_chapters(project.id)
        if not chapters:
            raise ValueError("no chapters available, run 章节切分 first")
        refs = await self._refresh_story_bible_from_chapters(project, chapter_step)
        if not refs:
            raise ValueError("failed to rebuild Story Bible references")
        chapter_step.output_ref = {
            **deepcopy(chapter_step.output_ref or {}),
            "story_bible": refs,
            "story_bible_rebuilt_at": datetime.now(timezone.utc).isoformat(),
        }
        self.db.add(chapter_step)
        self.db.add(project)
        self.db.commit()
        self.db.refresh(project)
        return project

    def list_storyboard_versions(self, project_id: str, step_id: str, chapter_id: str | None = None) -> list[StoryboardVersion]:
        step = self._get_step(project_id, step_id)
        if step.step_name != "storyboard_image":
            raise ValueError("storyboard versions are only available for storyboard_image step")
        versions = list(
            self.db.scalars(
                select(StoryboardVersion)
                .where(StoryboardVersion.project_id == project_id, StoryboardVersion.step_id == step_id)
                .order_by(StoryboardVersion.version_index.desc())
            ).all()
        )
        if not chapter_id:
            return versions
        return [item for item in versions if self._storyboard_version_chapter_id(item) == chapter_id]

    def select_storyboard_version(
        self,
        project: Project,
        step_id: str,
        version_id: str,
        payload: dict[str, Any],
    ) -> PipelineStep:
        step = self._get_step(project.id, step_id)
        if step.step_name != "storyboard_image":
            raise ValueError("select storyboard version is only allowed on storyboard_image step")

        version = self.db.scalar(
            select(StoryboardVersion).where(
                StoryboardVersion.id == version_id,
                StoryboardVersion.project_id == project.id,
                StoryboardVersion.step_id == step_id,
            )
        )
        if not version:
            raise ValueError("storyboard version not found")

        self._set_active_storyboard_version(step_id, version.id)
        restored_output = deepcopy(version.output_snapshot)
        restored_output["selected_storyboard_version_id"] = version.id
        restored_output["selection_source"] = "history_version"
        if payload.get("comment"):
            restored_output["selection_comment"] = payload["comment"]

        step.output_ref = restored_output
        step.model_provider = version.model_provider or step.model_provider
        step.model_name = version.model_name or step.model_name
        step.status = StepStatus.REVIEW_REQUIRED.value
        step.finished_at = datetime.now(timezone.utc)
        step.error_code = None
        step.error_message = None
        chapter_id = self._storyboard_version_chapter_id(version)
        if chapter_id:
            chapter = self._get_chapter(project.id, chapter_id)
            self._set_chapter_stage_state(
                chapter,
                step.step_name,
                status=StepStatus.REVIEW_REQUIRED.value,
                output=self._build_chapter_stage_output(restored_output, payload.get("comment")),
                attempt=version.source_attempt,
                provider=step.model_provider,
                model=step.model_name,
            )
        self.db.add(step)
        self._record_review(
            project.id,
            step.id,
            payload.get("scope_type", "step"),
            "select_storyboard_version",
            {"version_id": version.id, **payload},
            payload["created_by"],
        )
        self.db.commit()
        self.db.refresh(step)
        return step

    def project_timeline(self, project: Project) -> dict[str, Any]:
        steps = self._list_steps(project.id)
        budget = max(project.target_duration_sec, 1)
        per = round(budget / max(len(steps), 1), 2)
        summaries = [
            {
                "step_name": step.step_name,
                "status": step.status,
                "allocated_sec": per,
                "model": {"provider": step.model_provider, "name": step.model_name},
            }
            for step in steps
        ]
        return {"project_id": project.id, "target_duration_sec": budget, "step_summaries": summaries}

    async def render_final(self, project: Project) -> ExportJob:
        steps = self._list_steps(project.id)
        if not steps or not self._project_ready_for_final_render(project.id, steps):
            raise ValueError("all required stages must be APPROVED before final render")
        export_job = ExportJob(project_id=project.id, status="RUNNING")
        self.db.add(export_job)
        project.status = ProjectStatus.RENDERING.value
        self.db.add(project)
        self.db.commit()
        self.db.refresh(export_job)
        try:
            output_path = self._render_final_video(project, export_job.id)
            export_job.status = "COMPLETED"
            export_job.output_key = str(output_path)
            export_job.finished_at = datetime.now(timezone.utc)
            project.status = ProjectStatus.COMPLETED.value
            self.db.add_all([export_job, project])
            self.db.add(
                Asset(
                    project_id=project.id,
                    asset_type="final_video",
                    storage_key=str(output_path),
                    mime_type="video/mp4",
                    meta={"export_id": export_job.id, "preview_url": self._to_local_file_url(output_path)},
                )
            )
            self.db.commit()
            self.db.refresh(export_job)
            return export_job
        except Exception as exc:  # noqa: BLE001
            export_job.status = "FAILED"
            export_job.error_message = str(exc)
            export_job.finished_at = datetime.now(timezone.utc)
            project.status = ProjectStatus.FAILED.value
            self.db.add_all([export_job, project])
            self.db.commit()
            raise

    def _project_ready_for_final_render(self, project_id: str, steps: list[PipelineStep]) -> bool:
        for step in steps:
            if step.step_name in CHAPTER_SCOPED_STEPS:
                continue
            if step.status != StepStatus.APPROVED.value:
                return False
        chapters = self._list_project_chapters(project_id)
        if not chapters:
            return False
        for chapter in chapters:
            for step_name in CHAPTER_STEP_SEQUENCE:
                if self._chapter_step_status(chapter, step_name) != StepStatus.APPROVED.value:
                    return False
        return True

    def get_export(self, project_id: str, export_id: str) -> ExportJob:
        export = self.db.scalar(
            select(ExportJob).where(ExportJob.id == export_id, ExportJob.project_id == project_id)
        )
        if not export:
            raise ValueError("export not found")
        return export

    async def _auto_advance_after_gate(self, project_id: str, completed_step_name: str) -> PipelineStep | None:
        nxt = next_step_name(completed_step_name)
        project = self._get_project(project_id)
        if not nxt:
            project.status = ProjectStatus.APPROVED.value
            self.db.add(project)
            self.db.commit()
            return None
        project.status = ProjectStatus.RUNNING.value
        self.db.add(project)
        self.db.commit()
        return await self._run_next_eligible_step(project_id)

    def _advance_gate_only(self, project_id: str, completed_step_name: str) -> PipelineStep | None:
        nxt = next_step_name(completed_step_name)
        project = self._get_project(project_id)
        if not nxt:
            project.status = ProjectStatus.APPROVED.value
            self.db.add(project)
            self.db.commit()
            return None
        next_step = self.db.scalar(
            select(PipelineStep)
            .where(PipelineStep.project_id == project_id, PipelineStep.step_name == nxt)
            .limit(1)
        )
        project.status = ProjectStatus.RUNNING.value
        self.db.add(project)
        self.db.commit()
        if nxt in CHAPTER_SCOPED_STEPS:
            next_step.output_ref = self._build_step_queue_output(nxt, self._next_pending_chapter(project_id, nxt), None)
            self.db.add(next_step)
            self.db.commit()
            self.db.refresh(next_step)
        return next_step

    async def _run_next_eligible_step(self, project_id: str) -> PipelineStep | None:
        project = self._get_project(project_id)
        steps = self._list_steps(project_id)
        for step in steps:
            if step.status not in {StepStatus.PENDING.value, StepStatus.REWORK_REQUESTED.value, StepStatus.FAILED.value}:
                continue
            if not self._all_previous_approved(steps, step.step_order):
                continue
            if step.step_name in CHAPTER_SCOPED_STEPS:
                chapter = self._next_pending_chapter(project_id, step.step_name)
                if chapter is None:
                    step.status = StepStatus.APPROVED.value
                    self.db.add(step)
                    self.db.commit()
                    continue
                return await self._execute_step(project, step, params={"chapter_id": chapter.id})
            return await self._execute_step(project, step)
        return None

    async def _execute_step(
        self,
        project: Project,
        step: PipelineStep,
        params: dict[str, Any] | None = None,
    ) -> PipelineStep:
        params = params or {}
        chapter = None
        if step.step_name in CHAPTER_SCOPED_STEPS:
            chapter = self._resolve_target_chapter(project.id, step.step_name, params.get("chapter_id"), force=True)
        step.status = StepStatus.GENERATING.value
        step.attempt += 1
        step.error_code = None
        step.error_message = None
        step.started_at = datetime.now(timezone.utc)
        if step.step_name in LOCAL_ONLY_STEPS:
            step.model_provider, step.model_name = LOCAL_STEP_MODELS[step.step_name]
        self.db.add(step)
        self.db.commit()
        self.db.refresh(step)

        provider = step.model_provider or "openai"
        model = step.model_name or "gpt-5"
        try:
            system_prompt, task_prompt = self._get_active_prompts(project.id, step.step_name)
            style_directive = build_style_prompt(project.style_profile)
            step_input = self._build_step_input(project, step, chapter)
            step.input_ref = step_input
            adapter = None
            if step.step_name in LOCAL_ONLY_STEPS:
                response = self._invoke_local_step(step, step_input)
                estimated_cost = 0.0
            else:
                adapter = self.registry.resolve(provider)
                if not adapter.supports(self.step_def_map[step.step_name].step_type, model):
                    raise ValueError(f"model not supported by provider: {provider}/{model}")
                if step.step_name == "storyboard_image":
                    response, estimated_cost = await self._invoke_storyboard_image_step(
                        project,
                        step,
                        chapter,
                        adapter,
                        provider,
                        model,
                        system_prompt,
                        task_prompt,
                        style_directive,
                        params,
                    )
                elif step.step_name == "segment_video":
                    response, estimated_cost = await self._invoke_segment_video_step(
                        project,
                        step,
                        chapter,
                        adapter,
                        provider,
                        model,
                        system_prompt,
                        task_prompt,
                        style_directive,
                        params,
                    )
                else:
                    req = ProviderRequest(
                        step=self.step_def_map[step.step_name].step_type,
                        model=model,
                        input=step_input,
                        prompt=f"{system_prompt}\n{task_prompt}\n{style_directive}",
                        params=params,
                    )
                    response = await adapter.invoke(req)
                    estimated_cost = await adapter.estimate_cost(req)

            output = {
                "artifact": response.output,
                "prompt": {"system": system_prompt, "task": task_prompt, "style": style_directive},
                "params": params,
            }
            if chapter:
                output["chapter"] = self._serialize_chapter(chapter)
            output = self._enhance_step_output(project, step, output, chapter)
            if step.step_name == "chapter_chunking":
                output["chapters"] = self._synchronize_chapter_chunks(project, step_input)
                output = await self._augment_story_bible_after_chunking(project, step, output)
            if step.step_name == "segment_video":
                output = await self._poll_segment_video(project, step, adapter, output)
                output["video_consistency"] = self._build_video_consistency_report(project.id, chapter, output)
            output["execution_stats"] = self._build_execution_stats(
                step=step,
                provider=provider,
                model=model,
                usage=response.usage,
                estimated_cost=estimated_cost,
                execution_mode="local" if step.step_name in LOCAL_ONLY_STEPS else "provider",
            )
            output = self._materialize_step_output(project, step, output, chapter)
            step.output_ref = output
            step.finished_at = datetime.now(timezone.utc)
            storyboard_version: StoryboardVersion | None = None

            if step.step_name == "storyboard_image":
                storyboard_version = self._create_storyboard_version(
                    project=project,
                    step=step,
                    output=output,
                    system_prompt=system_prompt,
                    task_prompt=task_prompt,
                )
                output["storyboard_version_id"] = storyboard_version.id
                output["storyboard_version_index"] = storyboard_version.version_index
                step.output_ref = output
                storyboard_version.output_snapshot = deepcopy(output)
                self.db.add(storyboard_version)

            if step.step_name == "consistency_check":
                consistency_context = self._build_storyboard_consistency_context(project, chapter)
                consistency = await self._score_storyboard_consistency_with_model(
                    project,
                    step,
                    consistency_context,
                    threshold=settings.consistency_threshold,
                )
                chapter_scores = self._project_chapter_consistency_scores(
                    project,
                    current_chapter_id=chapter.id if chapter else None,
                    current_consistency=consistency,
                )
                output["consistency"] = {
                    "score": consistency.score,
                    "dimensions": consistency.dimensions,
                    "threshold": settings.consistency_threshold,
                    "scope": "project_storyboards",
                    "chapter_id": chapter.id if chapter else None,
                    "details": consistency.details,
                }
                output["chapter_consistency_scores"] = chapter_scores
                step.output_ref = output
                self._update_storyboard_consistency_snapshot(
                    project.id,
                    output["consistency"],
                    consistency.should_rework,
                    chapter_id=chapter.id if chapter else None,
                )
                step.status = StepStatus.REWORK_REQUESTED.value if consistency.should_rework else StepStatus.REVIEW_REQUIRED.value
            else:
                step.status = StepStatus.REVIEW_REQUIRED.value

            if chapter:
                chapter_stage_status = step.status
                self._set_chapter_stage_state(
                    chapter,
                    step.step_name,
                    status=chapter_stage_status,
                    output=self._build_chapter_stage_output(output),
                    attempt=step.attempt,
                    provider=provider,
                    model=model,
                )

            self.db.add(step)
            output_json_path = self._persist_step_output_json(project, step, output)
            self.db.add(
                ModelRun(
                    project_id=project.id,
                    step_id=step.id,
                    step_name=step.step_name,
                    provider=provider,
                    model_name=model,
                    request_summary={
                        "prompt": task_prompt,
                        "params": params,
                        "execution_mode": "local" if step.step_name in LOCAL_ONLY_STEPS else "provider",
                    },
                    response_summary=response.output,
                    usage=response.usage,
                    estimated_cost=estimated_cost,
                )
            )
            self.db.add(
                Asset(
                    project_id=project.id,
                    step_id=step.id,
                    asset_type=self._asset_type_for_step(step.step_name),
                    storage_key=str(output_json_path),
                    mime_type="application/json",
                    meta={
                        "step_name": step.step_name,
                        "attempt": step.attempt,
                        "preview_url": self._to_local_file_url(output_json_path),
                    },
                )
            )
            project.status = ProjectStatus.REVIEW_REQUIRED.value
            self.db.add(project)
            self.db.commit()
            self.db.refresh(step)
            if step.step_name == "consistency_check" and step.status == StepStatus.REWORK_REQUESTED.value:
                return self._rollback_storyboard_after_consistency_failure(
                    project,
                    step,
                    output["consistency"],
                    chapter_id=chapter.id if chapter else None,
                )
            return step
        except Exception as exc:  # noqa: BLE001
            step.status = StepStatus.FAILED.value
            step.error_code = "STEP_EXECUTION_FAILED"
            step.error_message = str(exc)
            step.finished_at = datetime.now(timezone.utc)
            if chapter:
                self._set_chapter_stage_state(
                    chapter,
                    step.step_name,
                    status=StepStatus.FAILED.value,
                    output={"error_message": str(exc)},
                    attempt=step.attempt,
                    provider=step.model_provider,
                    model=step.model_name,
                )
            project.status = ProjectStatus.FAILED.value
            self.db.add_all([step, project])
            self.db.commit()
            raise

    def _invoke_local_step(self, step: PipelineStep, step_input: dict[str, Any]) -> ProviderResponse:
        source_document = step_input.get("source_document") or {}
        if step.step_name == "ingest_parse":
            title = source_document.get("file_name") or "source-document"
            char_count = source_document.get("char_count") or len(str(source_document.get("full_content") or ""))
            return ProviderResponse(
                output={
                    "provider": "local",
                    "step": self.step_def_map[step.step_name].step_type,
                    "model": "builtin-parser",
                    "artifact_mode": "local_parse",
                    "title": title,
                    "summary": f"已在本地完成全文解析，共 {char_count} 字符。",
                },
                usage={},
                raw={"local": True, "step": step.step_name},
            )
        if step.step_name == "chapter_chunking":
            chapters = self._split_into_chapters(str(source_document.get("content") or ""))
            chapter_count = len({int(item.get("chapter_index", idx)) for idx, item in enumerate(chapters)})
            return ProviderResponse(
                output={
                    "provider": "local",
                    "step": self.step_def_map[step.step_name].step_type,
                    "model": "builtin-chunker",
                    "artifact_mode": "local_chunking",
                    "chapter_count": chapter_count,
                    "segment_count": len(chapters),
                    "summary": f"已在本地识别 {chapter_count} 个章节，共拆分为 {len(chapters)} 个可处理片段。",
                },
                usage={},
                raw={"local": True, "step": step.step_name},
            )
        raise ValueError(f"unsupported local-only step: {step.step_name}")

    def _materialize_step_output(
        self,
        project: Project,
        step: PipelineStep,
        output: dict[str, Any],
        chapter: ChapterChunk | None = None,
    ) -> dict[str, Any]:
        artifact = deepcopy(output.get("artifact", {}))

        if step.step_name == "storyboard_image":
            preview = self._materialize_storyboard_preview(project, chapter, step, artifact, output)
            artifact.update(preview)
            output["storyboard_gallery"] = self._gallery_payload_from_artifact(artifact)
        elif step.step_name == "consistency_check" and chapter is not None:
            output["storyboard_gallery"] = self._load_storyboard_gallery(chapter)
        elif step.step_name == "stitch_subtitle_tts":
            audio = self._materialize_binary_artifact(
                project.id,
                step,
                artifact.get("audio_base64"),
                artifact.get("mime_type", "audio/mpeg"),
                prefix="narration",
            )
            if audio:
                artifact.update(audio)
        elif step.step_name == "segment_video":
            artifact = self._materialize_segment_preview(project, chapter, step, artifact)
            if chapter is not None:
                output["storyboard_gallery"] = self._load_storyboard_gallery(chapter)

        output["artifact"] = artifact
        return output

    def _materialize_storyboard_preview(
        self,
        project: Project,
        chapter: ChapterChunk | None,
        step: PipelineStep,
        artifact: dict[str, Any],
        output: dict[str, Any],
    ) -> dict[str, Any]:
        result = deepcopy(artifact)
        summary = str(artifact.get("summary") or "Storyboard Preview")
        task_prompt = str(output.get("prompt", {}).get("task") or "No task prompt")
        frames = self._normalize_storyboard_frames(project, chapter, step, result)
        if not frames:
            raise ValueError("storyboard_image did not produce any real images")

        contact_sheet_path = self._write_storyboard_contact_sheet(project, chapter, step, frames)
        gallery_zip_path = self._write_storyboard_export_bundle(project, chapter, step, frames, summary, task_prompt)
        cover_image_url = str(frames[0].get("image_url") or "")
        cover_storage_key = str(frames[0].get("storage_key") or "")
        result.update(
            {
                "cover_image_url": cover_image_url,
                "cover_storage_key": cover_storage_key,
                "thumbnail_url": self._to_local_file_url(contact_sheet_path),
                "image_url": self._to_local_file_url(contact_sheet_path),
                "mime_type": "image/png",
                "storage_key": str(contact_sheet_path),
                "export_url": self._to_local_file_url(contact_sheet_path),
                "frame_count": len(frames),
                "frames": frames,
                "gallery_export_url": self._to_local_file_url(gallery_zip_path),
                "gallery_export_key": str(gallery_zip_path),
            }
        )
        return result

    def _materialize_segment_preview(
        self,
        project: Project,
        chapter: ChapterChunk | None,
        step: PipelineStep,
        artifact: dict[str, Any],
    ) -> dict[str, Any]:
        storage_key = artifact.get("storage_key")
        if isinstance(storage_key, str) and storage_key and Path(storage_key).exists() and self._is_playable_video(Path(storage_key)):
            preview_url = self._to_local_file_url(Path(storage_key))
            artifact.setdefault("preview_url", preview_url)
            artifact["export_url"] = preview_url
            artifact.setdefault("mime_type", "video/mp4")
            return artifact

        preview_url = artifact.get("preview_url")
        if isinstance(preview_url, str) and preview_url.startswith(("http://", "https://")):
            remote_asset = self._materialize_remote_artifact(project.id, step, preview_url, prefix="segment")
            if remote_asset and isinstance(remote_asset.get("storage_key"), str):
                remote_path = Path(str(remote_asset["storage_key"]))
                if self._is_playable_video(remote_path):
                    artifact.update(remote_asset)
                    artifact["preview_url"] = remote_asset.get("preview_url") or remote_asset.get("export_url")
                    artifact["export_url"] = remote_asset.get("export_url")
                    return artifact

        frame_paths = self._collect_storyboard_frame_paths_for_chapter(chapter)
        if frame_paths:
            output_path = self._generated_project_dir(project.id, step.step_name) / f"{self._chapter_media_prefix(chapter)}-attempt-{step.attempt}.mp4"
            self._render_storyboard_slideshow(project, frame_paths, output_path, duration_sec=self._chapter_segment_duration(project, chapter))
            artifact.update(
                {
                    "summary": str(artifact.get("summary") or "已根据当前章节分镜生成可预览片段。"),
                    "mime_type": "video/mp4",
                    "storage_key": str(output_path),
                    "preview_url": self._to_local_file_url(output_path),
                    "export_url": self._to_local_file_url(output_path),
                    "artifact_mode": "chapter_storyboard_preview",
                }
            )
            return artifact

        if not artifact.get("preview_url"):
            placeholder = self._write_text_placeholder(
                project.id,
                step.step_name,
                step.attempt,
                artifact.get("summary", "segment video placeholder"),
                suffix=".txt",
            )
            artifact["preview_url"] = self._to_local_file_url(placeholder)
            artifact["export_url"] = self._to_local_file_url(placeholder)
        return artifact

    def _materialize_binary_artifact(
        self,
        project_id: str,
        step: PipelineStep,
        encoded: Any,
        mime_type: str,
        *,
        prefix: str,
    ) -> dict[str, Any] | None:
        if not isinstance(encoded, str) or not encoded:
            return None
        try:
            content = base64.b64decode(encoded)
        except Exception:  # noqa: BLE001
            return None

        suffix = self._suffix_for_mime_type(mime_type)
        file_path = self._generated_project_dir(project_id, step.step_name) / f"{prefix}-attempt-{step.attempt}{suffix}"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)
        local_url = self._to_local_file_url(file_path)
        return {
            "thumbnail_url": local_url if mime_type.startswith("image/") else None,
            "image_url": local_url if mime_type.startswith("image/") else None,
            "audio_url": local_url if mime_type.startswith("audio/") else None,
            "preview_url": local_url if mime_type.startswith("video/") else None,
            "export_url": local_url,
            "storage_key": str(file_path),
        }

    def _materialize_data_url_artifact(
        self,
        project_id: str,
        step: PipelineStep,
        data_url: Any,
        *,
        prefix: str,
    ) -> dict[str, Any] | None:
        if not isinstance(data_url, str) or not data_url.startswith("data:") or ";base64," not in data_url:
            return None
        header, encoded = data_url.split(",", 1)
        mime_type = header[5:].split(";", 1)[0] or "image/png"
        try:
            content = base64.b64decode(encoded)
        except Exception:  # noqa: BLE001
            return None
        suffix = self._suffix_for_mime_type(mime_type)
        file_path = self._generated_project_dir(project_id, step.step_name) / f"{prefix}-attempt-{step.attempt}{suffix}"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)
        local_url = self._to_local_file_url(file_path)
        return {
            "mime_type": mime_type,
            "thumbnail_url": local_url,
            "image_url": local_url,
            "export_url": local_url,
            "storage_key": str(file_path),
        }

    def _materialize_remote_artifact(
        self,
        project_id: str,
        step: PipelineStep,
        url: Any,
        *,
        prefix: str,
    ) -> dict[str, Any] | None:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return None
        import httpx

        response = httpx.get(url, timeout=60)
        if response.status_code >= 400:
            return None
        mime_type = response.headers.get("content-type", "image/png").split(";", 1)[0]
        suffix = self._suffix_for_mime_type(mime_type)
        file_path = self._generated_project_dir(project_id, step.step_name) / f"{prefix}-attempt-{step.attempt}{suffix}"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(response.content)
        local_url = self._to_local_file_url(file_path)
        return {
            "mime_type": mime_type,
            "thumbnail_url": local_url if mime_type.startswith("image/") else None,
            "image_url": local_url if mime_type.startswith("image/") else None,
            "preview_url": local_url if mime_type.startswith("video/") else None,
            "export_url": local_url,
            "storage_key": str(file_path),
        }

    def _materialize_storyboard_frame_asset(
        self,
        project_id: str,
        chapter: ChapterChunk | None,
        step: PipelineStep,
        shot_index: int,
        artifact: dict[str, Any],
    ) -> dict[str, Any]:
        file_path: Path | None = None
        mime_type = str(artifact.get("mime_type") or "image/png")
        image_data_url = artifact.get("image_data_url")
        image_base64 = artifact.get("image_base64")
        image_url = artifact.get("image_url") or artifact.get("thumbnail_url")
        prefix = f"{self._chapter_media_prefix(chapter)}-attempt-{step.attempt}-frame-{shot_index:03d}"

        if isinstance(image_data_url, str) and image_data_url.startswith("data:") and ";base64," in image_data_url:
            header, encoded = image_data_url.split(",", 1)
            mime_type = header[5:].split(";", 1)[0] or mime_type
            content = base64.b64decode(encoded)
            suffix = self._suffix_for_mime_type(mime_type)
            file_path = self._generated_project_dir(project_id, step.step_name) / f"{prefix}{suffix}"
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(content)
        elif isinstance(image_base64, str) and image_base64:
            content = base64.b64decode(image_base64)
            suffix = self._suffix_for_mime_type(mime_type)
            file_path = self._generated_project_dir(project_id, step.step_name) / f"{prefix}{suffix}"
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(content)
        elif isinstance(image_url, str) and image_url.startswith(("http://", "https://")):
            import httpx

            response = httpx.get(image_url, timeout=90)
            response.raise_for_status()
            mime_type = response.headers.get("content-type", mime_type).split(";", 1)[0]
            suffix = self._suffix_for_mime_type(mime_type)
            file_path = self._generated_project_dir(project_id, step.step_name) / f"{prefix}{suffix}"
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(response.content)
        else:
            raise ValueError(f"storyboard_image did not return a real image for shot {shot_index}")

        local_url = self._to_local_file_url(file_path)
        return {
            "mime_type": mime_type,
            "thumbnail_url": local_url,
            "image_url": local_url,
            "export_url": local_url,
            "storage_key": str(file_path),
        }

    def _write_storyboard_png(self, project_id: str, attempt: int, summary: str, task_prompt: str) -> Path:
        from PIL import Image, ImageDraw, ImageFont

        file_path = self._generated_project_dir(project_id, "storyboard_image") / f"storyboard-attempt-{attempt}.png"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (1280, 720), "#f3eadb")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((32, 32, 1248, 688), radius=26, fill="#fffdf8", outline="#d6cfc3", width=2)
        draw.rounded_rectangle((64, 64, 440, 656), radius=20, fill="#e7dfd2")
        font_title = ImageFont.load_default()
        font_body = ImageFont.load_default()
        draw.text((500, 92), "Storyboard Preview", fill="#15233b", font=font_title)
        draw.text((500, 148), summary[:180], fill="#15233b", font=font_body)
        draw.multiline_text((500, 210), task_prompt[:420], fill="#6f7d94", font=font_body, spacing=8)
        draw.text((88, 620), f"Attempt {attempt}", fill="#15233b", font=font_title)
        image.save(file_path, format="PNG")
        return file_path

    def _chapter_media_prefix(self, chapter: ChapterChunk | None) -> str:
        if chapter is None:
            return "chapter-unknown"
        return f"chapter-{chapter.chapter_index + 1:03d}-chunk-{chapter.chunk_index + 1:02d}"

    def _chapter_shots(self, chapter: ChapterChunk | None) -> list[dict[str, Any]]:
        if chapter is None:
            return []
        stages = self._chapter_stages(chapter)
        stage = stages.get("shot_detailing")
        if not isinstance(stage, dict):
            return []
        output = deepcopy(stage.get("output") or {})
        artifact = deepcopy(output.get("artifact") or {})
        shots = artifact.get("shots")
        if not isinstance(shots, list):
            return []
        normalized: list[dict[str, Any]] = []
        for index, shot in enumerate(shots):
            if not isinstance(shot, dict):
                continue
            normalized.append(
                {
                    "shot_index": int(shot.get("shot_index") or index + 1),
                    "duration_sec": float(shot.get("duration_sec") or 0),
                    "frame_type": str(shot.get("frame_type") or "镜头"),
                    "visual": str(shot.get("visual") or ""),
                    "action": str(shot.get("action") or ""),
                    "dialogue": str(shot.get("dialogue") or ""),
                }
            )
        return normalized

    def _normalize_storyboard_frames(
        self,
        project: Project,
        chapter: ChapterChunk | None,
        step: PipelineStep,
        artifact: dict[str, Any],
    ) -> list[dict[str, Any]]:
        frames_value = artifact.get("frames")
        normalized: list[dict[str, Any]] = []
        if isinstance(frames_value, list) and frames_value:
            for index, frame in enumerate(frames_value):
                if not isinstance(frame, dict):
                    continue
                storage_key = frame.get("storage_key")
                image_url = frame.get("image_url") or frame.get("thumbnail_url")
                if isinstance(storage_key, str) and storage_key and Path(storage_key).exists():
                    file_path = Path(storage_key)
                elif isinstance(image_url, str) and image_url.startswith("/api/v1/local-files/"):
                    file_path = GENERATED_DIR / image_url.removeprefix("/api/v1/local-files/")
                else:
                    continue
                if not file_path.exists() or not file_path.is_file():
                    continue
                local_url = self._to_local_file_url(file_path)
                normalized.append(
                    {
                        "shot_index": int(frame.get("shot_index") or index + 1),
                        "title": str(frame.get("title") or f"镜头 {index + 1:02d}"),
                        "frame_type": str(frame.get("frame_type") or "镜头"),
                        "duration_sec": float(frame.get("duration_sec") or 0),
                        "visual": str(frame.get("visual") or frame.get("summary") or ""),
                        "action": str(frame.get("action") or ""),
                        "dialogue": str(frame.get("dialogue") or ""),
                        "summary": str(frame.get("summary") or frame.get("visual") or "")[:160],
                        "thumbnail_url": local_url,
                        "image_url": local_url,
                        "export_url": local_url,
                        "storage_key": str(file_path),
                        "prompt": frame.get("prompt"),
                        "provider": frame.get("provider"),
                        "model": frame.get("model"),
                        "artifact_id": frame.get("artifact_id"),
                    }
                )
        return normalized

    def _gallery_payload_from_artifact(self, artifact: dict[str, Any]) -> dict[str, Any]:
        frames = artifact.get("frames")
        return {
            "frame_count": len(frames) if isinstance(frames, list) else 0,
            "frames": deepcopy(frames) if isinstance(frames, list) else [],
            "contact_sheet_url": artifact.get("thumbnail_url"),
            "contact_sheet_storage_key": artifact.get("storage_key"),
            "gallery_export_url": artifact.get("gallery_export_url"),
            "gallery_export_key": artifact.get("gallery_export_key"),
            "cover_image_url": artifact.get("cover_image_url"),
            "cover_storage_key": artifact.get("cover_storage_key"),
        }

    def _load_storyboard_gallery(self, chapter: ChapterChunk) -> dict[str, Any]:
        stages = self._chapter_stages(chapter)
        stage = stages.get("storyboard_image")
        if not isinstance(stage, dict):
            return {}
        output = deepcopy(stage.get("output") or {})
        gallery = output.get("storyboard_gallery")
        if isinstance(gallery, dict):
            return gallery
        artifact = deepcopy(output.get("artifact") or {})
        return self._gallery_payload_from_artifact(artifact)

    def _write_storyboard_frame_png(
        self,
        project: Project,
        chapter: ChapterChunk | None,
        step: PipelineStep,
        shot: dict[str, Any],
        summary: str,
        task_prompt: str,
    ) -> Path:
        from PIL import Image, ImageDraw, ImageFont

        shot_index = max(1, int(shot.get("shot_index") or 1))
        palette = [
            ("#0d2238", "#e7c59a", "#f5f2ea"),
            ("#23314d", "#d95f23", "#fff7ee"),
            ("#263826", "#b5c86a", "#f5f7ef"),
            ("#382633", "#c9789d", "#fbf1f6"),
        ]
        bg, accent, panel = palette[(shot_index - 1) % len(palette)]
        file_path = self._generated_project_dir(project.id, "storyboard_image") / (
            f"{self._chapter_media_prefix(chapter)}-attempt-{step.attempt}-frame-{shot_index:03d}.png"
        )
        file_path.parent.mkdir(parents=True, exist_ok=True)

        image = Image.new("RGB", (1440, 810), bg)
        draw = ImageDraw.Draw(image)
        font_title = ImageFont.load_default()
        font_body = ImageFont.load_default()

        draw.rounded_rectangle((38, 38, 1402, 772), radius=32, fill=panel)
        draw.rounded_rectangle((72, 80, 520, 730), radius=26, fill=bg)
        draw.rounded_rectangle((96, 106, 496, 300), radius=20, fill=accent)
        draw.text((118, 132), f"镜头 {shot_index:02d}", fill="#101820", font=font_title)
        draw.text((118, 178), str(shot.get("frame_type") or "镜头"), fill="#101820", font=font_body)
        draw.text((118, 220), f"{float(shot.get('duration_sec') or 0):.1f}s", fill="#101820", font=font_body)
        chapter_title = chapter.meta.get("title") if chapter and isinstance(chapter.meta, dict) else None
        draw.multiline_text(
            (118, 338),
            textwrap.fill(str(chapter_title or project.name), width=18),
            fill="#f4efe7",
            font=font_body,
            spacing=8,
        )

        draw.text((570, 94), "Visual", fill=accent, font=font_title)
        draw.multiline_text(
            (570, 126),
            textwrap.fill(str(shot.get("visual") or summary or "无画面描述"), width=36)[:820],
            fill="#15233b",
            font=font_body,
            spacing=8,
        )
        draw.text((570, 402), "Action", fill=accent, font=font_title)
        draw.multiline_text(
            (570, 434),
            textwrap.fill(str(shot.get("action") or task_prompt or "无动作描述"), width=36)[:680],
            fill="#3a4558",
            font=font_body,
            spacing=8,
        )
        dialogue = str(shot.get("dialogue") or "").strip()
        if dialogue:
            draw.text((570, 632), "Dialogue", fill=accent, font=font_title)
            draw.multiline_text(
                (570, 664),
                textwrap.fill(dialogue, width=36)[:320],
                fill="#6f7d94",
                font=font_body,
                spacing=8,
            )
        image.save(file_path, format="PNG")
        return file_path

    def _write_storyboard_contact_sheet(
        self,
        project: Project,
        chapter: ChapterChunk | None,
        step: PipelineStep,
        frames: list[dict[str, Any]],
    ) -> Path:
        from PIL import Image, ImageDraw, ImageFont, ImageOps

        file_path = self._generated_project_dir(project.id, "storyboard_image") / (
            f"{self._chapter_media_prefix(chapter)}-attempt-{step.attempt}-contact-sheet.png"
        )
        file_path.parent.mkdir(parents=True, exist_ok=True)

        columns = 3
        rows = max(1, math.ceil(max(len(frames), 1) / columns))
        tile_width = 480
        tile_height = 270
        gutter = 24
        canvas_width = columns * tile_width + (columns + 1) * gutter
        canvas_height = rows * tile_height + (rows + 1) * gutter + 110
        image = Image.new("RGB", (canvas_width, canvas_height), "#151d2b")
        draw = ImageDraw.Draw(image)
        font_title = ImageFont.load_default()
        font_body = ImageFont.load_default()
        heading = chapter.meta.get("title") if chapter and isinstance(chapter.meta, dict) else project.name
        draw.text((gutter, 28), f"{heading} · 分镜总览", fill="#f4efe7", font=font_title)
        draw.text((gutter, 62), f"共 {len(frames)} 张分镜图", fill="#b7c0d1", font=font_body)

        resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
        for index, frame in enumerate(frames):
            x = gutter + (index % columns) * (tile_width + gutter)
            y = 110 + gutter + (index // columns) * (tile_height + gutter)
            source_path = Path(str(frame.get("storage_key") or ""))
            if source_path.exists() and source_path.is_file():
                try:
                    tile = Image.open(source_path).convert("RGB")
                    tile = ImageOps.fit(tile, (tile_width, tile_height), method=resampling)
                    image.paste(tile, (x, y))
                except Exception:  # noqa: BLE001
                    pass
            draw.rounded_rectangle((x, y, x + tile_width, y + tile_height), radius=18, outline="#344057", width=2)
            label = f"#{int(frame.get('shot_index') or index + 1):02d} {str(frame.get('frame_type') or '镜头')}"
            draw.rounded_rectangle((x + 16, y + 16, x + 206, y + 56), radius=16, fill="#fff8ec")
            draw.text((x + 30, y + 30), label, fill="#15233b", font=font_body)

        image.save(file_path, format="PNG")
        return file_path

    def _write_storyboard_export_bundle(
        self,
        project: Project,
        chapter: ChapterChunk | None,
        step: PipelineStep,
        frames: list[dict[str, Any]],
        summary: str,
        task_prompt: str,
    ) -> Path:
        bundle_path = self._generated_project_dir(project.id, "storyboard_image") / (
            f"{self._chapter_media_prefix(chapter)}-attempt-{step.attempt}-storyboards.zip"
        )
        manifest = {
            "chapter": self._serialize_chapter(chapter) if chapter else None,
            "attempt": step.attempt,
            "summary": summary,
            "task_prompt": task_prompt,
            "frame_count": len(frames),
            "frames": [
                {
                    "shot_index": frame.get("shot_index"),
                    "title": frame.get("title"),
                    "frame_type": frame.get("frame_type"),
                    "duration_sec": frame.get("duration_sec"),
                    "summary": frame.get("summary"),
                    "visual": frame.get("visual"),
                    "action": frame.get("action"),
                    "dialogue": frame.get("dialogue"),
                    "file_name": Path(str(frame.get("storage_key") or "")).name,
                }
                for frame in frames
            ],
        }
        with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            for frame in frames:
                storage_key = frame.get("storage_key")
                if isinstance(storage_key, str) and storage_key and Path(storage_key).exists() and Path(storage_key).is_file():
                    archive.write(storage_key, arcname=Path(storage_key).name)
        return bundle_path

    def _collect_storyboard_frame_paths_for_chapter(self, chapter: ChapterChunk | None) -> list[Path]:
        if chapter is None:
            return []
        gallery = self._load_storyboard_gallery(chapter)
        frames = gallery.get("frames")
        if not isinstance(frames, list):
            return []
        result: list[Path] = []
        for frame in frames:
            if not isinstance(frame, dict):
                continue
            storage_key = frame.get("storage_key")
            if isinstance(storage_key, str) and storage_key and Path(storage_key).exists():
                result.append(Path(storage_key))
        return result

    def _chapter_segment_duration(self, project: Project, chapter: ChapterChunk | None) -> float:
        shots = self._chapter_shots(chapter)
        total = 0.0
        for shot in shots:
            try:
                total += float(shot.get("duration_sec") or 0)
            except (TypeError, ValueError):
                continue
        if total > 0:
            return total
        chapter_count = max(len(self._list_project_chapters(project.id)), 1)
        return max(8.0, project.target_duration_sec / chapter_count)

    def _is_playable_video(self, path: Path) -> bool:
        if not path.exists() or path.suffix.lower() != ".mp4":
            return False
        try:
            head = path.read_bytes()[:64]
        except OSError:
            return False
        return b"ftyp" in head

    def _write_text_placeholder(
        self,
        project_id: str,
        step_name: str,
        attempt: int,
        text: str,
        *,
        suffix: str,
    ) -> Path:
        file_path = self._generated_project_dir(project_id, step_name) / f"artifact-attempt-{attempt}{suffix}"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(text, encoding="utf-8")
        return file_path

    def _persist_step_output_json(self, project: Project, step: PipelineStep, output: dict[str, Any]) -> Path:
        target = self._generated_project_dir(project.id, step.step_name) / f"{step.step_name}-attempt-{step.attempt}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    def _generated_project_dir(self, project_id: str, step_name: str) -> Path:
        project = self._get_project(project_id)
        return project_category_dir(project.id, project.name, step_category(step_name))

    def _to_local_file_url(self, file_path: Path) -> str:
        relative = file_path.resolve().relative_to(GENERATED_DIR.resolve()).as_posix()
        return f"/api/v1/local-files/{relative}"

    def _render_final_video(self, project: Project, export_id: str) -> Path:
        export_dir = project_category_dir(project.id, project.name, "exports")
        output_path = export_dir / f"final-{export_id}.mp4"
        storyboard_paths = self._collect_storyboard_paths(project.id)
        if storyboard_paths:
            self._render_storyboard_slideshow(project, storyboard_paths, output_path)
            return output_path

        segment_paths = self._collect_chapter_video_paths(project.id)
        if segment_paths:
            self._concat_video_segments(segment_paths, output_path)
            return output_path

        raise ValueError("no chapter video segments or storyboard images available for final export")

    def _collect_chapter_video_paths(self, project_id: str) -> list[Path]:
        result: list[Path] = []
        for chapter in self._list_project_chapters(project_id):
            stages = self._chapter_stages(chapter)
            segment = stages.get("segment_video")
            if not isinstance(segment, dict):
                continue
            output = deepcopy(segment.get("output") or {})
            artifact = deepcopy(output.get("artifact") or {})
            storage_key = artifact.get("storage_key")
            if isinstance(storage_key, str) and storage_key and Path(storage_key).exists():
                result.append(Path(storage_key))
        return result

    def _collect_storyboard_paths(self, project_id: str) -> list[Path]:
        result: list[Path] = []
        for chapter in self._list_project_chapters(project_id):
            frame_paths = self._collect_storyboard_frame_paths_for_chapter(chapter)
            if frame_paths:
                result.extend(frame_paths)
                continue
            stages = self._chapter_stages(chapter)
            storyboard = stages.get("storyboard_image")
            if not isinstance(storyboard, dict):
                continue
            output = deepcopy(storyboard.get("output") or {})
            artifact = deepcopy(output.get("artifact") or {})
            candidate = artifact.get("storage_key")
            if isinstance(candidate, str) and candidate and Path(candidate).exists():
                result.append(Path(candidate))
        return result

    def _concat_video_segments(self, segment_paths: list[Path], output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        concat_file = output_path.with_suffix(".concat.txt")
        concat_file.write_text(
            "\n".join(f"file '{path.as_posix()}'" for path in segment_paths),
            encoding="utf-8",
        )
        cmd = [
            self._ffmpeg_executable(),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            str(output_path),
        ]
        completed = subprocess.run(cmd, capture_output=True, text=True)
        if completed.returncode != 0:
            raise ValueError(f"ffmpeg concat failed: {completed.stderr.strip()}")

    def _render_storyboard_slideshow(
        self,
        project: Project,
        storyboard_paths: list[Path],
        output_path: Path,
        *,
        duration_sec: float | None = None,
    ) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image_list_file = output_path.with_suffix(".images.txt")
        total_duration = duration_sec if duration_sec and duration_sec > 0 else project.target_duration_sec
        per_image_duration = max(1.6, total_duration / max(len(storyboard_paths), 1))
        lines: list[str] = []
        for image_path in storyboard_paths:
            lines.append(f"file '{image_path.as_posix()}'")
            lines.append(f"duration {per_image_duration:.2f}")
        lines.append(f"file '{storyboard_paths[-1].as_posix()}'")
        image_list_file.write_text("\n".join(lines), encoding="utf-8")
        cmd = [
            self._ffmpeg_executable(),
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(image_list_file),
            "-vf",
            "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,format=yuv420p",
            "-r",
            "24",
            str(output_path),
        ]
        completed = subprocess.run(cmd, capture_output=True, text=True)
        if completed.returncode != 0:
            raise ValueError(f"ffmpeg storyboard render failed: {completed.stderr.strip()}")

    def _ffmpeg_executable(self) -> str:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()

    def _suffix_for_mime_type(self, mime_type: str) -> str:
        mapping = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
            "image/svg+xml": ".svg",
            "audio/mpeg": ".mp3",
            "audio/mp3": ".mp3",
            "audio/wav": ".wav",
            "video/mp4": ".mp4",
        }
        return mapping.get(mime_type, ".bin")

    async def _poll_segment_video(
        self,
        project: Project,
        step: PipelineStep,
        adapter: Any,
        output: dict[str, Any],
    ) -> dict[str, Any]:
        artifact = deepcopy(output.get("artifact", {}))
        video_id = artifact.get("video_id") or artifact.get("artifact_id")
        if not isinstance(video_id, str) or not video_id:
            return output

        poll_trace: list[dict[str, Any]] = []
        output["polling"] = {
            "job_id": video_id,
            "poll_interval_sec": settings.video_poll_interval_sec,
            "max_attempts": settings.video_poll_max_attempts,
            "trace": poll_trace,
        }

        step.output_ref = output
        self.db.add(step)
        self.db.commit()
        self.db.refresh(step)

        for attempt in range(1, settings.video_poll_max_attempts + 1):
            status_response = await adapter.get_video_status(video_id)
            artifact.update(status_response.output)
            poll_trace.append(
                {
                    "attempt": attempt,
                    "status": artifact.get("status"),
                    "progress": artifact.get("progress"),
                }
            )
            output["artifact"] = artifact
            output["polling"]["trace"] = poll_trace

            status = str(artifact.get("status") or "").lower()
            if status in {"completed", "succeeded"}:
                content, mime_type = await adapter.download_video(video_id)
                suffix = self._suffix_for_mime_type(mime_type)
                file_path = self._generated_project_dir(project.id, step.step_name) / f"segment-attempt-{step.attempt}{suffix}"
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_bytes(content)
                artifact["mime_type"] = mime_type
                artifact["storage_key"] = str(file_path)
                artifact["preview_url"] = self._to_local_file_url(file_path)
                artifact["export_url"] = self._to_local_file_url(file_path)
                artifact["downloaded"] = True
                output["artifact"] = artifact
                output["polling"]["final_status"] = artifact.get("status")
                return output

            if status in {"failed", "cancelled", "canceled"}:
                output["polling"]["final_status"] = artifact.get("status")
                raise ValueError(f"segment video generation failed: {artifact.get('status')}")

            await asyncio.sleep(settings.video_poll_interval_sec)

        raise ValueError("segment video generation timed out during polling")

    def _asset_type_for_step(self, step_name: str) -> str:
        mapping = {
            "ingest_parse": "parsed_text",
            "chapter_chunking": "chapter_chunks",
            "story_scripting": "story_script",
            "shot_detailing": "shot_specs",
            "storyboard_image": "storyboard_images",
            "consistency_check": "consistency_report",
            "segment_video": "segment_videos",
            "stitch_subtitle_tts": "rough_cut",
        }
        return mapping.get(step_name, "artifact")

    def _build_execution_stats(
        self,
        *,
        step: PipelineStep,
        provider: str,
        model: str,
        usage: dict[str, Any],
        estimated_cost: float,
        execution_mode: str,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        started_at = step.started_at or now
        elapsed_sec = max(0.0, (now - started_at).total_seconds())
        token_usage = self._normalize_token_usage(usage)
        return {
            "execution_mode": execution_mode,
            "provider": provider,
            "model": model,
            "attempt": step.attempt,
            "started_at": started_at.isoformat(),
            "finished_at": now.isoformat(),
            "elapsed_sec": round(elapsed_sec, 3),
            "elapsed_ms": int(round(elapsed_sec * 1000)),
            "estimated_cost": round(float(estimated_cost or 0.0), 6),
            "token_usage": token_usage,
            "raw_usage": usage or {},
        }

    def _normalize_token_usage(self, usage: dict[str, Any]) -> dict[str, int]:
        if not isinstance(usage, dict):
            return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        candidates = {
            "input_tokens": ("input_tokens", "inputTokens", "prompt_tokens", "promptTokens"),
            "output_tokens": ("output_tokens", "outputTokens", "completion_tokens", "completionTokens"),
            "total_tokens": ("total_tokens", "totalTokens", "tokens"),
        }
        normalized: dict[str, int] = {}
        for target, keys in candidates.items():
            value = 0
            for key in keys:
                current = usage.get(key)
                if isinstance(current, (int, float)):
                    value = int(current)
                    break
            normalized[target] = max(0, value)
        if normalized["total_tokens"] <= 0:
            normalized["total_tokens"] = normalized["input_tokens"] + normalized["output_tokens"]
        return normalized

    def _build_step_input(self, project: Project, step: PipelineStep, chapter: ChapterChunk | None = None) -> dict[str, Any]:
        previous = self.db.scalar(
            select(PipelineStep)
            .where(PipelineStep.project_id == project.id, PipelineStep.step_order == step.step_order - 1)
            .limit(1)
        )
        style_profile = normalize_style_profile(project.style_profile)
        payload = {
            "project_id": project.id,
            "project_name": project.name,
            "target_duration_sec": project.target_duration_sec,
            "style_profile": style_profile,
            "story_bible": style_profile.get("story_bible", {}),
            "current_step": step.step_name,
            "input_path": project.input_path,
            "source_document": self._build_source_document_input(project, step.step_name),
            "previous_output": previous.output_ref if previous else {},
        }
        if chapter:
            stage_chain = self._chapter_stage_chain(chapter)
            payload["chapter"] = self._serialize_chapter(chapter)
            payload["chapter_stage_chain"] = stage_chain
            dependency = CHAPTER_DEPENDENCIES.get(step.step_name)
            if dependency:
                payload["previous_output"] = stage_chain.get(dependency, payload["previous_output"])
            payload["chapter_storyboard_consistency_goal"] = "确保当前章节内所有分镜图片的人物、服装、场景、光线和动作连续一致。"
            payload["chapter_video_consistency_goal"] = "确保当前章节内所有视频片段的角色状态、动作承接、镜头节奏和视觉风格一致。"
        return payload

    def list_chapters(self, project_id: str) -> list[dict[str, Any]]:
        project = self._get_project(project_id)
        chapters = list(
            self.db.scalars(
                select(ChapterChunk)
                .where(ChapterChunk.project_id == project_id)
                .order_by(ChapterChunk.chapter_index.asc(), ChapterChunk.chunk_index.asc())
            ).all()
        )
        steps = {step.step_name: step for step in self._list_steps(project_id)}
        fallback_stage_status = self._derive_chapter_stage_status(steps)
        items: list[dict[str, Any]] = []
        for chapter in chapters:
            meta = self._hydrate_chapter_media_meta_for_read(project, chapter, steps, dict(chapter.meta or {}))
            consistency_summary = meta.get("consistency_summary") if isinstance(meta.get("consistency_summary"), dict) else {}
            stage_map = {
                step_name: self._chapter_step_status(chapter, step_name)
                for step_name in CHAPTER_STEP_SEQUENCE
            }
            items.append(
                {
                    "id": chapter.id,
                    "chapter_index": chapter.chapter_index,
                    "chunk_index": chapter.chunk_index,
                    "title": meta.get("title") or f"章节 {chapter.chapter_index + 1}",
                    "summary": meta.get("summary") or chapter.content[:80],
                    "content_excerpt": chapter.content[:200],
                    "stage_status": self._derive_chapter_stage_status(stage_map, fallback=fallback_stage_status),
                    "stage_map": stage_map,
                    "consistency_score": consistency_summary.get("score"),
                    "meta": meta,
                }
            )
        return items

    def _hydrate_chapter_media_meta_for_read(
        self,
        project: Project,
        chapter: ChapterChunk,
        steps: dict[str, PipelineStep],
        meta: dict[str, Any],
    ) -> dict[str, Any]:
        stages = deepcopy(meta.get("stages") or {})
        if not isinstance(stages, dict):
            return meta

        storyboard_stage = deepcopy(stages.get("storyboard_image") or {})
        if isinstance(storyboard_stage, dict):
            output = deepcopy(storyboard_stage.get("output") or {})
            artifact = deepcopy(output.get("artifact") or {})
            storyboard_step = steps.get("storyboard_image")
            if storyboard_step:
                versions = self.list_storyboard_versions(project.id, storyboard_step.id, chapter_id=chapter.id)
                output["storyboard_version_count"] = len(versions)
                active_version = next((item for item in versions if item.is_active), None)
                output["active_storyboard_version_id"] = active_version.id if active_version else None
            if storyboard_step and isinstance(artifact.get("frames"), list) and artifact.get("frames"):
                try:
                    artifact.update(self._materialize_storyboard_preview(project, chapter, storyboard_step, artifact, output))
                    output["artifact"] = artifact
                except Exception:  # noqa: BLE001
                    pass
            if isinstance(output.get("artifact"), dict):
                output["storyboard_gallery"] = self._gallery_payload_from_artifact(deepcopy(output["artifact"]))
                storyboard_stage["output"] = output
                stages["storyboard_image"] = storyboard_stage

        if isinstance(stages.get("consistency_check"), dict):
            consistency_stage = deepcopy(stages["consistency_check"])
            output = deepcopy(consistency_stage.get("output") or {})
            if not isinstance(output.get("storyboard_gallery"), dict):
                source_output = deepcopy((stages.get("storyboard_image") or {}).get("output") or {})
                source_artifact = deepcopy(source_output.get("artifact") or {})
                output["storyboard_gallery"] = self._gallery_payload_from_artifact(source_artifact)
                consistency_stage["output"] = output
                stages["consistency_check"] = consistency_stage

        if isinstance(stages.get("segment_video"), dict):
            segment_stage = deepcopy(stages["segment_video"])
            output = deepcopy(segment_stage.get("output") or {})
            artifact = deepcopy(output.get("artifact") or {})
            segment_step = steps.get("segment_video")
            if segment_step:
                artifact = self._materialize_segment_preview(project, chapter, segment_step, artifact)
                output["artifact"] = artifact
            if not isinstance(output.get("storyboard_gallery"), dict):
                source_output = deepcopy((stages.get("storyboard_image") or {}).get("output") or {})
                source_artifact = deepcopy(source_output.get("artifact") or {})
                output["storyboard_gallery"] = self._gallery_payload_from_artifact(source_artifact)
            segment_stage["output"] = output
            stages["segment_video"] = segment_stage

        meta["stages"] = stages
        return meta

    def _derive_chapter_stage_status(self, steps: dict[str, Any], fallback: str = "待开始") -> str:
        if steps and all(isinstance(item, PipelineStep) for item in steps.values()):
            status_order = [
                ("stitch_subtitle_tts", "成片合成"),
                ("segment_video", "视频生成"),
                ("consistency_check", "分镜校核"),
                ("storyboard_image", "分镜出图"),
                ("shot_detailing", "分镜细化"),
                ("story_scripting", "章节剧本"),
                ("chapter_chunking", "章节切分"),
                ("ingest_parse", "导入全文"),
            ]
            for step_name, label in status_order:
                step = steps.get(step_name)
                if step and step.status in {StepStatus.APPROVED.value, StepStatus.REVIEW_REQUIRED.value, StepStatus.GENERATING.value}:
                    return label
            return fallback

        label_map = {
            "segment_video": "视频生成",
            "consistency_check": "分镜校核",
            "storyboard_image": "分镜出图",
            "shot_detailing": "分镜细化",
            "story_scripting": "章节剧本",
        }
        for step_name in reversed(CHAPTER_STEP_SEQUENCE):
            status_value = str(steps.get(step_name) or "")
            if status_value in {StepStatus.APPROVED.value, StepStatus.REVIEW_REQUIRED.value, StepStatus.GENERATING.value}:
                return label_map.get(step_name, fallback)
        return fallback

    def _synchronize_chapter_chunks(self, project: Project, step_input: dict[str, Any]) -> list[dict[str, Any]]:
        source_document = step_input.get("source_document", {})
        content = source_document.get("content")
        if not isinstance(content, str) or not content.strip():
            content = source_document.get("content_excerpt")
        if not isinstance(content, str) or not content.strip():
            return []

        chapters = self._split_into_chapters(content)
        existing = list(
            self.db.scalars(select(ChapterChunk).where(ChapterChunk.project_id == project.id)).all()
        )
        for item in existing:
            self.db.delete(item)
        self.db.flush()

        chapter_records: list[dict[str, Any]] = []
        for index, chapter in enumerate(chapters):
            chapter_index = int(chapter.get("chapter_index", index))
            chunk_index = int(chapter.get("chunk_index", 0))
            row = ChapterChunk(
                project_id=project.id,
                chapter_index=chapter_index,
                chunk_index=chunk_index,
                content=chapter["content"],
                overlap_prev=None,
                overlap_next=None,
                meta={
                    "title": chapter["title"],
                    "summary": chapter["summary"],
                    "canonical_title": chapter.get("canonical_title") or chapter["title"],
                    "stages": {},
                },
            )
            self.db.add(row)
            self.db.flush()
            chapter_records.append(
                {
                    "chapter_id": row.id,
                    "chapter_index": chapter_index,
                    "chunk_index": chunk_index,
                    "title": chapter["title"],
                    "summary": chapter["summary"],
                    "content_excerpt": chapter["content"][:180],
                }
            )
        self.db.flush()
        return chapter_records

    def _split_into_chapters(self, text: str) -> list[dict[str, Any]]:
        normalized = text.replace("\r\n", "\n").strip()
        if not normalized:
            return [{"title": "章节 1", "summary": "", "content": "", "chapter_index": 0, "chunk_index": 0}]

        import re

        lines = normalized.splitlines()
        heading_indexes: list[int] = []

        def previous_non_empty(index: int) -> str:
            for cursor in range(index - 1, -1, -1):
                candidate = lines[cursor].strip()
                if candidate:
                    return candidate
            return ""

        def next_non_empty(index: int) -> str:
            for cursor in range(index + 1, len(lines)):
                candidate = lines[cursor].strip()
                if candidate:
                    return candidate
            return ""

        def is_heading(index: int, value: str) -> bool:
            stripped = value.strip()
            if not stripped or len(stripped) > 80:
                return False
            lowered = stripped.lower()
            chinese_heading = re.match(r"^第[0-9一二三四五六七八九十百千]+[章节回幕卷部篇集].*$", stripped)
            english_heading = re.match(
                r"^(chapter|part|book)\s+([0-9ivxlcdm]+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b.*$",
                lowered,
            )
            chinese_special_heading = stripped in {"序章", "楔子", "引子", "终章", "尾声", "后记", "附录"}
            prologue_heading = lowered in {"prologue", "epilogue", "preface", "afterword"}
            numeric_heading = re.match(r"^([0-9]{1,3}|[ivxlcdm]{1,8})[.)]?$", lowered)
            chinese_numeric_heading = re.match(r"^[零〇一二三四五六七八九十百千两]{1,8}$", stripped)
            if chinese_heading or english_heading or prologue_heading or chinese_special_heading:
                return True
            if numeric_heading:
                prev_line = previous_non_empty(index)
                next_line = next_non_empty(index)
                return not prev_line and bool(next_line)
            if chinese_numeric_heading:
                next_line = next_non_empty(index)
                return bool(next_line) and len(next_line) > 8
            return False

        for idx, line in enumerate(lines):
            if is_heading(idx, line):
                heading_indexes.append(idx)

        parts: list[tuple[str, str]] = []
        if heading_indexes:
            lead_in = "\n".join(lines[: heading_indexes[0]]).strip() if heading_indexes[0] > 0 else ""
            if lead_in and len(lead_in) > 80:
                parts.append(("前置内容", lead_in))
                lead_in = ""
            for offset, start_idx in enumerate(heading_indexes):
                end_idx = heading_indexes[offset + 1] if offset + 1 < len(heading_indexes) else len(lines)
                chunk = "\n".join(lines[start_idx:end_idx]).strip()
                if offset == 0 and lead_in:
                    chunk = f"{lead_in}\n\n{chunk}".strip()
                if chunk:
                    section_lines = [line.strip() for line in chunk.splitlines() if line.strip()]
                    if lead_in and offset == 0:
                        section_title = lines[start_idx].strip() or f"章节 {offset + 1}"
                    else:
                        section_title = section_lines[0] if section_lines else f"章节 {offset + 1}"
                    parts.append((section_title[:60], chunk))
        else:
            pattern = re.compile(r"(?=^第[0-9一二三四五六七八九十百千]+[章节回幕].*$)", re.MULTILINE)
            parts = []
            for index, part in enumerate([part.strip() for part in pattern.split(normalized) if part.strip()]):
                section_title = part.splitlines()[0].strip() if part.splitlines() else f"章节 {index + 1}"
                parts.append((section_title[:60], part))
        if len(parts) <= 1:
            paragraphs = [item.strip() for item in normalized.split("\n\n") if item.strip()]
            if len(paragraphs) > 1:
                if len(paragraphs) <= 8:
                    parts = [("章节 1", "\n\n".join(paragraphs))]
                else:
                    chunk_size = max(3, min(8, math.ceil(len(paragraphs) / 12)))
                    parts = [
                        (f"章节 {index + 1}", "\n\n".join(paragraphs[i : i + chunk_size]))
                        for index, i in enumerate(range(0, len(paragraphs), chunk_size))
                    ]
            else:
                words = normalized.split()
                word_chunk = 1800
                parts = [
                    (f"章节 {index + 1}", " ".join(words[i : i + word_chunk]).strip())
                    for index, i in enumerate(range(0, len(words), word_chunk))
                    if " ".join(words[i : i + word_chunk]).strip()
                ]

        chapters: list[dict[str, Any]] = []
        for chapter_index, (title, part) in enumerate(parts):
            chapters.extend(self._split_large_chapter(title, part, chapter_index))
        return chapters or [{"title": "章节 1", "summary": normalized[:100], "content": normalized, "chapter_index": 0, "chunk_index": 0}]

    def _split_large_chapter(self, title: str, content: str, chapter_index: int) -> list[dict[str, Any]]:
        import re

        normalized = content.strip()
        if not normalized:
            return [{"title": title[:60], "summary": "", "content": "", "chapter_index": chapter_index, "chunk_index": 0}]

        non_empty_lines = [line.strip() for line in normalized.splitlines() if line.strip()]
        heading = title.strip() or (non_empty_lines[0] if non_empty_lines else f"章节 {chapter_index + 1}")
        body_lines = non_empty_lines[1:] if len(non_empty_lines) > 1 and non_empty_lines[0][:40] == heading[:40] else non_empty_lines
        body = "\n".join(body_lines).strip()
        if len(normalized) <= LOCAL_CHAPTER_MAX_CHARS:
            summary_source = body or normalized
            return [
                {
                    "title": heading[:60],
                    "canonical_title": heading[:60],
                    "summary": summary_source[:100],
                    "content": normalized,
                    "chapter_index": chapter_index,
                    "chunk_index": 0,
                }
            ]

        paragraphs = [item.strip() for item in re.split(r"\n\s*\n", body or normalized) if item.strip()]
        if len(paragraphs) <= 1:
            paragraphs = [item.strip() for item in re.split(r"(?<=[。！？.!?])\s+", body or normalized) if item.strip()]

        chunks: list[list[str]] = []
        current: list[str] = []
        current_len = 0
        for paragraph in paragraphs:
            extra = len(paragraph) + (2 if current else 0)
            if current and current_len + extra > LOCAL_CHAPTER_MAX_CHARS:
                chunks.append(current)
                current = [paragraph]
                current_len = len(paragraph)
            else:
                current.append(paragraph)
                current_len += extra
        if current:
            chunks.append(current)

        segments: list[dict[str, Any]] = []
        for chunk_index, chunk_parts in enumerate(chunks):
            chunk_body = "\n\n".join(chunk_parts).strip()
            segment_title = heading[:60] if chunk_index == 0 else f"{heading[:48]} · 片段 {chunk_index + 1}"
            segment_content = f"{segment_title}\n\n{chunk_body}".strip()
            segments.append(
                {
                    "title": segment_title[:60],
                    "canonical_title": heading[:60],
                    "summary": chunk_body[:100],
                    "content": segment_content,
                    "chapter_index": chapter_index,
                    "chunk_index": chunk_index,
                }
            )
        return segments

    def _serialize_chapter(self, chapter: ChapterChunk) -> dict[str, Any]:
        meta = dict(chapter.meta or {})
        return {
            "id": chapter.id,
            "chapter_index": chapter.chapter_index,
            "chunk_index": chapter.chunk_index,
            "title": meta.get("title") or f"章节 {chapter.chapter_index + 1}",
            "summary": meta.get("summary") or chapter.content[:100],
            "content_excerpt": chapter.content[:200],
        }

    def _chapter_meta(self, chapter: ChapterChunk) -> dict[str, Any]:
        return deepcopy(chapter.meta or {})

    def _chapter_stages(self, chapter: ChapterChunk) -> dict[str, Any]:
        meta = self._chapter_meta(chapter)
        stages = meta.get("stages")
        return deepcopy(stages) if isinstance(stages, dict) else {}

    def _chapter_step_status(self, chapter: ChapterChunk, step_name: str) -> str:
        stages = self._chapter_stages(chapter)
        stage = stages.get(step_name)
        if isinstance(stage, dict):
            return str(stage.get("status") or StepStatus.PENDING.value)
        return StepStatus.PENDING.value

    def _chapter_stage_chain(self, chapter: ChapterChunk) -> dict[str, Any]:
        stages = self._chapter_stages(chapter)
        return {
            key: deepcopy(value.get("output", {}))
            for key, value in stages.items()
            if isinstance(value, dict)
        }

    def _set_chapter_stage_state(
        self,
        chapter: ChapterChunk,
        step_name: str,
        *,
        status: str,
        output: dict[str, Any],
        attempt: int,
        provider: str | None,
        model: str | None,
    ) -> None:
        meta = self._chapter_meta(chapter)
        stages = meta.get("stages")
        if not isinstance(stages, dict):
            stages = {}
        stages[step_name] = {
            "status": status,
            "output": deepcopy(output),
            "attempt": attempt,
            "provider": provider,
            "model": model,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if step_name == "consistency_check" and isinstance(output.get("consistency"), dict):
            meta["consistency_summary"] = deepcopy(output["consistency"])
        meta["stages"] = stages
        chapter.meta = meta
        self.db.add(chapter)

    def _build_chapter_stage_output(self, output: dict[str, Any] | None, comment: str | None = None) -> dict[str, Any]:
        merged = deepcopy(output or {})
        if comment:
            merged["review_comment"] = comment
        return merged

    def _chapter_dependency_satisfied(self, project_id: str, chapter: ChapterChunk, step_name: str) -> bool:
        dependency = CHAPTER_DEPENDENCIES.get(step_name)
        if dependency == "chapter_chunking":
            step = self.db.scalar(
                select(PipelineStep).where(PipelineStep.project_id == project_id, PipelineStep.step_name == "chapter_chunking")
            )
            return bool(step and step.status == StepStatus.APPROVED.value)
        if not dependency:
            return True
        return self._chapter_step_status(chapter, dependency) == StepStatus.APPROVED.value

    def _list_project_chapters(self, project_id: str) -> list[ChapterChunk]:
        return list(
            self.db.scalars(
                select(ChapterChunk)
                .where(ChapterChunk.project_id == project_id)
                .order_by(ChapterChunk.chapter_index.asc(), ChapterChunk.chunk_index.asc())
            ).all()
        )

    def _get_chapter(self, project_id: str, chapter_id: str) -> ChapterChunk:
        chapter = self.db.scalar(
            select(ChapterChunk).where(ChapterChunk.project_id == project_id, ChapterChunk.id == chapter_id)
        )
        if not chapter:
            raise ValueError("chapter not found")
        return chapter

    def _resolve_target_chapter(
        self,
        project_id: str,
        step_name: str,
        chapter_id: str | None,
        *,
        force: bool,
    ) -> ChapterChunk:
        if step_name not in CHAPTER_SCOPED_STEPS:
            raise ValueError("step is not chapter-scoped")
        if chapter_id:
            chapter = self._get_chapter(project_id, chapter_id)
            if not self._chapter_dependency_satisfied(project_id, chapter, step_name):
                raise ValueError("selected chapter is not ready for this step")
            chapter_status = self._chapter_step_status(chapter, step_name)
            if not force and chapter_status not in {
                StepStatus.PENDING.value,
                StepStatus.REWORK_REQUESTED.value,
                StepStatus.FAILED.value,
            }:
                raise ValueError("selected chapter is not runnable for this step")
            return chapter
        chapter = self._next_pending_chapter(project_id, step_name)
        if not chapter:
            raise ValueError("no runnable chapter found for this step")
        return chapter

    def _next_pending_chapter(self, project_id: str, step_name: str) -> ChapterChunk | None:
        for chapter in self._list_project_chapters(project_id):
            if not self._chapter_dependency_satisfied(project_id, chapter, step_name):
                continue
            if self._chapter_step_status(chapter, step_name) != StepStatus.APPROVED.value:
                return chapter
        return None

    def _get_current_step_chapter(self, project_id: str, step: PipelineStep) -> ChapterChunk:
        chapter_payload = deepcopy(step.output_ref or {}).get("chapter")
        if not isinstance(chapter_payload, dict):
            raise ValueError("current step does not have a chapter context")
        chapter_id = chapter_payload.get("id")
        if not isinstance(chapter_id, str) or not chapter_id:
            raise ValueError("current step chapter context is invalid")
        return self._get_chapter(project_id, chapter_id)

    def _build_step_queue_output(
        self,
        step_name: str,
        next_chapter: ChapterChunk | None,
        last_chapter: ChapterChunk | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"queue_state": "pending_chapter_selection"}
        if next_chapter:
            payload["next_chapter"] = self._serialize_chapter(next_chapter)
            payload["message"] = f"请选择并运行下一章：{self._serialize_chapter(next_chapter)['title']}"
        if last_chapter:
            payload["last_completed_chapter"] = self._serialize_chapter(last_chapter)
        payload["step_name"] = step_name
        return payload

    def _storyboard_version_chapter_id(self, version: StoryboardVersion) -> str | None:
        input_snapshot = deepcopy(version.input_snapshot or {})
        output_snapshot = deepcopy(version.output_snapshot or {})
        for candidate in (output_snapshot.get("chapter"), input_snapshot.get("chapter")):
            if isinstance(candidate, dict):
                chapter_id = candidate.get("id")
                if isinstance(chapter_id, str) and chapter_id:
                    return chapter_id
        return None

    def _build_video_consistency_report(
        self,
        project_id: str,
        chapter: ChapterChunk | None,
        output: dict[str, Any],
    ) -> dict[str, Any]:
        if chapter is None:
            return {}
        project = self._get_project(project_id)
        context = self._build_storyboard_consistency_context(project, chapter)
        context["video_artifact"] = deepcopy(output.get("artifact") or {})
        report = score_consistency(context, threshold=max(1, settings.consistency_threshold - 5))
        return {
            "scope": "chapter_video_clips",
            "chapter_id": chapter.id,
            "score": report.score,
            "dimensions": report.dimensions,
            "threshold": max(1, settings.consistency_threshold - 5),
            "details": report.details,
        }

    def _build_storyboard_consistency_context(self, project: Project, chapter: ChapterChunk | None) -> dict[str, Any]:
        story_bible = normalize_style_profile(project.style_profile).get("story_bible", {})
        frames = self._storyboard_frames_for_chapter(chapter)
        neighbor_frames: list[dict[str, Any]] = []
        if chapter is not None:
            chapters = self._list_project_chapters(project.id)
            try:
                current_index = next(index for index, item in enumerate(chapters) if item.id == chapter.id)
            except StopIteration:
                current_index = -1
            if current_index >= 0:
                for offset in (-1, 1):
                    neighbor_index = current_index + offset
                    if 0 <= neighbor_index < len(chapters):
                        neighbor_frames.extend(self._storyboard_frames_for_chapter(chapters[neighbor_index]))
        return {
            "project_id": project.id,
            "chapter_id": chapter.id if chapter else None,
            "chapter_title": str((chapter.meta or {}).get("title") or "") if chapter else "",
            "frames": frames,
            "neighbor_frames": neighbor_frames,
            "story_bible": story_bible if isinstance(story_bible, dict) else {},
        }

    def _storyboard_frames_for_chapter(self, chapter: ChapterChunk | None) -> list[dict[str, Any]]:
        if chapter is None:
            return []
        gallery = self._load_storyboard_gallery(chapter)
        frames = gallery.get("frames")
        if not isinstance(frames, list):
            return []
        return [deepcopy(item) for item in frames if isinstance(item, dict)]

    def _project_chapter_consistency_scores(
        self,
        project: Project,
        *,
        current_chapter_id: str | None,
        current_consistency: Any | None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for chapter in self._list_project_chapters(project.id):
            title = str((chapter.meta or {}).get("title") or f"章节 {chapter.chapter_index + 1}")
            if chapter.id == current_chapter_id and current_consistency is not None:
                report = current_consistency
            else:
                existing = (chapter.meta or {}).get("consistency_summary")
                if isinstance(existing, dict) and isinstance(existing.get("score"), int):
                    report = type(current_consistency)(
                        score=int(existing["score"]),
                        dimensions=deepcopy(existing.get("dimensions") or {}),
                        should_rework=bool(existing.get("score", 0) < settings.consistency_threshold),
                        details=deepcopy(existing.get("details") or {"scoring_mode": "vision_model_cached"}),
                    ) if current_consistency is not None else score_consistency(
                        self._build_storyboard_consistency_context(project, chapter),
                        threshold=settings.consistency_threshold,
                    )
                else:
                    frames = self._storyboard_frames_for_chapter(chapter)
                    if not frames:
                        continue
                    report = score_consistency(
                        self._build_storyboard_consistency_context(project, chapter),
                        threshold=settings.consistency_threshold,
                    )
            results.append(
                {
                    "chapter_id": chapter.id,
                    "chapter_title": title,
                    "score": report.score,
                    "dimensions": report.dimensions,
                    "should_rework": report.should_rework,
                    "frame_count": report.details.get("frame_count", 0),
                    "scoring_mode": report.details.get("scoring_mode", "heuristic"),
                }
            )
        return results

    async def _score_storyboard_consistency_with_model(
        self,
        project: Project,
        step: PipelineStep,
        consistency_context: dict[str, Any],
        *,
        threshold: int,
    ) -> Any:
        fallback = score_consistency(consistency_context, threshold=threshold)
        provider, model = self._resolve_binding(project, "consistency_check", "consistency")
        if provider == "local":
            details = deepcopy(fallback.details)
            details["scoring_mode"] = "heuristic_fallback"
            return type(fallback)(
                score=fallback.score,
                dimensions=fallback.dimensions,
                should_rework=fallback.should_rework,
                details=details,
            )

        try:
            adapter = self.registry.resolve(provider)
            visual_inputs = self._consistency_visual_inputs(consistency_context)
            if not visual_inputs:
                details = deepcopy(fallback.details)
                details["scoring_mode"] = "heuristic_fallback"
                return type(fallback)(
                    score=fallback.score,
                    dimensions=fallback.dimensions,
                    should_rework=fallback.should_rework,
                    details=details,
                )
            prompt = self._build_consistency_review_prompt(consistency_context, threshold)
            model_candidates = [model]
            if provider == "openrouter":
                for candidate in ("openrouter/auto", "openai/gpt-5", "google/gemini-2.5-pro", "anthropic/claude-sonnet-4"):
                    if candidate not in model_candidates:
                        model_candidates.append(candidate)
            last_error: Exception | None = None
            for candidate_model in model_candidates:
                try:
                    req = ProviderRequest(
                        step="consistency",
                        model=candidate_model,
                        input={
                            "text_prompt": prompt,
                            "visual_inputs": visual_inputs,
                        },
                        prompt=(
                            "你是电影制片流程中的视觉一致性校核师。你会同时查看 Story Bible 参考图、当前章节分镜图和相邻章节分镜图。"
                            "请严格输出 JSON，不要返回 markdown，不要解释。"
                            'JSON 结构必须是 {"score":0-100,"dimensions":{"chapter_internal_character":0-100,"chapter_internal_scene":0-100,"reference_adherence":0-100,"cross_chapter_style":0-100},"low_frames":[{"shot_index":1,"reason":""}],"summary":""}'
                        ),
                        params={"temperature": 0.1, "max_tokens": 900},
                    )
                    response = await adapter.invoke(req)
                    parsed = self._extract_json_object(str(response.output.get("text") or response.output.get("summary") or ""))
                    if not isinstance(parsed, dict):
                        raise ValueError("visual consistency model did not return valid JSON")
                    dimensions = parsed.get("dimensions") or {}
                    details = deepcopy(fallback.details)
                    details.update(
                        {
                            "scoring_mode": "vision_model",
                            "model_provider": provider,
                            "model_name": candidate_model,
                            "summary": parsed.get("summary"),
                            "low_frames": parsed.get("low_frames") or details.get("low_frames", []),
                        }
                    )
                    score = int(parsed.get("score") or fallback.score)
                    normalized_dimensions = {
                        "chapter_internal_character": int(dimensions.get("chapter_internal_character", fallback.dimensions.get("chapter_internal_character", 0))),
                        "chapter_internal_scene": int(dimensions.get("chapter_internal_scene", fallback.dimensions.get("chapter_internal_scene", 0))),
                        "reference_adherence": int(dimensions.get("reference_adherence", fallback.dimensions.get("reference_adherence", 0))),
                        "cross_chapter_style": int(dimensions.get("cross_chapter_style", fallback.dimensions.get("cross_chapter_style", 0))),
                    }
                    return type(fallback)(
                        score=score,
                        dimensions=normalized_dimensions,
                        should_rework=score < threshold,
                        details=details,
                    )
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    continue
            raise last_error or ValueError("visual consistency scoring failed")
        except Exception as exc:  # noqa: BLE001
            details = deepcopy(fallback.details)
            details["scoring_mode"] = "heuristic_fallback"
            details["fallback_reason"] = str(exc)
            return type(fallback)(
                score=fallback.score,
                dimensions=fallback.dimensions,
                should_rework=fallback.should_rework,
                details=details,
            )

    def _build_consistency_review_prompt(self, consistency_context: dict[str, Any], threshold: int) -> str:
        story_bible = consistency_context.get("story_bible") or {}
        characters = [item for item in story_bible.get("characters", []) if isinstance(item, dict)][:4]
        scenes = [item for item in story_bible.get("scenes", []) if isinstance(item, dict)][:4]
        frame_descriptions = []
        for frame in consistency_context.get("frames", [])[:8]:
            if not isinstance(frame, dict):
                continue
            frame_descriptions.append(
                {
                    "shot_index": frame.get("shot_index"),
                    "visual": frame.get("visual"),
                    "action": frame.get("action"),
                    "dialogue": frame.get("dialogue"),
                }
            )
        neighbor_descriptions = []
        for frame in consistency_context.get("neighbor_frames", [])[:4]:
            if not isinstance(frame, dict):
                continue
            neighbor_descriptions.append(
                {
                    "shot_index": frame.get("shot_index"),
                    "visual": frame.get("visual"),
                    "action": frame.get("action"),
                }
            )
        payload = {
            "chapter_title": consistency_context.get("chapter_title"),
            "threshold": threshold,
            "reference_characters": characters,
            "reference_scenes": scenes,
            "current_frames": frame_descriptions,
            "neighbor_frames": neighbor_descriptions,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _consistency_visual_inputs(self, consistency_context: dict[str, Any]) -> list[dict[str, Any]]:
        images: list[dict[str, Any]] = []
        story_bible = consistency_context.get("story_bible") or {}
        for item in (story_bible.get("characters") or [])[:3]:
            if not isinstance(item, dict):
                continue
            url = self._reference_image_data_url(item.get("reference_storage_key"), item.get("reference_image_url"))
            if url:
                images.append({"url": url, "label": f"character:{item.get('name')}"})
        for item in (story_bible.get("scenes") or [])[:2]:
            if not isinstance(item, dict):
                continue
            url = self._reference_image_data_url(item.get("reference_storage_key"), item.get("reference_image_url"))
            if url:
                images.append({"url": url, "label": f"scene:{item.get('name')}"})
        for frame in consistency_context.get("frames", [])[:8]:
            if not isinstance(frame, dict):
                continue
            url = self._reference_image_data_url(frame.get("storage_key"), frame.get("image_url"))
            if url:
                images.append({"url": url, "label": f"current-shot:{frame.get('shot_index')}"})
        for frame in consistency_context.get("neighbor_frames", [])[:4]:
            if not isinstance(frame, dict):
                continue
            url = self._reference_image_data_url(frame.get("storage_key"), frame.get("image_url"))
            if url:
                images.append({"url": url, "label": f"neighbor-shot:{frame.get('shot_index')}"})
        return images

    def _reference_image_data_url(self, storage_key: Any, fallback_url: Any) -> str | None:
        if isinstance(storage_key, str) and storage_key and Path(storage_key).exists():
            path = Path(storage_key)
            mime_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            return f"data:{mime_type};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
        if isinstance(fallback_url, str) and fallback_url.startswith("data:"):
            return fallback_url
        return None

    def _enhance_step_output(
        self,
        project: Project,
        step: PipelineStep,
        output: dict[str, Any],
        chapter: ChapterChunk | None,
    ) -> dict[str, Any]:
        artifact = deepcopy(output.get("artifact") or {})
        if step.step_name == "ingest_parse":
            source_document = deepcopy(step.input_ref.get("source_document") or {})
            full_content = source_document.get("full_content") or source_document.get("content") or ""
            artifact.update(
                {
                    "title": source_document.get("file_name") or project.name,
                    "full_text": full_content,
                    "char_count": source_document.get("char_count", len(str(full_content))),
                    "line_count": source_document.get("line_count"),
                    "summary": f"已导入全文，共 {source_document.get('char_count', len(str(full_content)))} 字符。",
                }
            )
        elif step.step_name == "chapter_chunking":
            source_document = deepcopy(step.input_ref.get("source_document") or {})
            full_content = str(source_document.get("full_content") or source_document.get("content") or "")
            chapters = self._split_into_chapters(full_content)
            chapter_count = len({int(item.get("chapter_index", idx)) for idx, item in enumerate(chapters)})
            artifact.update(
                {
                    "chapter_count": chapter_count,
                    "segment_count": len(chapters),
                    "chapter_titles": [item["title"] for item in chapters],
                    "summary": f"已在本地识别 {chapter_count} 个章节，共拆分为 {len(chapters)} 个可处理片段。",
                }
            )
        elif step.step_name == "story_scripting" and chapter is not None:
            script_payload = self._build_chapter_script_payload(project, chapter)
            artifact.update(script_payload)
        elif step.step_name == "shot_detailing" and chapter is not None:
            shot_payload = self._build_shot_detail_payload(project, chapter)
            artifact.update(shot_payload)
        output["artifact"] = artifact
        return output

    async def _augment_story_bible_after_chunking(
        self,
        project: Project,
        step: PipelineStep,
        output: dict[str, Any],
    ) -> dict[str, Any]:
        story_bible_refs = await self._refresh_story_bible_from_chapters(project, step)
        if not story_bible_refs:
            return output

        style_profile = normalize_style_profile(project.style_profile)
        story_bible = deepcopy(style_profile.get("story_bible") or {})
        story_bible["characters"] = story_bible_refs["characters"]
        story_bible["scenes"] = story_bible_refs["scenes"]
        story_bible["reference_digest"] = story_bible_refs["reference_digest"]
        style_profile["story_bible"] = story_bible
        project.style_profile = style_profile
        self.db.add(project)

        artifact = deepcopy(output.get("artifact") or {})
        artifact["story_bible"] = {
            "characters": story_bible_refs["characters"],
            "scenes": story_bible_refs["scenes"],
            "reference_digest": story_bible_refs["reference_digest"],
        }
        output["artifact"] = artifact
        return output

    async def _refresh_story_bible_from_chapters(
        self,
        project: Project,
        step: PipelineStep,
    ) -> dict[str, Any]:
        chapters = self._list_project_chapters(project.id)
        if not chapters:
            return {}
        chapter_digest = self._build_story_bible_reference_digest_from_chunks(chapters)
        story_bible_refs = await self._extract_story_bible_reference_bundle(project, step, chapters, chapter_digest)
        if not story_bible_refs:
            return {}

        style_profile = normalize_style_profile(project.style_profile)
        story_bible = deepcopy(style_profile.get("story_bible") or {})
        story_bible["characters"] = story_bible_refs["characters"]
        story_bible["scenes"] = story_bible_refs["scenes"]
        story_bible["reference_digest"] = story_bible_refs["reference_digest"]
        style_profile["story_bible"] = story_bible
        project.style_profile = style_profile
        self.db.add(project)
        return story_bible_refs

    async def _extract_story_bible_reference_bundle(
        self,
        project: Project,
        step: PipelineStep,
        chapters: list[ChapterChunk],
        chapter_digest: list[dict[str, Any]],
    ) -> dict[str, Any]:
        try:
            extracted = await self._extract_story_bible_entities_with_model(project, chapters)
        except Exception:  # noqa: BLE001
            extracted = None
        if not extracted:
            extracted = self._build_local_story_bible_fallback(project, chapters, chapter_digest)

        characters = self._normalize_story_bible_entities(extracted.get("characters"), kind="character")
        scenes = self._normalize_story_bible_entities(extracted.get("scenes"), kind="scene")
        if not characters and not scenes:
            fallback = self._build_local_story_bible_fallback(project, chapters, chapter_digest)
            characters = self._normalize_story_bible_entities(fallback.get("characters"), kind="character")
            scenes = self._normalize_story_bible_entities(fallback.get("scenes"), kind="scene")
        if not characters and not scenes:
            return {}

        try:
            await self._generate_story_bible_reference_images(project, step, characters, scenes)
        except Exception:  # noqa: BLE001
            pass
        return {
            "characters": characters,
            "scenes": scenes,
            "reference_digest": chapter_digest[:12],
        }

    async def _extract_story_bible_entities_with_model(
        self,
        project: Project,
        chapters: list[ChapterChunk],
    ) -> dict[str, Any] | None:
        if not chapters:
            return None
        provider, model = self._resolve_binding(project, "story_scripting", "script")
        if provider == "local":
            return None
        adapter = self.registry.resolve(provider)
        chapter_inputs = self._build_story_bible_reference_digest_from_chunks(chapters)

        async def extract_for_chapter(chapter_input: dict[str, Any]) -> dict[str, Any]:
            prompt = (
                "你是小说实体抽取与影视化设定专家。请只依据当前章节提供的原文上下文抽取实体，禁止虚构。\n"
                "严格返回 JSON 对象，不要返回 markdown，不要解释，不要输出代码块。\n"
                "JSON 结构必须是："
                '{"characters":[{"name":"","aliases":[],"description":"","visual_anchor":"","wardrobe_anchor":"","priority":1,"evidence":""}],'
                '"scenes":[{"name":"","aliases":[],"description":"","visual_anchor":"","mood":"","priority":1,"evidence":""}]}\n'
                "要求：\n"
                "1) name 必须来自章节原文（或 name_candidates），不允许出现未提及的人名/地名。\n"
                "2) characters 保留本章最关键人物，最多 6 个；scenes 保留本章关键场景，最多 6 个。\n"
                "3) aliases 最多 3 个，必须是原文中的同一实体别称。\n"
                "4) evidence 给出原文中的短语证据（10~30字）。\n"
                "5) description/visual_anchor 要可用于后续分镜一致性控制。"
            )
            req = ProviderRequest(
                step="script",
                model=model,
                input={"project_name": project.name, "chapter": chapter_input},
                prompt=prompt,
                params={"temperature": 0.05, "max_tokens": 1600},
            )
            response = await adapter.invoke(req)
            text = str(response.output.get("text") or response.output.get("summary") or "").strip()
            parsed = self._extract_json_object(text)
            if not isinstance(parsed, dict):
                return {"characters": [], "scenes": []}
            parsed["chapter_ids"] = [chapter_input.get("chapter_id")]
            parsed["chapter_titles"] = [chapter_input.get("title")]
            return parsed

        semaphore = asyncio.Semaphore(3)

        async def guarded_extract(chapter_input: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                return await extract_for_chapter(chapter_input)

        chapter_entities = await asyncio.gather(*(guarded_extract(item) for item in chapter_inputs), return_exceptions=True)
        collected_characters: list[dict[str, Any]] = []
        collected_scenes: list[dict[str, Any]] = []
        for chapter_input, entity_block in zip(chapter_inputs, chapter_entities):
            if isinstance(entity_block, Exception):
                continue
            for item in entity_block.get("characters") or []:
                if not isinstance(item, dict):
                    continue
                candidate = deepcopy(item)
                candidate["chapter_ids"] = [chapter_input["chapter_id"]]
                candidate["chapter_titles"] = [chapter_input["title"]]
                candidate["occurrence_count"] = int(candidate.get("occurrence_count") or 1)
                collected_characters.append(candidate)
            for item in entity_block.get("scenes") or []:
                if not isinstance(item, dict):
                    continue
                candidate = deepcopy(item)
                candidate["chapter_ids"] = [chapter_input["chapter_id"]]
                candidate["chapter_titles"] = [chapter_input["title"]]
                candidate["occurrence_count"] = int(candidate.get("occurrence_count") or 1)
                collected_scenes.append(candidate)

        consolidated = await self._consolidate_story_bible_entities_with_model(
            project,
            collected_characters,
            collected_scenes,
        )
        if isinstance(consolidated, dict):
            collected_characters = list(consolidated.get("characters") or collected_characters)
            collected_scenes = list(consolidated.get("scenes") or collected_scenes)

        return {
            "characters": self._dedupe_story_bible_entities(collected_characters, kind="character"),
            "scenes": self._dedupe_story_bible_entities(collected_scenes, kind="scene"),
        }

    async def _consolidate_story_bible_entities_with_model(
        self,
        project: Project,
        characters: list[dict[str, Any]],
        scenes: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        if not characters and not scenes:
            return None
        provider, model = self._resolve_binding(project, "story_scripting", "script")
        if provider == "local":
            return None

        adapter = self.registry.resolve(provider)
        compact_characters = [
            {
                "name": str(item.get("name") or ""),
                "aliases": list(item.get("aliases") or [])[:4],
                "description": str(item.get("description") or ""),
                "visual_anchor": str(item.get("visual_anchor") or ""),
                "wardrobe_anchor": str(item.get("wardrobe_anchor") or ""),
                "chapter_ids": list(item.get("chapter_ids") or [])[:8],
                "chapter_titles": list(item.get("chapter_titles") or [])[:8],
                "occurrence_count": int(item.get("occurrence_count") or 1),
            }
            for item in characters[:220]
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        compact_scenes = [
            {
                "name": str(item.get("name") or ""),
                "aliases": list(item.get("aliases") or [])[:4],
                "description": str(item.get("description") or ""),
                "visual_anchor": str(item.get("visual_anchor") or ""),
                "mood": str(item.get("mood") or ""),
                "chapter_ids": list(item.get("chapter_ids") or [])[:8],
                "chapter_titles": list(item.get("chapter_titles") or [])[:8],
                "occurrence_count": int(item.get("occurrence_count") or 1),
            }
            for item in scenes[:220]
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        if not compact_characters and not compact_scenes:
            return None

        prompt = (
            "你是影视开发中的设定总监。任务是对候选人物/场景做跨章节去重与别名归并。\n"
            "只允许基于输入候选做归并，不得新增不存在的实体。\n"
            "严格返回 JSON 对象，不要 markdown，不要解释。\n"
            "返回结构："
            '{"characters":[{"name":"","aliases":[],"description":"","visual_anchor":"","wardrobe_anchor":"","priority":1,'
            '"chapter_ids":[],"chapter_titles":[],"occurrence_count":1}],'
            '"scenes":[{"name":"","aliases":[],"description":"","visual_anchor":"","mood":"","priority":1,'
            '"chapter_ids":[],"chapter_titles":[],"occurrence_count":1}]}\n'
            "规则：\n"
            "1) 将同一实体不同称呼合并（例如昵称/全名/职务称呼）。\n"
            "2) name 使用最可辨识、最稳定的主称呼；aliases 留其余称呼。\n"
            "3) 角色和场景分别最多保留 8 个，按 occurrence_count 与叙事重要性排序。\n"
            "4) chapter_ids/chapter_titles 保留并集；occurrence_count 使用合并后总出现次数。"
        )
        req = ProviderRequest(
            step="script",
            model=model,
            input={
                "project_name": project.name,
                "characters": compact_characters,
                "scenes": compact_scenes,
            },
            prompt=prompt,
            params={"temperature": 0.05, "max_tokens": 1800},
        )
        try:
            response = await adapter.invoke(req)
        except Exception:  # noqa: BLE001
            return None
        text = str(response.output.get("text") or response.output.get("summary") or "").strip()
        parsed = self._extract_json_object(text)
        if not isinstance(parsed, dict):
            return None
        if not isinstance(parsed.get("characters"), list) and not isinstance(parsed.get("scenes"), list):
            return None
        return parsed

    def _extract_json_object(self, text: str) -> dict[str, Any] | None:
        if not text:
            return None
        stripped = text.strip()
        candidates = [stripped]
        if "```" in stripped:
            code_blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.S)
            candidates = code_blocks + candidates
        candidates.extend(re.findall(r"(\{.*\})", stripped, flags=re.S))
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    def _build_story_bible_reference_digest_from_chunks(self, chapters: list[ChapterChunk]) -> list[dict[str, Any]]:
        digest: list[dict[str, Any]] = []
        canonical_groups: dict[str, list[ChapterChunk]] = {}
        for chapter in chapters:
            title = str((chapter.meta or {}).get("canonical_title") or (chapter.meta or {}).get("title") or chapter.id)
            canonical_groups.setdefault(title, []).append(chapter)
        all_groups = list(canonical_groups.values())
        if len(all_groups) <= STORY_BIBLE_MAX_CHAPTERS:
            selected_groups = all_groups
        else:
            indexes = {
                round(index * (len(all_groups) - 1) / max(STORY_BIBLE_MAX_CHAPTERS - 1, 1))
                for index in range(STORY_BIBLE_MAX_CHAPTERS)
            }
            selected_groups = [all_groups[index] for index in sorted(indexes)]
        for group in selected_groups:
            first = group[0]
            title = str((first.meta or {}).get("canonical_title") or (first.meta or {}).get("title") or f"章节 {first.chapter_index + 1}")
            if self._is_meta_chapter_title(title):
                continue
            body = "\n\n".join(self._chapter_body_text(item) for item in group).strip()
            context = self._story_bible_chapter_context(body)
            name_candidates = self._extract_story_bible_name_candidates(body, limit=18)
            digest.append(
                {
                    "chapter_id": first.id,
                    "chapter_index": first.chapter_index,
                    "chunk_index": first.chunk_index,
                    "title": title,
                    "summary": str((first.meta or {}).get("summary") or context[:280])[:280],
                    "excerpt": context[:680],
                    "context": context,
                    "name_candidates": name_candidates,
                }
            )
        return digest

    def _story_bible_chapter_context(self, body: str, max_chars: int = STORY_BIBLE_CONTEXT_CHARS) -> str:
        cleaned = re.sub(r"\s+", " ", body).strip()
        if len(cleaned) <= max_chars:
            return cleaned
        head_len = max_chars // 2
        mid_len = max_chars // 4
        tail_len = max_chars - head_len - mid_len
        head = cleaned[:head_len]
        middle_start = max(0, (len(cleaned) // 2) - (mid_len // 2))
        middle = cleaned[middle_start : middle_start + mid_len]
        tail = cleaned[-tail_len:] if tail_len > 0 else ""
        return "\n...\n".join(part for part in [head, middle, tail] if part)

    def _extract_story_bible_name_candidates(self, text: str, limit: int = 18) -> list[str]:
        counts: dict[str, int] = {}
        stop_words = {
            "他们", "我们", "自己", "一个", "前置内容", "章节", "时候", "事情", "地方", "声音", "问题", "先生们", "小姐", "女士",
            "男人", "女人", "孩子", "警察", "狱警", "囚犯", "典狱长", "监狱", "公司", "学校", "医院", "美国", "这里", "那里",
            "今天", "明天", "昨天", "现在", "后来", "开始", "然后", "已经", "没有", "不是", "这样", "那个", "这个",
            "Chapter", "CHAPTER", "chapter", "Part", "PART", "part",
        }
        for token in re.findall(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}|[\u4e00-\u9fff]{2,6}", text):
            cleaned = token.strip().strip("“”\"'()[]{}<>《》【】,，.。:：;；!?！？")
            if len(cleaned) < 2 or cleaned in stop_words or not self._is_valid_story_bible_entity_name(cleaned, kind="character"):
                continue
            counts[cleaned] = counts.get(cleaned, 0) + 1
        ranked = sorted(counts.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))
        return [name for name, _count in ranked[:limit]]

    def _is_meta_chapter_title(self, title: str) -> bool:
        compact = re.sub(r"\s+", "", title).lower()
        markers = ("前置内容", "前言", "序", "引言", "目录", "版权", "后记", "附录")
        return any(marker in compact for marker in markers)

    def _is_valid_story_bible_entity_name(self, name: str, *, kind: str) -> bool:
        value = str(name or "").strip()
        if len(value) < 2:
            return False
        if re.search(r"[，。！？:：;；,!?/\\\\|<>\\[\\](){}]", value):
            return False
        lowered = value.lower()
        if lowered.startswith("chapter") or lowered.startswith("part "):
            return False
        if re.match(r"^第[一二三四五六七八九十0-9百千]+章$", value):
            return False

        generic_terms = {"作者", "前言", "目录", "章节", "小说", "故事", "附录", "后记", "版权", "引言"}
        if any(term in value for term in generic_terms):
            return False

        if kind == "character":
            pronouns = {"他", "她", "他们", "她们", "我们", "你们", "大家", "有人", "某人"}
            if value in pronouns:
                return False
            invalid_suffix = ("说", "道", "问", "想", "看", "听", "笑", "哭", "喊", "答", "讲")
            if value.endswith(invalid_suffix) and len(value) <= 4:
                return False
        return True

    def _build_local_story_bible_fallback(
        self,
        project: Project,
        chapters: list[ChapterChunk],
        chapter_digest: list[dict[str, Any]],
    ) -> dict[str, Any]:
        source_text = "\n".join(
            f"{item.get('title', '')}\n{item.get('summary', '')}\n{item.get('context', item.get('excerpt', ''))}"
            for item in chapter_digest
        )
        character_counts: dict[str, int] = {}
        for item in chapter_digest:
            for name in item.get("name_candidates") or []:
                token = str(name).strip()
                if token:
                    character_counts[token] = character_counts.get(token, 0) + 2
        for token in re.findall(r"[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})?|[\u4e00-\u9fff]{2,4}", source_text):
            if token in {"他们", "我们", "自己", "一个", "前置内容", "章节"}:
                continue
            character_counts[token] = character_counts.get(token, 0) + 1

        scene_keywords = [
            "家", "学校", "医院", "公路", "墓地", "森林", "小镇", "客厅", "卧室", "厨房", "庭院", "旅馆", "教堂", "地下室",
        ]
        scene_counts: dict[str, int] = {}
        for keyword in scene_keywords:
            count = source_text.count(keyword)
            if count:
                scene_counts[keyword] = count

        characters = [
            {
                "name": name,
                "description": f"{name} 是故事中的核心人物，需要在后续镜头中保持外貌、年龄感和服装连续一致。",
                "visual_anchor": f"{name}，写实电影角色设定，稳定面部特征，稳定服装层次。",
                "wardrobe_anchor": "保持连续一致的服装与材质细节。",
                "priority": index + 1,
            }
            for index, (name, count) in enumerate(sorted(character_counts.items(), key=lambda item: (-item[1], item[0]))[:6])
            if count >= 2
        ]
        scenes = [
            {
                "name": name,
                "description": f"{name} 是故事高频场景，需要在后续镜头中保持空间结构、色调和光线逻辑一致。",
                "visual_anchor": f"{name}，真实环境设定图，稳定空间布局，稳定光影氛围。",
                "mood": "连贯、稳定、可复现",
                "priority": index + 1,
            }
            for index, (name, count) in enumerate(sorted(scene_counts.items(), key=lambda item: (-item[1], item[0]))[:6])
            if count >= 1
        ]
        return {
            "characters": self._dedupe_story_bible_entities(characters, kind="character"),
            "scenes": self._dedupe_story_bible_entities(scenes, kind="scene"),
        }

    def _dedupe_story_bible_entities(self, items: list[dict[str, Any]], *, kind: str) -> list[dict[str, Any]]:
        buckets: list[dict[str, Any]] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            aliases = self._story_bible_aliases(raw)
            alias_keys = {key for alias in aliases for key in self._story_bible_entity_keys(alias)}
            match_bucket = None
            for bucket in buckets:
                if alias_keys.intersection(bucket["_alias_keys"]):
                    match_bucket = bucket
                    break
                if self._names_likely_same(aliases, bucket["_aliases"]):
                    match_bucket = bucket
                    break

            if match_bucket is None:
                match_bucket = {
                    "name": name,
                    "aliases": [],
                    "description": str(raw.get("description") or ""),
                    "visual_anchor": str(raw.get("visual_anchor") or raw.get("description") or ""),
                    "priority": int(raw.get("priority") or (len(buckets) + 1)),
                    "chapter_ids": list(dict.fromkeys(raw.get("chapter_ids") or [])),
                    "chapter_titles": list(dict.fromkeys(raw.get("chapter_titles") or [])),
                    "occurrence_count": max(1, int(raw.get("occurrence_count") or 1)),
                    "_aliases": set(aliases),
                    "_alias_keys": set(alias_keys),
                    "_name_counts": {name: max(1, int(raw.get("occurrence_count") or 1))},
                }
                if kind == "character":
                    match_bucket["wardrobe_anchor"] = str(raw.get("wardrobe_anchor") or "")
                else:
                    match_bucket["mood"] = str(raw.get("mood") or "")
                buckets.append(match_bucket)
                continue

            for alias in aliases:
                match_bucket["_aliases"].add(alias)
            match_bucket["_alias_keys"].update(alias_keys)
            match_bucket["description"] = self._choose_longer_text(match_bucket.get("description"), raw.get("description"))
            match_bucket["visual_anchor"] = self._choose_longer_text(match_bucket.get("visual_anchor"), raw.get("visual_anchor"))
            if kind == "character":
                match_bucket["wardrobe_anchor"] = self._choose_longer_text(match_bucket.get("wardrobe_anchor"), raw.get("wardrobe_anchor"))
            else:
                match_bucket["mood"] = self._choose_longer_text(match_bucket.get("mood"), raw.get("mood"))
            match_bucket["chapter_ids"] = list(
                dict.fromkeys([*(match_bucket.get("chapter_ids") or []), *(raw.get("chapter_ids") or [])])
            )
            match_bucket["chapter_titles"] = list(
                dict.fromkeys([*(match_bucket.get("chapter_titles") or []), *(raw.get("chapter_titles") or [])])
            )
            match_bucket["occurrence_count"] = int(match_bucket.get("occurrence_count") or 0) + max(
                1,
                int(raw.get("occurrence_count") or 1),
            )
            match_bucket["_name_counts"][name] = match_bucket["_name_counts"].get(name, 0) + max(
                1,
                int(raw.get("occurrence_count") or 1),
            )

        normalized_buckets: list[dict[str, Any]] = []
        for bucket in buckets:
            preferred_name = self._choose_preferred_entity_name(bucket["_name_counts"])
            alias_list = [alias for alias in bucket["_aliases"] if alias != preferred_name]
            item = {
                "name": preferred_name,
                "aliases": sorted(alias_list, key=lambda value: (len(value), value), reverse=True)[:6],
                "description": bucket.get("description") or "",
                "visual_anchor": bucket.get("visual_anchor") or "",
                "priority": int(bucket.get("priority") or 999),
                "chapter_ids": list(dict.fromkeys(bucket.get("chapter_ids") or [])),
                "chapter_titles": list(dict.fromkeys(bucket.get("chapter_titles") or [])),
                "occurrence_count": max(int(bucket.get("occurrence_count") or 1), len(bucket.get("chapter_titles") or []), 1),
            }
            if kind == "character":
                item["wardrobe_anchor"] = bucket.get("wardrobe_anchor") or "保持服装、发型和年龄感稳定一致。"
            else:
                item["mood"] = bucket.get("mood") or "保持空间结构、色调和光线一致。"
            normalized_buckets.append(item)

        ordered = sorted(
            normalized_buckets,
            key=lambda item: (
                -int(item.get("occurrence_count") or 0),
                int(item.get("priority") or 999),
                str(item.get("name") or ""),
            ),
        )
        return ordered[:8]

    def _story_bible_aliases(self, raw: dict[str, Any]) -> list[str]:
        aliases: list[str] = []
        name = str(raw.get("name") or "").strip()
        if name:
            aliases.append(name)
        raw_aliases = raw.get("aliases")
        if isinstance(raw_aliases, str) and raw_aliases.strip():
            aliases.append(raw_aliases.strip())
        elif isinstance(raw_aliases, list):
            for alias in raw_aliases:
                value = str(alias or "").strip()
                if value:
                    aliases.append(value)
        cleaned = [self._strip_entity_title(value) for value in aliases]
        merged = list(dict.fromkeys([*aliases, *cleaned]))
        return [item for item in merged if item]

    def _story_bible_entity_keys(self, name: str) -> set[str]:
        lowered = name.lower()
        compact = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", lowered)
        keys: set[str] = {compact} if compact else set()
        english_tokens = [item for item in re.findall(r"[a-z0-9]+", lowered) if item]
        if english_tokens:
            keys.add(" ".join(english_tokens))
            if len(english_tokens) >= 2:
                keys.add(english_tokens[-1])
                keys.add(english_tokens[0])
        return {item for item in keys if item}

    def _names_likely_same(self, left_aliases: list[str] | set[str], right_aliases: list[str] | set[str]) -> bool:
        left = [item for item in left_aliases if item]
        right = [item for item in right_aliases if item]
        for left_name in left:
            for right_name in right:
                left_key = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", left_name.lower())
                right_key = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", right_name.lower())
                if not left_key or not right_key:
                    continue
                if left_key == right_key:
                    return True
                if min(len(left_key), len(right_key)) >= 2 and (left_key in right_key or right_key in left_key):
                    return True
                left_tokens = set(re.findall(r"[a-z0-9]+", left_key))
                right_tokens = set(re.findall(r"[a-z0-9]+", right_key))
                if left_tokens and right_tokens and len(left_tokens.intersection(right_tokens)) >= 2:
                    return True
        return False

    def _strip_entity_title(self, name: str) -> str:
        cleaned = name.strip()
        title_prefixes = [
            "mr ", "mrs ", "ms ", "dr ", "officer ", "warden ",
            "老", "小", "典狱长", "警官", "医生", "老师", "先生", "女士", "太太",
        ]
        lowered = cleaned.lower()
        for prefix in title_prefixes:
            if lowered.startswith(prefix):
                cleaned = cleaned[len(prefix) :].strip()
                lowered = cleaned.lower()
        return cleaned

    def _choose_preferred_entity_name(self, counts: dict[str, int]) -> str:
        candidates = [(name, count) for name, count in counts.items() if str(name).strip()]
        if not candidates:
            return ""
        scored = sorted(
            candidates,
            key=lambda item: (
                item[1],
                len(self._strip_entity_title(item[0])),
                len(item[0]),
            ),
            reverse=True,
        )
        return scored[0][0]

    def _choose_longer_text(self, left: Any, right: Any) -> str:
        left_text = str(left or "").strip()
        right_text = str(right or "").strip()
        return right_text if len(right_text) > len(left_text) else left_text

    def _normalize_story_bible_entities(self, items: Any, *, kind: str) -> list[dict[str, Any]]:
        if not isinstance(items, list):
            return []
        normalized: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for raw in items:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            key_candidates = self._story_bible_entity_keys(name)
            primary_key = next(iter(key_candidates), "")
            if not name or (primary_key and primary_key in seen_keys):
                continue
            if primary_key:
                seen_keys.add(primary_key)
            base = {
                "name": name,
                "description": str(raw.get("description") or raw.get("visual_anchor") or "").strip(),
                "visual_anchor": str(raw.get("visual_anchor") or raw.get("description") or "").strip(),
                "priority": int(raw.get("priority") or (len(normalized) + 1)),
                "chapter_ids": list(dict.fromkeys(raw.get("chapter_ids") or [])),
                "chapter_titles": list(dict.fromkeys(raw.get("chapter_titles") or [])),
                "occurrence_count": int(raw.get("occurrence_count") or max(1, len(raw.get("chapter_titles") or []))),
                "aliases": list(
                    dict.fromkeys(
                        [
                            str(item).strip()
                            for item in (raw.get("aliases") if isinstance(raw.get("aliases"), list) else [])
                            if str(item).strip()
                        ]
                    )
                )[:6],
            }
            if kind == "character":
                base["wardrobe_anchor"] = str(raw.get("wardrobe_anchor") or "保持服装、发型和年龄感稳定一致。").strip()
            else:
                base["mood"] = str(raw.get("mood") or "保持空间结构、色调和光线一致。").strip()
            normalized.append(base)
            if len(normalized) >= 6:
                break
        return normalized

    async def _generate_story_bible_reference_images(
        self,
        project: Project,
        step: PipelineStep,
        characters: list[dict[str, Any]],
        scenes: list[dict[str, Any]],
    ) -> None:
        provider, model = self._resolve_binding(project, "storyboard_image", "image")
        if provider == "local":
            return
        adapter = self.registry.resolve(provider)
        if not adapter.supports("image", model):
            image_catalog = next(
                (item for item in self.registry.list_catalog() if item.provider == provider and item.step == "image"),
                None,
            )
            fallback_models = image_catalog.models if image_catalog else []
            if not fallback_models:
                return
            model = fallback_models[0]

        async def render_item(category: str, item: dict[str, Any], index: int) -> None:
            prompt = self._build_story_bible_reference_prompt(project, category, item)
            request_params = {
                "aspect_ratio": "4:5" if category == "characters" else "16:9",
                "size": "1024x1280" if category == "characters" else "1536x1024",
            }
            _, artifact, _, _ = await self._generate_storyboard_frame_with_fallback(
                adapter=adapter,
                provider=provider,
                primary_model=model,
                system_prompt="你是电影前期设定美术师。只返回一张真实参考图，不要解释。",
                image_prompt=prompt,
                request_params=request_params,
                shot_index=index + 1,
            )
            reference_asset = self._materialize_story_bible_reference_asset(project, step, category, item["name"], index + 1, artifact)
            item["reference_image_url"] = reference_asset["image_url"]
            item["reference_storage_key"] = reference_asset["storage_key"]
            item["reference_provider"] = artifact.get("provider") or provider
            item["reference_model"] = artifact.get("model") or model

        for index, item in enumerate(characters[:4]):
            try:
                await render_item("characters", item, index)
            except Exception:  # noqa: BLE001
                continue
        for index, item in enumerate(scenes[:4]):
            try:
                await render_item("scenes", item, index)
            except Exception:  # noqa: BLE001
                continue

    def _build_story_bible_reference_prompt(self, project: Project, category: str, item: dict[str, Any]) -> str:
        story_bible = normalize_style_profile(project.style_profile).get("story_bible", {})
        visual_style = story_bible.get("visual_style", {}) if isinstance(story_bible, dict) else {}
        lines = [
            f"Project: {project.name}",
            f"Reference type: {category}",
            f"Name: {item.get('name')}",
            f"Description: {item.get('description')}",
            f"Visual anchor: {item.get('visual_anchor')}",
            f"Base style: {visual_style.get('preset_label') if isinstance(visual_style, dict) else '电影质感'}",
            f"Rendering: {visual_style.get('rendering') if isinstance(visual_style, dict) else '写实电影画面'}",
            f"Lighting: {visual_style.get('lighting') if isinstance(visual_style, dict) else ''}",
            "Hard constraints: single subject reference board, no text, no split panels, stable identity, production design reference.",
        ]
        return "\n".join(line for line in lines if line and not line.endswith(": "))

    def _materialize_story_bible_reference_asset(
        self,
        project: Project,
        step: PipelineStep,
        category: str,
        name: str,
        index: int,
        artifact: dict[str, Any],
    ) -> dict[str, Any]:
        file_path: Path | None = None
        mime_type = str(artifact.get("mime_type") or "image/png")
        image_data_url = artifact.get("image_data_url")
        image_base64 = artifact.get("image_base64")
        image_url = artifact.get("image_url") or artifact.get("thumbnail_url")
        prefix = f"{category}-{index:02d}-{sanitize_component(name)}"
        target_dir = project_category_dir(project.id, project.name, "references") / category
        target_dir.mkdir(parents=True, exist_ok=True)

        if isinstance(image_data_url, str) and image_data_url.startswith("data:") and ";base64," in image_data_url:
            header, encoded = image_data_url.split(",", 1)
            mime_type = header[5:].split(";", 1)[0] or mime_type
            content = base64.b64decode(encoded)
            suffix = self._suffix_for_mime_type(mime_type)
            file_path = target_dir / f"{prefix}{suffix}"
            file_path.write_bytes(content)
        elif isinstance(image_base64, str) and image_base64:
            content = base64.b64decode(image_base64)
            suffix = self._suffix_for_mime_type(mime_type)
            file_path = target_dir / f"{prefix}{suffix}"
            file_path.write_bytes(content)
        elif isinstance(image_url, str) and image_url.startswith(("http://", "https://")):
            import httpx

            response = httpx.get(image_url, timeout=90)
            response.raise_for_status()
            mime_type = response.headers.get("content-type", mime_type).split(";", 1)[0]
            suffix = self._suffix_for_mime_type(mime_type)
            file_path = target_dir / f"{prefix}{suffix}"
            file_path.write_bytes(response.content)
        else:
            raise ValueError("reference image generation did not return a real image")

        local_url = self._to_local_file_url(file_path)
        return {
            "mime_type": mime_type,
            "image_url": local_url,
            "thumbnail_url": local_url,
            "storage_key": str(file_path),
            "export_url": local_url,
        }

    def _build_chapter_script_payload(self, project: Project, chapter: ChapterChunk) -> dict[str, Any]:
        content = self._chapter_body_text(chapter)
        paragraphs = [item.strip() for item in content.split("\n\n") if item.strip()]
        beat_count = max(4, min(10, math.ceil(len(paragraphs) / 3) or 4))
        chunk_size = max(1, math.ceil(len(paragraphs) / beat_count))
        beats: list[dict[str, Any]] = []
        for index in range(beat_count):
            part = paragraphs[index * chunk_size : (index + 1) * chunk_size]
            if not part:
                continue
            source = " ".join(part)
            beats.append(
                {
                    "beat_index": index + 1,
                    "summary": source[:180],
                    "conflict": source[:80],
                    "turn": source[80:160] or source[:80],
                }
            )
        return {
            "beat_count": len(beats),
            "beats": beats,
            "summary": f"章节剧本已生成，共 {len(beats)} 个情节点。",
        }

    def _build_shot_detail_payload(self, project: Project, chapter: ChapterChunk) -> dict[str, Any]:
        content = self._chapter_body_text(chapter)
        words = len(content.split())
        paragraphs = [item.strip() for item in content.split("\n\n") if item.strip()]
        chapter_count = max(len(self._list_project_chapters(project.id)), 1)
        chapter_budget = max(20, round(project.target_duration_sec / chapter_count))
        shot_count = max(8, min(24, max(math.ceil(words / 120), math.ceil(chapter_budget / 4), len(paragraphs))))
        flat_text = re.sub(r"\s+", " ", content.replace("\n", " ")).strip()
        sentences = [item.strip(" \t\r\n-—\"'“”‘’") for item in re.split(r"(?<=[。！？!?；;:：.])\s+", flat_text) if item.strip()]
        if not sentences:
            sentences = [item.strip() for item in re.split(r"[。！？!?；;:：.]+", flat_text) if item.strip()]
        shots: list[dict[str, Any]] = []
        for index in range(shot_count):
            source = sentences[index % len(sentences)] if sentences else content[:180]
            shots.append(
                {
                    "shot_index": index + 1,
                    "duration_sec": max(2.5, round(chapter_budget / shot_count, 1)),
                    "frame_type": "中景" if index % 3 else "远景",
                    "visual": source[:160],
                    "action": source[:120],
                    "dialogue": source[:90],
                }
            )
        return {
            "shot_count": len(shots),
            "shots": shots,
            "summary": f"已细化 {len(shots)} 个分镜镜头。",
        }

    def _chapter_body_text(self, chapter: ChapterChunk) -> str:
        content = chapter.content.strip()
        if not content:
            return ""
        lines = [line.rstrip() for line in content.splitlines()]
        title = str((chapter.meta or {}).get("title") or "").strip()
        if title and lines and lines[0].strip() == title:
            body = "\n".join(lines[1:]).strip()
            return body or content
        return content

    async def _invoke_storyboard_image_step(
        self,
        project: Project,
        step: PipelineStep,
        chapter: ChapterChunk | None,
        adapter: Any,
        provider: str,
        model: str,
        system_prompt: str,
        task_prompt: str,
        style_directive: str,
        params: dict[str, Any],
    ) -> tuple[ProviderResponse, float]:
        if chapter is None:
            raise ValueError("storyboard_image requires a chapter context")

        shots = self._chapter_shots(chapter)
        if not shots:
            generated = self._build_shot_detail_payload(project, chapter)
            shots = list(generated.get("shots") or [])
        if not shots:
            raise ValueError("storyboard_image requires shot_detailing output before image generation")

        frames: list[dict[str, Any]] = []
        raw_outputs: list[dict[str, Any]] = []
        total_estimated_cost = 0.0
        system = (
            f"{system_prompt}\n"
            "你现在是电影分镜美术师。每次只返回一张真实图片，不要返回 markdown、JSON、镜头列表或文字解释。"
        )
        for shot in shots:
            shot_index = max(1, int(shot.get("shot_index") or len(frames) + 1))
            image_prompt = self._build_storyboard_image_prompt(project, chapter, shot, task_prompt, style_directive)
            reference_images = self._story_bible_reference_images_for_shot(project, shot)
            request_params = {
                **params,
                "aspect_ratio": str(params.get("aspect_ratio") or "16:9"),
                "size": params.get("size") or "1536x1024",
                "shot_index": shot_index,
                "reference_images": reference_images,
            }
            frame_response, frame_artifact, used_model, cost = await self._generate_storyboard_frame_with_fallback(
                adapter=adapter,
                provider=provider,
                primary_model=model,
                system_prompt=system,
                image_prompt=image_prompt,
                request_params=request_params,
                shot_index=shot_index,
            )
            total_estimated_cost += cost
            frame_asset = self._materialize_storyboard_frame_asset(project.id, chapter, step, shot_index, frame_artifact)
            raw_outputs.append(
                {
                    "shot_index": shot_index,
                    "prompt": image_prompt,
                    "used_model": used_model,
                    "provider_output": frame_artifact,
                }
            )
            frames.append(
                {
                    "shot_index": shot_index,
                    "title": f"镜头 {shot_index:02d}",
                    "frame_type": str(shot.get("frame_type") or "镜头"),
                    "duration_sec": float(shot.get("duration_sec") or 0),
                    "visual": str(shot.get("visual") or ""),
                    "action": str(shot.get("action") or ""),
                    "dialogue": str(shot.get("dialogue") or ""),
                    "summary": str(shot.get("visual") or "")[:160],
                    "prompt": image_prompt,
                    "provider": frame_artifact.get("provider") or provider,
                    "model": frame_artifact.get("model") or used_model,
                    "artifact_id": frame_artifact.get("artifact_id"),
                    **frame_asset,
                }
            )

        artifact = {
            "provider": provider,
            "step": "image",
            "model": model,
            "artifact_mode": "real_storyboard_frames",
            "summary": f"已真实生成当前章节 {len(frames)} 张分镜图。",
            "frame_count": len(frames),
            "frames": frames,
            "image_url": frames[0]["image_url"],
            "thumbnail_url": frames[0]["thumbnail_url"],
            "cover_image_url": frames[0]["image_url"],
            "storage_key": frames[0]["storage_key"],
        }
        return ProviderResponse(output=artifact, usage={"frame_count": len(frames)}, raw={"frames": raw_outputs}), total_estimated_cost

    async def _invoke_segment_video_step(
        self,
        project: Project,
        step: PipelineStep,
        chapter: ChapterChunk | None,
        adapter: Any,
        provider: str,
        model: str,
        system_prompt: str,
        task_prompt: str,
        style_directive: str,
        params: dict[str, Any],
    ) -> tuple[ProviderResponse, float]:
        if chapter is None:
            raise ValueError("segment_video requires a chapter context")
        reference_images = self._story_bible_reference_images_for_chapter(project)
        reference_paths = [
            str(item.get("storage_key"))
            for group in (
                normalize_style_profile(project.style_profile).get("story_bible", {}).get("characters", []),
                normalize_style_profile(project.style_profile).get("story_bible", {}).get("scenes", []),
            )
            for item in group if isinstance(item, dict) and item.get("reference_storage_key")
        ]
        prompt = self._build_segment_video_prompt(project, chapter, task_prompt, style_directive)
        req = ProviderRequest(
            step="video",
            model=model,
            input={
                "video_prompt": prompt,
                "reference_images": reference_images,
                "chapter": self._serialize_chapter(chapter),
            },
            prompt=system_prompt,
            params={
                **params,
                "seconds": int(params.get("seconds") or max(4, round(self._chapter_segment_duration(project, chapter)))),
                "size": params.get("size") or "1280x720",
                "input_reference_path": reference_paths[0] if reference_paths else None,
            },
        )
        response = await adapter.invoke(req)
        estimated_cost = await adapter.estimate_cost(req)
        return response, estimated_cost

    def _build_segment_video_prompt(
        self,
        project: Project,
        chapter: ChapterChunk,
        task_prompt: str,
        style_directive: str,
    ) -> str:
        shots = self._chapter_shots(chapter)[:8]
        shot_summaries = [
            {
                "shot_index": shot.get("shot_index"),
                "visual": shot.get("visual"),
                "action": shot.get("action"),
                "dialogue": shot.get("dialogue"),
            }
            for shot in shots
        ]
        payload = {
            "chapter_title": str((chapter.meta or {}).get("title") or f"章节 {chapter.chapter_index + 1}"),
            "chapter_summary": str((chapter.meta or {}).get("summary") or self._chapter_body_text(chapter)[:240]),
            "target_duration_sec": round(self._chapter_segment_duration(project, chapter), 1),
            "shot_summaries": shot_summaries,
            "user_video_directive": task_prompt,
            "style_bible": style_directive,
            "hard_constraints": [
                "maintain identity consistency with reference images",
                "maintain location continuity",
                "keep wardrobe and props stable",
                "cinematic motion only, no montage collage",
            ],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    async def _generate_storyboard_frame_with_fallback(
        self,
        *,
        adapter: Any,
        provider: str,
        primary_model: str,
        system_prompt: str,
        image_prompt: str,
        request_params: dict[str, Any],
        shot_index: int,
    ) -> tuple[ProviderResponse, dict[str, Any], str, float]:
        models_to_try = self._storyboard_image_model_candidates(provider, primary_model)
        last_error: Exception | None = None
        for candidate_model in models_to_try:
            try:
                req = ProviderRequest(
                    step="image",
                    model=candidate_model,
                    input={"prompt": image_prompt, "reference_images": request_params.get("reference_images", [])},
                    prompt=system_prompt,
                    params=request_params,
                )
                response = await adapter.invoke(req)
                artifact = deepcopy(response.output or {})
                if not any(isinstance(artifact.get(key), str) and artifact.get(key) for key in ("image_data_url", "image_base64", "image_url", "thumbnail_url")):
                    raise ValueError(f"storyboard_image did not return a real image for shot {shot_index}")
                cost = await adapter.estimate_cost(req)
                return response, artifact, candidate_model, cost
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
        raise ValueError(str(last_error) if last_error else f"storyboard_image failed on shot {shot_index}")

    def _storyboard_image_model_candidates(self, provider: str, primary_model: str) -> list[str]:
        if provider != "openrouter":
            return [primary_model]
        catalog = [item for item in self.registry.list_catalog() if item.provider == provider and item.step == "image"]
        available = catalog[0].models if catalog else []
        preferred = [
            primary_model,
            "openai/gpt-5-image",
            "google/gemini-3.1-flash-image-preview",
            "google/gemini-3-pro-image-preview",
            "openrouter/auto",
        ]
        candidates: list[str] = []
        for item in preferred:
            if item in available and item not in candidates:
                candidates.append(item)
        for item in available:
            if item not in candidates:
                candidates.append(item)
        if not candidates:
            candidates.append(primary_model)
        return candidates

    def _build_storyboard_image_prompt(
        self,
        project: Project,
        chapter: ChapterChunk,
        shot: dict[str, Any],
        task_prompt: str,
        style_directive: str,
    ) -> str:
        title = str((chapter.meta or {}).get("title") or f"章节 {chapter.chapter_index + 1}")
        summary = str((chapter.meta or {}).get("summary") or chapter.content[:160])
        story_bible = normalize_style_profile(project.style_profile).get("story_bible", {})
        visual_style = story_bible.get("visual_style", {}) if isinstance(story_bible, dict) else {}
        keywords = ", ".join(visual_style.get("keywords", [])) if isinstance(visual_style, dict) else ""
        palette = ", ".join(visual_style.get("palette", [])) if isinstance(visual_style, dict) else ""
        related_characters = self._story_bible_entities_for_prompt(story_bible.get("characters"), shot)
        related_scenes = self._story_bible_entities_for_prompt(story_bible.get("scenes"), shot)
        constraints = [
            "single cinematic storyboard frame",
            "no text overlay",
            "no subtitles",
            "no split panels",
            "no comic layout",
            "consistent character identity",
            "consistent wardrobe",
            "consistent location continuity",
        ]
        lines = [
            f"Chapter: {title}",
            f"Chapter summary: {summary}",
            f"Shot {int(shot.get('shot_index') or 1)}",
            f"Visual style keywords: {keywords}",
            f"Palette: {palette}",
            f"Rendering: {visual_style.get('rendering') if isinstance(visual_style, dict) else '写实电影画面'}",
            f"Lighting: {visual_style.get('lighting') if isinstance(visual_style, dict) else ''}",
            f"Camera language: {visual_style.get('camera_language') if isinstance(visual_style, dict) else ''}",
            f"Scene description: {shot.get('visual') or summary}",
            f"Character action: {shot.get('action') or ''}",
            f"Dialogue context: {shot.get('dialogue') or ''}",
            f"Shot type: {shot.get('frame_type') or '中景'}",
            f"Character reference anchors: {related_characters}",
            f"Scene reference anchors: {related_scenes}",
            f"User image directive: {task_prompt}",
            f"Style bible: {style_directive}",
            f"Hard constraints: {', '.join(constraints)}",
        ]
        return "\n".join(line for line in lines if line and not line.endswith(": "))

    def _story_bible_entities_for_prompt(self, items: Any, shot: dict[str, Any]) -> str:
        if not isinstance(items, list):
            return ""
        shot_text = " ".join(
            [
                str(shot.get("visual") or ""),
                str(shot.get("action") or ""),
                str(shot.get("dialogue") or ""),
            ]
        ).lower()
        selected: list[str] = []
        fallback: list[str] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            description = str(raw.get("visual_anchor") or raw.get("description") or "").strip()
            if not name:
                continue
            serialized = f"{name}: {description}"
            fallback.append(serialized)
            if name.lower() in shot_text or any(token in shot_text for token in self._anchor_tokens(description)):
                selected.append(serialized)
        chosen = selected[:3] or fallback[:3]
        return " | ".join(chosen)

    def _anchor_tokens(self, text: str) -> list[str]:
        return [
            token
            for token in re.findall(r"[A-Za-z]{2,}|[\u4e00-\u9fff]{2,6}", text.lower())
            if token not in {"保持", "一致", "稳定", "角色", "场景", "镜头", "光线", "空间", "服装"}
        ][:8]

    def _story_bible_reference_images_for_shot(self, project: Project, shot: dict[str, Any]) -> list[dict[str, Any]]:
        story_bible = normalize_style_profile(project.style_profile).get("story_bible", {})
        shot_text = " ".join(
            [
                str(shot.get("visual") or ""),
                str(shot.get("action") or ""),
                str(shot.get("dialogue") or ""),
            ]
        ).lower()
        selected: list[dict[str, Any]] = []
        for group in ("characters", "scenes"):
            for raw in (story_bible.get(group, []) if isinstance(story_bible, dict) else []):
                if not isinstance(raw, dict):
                    continue
                name = str(raw.get("name") or "").strip()
                if not name:
                    continue
                tokens = self._anchor_tokens(" ".join([name, str(raw.get("description") or ""), str(raw.get("visual_anchor") or "")]))
                if name.lower() not in shot_text and not any(token in shot_text for token in tokens):
                    continue
                data_url = self._reference_image_data_url(raw.get("reference_storage_key"), raw.get("reference_image_url"))
                if data_url:
                    selected.append({"url": data_url, "label": f"{group}:{name}"})
        if selected:
            return selected[:4]
        return self._story_bible_reference_images_for_chapter(project)[:4]

    def _story_bible_reference_images_for_chapter(self, project: Project) -> list[dict[str, Any]]:
        story_bible = normalize_style_profile(project.style_profile).get("story_bible", {})
        selected: list[dict[str, Any]] = []
        for group in ("characters", "scenes"):
            for raw in (story_bible.get(group, []) if isinstance(story_bible, dict) else []):
                if not isinstance(raw, dict):
                    continue
                data_url = self._reference_image_data_url(raw.get("reference_storage_key"), raw.get("reference_image_url"))
                if data_url:
                    selected.append({"url": data_url, "label": f"{group}:{raw.get('name')}"})
        return selected[:5]

    def _build_source_document_input(self, project: Project, step_name: str) -> dict[str, Any]:
        document = self.db.scalar(
            select(SourceDocument)
            .where(SourceDocument.project_id == project.id)
            .order_by(SourceDocument.created_at.desc())
            .limit(1)
        )
        storage_key = document.storage_key if document and document.storage_key else project.input_path
        result: dict[str, Any] = {
            "project_input_path": project.input_path,
            "storage_key": storage_key,
        }

        if document:
            result.update(
                {
                    "document_id": document.id,
                    "file_name": document.file_name,
                    "file_type": document.file_type,
                    "parse_status": document.parse_status,
                    "page_map": document.page_map,
                }
            )

        if not storage_key:
            return result

        path = Path(storage_key)
        result["exists"] = path.exists()
        if not path.exists():
            return result

        result["size_bytes"] = path.stat().st_size
        suffix = path.suffix.lower().lstrip(".")
        if suffix == "txt":
            text, encoding = self._read_text_file(path)
            result.update(self._build_text_payload(text, encoding=encoding))
        elif suffix == "pdf":
            text, page_map = self._read_pdf_file(path)
            result.update(self._build_text_payload(text, encoding="pdf-extracted"))
            result["page_map"] = page_map
        else:
            result["content_unavailable_reason"] = f"preview not implemented for .{suffix or 'unknown'}"

        if step_name not in LOCAL_ONLY_STEPS:
            result.pop("content", None)
            result.pop("full_content", None)
            result["content_scope"] = "excerpt_only_for_model_steps"

        if step_name in {"ingest_parse", "chapter_chunking"} and "content" not in result and result.get("full_content"):
            result["content"] = result["full_content"]
            result["content_truncated"] = False

        return result

    def _build_text_payload(self, text: str, *, encoding: str) -> dict[str, Any]:
        return {
            "encoding": encoding,
            "char_count": len(text),
            "line_count": len(text.splitlines()),
            "content_excerpt": text[:SOURCE_EXCERPT_LIMIT],
            "content_excerpt_truncated": len(text) > SOURCE_EXCERPT_LIMIT,
            "content": text[:SOURCE_CONTENT_LIMIT],
            "content_truncated": len(text) > SOURCE_CONTENT_LIMIT,
            "full_content": text,
        }

    def _read_text_file(self, path: Path) -> tuple[str, str]:
        encodings = ("utf-8-sig", "utf-8", "utf-16", "utf-16le", "gb18030")
        for encoding in encodings:
            try:
                return path.read_text(encoding=encoding), encoding
            except UnicodeDecodeError:
                continue
        return path.read_bytes().decode("utf-8", errors="replace"), "utf-8-replace"

    def _read_pdf_file(self, path: Path) -> tuple[str, dict[str, Any]]:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        page_texts: list[str] = []
        page_map: dict[str, Any] = {}
        for index, page in enumerate(reader.pages):
            extracted = (page.extract_text() or "").strip()
            page_texts.append(extracted)
            page_map[str(index + 1)] = {
                "char_count": len(extracted),
                "excerpt": extracted[:200],
            }
        return "\n\n".join(item for item in page_texts if item), page_map

    def _resolve_binding(self, project: Project, step_name: str, step_type: str) -> tuple[str, str]:
        if step_name in LOCAL_ONLY_STEPS:
            return LOCAL_STEP_MODELS[step_name]
        bindings = project.model_bindings or {}
        step_binding = bindings.get(step_name) or bindings.get(step_type)
        if isinstance(step_binding, list) and step_binding:
            provider = step_binding[0].get("provider")
            model = step_binding[0].get("model")
            if provider and model:
                return provider, model
        return self.registry.suggest_model(step_type)  # type: ignore[arg-type]

    def _list_steps(self, project_id: str) -> list[PipelineStep]:
        return list(
            self.db.scalars(
                select(PipelineStep).where(PipelineStep.project_id == project_id).order_by(PipelineStep.step_order.asc())
            ).all()
        )

    def _all_previous_approved(self, ordered_steps: list[PipelineStep], current_order: int) -> bool:
        for item in ordered_steps:
            if item.step_order >= current_order:
                break
            if item.status != StepStatus.APPROVED.value:
                return False
        return True

    def _get_step(self, project_id: str, step_id: str) -> PipelineStep:
        step = self.db.scalar(select(PipelineStep).where(PipelineStep.id == step_id, PipelineStep.project_id == project_id))
        if not step:
            raise ValueError("step not found")
        return step

    def _get_project(self, project_id: str) -> Project:
        project = self.db.scalar(select(Project).where(Project.id == project_id))
        if not project:
            raise ValueError("project not found")
        return project

    def _get_storyboard_step(self, project_id: str) -> PipelineStep:
        step = self.db.scalar(
            select(PipelineStep).where(PipelineStep.project_id == project_id, PipelineStep.step_name == "storyboard_image")
        )
        if not step:
            raise ValueError("storyboard_image step not found")
        return step

    def _set_active_storyboard_version(self, step_id: str, active_version_id: str) -> None:
        versions = list(
            self.db.scalars(select(StoryboardVersion).where(StoryboardVersion.step_id == step_id)).all()
        )
        active_version = next((item for item in versions if item.id == active_version_id), None)
        active_chapter_id = self._storyboard_version_chapter_id(active_version) if active_version else None
        for version in versions:
            if active_chapter_id and self._storyboard_version_chapter_id(version) != active_chapter_id:
                continue
            version.is_active = version.id == active_version_id
            self.db.add(version)

    def _create_storyboard_version(
        self,
        *,
        project: Project,
        step: PipelineStep,
        output: dict[str, Any],
        system_prompt: str,
        task_prompt: str,
    ) -> StoryboardVersion:
        chapter_id = None
        if isinstance(output.get("chapter"), dict):
            candidate = output["chapter"].get("id")
            if isinstance(candidate, str) and candidate:
                chapter_id = candidate
        versions = list(
            self.db.scalars(
                select(StoryboardVersion)
                .where(StoryboardVersion.project_id == project.id, StoryboardVersion.step_id == step.id)
                .order_by(StoryboardVersion.version_index.desc())
            ).all()
        )
        chapter_versions = [item for item in versions if self._storyboard_version_chapter_id(item) == chapter_id]
        latest = chapter_versions[0] if chapter_versions else None
        next_index = (latest.version_index + 1) if latest else 1
        if latest:
            latest.is_active = False
            self.db.add(latest)
        version = StoryboardVersion(
            project_id=project.id,
            step_id=step.id,
            version_index=next_index,
            source_attempt=step.attempt,
            model_provider=step.model_provider,
            model_name=step.model_name,
            input_snapshot=deepcopy(step.input_ref or {}),
            output_snapshot=deepcopy(output),
            prompt_snapshot={"system": system_prompt, "task": task_prompt},
            is_active=True,
        )
        self.db.add(version)
        self.db.flush()
        return version

    def _get_active_storyboard_version(self, step_id: str, chapter_id: str | None = None) -> StoryboardVersion | None:
        versions = list(
            self.db.scalars(
                select(StoryboardVersion)
                .where(StoryboardVersion.step_id == step_id, StoryboardVersion.is_active.is_(True))
                .order_by(StoryboardVersion.version_index.desc())
            ).all()
        )
        if chapter_id is None:
            return versions[0] if versions else None
        for version in versions:
            if self._storyboard_version_chapter_id(version) == chapter_id:
                return version
        return None

    def _update_storyboard_consistency_snapshot(
        self,
        project_id: str,
        consistency_report: dict[str, Any],
        should_rework: bool,
        *,
        chapter_id: str | None = None,
    ) -> None:
        storyboard_step = self._get_storyboard_step(project_id)
        active_version = self._get_active_storyboard_version(storyboard_step.id, chapter_id=chapter_id)
        if not active_version:
            return
        active_version.consistency_score = consistency_report.get("score")
        active_version.consistency_report = deepcopy(consistency_report)
        active_version.rollback_reason = (
            "Consistency check failed and requires rollback to storyboard image."
            if should_rework
            else None
        )
        self.db.add(active_version)

    def _rollback_storyboard_after_consistency_failure(
        self,
        project: Project,
        consistency_step: PipelineStep,
        consistency_report: dict[str, Any],
        *,
        chapter_id: str | None = None,
    ) -> PipelineStep:
        storyboard_step = self._get_storyboard_step(project.id)
        active_version = self._get_active_storyboard_version(storyboard_step.id, chapter_id=chapter_id)
        rollback_payload = {
            "triggered_by_step_id": consistency_step.id,
            "triggered_by_step_name": consistency_step.step_name,
            "consistency": consistency_report,
            "rollback_to_step_name": storyboard_step.step_name,
            "active_storyboard_version_id": active_version.id if active_version else None,
            "reason": "Consistency check failed. Compare storyboard versions and choose a new storyboard before continuing.",
            "chapter_id": chapter_id,
        }

        storyboard_output = deepcopy(storyboard_step.output_ref or {})
        history = list(storyboard_output.get("rollback_history", []))
        history.append(rollback_payload)
        storyboard_output["rollback_required"] = rollback_payload
        storyboard_output["rollback_history"] = history[-10:]
        storyboard_output["version_candidates"] = len(
            self.list_storyboard_versions(project.id, storyboard_step.id, chapter_id=chapter_id)
        )
        storyboard_step.output_ref = storyboard_output
        storyboard_step.status = StepStatus.REVIEW_REQUIRED.value
        storyboard_step.error_code = None
        storyboard_step.error_message = None
        storyboard_step.finished_at = datetime.now(timezone.utc)
        self.db.add(storyboard_step)

        if chapter_id:
            chapter = self._get_chapter(project.id, chapter_id)
            self._set_chapter_stage_state(
                chapter,
                "storyboard_image",
                status=StepStatus.REVIEW_REQUIRED.value,
                output=self._build_chapter_stage_output(storyboard_output),
                attempt=storyboard_step.attempt,
                provider=storyboard_step.model_provider,
                model=storyboard_step.model_name,
            )
            self._set_chapter_stage_state(
                chapter,
                "consistency_check",
                status=StepStatus.REWORK_REQUESTED.value,
                output=self._build_chapter_stage_output({"consistency": consistency_report}),
                attempt=consistency_step.attempt,
                provider=consistency_step.model_provider,
                model=consistency_step.model_name,
            )

        consistency_step.error_code = "CONSISTENCY_FAILED"
        consistency_step.error_message = "Consistency failed; rolled back to storyboard_image for version comparison."
        self.db.add(consistency_step)

        downstream_steps = self._list_steps(project.id)
        for item in downstream_steps:
            if item.step_order <= consistency_step.step_order:
                continue
            if item.step_name in CHAPTER_SCOPED_STEPS and chapter_id:
                chapter = self._get_chapter(project.id, chapter_id)
                self._set_chapter_stage_state(
                    chapter,
                    item.step_name,
                    status=StepStatus.PENDING.value,
                    output={},
                    attempt=0,
                    provider=item.model_provider,
                    model=item.model_name,
                )
                continue
            item.status = StepStatus.PENDING.value
            item.error_code = None
            item.error_message = None
            self.db.add(item)

        project.status = ProjectStatus.REVIEW_REQUIRED.value
        self.db.add(project)
        self._record_review(project.id, storyboard_step.id, "step", "rollback_to_storyboard_image", rollback_payload, "system")
        self.db.commit()
        self.db.refresh(storyboard_step)
        return storyboard_step

    def _record_review(
        self,
        project_id: str,
        step_id: str,
        scope_type: str,
        action_type: str,
        payload: dict[str, Any],
        created_by: str,
    ) -> None:
        self.db.add(
            ReviewAction(
                project_id=project_id,
                step_id=step_id,
                scope_type=scope_type,
                action_type=action_type,
                editor_payload=payload,
                created_by=created_by,
            )
        )

    def _active_prompt_query(self, project_id: str, step_name: str) -> Select[tuple[PromptVersion]]:
        return select(PromptVersion).where(
            PromptVersion.project_id == project_id,
            PromptVersion.step_name == step_name,
            PromptVersion.is_active.is_(True),
        )

    def _get_active_prompts(self, project_id: str, step_name: str) -> tuple[str, str]:
        active = self.db.scalar(self._active_prompt_query(project_id, step_name))
        if active:
            return active.system_prompt, active.task_prompt
        system_prompt, task_prompt = get_baseline_prompts(step_name)
        self.db.add(
            PromptVersion(
                project_id=project_id,
                step_name=step_name,
                system_prompt=system_prompt,
                task_prompt=task_prompt,
                is_active=True,
            )
        )
        self.db.commit()
        return system_prompt, task_prompt

    def _upsert_prompt_version(
        self,
        project_id: str,
        step_name: str,
        task_prompt: str,
        system_prompt: str | None = None,
    ) -> PromptVersion:
        active = self.db.scalar(self._active_prompt_query(project_id, step_name))
        if not active:
            base_system, base_task = get_baseline_prompts(step_name)
            active = PromptVersion(
                project_id=project_id,
                step_name=step_name,
                system_prompt=base_system,
                task_prompt=base_task,
                is_active=True,
            )
            self.db.add(active)
            self.db.flush()
        active.is_active = False
        self.db.add(active)

        next_system = system_prompt if system_prompt is not None else active.system_prompt
        diff_patch = f"TASK:\n- {active.task_prompt}\n+ {task_prompt}"
        next_version = PromptVersion(
            project_id=project_id,
            step_name=step_name,
            system_prompt=next_system,
            task_prompt=task_prompt,
            parent_version_id=active.id,
            diff_patch=diff_patch,
            is_active=True,
        )
        self.db.add(next_version)
        self.db.flush()
        return next_version
