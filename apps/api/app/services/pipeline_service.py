from __future__ import annotations

import asyncio
import base64
import html
import io
import json
import math
import re
import tempfile
import subprocess
import textwrap
import zipfile
from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from consistency_engine import score_consistency
from provider_adapters import ProviderRegistry, ProviderRequest, ProviderResponse
from sqlalchemy import Select, select
from sqlalchemy.orm import Session
from workflow_engine import PIPELINE_STEPS, ProjectStatus, StepStatus
from workflow_engine.pipeline import next_step_name

from app.core.config import settings
from app.db.session import SessionLocal
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
REFERENCE_IMAGE_MAX_EDGE = 640
REFERENCE_IMAGE_JPEG_QUALITY = 78
REFERENCE_IMAGE_INLINE_BYTES = 250_000
STORYBOARD_REFERENCE_IMAGE_LIMIT = 3
STORYBOARD_IMAGE_FALLBACK_LIMIT = 3
CONSISTENCY_REFERENCE_CHARACTER_LIMIT = 3
CONSISTENCY_REFERENCE_SCENE_LIMIT = 2
CONSISTENCY_CURRENT_FRAME_LIMIT = 4
CONSISTENCY_NEIGHBOR_FRAME_LIMIT = 2
CONSISTENCY_BATCH_CONCURRENCY = 3
STORYBOARD_IMAGE_SEXUAL_RISK_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"乳罩|胸罩|bra|lingerie",
        r"内衣|内裤|underwear|pant(?:y|ies)",
        r"半透明|透视|see[- ]?through|sheer",
        r"别的什么都没穿|什么都没穿|一丝不挂|未着寸缕",
        r"裸体|裸露|赤裸|裸身|nude|nudity|topless|bottomless",
        r"性|性爱|性交|sex|sexual",
    )
]
CONSISTENCY_HARD_IDENTITY_PATTERNS = (
    "脸型",
    "面部特征",
    "身份",
    "发型",
    "年龄感",
    "看起来更年轻",
    "看起来更老",
    "不一致",
    "人物形象",
)
CONSISTENCY_SOFT_MISMATCH_PATTERNS = (
    "服装",
    "外套",
    "夹克",
    "毛衣",
    "背景",
    "光影",
    "氛围",
    "建筑风格",
    "网球场",
    "走廊",
    "书房",
    "办公室",
)
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


@lru_cache(maxsize=512)
def _cached_reference_image_data_url(path_str: str) -> str | None:
    path = Path(path_str)
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
        suffix = path.suffix.lower()
        if len(raw) <= REFERENCE_IMAGE_INLINE_BYTES:
            mime_type = "image/png" if suffix == ".png" else "image/jpeg"
            return f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}"

        from PIL import Image, ImageOps

        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image)
            if image.mode in {"RGBA", "LA"}:
                background = Image.new("RGB", image.size, (255, 255, 255))
                background.paste(image, mask=image.getchannel("A"))
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")

            resampling = getattr(Image, "Resampling", Image).LANCZOS
            image.thumbnail((REFERENCE_IMAGE_MAX_EDGE, REFERENCE_IMAGE_MAX_EDGE), resampling)
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=REFERENCE_IMAGE_JPEG_QUALITY, optimize=True)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"
    except Exception:
        try:
            raw = path.read_bytes()
        except Exception:
            return None
        mime_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        return f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}"

@lru_cache(maxsize=512)
def _cached_reference_image_variant_data_url(path_str: str, variant: str) -> str | None:
    if variant == "full":
        return _cached_reference_image_data_url(path_str)
    path = Path(path_str)
    if not path.exists():
        return None
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image)
            if image.mode in {"RGBA", "LA"}:
                background = Image.new("RGB", image.size, (255, 255, 255))
                background.paste(image, mask=image.getchannel("A"))
                image = background
            elif image.mode != "RGB":
                image = image.convert("RGB")
            if variant == "portrait":
                width, height = image.size
                crop_w = max(1, int(width * 0.62))
                crop_h = max(1, int(height * 0.74))
                left = max(0, (width - crop_w) // 2)
                top = max(0, int(height * 0.06))
                right = min(width, left + crop_w)
                bottom = min(height, top + crop_h)
                image = image.crop((left, top, right, bottom))
            resampling = getattr(Image, "Resampling", Image).LANCZOS
            image.thumbnail((REFERENCE_IMAGE_MAX_EDGE, REFERENCE_IMAGE_MAX_EDGE), resampling)
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=REFERENCE_IMAGE_JPEG_QUALITY, optimize=True)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"
    except Exception:
        return _cached_reference_image_data_url(path_str)


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
        step = self.db.scalar(select(PipelineStep).where(PipelineStep.project_id == project.id, PipelineStep.step_name == step_name))
        if not step:
            raise ValueError(f"step not found: {step_name}")

        eligible_chapters = [
            chapter
            for chapter in chapters
            if not self._should_skip_chapter_for_batch_step(chapter, step_name)
            and self._chapter_dependency_satisfied(project.id, chapter, step_name)
            and (force or self._chapter_step_status(chapter, step_name) != StepStatus.APPROVED.value)
        ]
        run_results = await self._run_step_for_chapter_batch(
            project,
            step_name,
            chapters,
            eligible_chapters,
            force=force,
            params=params,
            skipped_detail="当前章节该阶段已通过，已跳过。",
            rerun_detail="当前章节已运行完成。",
            fatal_skip_detail="检测到 provider 余额/鉴权级致命错误，批量运行已中止。",
        )
        current_step = self._sync_global_chapter_scoped_step(project, step)

        return {
            "project_id": project.id,
            "step_name": step_name,
            "total": len(chapters),
            "succeeded": run_results["succeeded"],
            "failed": run_results["failed"],
            "skipped": run_results["skipped"],
            "chapter_results": run_results["results"],
            "current_step": run_results["last_step"] or current_step,
        }

    async def run_step_for_failed_chapters(
        self,
        project: Project,
        step_name: str,
        *,
        force: bool = True,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if step_name not in CHAPTER_SCOPED_STEPS:
            raise ValueError("run-failed-chapters is only allowed on chapter-scoped steps")
        params = params or {}
        chapters = self._list_project_chapters(project.id)
        if not chapters:
            raise ValueError("no chapters available")
        step = self.db.scalar(select(PipelineStep).where(PipelineStep.project_id == project.id, PipelineStep.step_name == step_name))
        if not step:
            raise ValueError(f"step not found: {step_name}")

        eligible_chapters = [
            chapter
            for chapter in chapters
            if not self._should_skip_chapter_for_batch_step(chapter, step_name)
            and self._chapter_step_status(chapter, step_name) == StepStatus.FAILED.value
            and self._chapter_dependency_satisfied(project.id, chapter, step_name)
        ]
        run_results = await self._run_step_for_chapter_batch(
            project,
            step_name,
            chapters,
            eligible_chapters,
            force=force,
            params=params,
            skipped_detail_template="当前状态为 {status}，不是失败章节，已跳过。",
            rerun_detail="失败章节已重新运行。",
            fatal_skip_detail="检测到 provider 余额/鉴权级致命错误，失败章节批量重跑已中止。",
            failed_only=True,
        )
        current_step = self._sync_global_chapter_scoped_step(project, step)
        return {
            "project_id": project.id,
            "step_name": step_name,
            "total": len(chapters),
            "succeeded": run_results["succeeded"],
            "failed": run_results["failed"],
            "skipped": run_results["skipped"],
            "chapter_results": run_results["results"],
            "current_step": run_results["last_step"] or current_step,
        }

    async def _run_step_for_chapter_batch(
        self,
        project: Project,
        step_name: str,
        chapters: list[ChapterChunk],
        eligible_chapters: list[ChapterChunk],
        *,
        force: bool,
        params: dict[str, Any],
        rerun_detail: str,
        fatal_skip_detail: str,
        skipped_detail: str | None = None,
        skipped_detail_template: str | None = None,
        failed_only: bool = False,
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        succeeded = 0
        failed = 0
        skipped = 0
        last_step: PipelineStep | None = None
        concurrency = self._batch_step_concurrency(step_name)
        eligible_ids = {chapter.id for chapter in eligible_chapters}
        fatal_error = False

        for chapter in chapters:
            title = str((chapter.meta or {}).get("title") or f"章节 {chapter.chapter_index + 1}")
            if self._should_skip_chapter_for_batch_step(chapter, step_name):
                skipped += 1
                results.append(
                    {
                        "chapter_id": chapter.id,
                        "chapter_title": title,
                        "status": "SKIPPED",
                        "detail": "该章节属于前置/附录元内容，当前阶段已自动跳过。",
                    }
                )
                continue
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
            chapter_status = self._chapter_step_status(chapter, step_name)
            if failed_only and chapter_status != StepStatus.FAILED.value:
                skipped += 1
                results.append(
                    {
                        "chapter_id": chapter.id,
                        "chapter_title": title,
                        "status": "SKIPPED",
                        "detail": str(skipped_detail_template or "当前状态为 {status}，不是失败章节，已跳过。").format(
                            status=chapter_status
                        ),
                    }
                )
                continue
            if not failed_only and not force and chapter_status == StepStatus.APPROVED.value:
                skipped += 1
                results.append(
                    {
                        "chapter_id": chapter.id,
                        "chapter_title": title,
                        "status": "SKIPPED",
                        "detail": skipped_detail or "当前章节该阶段已通过，已跳过。",
                    }
                )
                continue
            if chapter.id not in eligible_ids:
                skipped += 1
                results.append(
                    {
                        "chapter_id": chapter.id,
                        "chapter_title": title,
                        "status": "SKIPPED",
                        "detail": skipped_detail or "当前章节该阶段已通过，已跳过。",
                    }
                )
                continue
            if fatal_error:
                skipped += 1
                results.append(
                    {
                        "chapter_id": chapter.id,
                        "chapter_title": title,
                        "status": "SKIPPED",
                        "detail": fatal_skip_detail,
                    }
                )
                continue

        pending_chapters = [chapter for chapter in chapters if chapter.id in eligible_ids]
        for index in range(0, len(pending_chapters), concurrency):
            if fatal_error:
                break
            window = pending_chapters[index : index + concurrency]
            task_results = await asyncio.gather(
                *[
                    self._run_chapter_step_in_isolated_session(
                        project.id,
                        step_name,
                        chapter.id,
                        force=force,
                        params=params,
                    )
                    for chapter in window
                ],
                return_exceptions=True,
            )
            result_map = {item["chapter_id"]: item for item in results}
            for chapter, task_result in zip(window, task_results):
                title = str((chapter.meta or {}).get("title") or f"章节 {chapter.chapter_index + 1}")
                if isinstance(task_result, Exception):
                    failed += 1
                    detail = str(task_result)
                    result_map[chapter.id] = {
                        "chapter_id": chapter.id,
                        "chapter_title": title,
                        "status": "FAILED",
                        "detail": detail,
                    }
                    if self._is_fatal_batch_error(task_result):
                        fatal_error = True
                else:
                    last_step = self._get_step(project.id, task_result["step_id"])
                    succeeded += 1
                    result_map[chapter.id] = {
                        "chapter_id": chapter.id,
                        "chapter_title": title,
                        "status": task_result["status"],
                        "detail": rerun_detail,
                    }
            results = [result_map[item.id] for item in chapters if item.id in result_map]

        if fatal_error:
            executed_ids = {item["chapter_id"] for item in results if item["status"] != "SKIPPED"}
            for chapter in chapters:
                if chapter.id in executed_ids or chapter.id not in eligible_ids:
                    continue
                title = str((chapter.meta or {}).get("title") or f"章节 {chapter.chapter_index + 1}")
                skipped += 1
                results.append(
                    {
                        "chapter_id": chapter.id,
                        "chapter_title": title,
                        "status": "SKIPPED",
                        "detail": fatal_skip_detail,
                    }
                )

        return {
            "results": results,
            "succeeded": succeeded,
            "failed": failed,
            "skipped": skipped,
            "last_step": last_step,
        }

    def _batch_step_concurrency(self, step_name: str) -> int:
        if step_name == "consistency_check":
            return CONSISTENCY_BATCH_CONCURRENCY
        return 1

    async def _run_chapter_step_in_isolated_session(
        self,
        project_id: str,
        step_name: str,
        chapter_id: str,
        *,
        force: bool,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        db = SessionLocal()
        try:
            svc = PipelineService(db)
            project = svc._get_project(project_id)
            step = await svc.run_specific_step(
                project,
                step_name,
                force=force,
                params={**params, "chapter_id": chapter_id},
            )
            return {"chapter_id": chapter_id, "step_id": step.id, "status": step.status}
        finally:
            db.close()

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
        succeeded = sum(
            1
            for item in results
            if item["status"] in {StepStatus.APPROVED.value, StepStatus.REVIEW_REQUIRED.value}
        )
        failed = sum(1 for item in results if item["status"] in {"FAILED", StepStatus.REWORK_REQUESTED.value})
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

    async def approve_review_required_consistency_chapters(
        self,
        project: Project,
        step_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        step = self._get_step(project.id, step_id)
        if step.step_name != "consistency_check":
            raise ValueError("approve-review-required-consistency-chapters is only allowed on consistency_check")
        chapters = self._list_project_chapters(project.id)
        approved_results: list[dict[str, Any]] = []
        approved_ids: list[str] = []
        for chapter in chapters:
            if self._chapter_step_status(chapter, step.step_name) != StepStatus.REVIEW_REQUIRED.value:
                continue
            title = str((chapter.meta or {}).get("title") or f"章节 {chapter.chapter_index + 1}")
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
            approved_ids.append(chapter.id)
            approved_results.append(
                {
                    "chapter_id": chapter.id,
                    "chapter_title": title,
                    "status": StepStatus.APPROVED.value,
                    "detail": "当前章节已批量通过。",
                }
            )
        if approved_ids:
            self._record_review(
                project.id,
                step.id,
                "batch",
                "approve_review_required_consistency",
                {
                    **payload,
                    "chapter_ids": approved_ids,
                    "chapter_count": len(approved_ids),
                },
                payload["created_by"],
            )
        synced = self._sync_global_chapter_scoped_step(project, step)
        return {
            "project_id": project.id,
            "step_name": step.step_name,
            "total": len(chapters),
            "succeeded": len(approved_ids),
            "failed": 0,
            "skipped": max(0, len(chapters) - len(approved_ids)),
            "chapter_results": approved_results,
            "current_step": synced,
        }

    async def approve_failed_step_for_all_chapters(self, project: Project, step_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        step = self._get_step(project.id, step_id)
        if step.step_name not in CHAPTER_SCOPED_STEPS:
            raise ValueError("approve-failed-chapters is only allowed on chapter-scoped steps")
        results: list[dict[str, Any]] = []
        for chapter in self._list_project_chapters(project.id):
            title = str((chapter.meta or {}).get("title") or f"章节 {chapter.chapter_index + 1}")
            chapter_status = self._chapter_step_status(chapter, step.step_name)
            if chapter_status != StepStatus.FAILED.value:
                results.append({"chapter_id": chapter.id, "chapter_title": title, "status": "SKIPPED", "detail": f"当前状态为 {chapter_status}，不是失败章节，已跳过。"})
                continue
            stage_output = deepcopy(self._chapter_stage_chain(chapter).get(step.step_name, {}))
            stage_output["manual_failure_override"] = True
            self._set_chapter_stage_state(
                chapter,
                step.step_name,
                status=StepStatus.APPROVED.value,
                output=self._build_chapter_stage_output(stage_output, payload.get("comment")),
                attempt=step.attempt,
                provider=step.model_provider,
                model=step.model_name,
            )
            self._record_review(project.id, step.id, "chapter", "approve_failed", {**payload, "chapter_id": chapter.id}, payload["created_by"])
            results.append({"chapter_id": chapter.id, "chapter_title": title, "status": StepStatus.APPROVED.value, "detail": "失败章节已人工强制通过。"})
        synced = self._sync_global_chapter_scoped_step(project, step)
        return self._batch_action_response(project.id, step.step_name, results, synced)

    async def rerun_pending_step_for_all_chapters(
        self,
        project: Project,
        step_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        step = self._get_step(project.id, step_id)
        if step.step_name not in CHAPTER_SCOPED_STEPS:
            raise ValueError("rerun-pending-chapters is only allowed on chapter-scoped steps")
        results: list[dict[str, Any]] = []
        last_step: PipelineStep | None = None

        for index, chapter in enumerate(self._list_project_chapters(project.id)):
            title = str((chapter.meta or {}).get("title") or f"章节 {chapter.chapter_index + 1}")
            if self._should_skip_chapter_for_batch_step(chapter, step.step_name):
                results.append(
                    {
                        "chapter_id": chapter.id,
                        "chapter_title": title,
                        "status": "SKIPPED",
                        "detail": "该章节不参与当前阶段，已自动跳过。",
                    }
                )
                continue
            if not self._chapter_dependency_satisfied(project.id, chapter, step.step_name):
                results.append(
                    {
                        "chapter_id": chapter.id,
                        "chapter_title": title,
                        "status": "SKIPPED",
                        "detail": "前置阶段尚未通过，已跳过。",
                    }
                )
                continue
            chapter_status = self._chapter_step_status(chapter, step.step_name)
            if chapter_status != StepStatus.PENDING.value:
                results.append(
                    {
                        "chapter_id": chapter.id,
                        "chapter_title": title,
                        "status": "SKIPPED",
                        "detail": f"当前状态为 {chapter_status}，不是待重新评分章节，已跳过。",
                    }
                )
                continue
            try:
                last_step = await self.run_specific_step(
                    project,
                    step.step_name,
                    force=True,
                    params={"chapter_id": chapter.id, **payload.get("params", {})},
                )
                current_chapter = self._get_chapter(project.id, chapter.id)
                final_status = self._chapter_step_status(current_chapter, step.step_name)
                results.append(
                    {
                        "chapter_id": chapter.id,
                        "chapter_title": title,
                        "status": final_status,
                        "detail": "已重新执行当前章节的分镜校核评分。",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                results.append(
                    {
                        "chapter_id": chapter.id,
                        "chapter_title": title,
                        "status": "FAILED",
                        "detail": str(exc),
                    }
                )
                if self._is_fatal_batch_error(exc):
                    for remaining in self._list_project_chapters(project.id)[index + 1 :]:
                        results.append(
                            {
                                "chapter_id": remaining.id,
                                "chapter_title": str((remaining.meta or {}).get("title") or f"章节 {remaining.chapter_index + 1}"),
                                "status": "SKIPPED",
                                "detail": "检测到 provider 余额/鉴权级致命错误，批量重新评分已中止。",
                            }
                        )
                    break

        synced = self._sync_global_chapter_scoped_step(project, step)
        return self._batch_action_response(project.id, step.step_name, results, last_step or synced)

    async def regenerate_rework_requested_consistency_chapters(
        self,
        project: Project,
        step_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        step = self._get_step(project.id, step_id)
        if step.step_name != "consistency_check":
            raise ValueError("rework-regenerate-rescore is only allowed on consistency_check")
        storyboard_step = self._get_storyboard_step(project.id)
        results: list[dict[str, Any]] = []
        last_step: PipelineStep | None = None
        chapters = self._list_project_chapters(project.id)

        for index, chapter in enumerate(chapters):
            title = str((chapter.meta or {}).get("title") or f"章节 {chapter.chapter_index + 1}")
            if not self._chapter_participates_in_step(chapter, "consistency_check"):
                results.append(
                    {
                        "chapter_id": chapter.id,
                        "chapter_title": title,
                        "status": "SKIPPED",
                        "detail": "该章节作为片头/片尾画面，不参与分镜校核。",
                    }
                )
                continue
            chapter_status = self._chapter_step_status(chapter, "consistency_check")
            if chapter_status != StepStatus.REWORK_REQUESTED.value:
                results.append(
                    {
                        "chapter_id": chapter.id,
                        "chapter_title": title,
                        "status": "SKIPPED",
                        "detail": f"当前状态为 {chapter_status}，不是待修正章节，已跳过。",
                    }
                )
                continue
            try:
                auto_revision_prompt = self._build_consistency_revision_prompt(chapter)
                target_shot_indexes = self._consistency_rework_target_shots(chapter)
                rerun_params = {
                    **payload.get("params", {}),
                    "chapter_id": chapter.id,
                    "auto_revision_prompt": auto_revision_prompt,
                    "target_shot_indexes": target_shot_indexes,
                }
                storyboard_result = await self._execute_step(project, storyboard_step, params=rerun_params)
                regenerated_chapter = self._get_chapter(project.id, chapter.id)
                regenerated_output = deepcopy(self._chapter_stage_chain(regenerated_chapter).get("storyboard_image") or {})
                regenerated_output["auto_regenerated_for_consistency"] = True
                self._set_chapter_stage_state(
                    regenerated_chapter,
                    "storyboard_image",
                    status=StepStatus.APPROVED.value,
                    output=self._build_chapter_stage_output(regenerated_output),
                    attempt=storyboard_result.attempt,
                    provider=storyboard_result.model_provider,
                    model=storyboard_result.model_name,
                )
                last_step = await self.run_specific_step(project, "consistency_check", force=True, params=rerun_params)
                current_chapter = self._get_chapter(project.id, chapter.id)
                final_status = self._chapter_step_status(current_chapter, "consistency_check")
                detail = "已根据低分镜头原因补充修正提示词，重生成分镜并重新完成分镜校核。"
                if final_status == StepStatus.REWORK_REQUESTED.value:
                    detail = "已自动补充修正提示词并重跑，但当前章节仍需继续返工。"
                self._record_review(
                    project.id,
                    step.id,
                    "chapter",
                    "consistency_rework_regenerate_rescore",
                    {
                        **payload,
                        "chapter_id": chapter.id,
                        "auto_revision_prompt": auto_revision_prompt,
                        "target_shot_indexes": target_shot_indexes,
                    },
                    payload["created_by"],
                )
                results.append(
                    {
                        "chapter_id": chapter.id,
                        "chapter_title": title,
                        "status": final_status,
                        "detail": detail,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                results.append(
                    {
                        "chapter_id": chapter.id,
                        "chapter_title": title,
                        "status": "FAILED",
                        "detail": str(exc),
                    }
                )
                if self._is_fatal_batch_error(exc):
                    for remaining in chapters[index + 1 :]:
                        remaining_title = str((remaining.meta or {}).get("title") or f"章节 {remaining.chapter_index + 1}")
                        results.append(
                            {
                                "chapter_id": remaining.id,
                                "chapter_title": remaining_title,
                                "status": "SKIPPED",
                                "detail": "检测到 provider 余额/鉴权级致命错误，自动修正流程已中止。",
                            }
                        )
                    break

        self.db.commit()
        synced = self._sync_global_chapter_scoped_step(project, step)
        return self._batch_action_response(project.id, step.step_name, results, last_step or synced)

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

    async def switch_model_rerun_failed_chapters(self, project: Project, step_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        step = self._get_step(project.id, step_id)
        if step.step_name not in CHAPTER_SCOPED_STEPS:
            raise ValueError("switch-model-rerun-failed-chapters is only allowed on chapter-scoped steps")
        step.model_provider = payload["provider"]
        step.model_name = payload["model_name"]
        self.db.add(step)
        self.db.commit()
        result = await self.run_step_for_failed_chapters(project, step.step_name, force=True, params=payload.get("params", {}))
        for item in result["chapter_results"]:
            if item["status"] != "SKIPPED":
                self._record_review(project.id, step.id, "chapter", "switch_model_rerun_failed", {**payload, "chapter_id": item["chapter_id"]}, payload["created_by"])
        self.db.commit()
        return result

    async def rerun_with_prompt_update(self, project: Project, step_name: str, payload: dict[str, Any]) -> PipelineStep:
        step = self.db.scalar(select(PipelineStep).where(PipelineStep.project_id == project.id, PipelineStep.step_name == step_name))
        if not step:
            raise ValueError(f"step not found: {step_name}")
        prompt_version = self._upsert_prompt_version(
            project_id=project.id,
            step_name=step_name,
            task_prompt=payload["task_prompt"],
            system_prompt=payload.get("system_prompt"),
        )
        self._record_review(
            project.id,
            step.id,
            payload.get("scope_type", "step"),
            payload.get("action_type", "agent_prompt_refine_rerun"),
            {**payload, "prompt_version_id": prompt_version.id},
            payload.get("created_by", "filmit-agent"),
        )
        self.db.commit()
        params = dict(payload.get("params") or {})
        if payload.get("chapter_id") and step_name in CHAPTER_SCOPED_STEPS:
            params["chapter_id"] = payload["chapter_id"]
        return await self.run_specific_step(project, step_name, force=True, params=params)

    async def rerun_all_chapters_with_prompt_update(self, project: Project, step_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        step = self.db.scalar(select(PipelineStep).where(PipelineStep.project_id == project.id, PipelineStep.step_name == step_name))
        if not step:
            raise ValueError(f"step not found: {step_name}")
        if step_name not in CHAPTER_SCOPED_STEPS:
            raise ValueError("rerun-all-chapters-with-prompt-update is only allowed on chapter-scoped steps")
        prompt_version = self._upsert_prompt_version(
            project_id=project.id,
            step_name=step_name,
            task_prompt=payload["task_prompt"],
            system_prompt=payload.get("system_prompt"),
        )
        self.db.commit()
        result = await self.run_step_for_all_chapters(project, step_name, force=True, params=payload.get("params", {}))
        for item in result["chapter_results"]:
            if item["status"] == "SKIPPED":
                continue
            self._record_review(
                project.id,
                step.id,
                "chapter",
                payload.get("action_type", "agent_prompt_refine_rerun"),
                {**payload, "chapter_id": item["chapter_id"], "prompt_version_id": prompt_version.id},
                payload.get("created_by", "filmit-agent"),
            )
        self.db.commit()
        return result

    async def rerun_failed_chapters_with_prompt_update(self, project: Project, step_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        step = self.db.scalar(select(PipelineStep).where(PipelineStep.project_id == project.id, PipelineStep.step_name == step_name))
        if not step:
            raise ValueError(f"step not found: {step_name}")
        if step_name not in CHAPTER_SCOPED_STEPS:
            raise ValueError("rerun-failed-chapters-with-prompt-update is only allowed on chapter-scoped steps")
        prompt_version = self._upsert_prompt_version(
            project_id=project.id,
            step_name=step_name,
            task_prompt=payload["task_prompt"],
            system_prompt=payload.get("system_prompt"),
        )
        self.db.commit()
        result = await self.run_step_for_failed_chapters(project, step_name, force=True, params=payload.get("params", {}))
        for item in result["chapter_results"]:
            if item["status"] == "SKIPPED":
                continue
            self._record_review(
                project.id,
                step.id,
                "chapter",
                payload.get("action_type", "agent_prompt_refine_rerun_failed"),
                {**payload, "chapter_id": item["chapter_id"], "prompt_version_id": prompt_version.id},
                payload.get("created_by", "filmit-agent"),
            )
        self.db.commit()
        return result

    def get_active_prompt_snapshot(self, project_id: str, step_name: str) -> dict[str, Any]:
        active = self.db.scalar(self._active_prompt_query(project_id, step_name))
        if not active:
            system_prompt, task_prompt = self._get_active_prompts(project_id, step_name)
            active = self.db.scalar(self._active_prompt_query(project_id, step_name))
            return {
                "prompt_version_id": getattr(active, "id", None),
                "system_prompt": system_prompt,
                "task_prompt": task_prompt,
                "created_at": getattr(active, "created_at", None),
            }
        return {
            "prompt_version_id": active.id,
            "system_prompt": active.system_prompt,
            "task_prompt": active.task_prompt,
            "created_at": active.created_at,
        }

    def estimate_step_action_cost(
        self,
        project: Project,
        step_name: str,
        *,
        scope_mode: str = "single",
        chapter_id: str | None = None,
        provider: str | None = None,
        model_name: str | None = None,
    ) -> dict[str, Any]:
        step = self.db.scalar(select(PipelineStep).where(PipelineStep.project_id == project.id, PipelineStep.step_name == step_name))
        if not step:
            raise ValueError(f"step not found: {step_name}")
        bound_provider = provider or step.model_provider or self._resolve_binding(project, step_name, self.step_def_map[step_name].step_type)[0]
        bound_model = model_name or step.model_name or self._resolve_binding(project, step_name, self.step_def_map[step_name].step_type)[1]
        if step_name in LOCAL_ONLY_STEPS:
            return {
                "estimated_cost": 0.0,
                "unit_cost": 0.0,
                "unit_count": 1 if scope_mode == "single" else 0,
                "currency": "USD",
                "provider": bound_provider,
                "model_name": bound_model,
                "source": "local_fixed_step",
                "summary": "当前动作对应本地固定步骤，预计不产生模型费用。",
            }

        unit_count = self._estimate_action_unit_count(project.id, step_name, scope_mode=scope_mode, chapter_id=chapter_id)
        model_runs = list(
            self.db.scalars(
                select(ModelRun)
                .where(ModelRun.project_id == project.id, ModelRun.step_name == step_name)
                .order_by(ModelRun.created_at.desc())
                .limit(24)
            ).all()
        )
        matched = next(
            (
                item
                for item in model_runs
                if item.provider == bound_provider
                and item.model_name == bound_model
                and self._cost_run_matches_scope(item, chapter_id=chapter_id, scope_mode=scope_mode)
                and float(item.estimated_cost or 0.0) > 0
            ),
            None,
        )
        fallback = next(
            (
                item
                for item in model_runs
                if item.provider == bound_provider
                and item.model_name == bound_model
                and float(item.estimated_cost or 0.0) > 0
            ),
            None,
        ) or next((item for item in model_runs if float(item.estimated_cost or 0.0) > 0), None)
        reference_run = matched or fallback
        if not reference_run:
            return {
                "estimated_cost": None,
                "unit_cost": None,
                "unit_count": unit_count,
                "currency": "USD",
                "provider": bound_provider,
                "model_name": bound_model,
                "source": "unavailable",
                "summary": f"暂时没有足够的历史运行成本来估算 {step_name} 的本次动作费用。",
            }

        unit_cost = round(float(reference_run.estimated_cost or 0.0), 6)
        estimated_cost = round(unit_cost * max(unit_count, 1), 6)
        scope_label = {
            "single": "单次重跑",
            "all_chapters": "全章节批量",
            "failed_chapters": "失败章节批量",
        }.get(scope_mode, scope_mode)
        return {
            "estimated_cost": estimated_cost,
            "unit_cost": unit_cost,
            "unit_count": unit_count,
            "currency": "USD",
            "provider": bound_provider,
            "model_name": bound_model,
            "source": "recent_model_run",
            "reference_run_id": reference_run.id,
            "summary": f"基于最近一次 {step_name} 的历史运行估算，{scope_label} 预计费用约 ${estimated_cost:.4f}。",
        }

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

        step_output = deepcopy(step.output_ref or {})
        step_output["selected_storyboard_version_id"] = version.id
        step_output["selected_storyboard_version_chapter_id"] = self._storyboard_version_chapter_id(version)
        step_output["selection_source"] = "history_version"
        if payload.get("comment"):
            step_output["selection_comment"] = payload["comment"]
        step.output_ref = step_output
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
            if not self._is_playable_video(output_path):
                raise ValueError(f"final export is not playable: {output_path}")
            export_job.status = "COMPLETED"
            export_job.output_key = str(output_path)
            export_job.finished_at = datetime.now(timezone.utc)
            project.output_path = str(output_path)
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

    async def generate_final_cut(self, project: Project, *, force: bool = True) -> ExportJob:
        self.ensure_pipeline_steps(project)
        steps = {item.step_name: item for item in self._list_steps(project.id)}
        final_step = steps.get("stitch_subtitle_tts")
        if final_step is None:
            raise ValueError("final cut step not found")

        if final_step.status != StepStatus.APPROVED.value or force:
            rerun_step = await self._execute_step(project, final_step, params={"force_final_cut": force})
            if rerun_step.status == StepStatus.REVIEW_REQUIRED.value:
                await self.approve_step(
                    project,
                    rerun_step.id,
                    {
                        "scope_type": "step",
                        "created_by": "system-final-cut",
                        "comment": "一键生成成片时自动通过成片合成方案。",
                    },
                )
            elif rerun_step.status != StepStatus.APPROVED.value:
                raise ValueError(f"final cut preparation failed in status {rerun_step.status}")

        refreshed = self._get_project(project.id)
        return await self.render_final(refreshed)

    def _project_ready_for_final_render(self, project_id: str, steps: list[PipelineStep]) -> bool:
        chapter_chunking_approved = any(
            step.step_name == "chapter_chunking" and step.status == StepStatus.APPROVED.value
            for step in steps
        )
        for step in steps:
            if step.step_name in CHAPTER_SCOPED_STEPS:
                continue
            if step.step_name == "ingest_parse" and chapter_chunking_approved:
                continue
            if step.status != StepStatus.APPROVED.value:
                return False
        chapters = self._list_project_chapters(project_id)
        if not chapters:
            return False
        approved_segments = 0
        for chapter in chapters:
            if self._chapter_step_status(chapter, "segment_video") == StepStatus.APPROVED.value:
                approved_segments += 1
        return approved_segments > 0

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
        else:
            step.model_provider, step.model_name = self._resolve_binding(
                project,
                step.step_name,
                self.step_def_map[step.step_name].step_type,
            )
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
            execution_provider = provider
            execution_model = model
            consistency_report = None
            consistency_skip_approved = False
            if step.step_name in LOCAL_ONLY_STEPS:
                response = self._invoke_local_step(step, step_input)
                estimated_cost = 0.0
            elif step.step_name == "consistency_check" and chapter is not None and not self._chapter_participates_in_step(chapter, step.step_name):
                consistency_skip_approved = True
                details = {
                    "scoring_mode": "meta_chapter_skip",
                    "summary": "前置内容/后记作为片头片尾画面，不参与主剧情分镜校核。",
                    "excluded_from_consistency": True,
                }
                consistency_report = score_consistency({"frames": [{"shot_index": 1}]}, threshold=settings.consistency_threshold)
                consistency_report = type(consistency_report)(
                    score=100,
                    dimensions={
                        "chapter_internal_character": 100,
                        "chapter_internal_scene": 100,
                        "reference_adherence": 100,
                        "cross_chapter_style": 100,
                    },
                    should_rework=False,
                    details=details,
                )
                response = ProviderResponse(
                    output={"summary": details["summary"], "scoring_mode": "meta_chapter_skip"},
                    usage={},
                    raw={"meta_chapter_skip": True},
                )
                execution_provider = "local"
                execution_model = "meta-chapter-skip"
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
                elif step.step_name == "stitch_subtitle_tts":
                    response, estimated_cost = await self._invoke_stitch_subtitle_tts_step(
                        project,
                        step,
                        adapter,
                        provider,
                        model,
                        system_prompt,
                        task_prompt,
                        style_directive,
                        params,
                    )
                elif step.step_name == "consistency_check":
                    consistency_context = self._build_storyboard_consistency_context(project, chapter)
                    consistency_run = await self._score_storyboard_consistency_with_model(
                        project,
                        step,
                        consistency_context,
                        threshold=settings.consistency_threshold,
                    )
                    response = consistency_run["response"]
                    estimated_cost = float(consistency_run["estimated_cost"])
                    execution_provider = str(consistency_run["provider"] or provider)
                    execution_model = str(consistency_run["model"] or model)
                    consistency_report = consistency_run["report"]
                else:
                    req = ProviderRequest(
                        step=self.step_def_map[step.step_name].step_type,
                        model=model,
                        input=step_input,
                        prompt=f"{system_prompt}\n{task_prompt}\n{style_directive}",
                        params=params,
                    )
                    response = await adapter.invoke(req)
                    estimated_cost = await adapter.estimate_cost(req, response.usage)

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
                provider=execution_provider,
                model=execution_model,
                usage=response.usage,
                estimated_cost=estimated_cost,
                execution_mode="local" if step.step_name in LOCAL_ONLY_STEPS else "provider",
            )
            output = self._materialize_step_output(project, step, output, chapter)
            if step.step_name == "stitch_subtitle_tts":
                output["final_cut"] = self._build_final_cut_summary(project, output)
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
                if consistency_report is None:
                    raise ValueError("consistency_check did not produce a consistency report")
                consistency = consistency_report
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
                if consistency_skip_approved:
                    step.status = StepStatus.APPROVED.value
                else:
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
                    provider=execution_provider,
                    model=execution_model,
                )

            self.db.add(step)
            output_json_path = self._persist_step_output_json(project, step, output)
            self.db.add(
                ModelRun(
                    project_id=project.id,
                    step_id=step.id,
                    step_name=step.step_name,
                    provider=execution_provider,
                    model_name=execution_model,
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
                storage_key = audio.get("storage_key")
                if isinstance(storage_key, str) and storage_key:
                    target_duration = 0.0
                    try:
                        target_duration = float(artifact.get("target_duration_sec") or 0.0)
                    except (TypeError, ValueError):
                        target_duration = 0.0
                    audio_path = Path(storage_key)
                    spoken_duration = self._probe_media_duration(audio_path)
                    if spoken_duration:
                        artifact["spoken_audio_duration_sec"] = round(spoken_duration, 3)
                        if isinstance(artifact.get("subtitle_entries"), list):
                            artifact["subtitle_entries"] = self._retime_subtitle_entries_to_total_duration(
                                artifact["subtitle_entries"],
                                spoken_duration,
                            )
                    fitted_duration = self._fit_audio_file_duration(
                        audio_path,
                        target_duration,
                        mime_type=str(artifact.get("mime_type") or "audio/mpeg"),
                    ) if target_duration > 0 else spoken_duration
                    if fitted_duration:
                        artifact["audio_duration_sec"] = round(fitted_duration, 3)
            subtitle = self._materialize_subtitle_artifact(project.id, step, artifact)
            if subtitle:
                artifact.update(subtitle)
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

    def _materialize_subtitle_artifact(
        self,
        project_id: str,
        step: PipelineStep,
        artifact: dict[str, Any],
    ) -> dict[str, Any] | None:
        entries = artifact.get("subtitle_entries")
        if not isinstance(entries, list) or not entries:
            return None
        file_path = self._generated_project_dir(project_id, step.step_name) / f"subtitles-attempt-{step.attempt}.srt"
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(self._subtitle_entries_to_srt(entries), encoding="utf-8")
        local_url = self._to_local_file_url(file_path)
        return {
            "subtitle_url": local_url,
            "subtitle_export_url": local_url,
            "subtitle_storage_key": str(file_path),
            "subtitle_count": len(entries),
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
        project = self._get_project(chapter.project_id)
        story_bible = normalize_style_profile(project.style_profile).get("story_bible", {})
        normalized: list[dict[str, Any]] = []
        for index, shot in enumerate(shots):
            if not isinstance(shot, dict):
                continue
            normalized.append(
                self._enrich_shot_payload(
                    chapter,
                    {
                        "shot_index": int(shot.get("shot_index") or index + 1),
                        "duration_sec": float(shot.get("duration_sec") or 0),
                        "frame_type": str(shot.get("frame_type") or "镜头"),
                        "visual": str(shot.get("visual") or ""),
                        "action": str(shot.get("action") or ""),
                        "dialogue": str(shot.get("dialogue") or ""),
                        "characters": list(shot.get("characters") or []) if isinstance(shot.get("characters"), list) else [],
                        "scene": str(shot.get("scene") or ""),
                        "scene_hint": str(shot.get("scene_hint") or ""),
                        "continuity_anchor": str(shot.get("continuity_anchor") or ""),
                    },
                    story_bible=story_bible,
                )
            )
        return normalized

    def _enrich_shot_payload(
        self,
        chapter: ChapterChunk,
        payload: dict[str, Any],
        *,
        story_bible: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        enriched = deepcopy(payload)
        if not isinstance(story_bible, dict):
            project = self._get_project(chapter.project_id)
            story_bible = normalize_style_profile(project.style_profile).get("story_bible", {})
        matching_text = self._story_bible_matching_text(enriched)
        if not (enriched.get("characters") or []):
            enriched["characters"] = self._extract_shot_entities(
                story_bible.get("characters") if isinstance(story_bible, dict) else [],
                matching_text,
                chapter=chapter,
                limit=3,
            )
        scene_value = str(enriched.get("scene") or "").strip()
        scene_hint = str(enriched.get("scene_hint") or "").strip()
        if not scene_value and not scene_hint:
            scene_candidates = self._extract_shot_entities(
                story_bible.get("scenes") if isinstance(story_bible, dict) else [],
                matching_text,
                chapter=chapter,
                limit=2,
            )
            if scene_candidates:
                enriched["scene"] = scene_candidates[0]
                enriched["scene_hint"] = " / ".join(scene_candidates)
        if not str(enriched.get("continuity_anchor") or "").strip():
            enriched["continuity_anchor"] = "保持同一人物外貌、服装和同一场景光线连续一致。"
        return enriched

    def _enrich_storyboard_gallery(self, chapter: ChapterChunk, gallery: dict[str, Any]) -> dict[str, Any]:
        frames = gallery.get("frames")
        if not isinstance(frames, list):
            return gallery
        shot_map = {int(shot.get("shot_index") or 0): shot for shot in self._chapter_shots(chapter) if isinstance(shot, dict)}
        if not shot_map:
            return gallery
        project = self._get_project(chapter.project_id)
        story_bible = normalize_style_profile(project.style_profile).get("story_bible", {})
        enriched_frames: list[dict[str, Any]] = []
        for index, frame in enumerate(frames):
            if not isinstance(frame, dict):
                continue
            shot_index = int(frame.get("shot_index") or index + 1)
            source_shot = shot_map.get(shot_index, {})
            merged = deepcopy(frame)
            for key in ("frame_type", "duration_sec", "visual", "action", "dialogue", "continuity_anchor"):
                if not merged.get(key) and source_shot.get(key):
                    merged[key] = deepcopy(source_shot[key])
            if not (merged.get("characters") or []):
                merged["characters"] = list(source_shot.get("characters") or [])
            if not str(merged.get("scene") or "").strip():
                merged["scene"] = str(source_shot.get("scene") or "")
            if not str(merged.get("scene_hint") or "").strip():
                merged["scene_hint"] = str(source_shot.get("scene_hint") or "")
            enriched_frames.append(self._enrich_shot_payload(chapter, merged, story_bible=story_bible))
        result = deepcopy(gallery)
        result["frames"] = enriched_frames
        result["frame_count"] = len(enriched_frames)
        return result

    def _normalize_storyboard_frames(
        self,
        project: Project,
        chapter: ChapterChunk | None,
        step: PipelineStep,
        artifact: dict[str, Any],
    ) -> list[dict[str, Any]]:
        frames_value = artifact.get("frames")
        normalized: list[dict[str, Any]] = []
        shot_map = {int(shot.get("shot_index") or 0): shot for shot in self._chapter_shots(chapter) if isinstance(shot, dict)} if chapter else {}
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
                shot_index = int(frame.get("shot_index") or index + 1)
                source_shot = shot_map.get(shot_index, {})
                normalized_frame = {
                    "shot_index": shot_index,
                    "title": str(frame.get("title") or f"镜头 {index + 1:02d}"),
                    "frame_type": str(frame.get("frame_type") or source_shot.get("frame_type") or "镜头"),
                    "duration_sec": float(frame.get("duration_sec") or source_shot.get("duration_sec") or 0),
                    "visual": str(frame.get("visual") or frame.get("summary") or source_shot.get("visual") or ""),
                    "action": str(frame.get("action") or source_shot.get("action") or ""),
                    "dialogue": str(frame.get("dialogue") or source_shot.get("dialogue") or ""),
                    "characters": list(frame.get("characters") or source_shot.get("characters") or []) if isinstance(frame.get("characters") or source_shot.get("characters") or [], list) else [],
                    "scene": str(frame.get("scene") or source_shot.get("scene") or ""),
                    "scene_hint": str(frame.get("scene_hint") or source_shot.get("scene_hint") or ""),
                    "continuity_anchor": str(frame.get("continuity_anchor") or source_shot.get("continuity_anchor") or ""),
                    "summary": str(frame.get("summary") or frame.get("visual") or source_shot.get("visual") or "")[:160],
                    "thumbnail_url": local_url,
                    "image_url": local_url,
                    "export_url": local_url,
                    "storage_key": str(file_path),
                    "prompt": frame.get("prompt"),
                    "provider": frame.get("provider"),
                    "model": frame.get("model"),
                    "artifact_id": frame.get("artifact_id"),
                }
                if chapter is not None:
                    normalized_frame = self._enrich_shot_payload(chapter, normalized_frame)
                normalized.append(normalized_frame)
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
            stage = {}
        output = deepcopy(stage.get("output") or {})
        gallery = output.get("storyboard_gallery")
        if isinstance(gallery, dict):
            return self._enrich_storyboard_gallery(chapter, gallery)
        artifact = deepcopy(output.get("artifact") or {})
        derived = self._gallery_payload_from_artifact(artifact)
        if derived.get("frame_count"):
            return self._enrich_storyboard_gallery(chapter, derived)

        storyboard_step = self._get_storyboard_step(chapter.project_id)
        active_version = self._get_active_storyboard_version(storyboard_step.id, chapter_id=chapter.id)
        if active_version and isinstance(active_version.output_snapshot, dict):
            version_output = deepcopy(active_version.output_snapshot)
            version_gallery = version_output.get("storyboard_gallery")
            if isinstance(version_gallery, dict) and version_gallery.get("frame_count"):
                return self._enrich_storyboard_gallery(chapter, version_gallery)
            version_artifact = deepcopy(version_output.get("artifact") or {})
            version_payload = self._gallery_payload_from_artifact(version_artifact)
            if version_payload.get("frame_count"):
                return self._enrich_storyboard_gallery(chapter, version_payload)
        return {}

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

    def _probe_media_duration(self, path: Path) -> float | None:
        if not path.exists():
            return None
        cmd = [self._ffmpeg_executable(), "-hide_banner", "-i", str(path)]
        completed = subprocess.run(cmd, capture_output=True, text=True)
        output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        match = re.search(r"Duration:\s*(\d{2}):(\d{2}):(\d{2}(?:\.\d+)?)", output)
        if not match:
            return None
        hours = int(match.group(1))
        minutes = int(match.group(2))
        seconds = float(match.group(3))
        duration = hours * 3600 + minutes * 60 + seconds
        return duration if duration > 0 else None

    def _artifact_audio_bytes(self, artifact: dict[str, Any]) -> tuple[bytes | None, str]:
        encoded = artifact.get("audio_base64")
        if not isinstance(encoded, str) or not encoded:
            return None, ".mp3"
        try:
            content = base64.b64decode(encoded)
        except Exception:
            return None, ".mp3"
        mime_type = str(artifact.get("mime_type") or "audio/mpeg")
        return content, self._suffix_for_mime_type(mime_type)

    def _probe_audio_duration_from_artifact(self, artifact: dict[str, Any]) -> float | None:
        content, suffix = self._artifact_audio_bytes(artifact)
        if not content:
            return None
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
            temp_path = Path(handle.name)
            handle.write(content)
        try:
            return self._probe_media_duration(temp_path)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass

    def _fit_audio_file_duration(self, path: Path, target_duration_sec: float, *, mime_type: str = "audio/mpeg") -> float | None:
        if not path.exists() or target_duration_sec <= 0:
            return self._probe_media_duration(path)
        current_duration = self._probe_media_duration(path)
        if current_duration is None:
            return None
        if abs(current_duration - target_duration_sec) <= 1.0:
            return current_duration
        temp_output = path.with_name(f"{path.stem}.fitted{path.suffix}")
        codec_args: list[str] = []
        suffix = self._suffix_for_mime_type(mime_type)
        if suffix == ".mp3":
            codec_args = ["-c:a", "libmp3lame"]
        elif suffix == ".wav":
            codec_args = ["-c:a", "pcm_s16le"]
        else:
            codec_args = ["-c:a", "aac"]
        cmd = [
            self._ffmpeg_executable(),
            "-y",
            "-i",
            str(path),
            "-af",
            "apad",
            "-t",
            f"{target_duration_sec:.3f}",
            *codec_args,
            str(temp_output),
        ]
        completed = subprocess.run(cmd, capture_output=True, text=True)
        if completed.returncode != 0:
            raise ValueError(f"ffmpeg audio fit failed: {completed.stderr.strip()}")
        temp_output.replace(path)
        return self._probe_media_duration(path)

    def _retime_subtitle_entries_to_total_duration(self, entries: list[dict[str, Any]], total_duration_sec: float) -> list[dict[str, Any]]:
        if total_duration_sec <= 0 or not entries:
            return entries
        normalized: list[dict[str, Any]] = []
        weights: list[int] = []
        for entry in entries:
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            normalized.append(deepcopy(entry))
            weights.append(max(len(text), 1))
        if not normalized:
            return entries
        total_weight = max(sum(weights), 1)
        cursor = 0.0
        for index, entry in enumerate(normalized):
            remaining = max(total_duration_sec - cursor, 0.2)
            if index == len(normalized) - 1:
                end_sec = total_duration_sec
            else:
                ratio = weights[index] / total_weight
                end_sec = min(total_duration_sec, cursor + max(0.8, total_duration_sec * ratio))
                end_sec = max(end_sec, cursor + 0.45)
            entry["start_sec"] = round(cursor, 3)
            entry["end_sec"] = round(min(end_sec, cursor + remaining), 3)
            cursor = float(entry["end_sec"])
        normalized[-1]["end_sec"] = round(total_duration_sec, 3)
        return normalized

    def _is_playable_video(self, path: Path) -> bool:
        if not path.exists() or path.suffix.lower() != ".mp4":
            return False
        duration = self._probe_media_duration(path)
        return bool(duration and duration > 0)

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

    def _get_step_by_name(self, project_id: str, step_name: str) -> PipelineStep | None:
        return self.db.scalar(
            select(PipelineStep).where(PipelineStep.project_id == project_id, PipelineStep.step_name == step_name).limit(1)
        )

    def _render_final_video(self, project: Project, export_id: str) -> Path:
        export_dir = project_category_dir(project.id, project.name, "exports")
        output_path = export_dir / f"final-{export_id}.mp4"

        segment_paths = self._collect_chapter_video_paths(project.id)
        if segment_paths:
            stitched_video = export_dir / f"final-{export_id}.segments.mp4"
            self._concat_video_segments(segment_paths, stitched_video)
            stitch_step = self._get_step_by_name(project.id, "stitch_subtitle_tts")
            artifact = deepcopy(stitch_step.output_ref.get("artifact") if stitch_step and isinstance(stitch_step.output_ref, dict) else {})
            segment_manifest = artifact.get("segment_manifest")
            duration_limit_sec = 0.0
            if isinstance(segment_manifest, list):
                for item in segment_manifest:
                    if not isinstance(item, dict):
                        continue
                    try:
                        duration_limit_sec += float(item.get("duration_sec") or 0.0)
                    except (TypeError, ValueError):
                        continue
            if duration_limit_sec <= 0:
                duration_limit_sec = float(project.target_duration_sec or 0.0)
            narration_path = None
            candidate_audio = artifact.get("storage_key")
            if isinstance(candidate_audio, str) and candidate_audio and Path(candidate_audio).exists():
                narration_path = Path(candidate_audio)
            subtitle_path = None
            candidate_subtitle = artifact.get("subtitle_storage_key")
            if isinstance(candidate_subtitle, str) and candidate_subtitle and Path(candidate_subtitle).exists():
                subtitle_path = Path(candidate_subtitle)
            if narration_path or subtitle_path:
                # Keep subtitles as sidecar SRT for long-form exports; embedding subtitle streams
                # caused unstable final mux times on multi-chapter renders.
                self._mux_final_cut_assets(
                    stitched_video,
                    output_path,
                    narration_path=narration_path,
                    subtitle_path=None,
                    duration_limit_sec=duration_limit_sec,
                )
                try:
                    stitched_video.unlink(missing_ok=True)
                except Exception:
                    pass
                return output_path
            stitched_video.replace(output_path)
            return output_path

        storyboard_paths = self._collect_storyboard_paths(project.id)
        if storyboard_paths:
            self._render_storyboard_slideshow(project, storyboard_paths, output_path)
            return output_path

        raise ValueError("no chapter video segments or storyboard images available for final export")

    def _mux_final_cut_assets(
        self,
        stitched_video: Path,
        output_path: Path,
        *,
        narration_path: Path | None = None,
        subtitle_path: Path | None = None,
        duration_limit_sec: float | None = None,
    ) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        av_output_path = output_path if subtitle_path is None else output_path.with_name(f"{output_path.stem}.av.mp4")
        av_temp_path = av_output_path.with_name(f"{av_output_path.stem}.tmp{av_output_path.suffix}")
        av_cmd = [self._ffmpeg_executable(), "-y", "-i", str(stitched_video)]
        narration_index: int | None = None
        if narration_path is not None:
            av_cmd.extend(["-i", str(narration_path)])
            narration_index = 1

        av_cmd.extend(["-map", "0:v:0"])
        if narration_index is not None:
            av_cmd.extend(["-map", f"{narration_index}:a:0", "-c:a", "aac", "-af", "apad", "-shortest"])
        else:
            av_cmd.extend(["-map", "0:a?", "-c:a", "copy"])
        if duration_limit_sec and duration_limit_sec > 0:
            av_cmd.extend(["-t", f"{duration_limit_sec:.3f}"])
        av_cmd.extend(["-c:v", "copy", "-movflags", "+faststart", str(av_temp_path)])
        completed = subprocess.run(av_cmd, capture_output=True, text=True)
        if completed.returncode != 0:
            raise ValueError(f"ffmpeg final audio mux failed: {completed.stderr.strip()}")
        av_temp_path.replace(av_output_path)

        if subtitle_path is None:
            return

        subtitle_temp_path = output_path.with_name(f"{output_path.stem}.tmp{output_path.suffix}")
        subtitle_cmd = [
            self._ffmpeg_executable(),
            "-y",
            "-i",
            str(av_output_path),
            "-i",
            str(subtitle_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-map",
            "1:0",
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-c:s",
            "mov_text",
            str(subtitle_temp_path),
        ]
        completed = subprocess.run(subtitle_cmd, capture_output=True, text=True)
        if completed.returncode != 0:
            raise ValueError(f"ffmpeg subtitle mux failed: {completed.stderr.strip()}")
        subtitle_temp_path.replace(output_path)
        try:
            av_output_path.unlink(missing_ok=True)
        except Exception:
            pass

    def _build_final_cut_segment_manifest(self, project: Project) -> list[dict[str, Any]]:
        manifest: list[dict[str, Any]] = []
        for chapter in self._list_project_chapters(project.id):
            stages = self._chapter_stages(chapter)
            segment = stages.get("segment_video")
            if not isinstance(segment, dict):
                continue
            output = deepcopy(segment.get("output") or {})
            artifact = deepcopy(output.get("artifact") or {})
            storage_key = artifact.get("storage_key")
            if not isinstance(storage_key, str) or not storage_key or not Path(storage_key).exists():
                continue
            title = str((chapter.meta or {}).get("title") or f"章节 {chapter.chapter_index + 1}")
            summary = str((chapter.meta or {}).get("summary") or chapter.content[:180]).strip()
            actual_duration = self._probe_media_duration(Path(storage_key))
            script_stage = stages.get("story_scripting")
            if isinstance(script_stage, dict):
                script_artifact = deepcopy((script_stage.get("output") or {}).get("artifact") or {})
                beats = script_artifact.get("beats")
                if isinstance(beats, list):
                    beat_summaries = [
                        str(as_record.get("summary") or "").strip()
                        for as_record in [item if isinstance(item, dict) else {} for item in beats[:2]]
                        if str(as_record.get("summary") or "").strip()
                    ]
                    if beat_summaries:
                        summary = " ".join(beat_summaries)
            manifest.append(
                {
                    "chapter_id": chapter.id,
                    "chapter_index": chapter.chapter_index,
                    "chunk_index": chapter.chunk_index,
                    "title": title,
                    "summary": summary,
                    "duration_sec": round(actual_duration or self._chapter_segment_duration(project, chapter), 3),
                    "storage_key": storage_key,
                    "preview_url": artifact.get("preview_url"),
                    "chapter_excerpt": self._compact_final_cut_text(self._chapter_body_text(chapter), limit=220),
                }
            )
        return manifest

    def _build_final_cut_narration_plan(
        self,
        project: Project,
        *,
        manifest: list[dict[str, Any]] | None = None,
        voice: str | None = None,
        segment_lines: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        items = manifest if manifest is not None else self._build_final_cut_segment_manifest(project)
        if not items:
            return {"chapter_count": 0, "segment_count": 0, "voice": voice or "alloy", "narration_text": "", "subtitle_entries": []}

        line_map = {
            str(item.get("chapter_id") or ""): str(item.get("narration") or "").strip()
            for item in (segment_lines or [])
            if isinstance(item, dict) and str(item.get("chapter_id") or "").strip()
        }
        blocks: list[str] = []
        entries: list[dict[str, Any]] = []
        cursor = 0.0
        for item in items:
            title = str(item.get("title") or "章节")
            chapter_id = str(item.get("chapter_id") or "").strip()
            chosen = line_map.get(chapter_id, "")
            summary = self._compact_final_cut_text(str(item.get("summary") or ""), limit=84)
            block_source = chosen or (f"{title}。{summary}" if summary else title)
            block = self._compact_final_cut_text(block_source, limit=120 if chosen else 96)
            if not block:
                continue
            blocks.append(block)
            duration = max(float(item.get("duration_sec") or 0), 2.0)
            parts = self._split_final_cut_sentences(block)
            total_chars = max(sum(len(part) for part in parts), 1)
            local_cursor = cursor
            for index, part in enumerate(parts):
                remaining = max(cursor + duration - local_cursor, 0.6)
                if index == len(parts) - 1:
                    end_sec = cursor + duration
                else:
                    ratio = max(len(part), 1) / total_chars
                    end_sec = min(cursor + duration, local_cursor + max(1.2, duration * ratio))
                    end_sec = max(end_sec, local_cursor + 0.8)
                entries.append(
                    {
                        "index": len(entries) + 1,
                        "chapter_title": title,
                        "start_sec": round(local_cursor, 3),
                        "end_sec": round(min(end_sec, local_cursor + remaining), 3),
                        "text": part,
                    }
                )
                local_cursor = end_sec
            cursor += duration

        return {
            "chapter_count": len({str(item.get('chapter_id') or '') for item in items}),
            "segment_count": len(items),
            "voice": voice or "alloy",
            "narration_text": "\n".join(blocks),
            "subtitle_entries": entries,
        }

    def _final_cut_writer_binding(self, project: Project) -> tuple[str, str]:
        try:
            provider, model = self.registry.suggest_model("script")
            adapter = self.registry.resolve(provider)
            if adapter.supports("script", model):
                return provider, model
        except Exception:
            pass
        story_step = self._get_step_by_name(project.id, "story_scripting")
        provider = str(story_step.model_provider or "").strip() if story_step else ""
        model = str(story_step.model_name or "").strip() if story_step else ""
        if provider and model:
            try:
                adapter = self.registry.resolve(provider)
                if adapter.supports("script", model):
                    return provider, model
            except Exception:
                pass
        return self.registry.suggest_model("script")

    def _heuristic_narration_lines_for_manifest(self, manifest: list[dict[str, Any]]) -> list[dict[str, Any]]:
        lines: list[dict[str, Any]] = []
        for item in manifest:
            chapter_id = str(item.get("chapter_id") or "").strip()
            if not chapter_id:
                continue
            source = (
                str(item.get("summary") or "").strip()
                or str(item.get("chapter_excerpt") or "").strip()
                or str(item.get("title") or "").strip()
            )
            narration = self._compact_final_cut_text(source, limit=96)
            if narration:
                lines.append({"chapter_id": chapter_id, "narration": narration})
        return lines

    def _chunk_final_cut_manifest(self, manifest: list[dict[str, Any]], *, batch_size: int = 10) -> list[list[dict[str, Any]]]:
        size = max(1, batch_size)
        return [manifest[index : index + size] for index in range(0, len(manifest), size)]

    async def _generate_final_cut_narration_with_model(
        self,
        project: Project,
        *,
        manifest: list[dict[str, Any]],
        task_prompt: str,
        style_directive: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], float, str, str, str]:
        provider, model = self._final_cut_writer_binding(project)
        adapter = self.registry.resolve(provider)
        aggregated_usage: dict[str, Any] = {}
        total_estimated_cost = 0.0
        normalized: list[dict[str, Any]] = []
        had_model_success = False
        had_fallback = False
        recent_lines: list[str] = []

        for batch in self._chunk_final_cut_manifest(manifest, batch_size=10):
            compact_manifest = [
                {
                    "chapter_id": item.get("chapter_id"),
                    "title": item.get("title"),
                    "duration_sec": item.get("duration_sec"),
                    "summary": item.get("summary"),
                    "chapter_excerpt": item.get("chapter_excerpt"),
                }
                for item in batch
            ]
            req = ProviderRequest(
                step="script",
                model=model,
                input={
                    "project_name": project.name,
                    "target_duration_sec": project.target_duration_sec,
                    "segment_manifest": compact_manifest,
                    "continuity_context": recent_lines[-2:],
                    "requirements": {
                        "language": "zh-CN",
                        "tone": "cinematic narration",
                        "one_or_two_sentences_per_segment": True,
                        "return_exactly_one_item_per_segment": True,
                        "avoid_meta_phrases": [
                            "章节剧本已生成",
                            "情节点",
                            "第X章",
                            "在这一章里",
                            "接下来我们看到",
                        ],
                    },
                },
                prompt=(
                    "你是电影旁白编剧。请根据当前这批章节片段清单，为每个片段写一段中文电影化旁白。"
                    "要求：只写剧情、情绪、冲突和转折；不要复述元信息；不要写'章节'、'情节点'、'本段'这类字眼；"
                    "每段最多两句，长度适合配音；必须保留原 chapter_id，并按输入顺序逐条返回。"
                    "只返回 JSON，对象格式为 {\"segments\":[{\"chapter_id\":\"...\",\"narration\":\"...\"}]}，不要解释。"
                    f"\n用户要求：{task_prompt}\n风格约束：{style_directive}"
                ),
                params={
                    "temperature": 0.35,
                    "max_tokens": max(1200, min(3200, 320 * len(compact_manifest))),
                },
            )
            batch_lines: list[dict[str, Any]] = []
            try:
                response = await adapter.invoke(req)
                aggregated_usage = self._merge_usage_metrics(aggregated_usage, response.usage)
                total_estimated_cost += await adapter.estimate_cost(req, response.usage)
                artifact_text = str(response.output.get("text") or "").strip()
                parsed = self._parse_json_object_from_text(artifact_text)
                segments = parsed.get("segments") if isinstance(parsed, dict) else None
                if not isinstance(segments, list) or not segments:
                    raise ValueError("narration writer did not return valid segments JSON")
                requested_ids = [str(item.get("chapter_id") or "").strip() for item in batch]
                allowed_ids = {chapter_id for chapter_id in requested_ids if chapter_id}
                for item in segments:
                    if not isinstance(item, dict):
                        continue
                    chapter_id = str(item.get("chapter_id") or "").strip()
                    narration = self._compact_final_cut_text(str(item.get("narration") or "").strip(), limit=120)
                    if not chapter_id or chapter_id not in allowed_ids or not narration:
                        continue
                    batch_lines.append({"chapter_id": chapter_id, "narration": narration})
                line_map = {str(item.get("chapter_id") or ""): item for item in batch_lines}
                batch_lines = [line_map[chapter_id] for chapter_id in requested_ids if chapter_id in line_map]
                if len(batch_lines) != len(requested_ids):
                    raise ValueError("narration writer returned incomplete batch")
                had_model_success = True
            except Exception:
                batch_lines = self._heuristic_narration_lines_for_manifest(batch)
                had_fallback = True

            normalized.extend(batch_lines)
            recent_lines.extend(str(item.get("narration") or "").strip() for item in batch_lines if str(item.get("narration") or "").strip())

        if not normalized:
            raise ValueError("narration writer returned empty usable narration")
        generation_mode = "model" if had_model_success and not had_fallback else "mixed" if had_model_success else "heuristic"
        return normalized, aggregated_usage, total_estimated_cost, provider, model, generation_mode

    def _parse_json_object_from_text(self, text: str) -> dict[str, Any]:
        candidate = (text or "").strip()
        if not candidate:
            return {}
        fenced_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", candidate, flags=re.DOTALL)
        if fenced_match:
            candidate = fenced_match.group(1).strip()
        else:
            start = candidate.find("{")
            end = candidate.rfind("}")
            if start >= 0 and end > start:
                candidate = candidate[start : end + 1]
        try:
            parsed = json.loads(candidate)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _compact_final_cut_text(self, value: str, *, limit: int) -> str:
        cleaned = re.sub(r"\s+", " ", value or "").strip()
        cleaned = cleaned.replace("——", "，").replace("--", "，")
        cleaned = re.sub(r"[“”\"'`]+", "", cleaned)
        cleaned = re.sub(r"[()（）\\[\\]{}]+", "", cleaned)
        if len(cleaned) <= limit:
            return cleaned
        for token in ("。", "！", "？", "；", "，"):
            position = cleaned.rfind(token, 0, limit)
            if position >= max(12, limit // 2):
                return cleaned[: position + 1]
        return f"{cleaned[:limit].rstrip('，。； ')}。"

    def _split_final_cut_sentences(self, text: str) -> list[str]:
        pieces = [part.strip() for part in re.split(r"(?<=[。！？；])\s*", text) if part.strip()]
        return pieces or [text.strip()]

    def _subtitle_entries_to_srt(self, entries: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for index, entry in enumerate(entries, start=1):
            start_sec = float(entry.get("start_sec") or 0)
            end_sec = float(entry.get("end_sec") or max(start_sec + 1.2, 1.2))
            if end_sec <= start_sec:
                end_sec = start_sec + 1.2
            text = str(entry.get("text") or "").strip() or "..."
            lines.extend(
                [
                    str(index),
                    f"{self._format_srt_timestamp(start_sec)} --> {self._format_srt_timestamp(end_sec)}",
                    text,
                    "",
                ]
            )
        return "\n".join(lines).strip() + "\n"

    def _format_srt_timestamp(self, seconds: float) -> str:
        total_ms = max(int(round(seconds * 1000)), 0)
        hours, remainder = divmod(total_ms, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        secs, millis = divmod(remainder, 1000)
        return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"

    def _build_final_cut_summary(self, project: Project, output: dict[str, Any]) -> dict[str, Any]:
        artifact = deepcopy(output.get("artifact") or {})
        manifest = artifact.get("segment_manifest")
        subtitle_entries = artifact.get("subtitle_entries")
        return {
            "chapter_count": len({str(item.get('chapter_id') or '') for item in manifest}) if isinstance(manifest, list) else 0,
            "segment_count": len(manifest) if isinstance(manifest, list) else 0,
            "has_narration_audio": bool(artifact.get("audio_url") or artifact.get("storage_key")),
            "has_subtitles": bool(isinstance(subtitle_entries, list) and subtitle_entries),
            "narration_text": artifact.get("narration_text"),
            "audio_url": artifact.get("audio_url"),
            "subtitle_url": artifact.get("subtitle_url"),
            "subtitle_count": len(subtitle_entries) if isinstance(subtitle_entries, list) else 0,
            "narration_generation_mode": artifact.get("narration_generation_mode"),
            "narration_writer_provider": artifact.get("narration_writer_provider"),
            "narration_writer_model": artifact.get("narration_writer_model"),
            "target_duration_sec": project.target_duration_sec,
        }

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
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
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
            "cost_source": self._cost_source(provider, usage, execution_mode),
            "token_usage": token_usage,
            "raw_usage": usage or {},
        }

    def _cost_source(self, provider: str, usage: dict[str, Any], execution_mode: str) -> str:
        if execution_mode == "local":
            return "local"
        if isinstance(usage, dict):
            if isinstance(usage.get("cost"), (int, float)):
                return "provider_reported"
            cost_details = usage.get("cost_details")
            if isinstance(cost_details, dict) and isinstance(cost_details.get("upstream_inference_cost"), (int, float)):
                return "provider_reported"
        if provider == "openrouter":
            return "openrouter_catalog_estimated"
        return "heuristic_estimated"

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

    def _merge_usage_metrics(self, base: dict[str, Any] | None, incoming: dict[str, Any] | None) -> dict[str, Any]:
        merged = deepcopy(base or {})
        if not isinstance(incoming, dict):
            return merged
        for key, value in incoming.items():
            if isinstance(value, bool):
                merged[key] = bool(merged.get(key) or value)
            elif isinstance(value, (int, float)):
                previous = merged.get(key)
                merged[key] = float(previous) + float(value) if isinstance(previous, (int, float)) else float(value)
            elif isinstance(value, dict):
                existing = merged.get(key)
                merged[key] = self._merge_usage_metrics(existing if isinstance(existing, dict) else {}, value)
            elif key not in merged:
                merged[key] = deepcopy(value)
        return merged

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
        elif step.step_name == "stitch_subtitle_tts":
            payload["segment_manifest"] = self._build_final_cut_segment_manifest(project)
            payload["narration_plan"] = self._build_final_cut_narration_plan(project)
            payload["final_cut_goal"] = "将所有已通过章节片段合成为一条完整成片，生成字幕、旁白脚本与 AI 配音。"
        return payload

    def list_chapters(self, project_id: str) -> list[dict[str, Any]]:
        chapters = self._list_project_chapters(project_id)
        steps = {step.step_name: step for step in self._list_steps(project_id)}
        fallback_stage_status = self._derive_chapter_stage_status(steps)
        items: list[dict[str, Any]] = []
        for chapter in chapters:
            # Keep chapter list reads cheap. The persisted chapter meta already contains the
            # latest stage outputs; regenerating contact sheets, gallery zips, or slideshow
            # previews for every chapter on every refresh makes the page appear "empty"
            # while the backend is busy recomputing artifacts that already exist.
            meta = dict(chapter.meta or {})
            if isinstance(meta.get("stages"), dict):
                meta["stages"] = self._chapter_stages(chapter)
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
        elif not self._chapter_participates_in_step(chapter, "consistency_check"):
            source_output = deepcopy((stages.get("storyboard_image") or {}).get("output") or {})
            source_artifact = deepcopy(source_output.get("artifact") or {})
            stages["consistency_check"] = {
                "status": StepStatus.APPROVED.value,
                "output": {
                    "storyboard_gallery": self._gallery_payload_from_artifact(source_artifact),
                    "consistency": {
                        "score": 100,
                        "threshold": settings.consistency_threshold,
                        "scope": "project_storyboards",
                        "chapter_id": chapter.id,
                        "details": {
                            "scoring_mode": "meta_chapter_skip",
                            "summary": "前置内容/后记作为片头片尾画面，不参与主剧情分镜校核。",
                            "excluded_from_consistency": True,
                        },
                    },
                },
                "attempt": 0,
                "provider": "local",
                "model": "meta-chapter-skip",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

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
        filtered_parts = [
            (title, part)
            for title, part in parts
            if not self._is_auxiliary_literary_chapter(title, part)
        ]
        if filtered_parts:
            parts = filtered_parts
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

    def _is_auxiliary_literary_chapter(self, title: str, content: str) -> bool:
        normalized_title = re.sub(r"\s+", " ", str(title or "")).strip()
        compact_title = re.sub(r"\s+", "", normalized_title)
        lowered_title = normalized_title.lower()
        is_part_heading = bool(
            re.match(r"^第[0-9一二三四五六七八九十百千两〇零]+部", compact_title)
            or re.match(r"^(part|book)\s+[0-9ivxlcdm]+", lowered_title)
        )
        is_auxiliary_title = is_part_heading or self._is_meta_chapter_title(normalized_title) or normalized_title in {
            "题记",
            "作者序",
            "作者的话",
        }
        if not is_auxiliary_title:
            return False

        body = str(content or "").strip()
        if not body:
            return True
        if normalized_title and body.startswith(normalized_title):
            body = body[len(normalized_title) :].strip()
        non_empty_lines = [line.strip() for line in body.splitlines() if line.strip()]
        short_quote_block = len(body) <= 900 and len(non_empty_lines) <= 18
        quote_like_lines = sum(
            1
            for line in non_empty_lines
            if any(marker in line for marker in ("“", "”", "——", "《", "》", "——《", "—《"))
        )
        citation_like_lines = sum(
            1
            for line in non_empty_lines
            if line.startswith("——")
            or bool(re.search(r"《[^》]+》", line))
            or bool(re.search(r"[A-Za-z]\s*[·.、]\s*[A-Za-z]", line))
        )
        return short_quote_block and (quote_like_lines >= max(2, len(non_empty_lines) // 3) or citation_like_lines >= 1)

    def _chapter_is_auxiliary_filtered(self, chapter: ChapterChunk) -> bool:
        meta = chapter.meta or {}
        if bool(meta.get("auxiliary_filtered")):
            return True
        title = str(meta.get("canonical_title") or meta.get("title") or "")
        return self._is_auxiliary_literary_chapter(title, chapter.content)

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

    def _chapter_stage_output_id(self, output: Any) -> str | None:
        if not isinstance(output, dict):
            return None
        chapter_payload = output.get("chapter")
        if not isinstance(chapter_payload, dict):
            return None
        candidate = chapter_payload.get("id")
        return str(candidate).strip() if isinstance(candidate, str) and candidate.strip() else None

    def _repair_storyboard_stage_entry(self, chapter: ChapterChunk, stage: dict[str, Any]) -> dict[str, Any]:
        output = deepcopy(stage.get("output") or {})
        output_chapter_id = self._chapter_stage_output_id(output)
        if output_chapter_id in {None, chapter.id}:
            return stage
        storyboard_step = self._get_storyboard_step(chapter.project_id)
        active_version = self._get_active_storyboard_version(storyboard_step.id, chapter_id=chapter.id)
        if not active_version or not isinstance(active_version.output_snapshot, dict):
            return stage
        repaired = deepcopy(stage)
        repaired["output"] = deepcopy(active_version.output_snapshot)
        repaired["attempt"] = int(active_version.source_attempt or repaired.get("attempt") or 0)
        repaired["provider"] = active_version.model_provider or repaired.get("provider")
        repaired["model"] = active_version.model_name or repaired.get("model")
        repaired["updated_at"] = datetime.now(timezone.utc).isoformat()
        return repaired

    def _chapter_stages(self, chapter: ChapterChunk) -> dict[str, Any]:
        meta = self._chapter_meta(chapter)
        stages = meta.get("stages")
        result = deepcopy(stages) if isinstance(stages, dict) else {}
        storyboard_stage = result.get("storyboard_image")
        if isinstance(storyboard_stage, dict):
            result["storyboard_image"] = self._repair_storyboard_stage_entry(chapter, storyboard_stage)
        return result

    def _chapter_step_status(self, chapter: ChapterChunk, step_name: str) -> str:
        if not self._chapter_participates_in_step(chapter, step_name):
            return StepStatus.APPROVED.value
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
        if not self._chapter_participates_in_step(chapter, step_name):
            return True
        dependency = CHAPTER_DEPENDENCIES.get(step_name)
        if dependency == "chapter_chunking":
            step = self.db.scalar(
                select(PipelineStep).where(PipelineStep.project_id == project_id, PipelineStep.step_name == "chapter_chunking")
            )
            return bool(step and step.status == StepStatus.APPROVED.value)
        if not dependency:
            return True
        if dependency == "storyboard_image":
            if self._chapter_step_status(chapter, dependency) == StepStatus.APPROVED.value:
                return True
            return bool(self._active_storyboard_frames_for_chapter(project_id, chapter))
        return self._chapter_step_status(chapter, dependency) == StepStatus.APPROVED.value

    def _chapter_participates_in_step(self, chapter: ChapterChunk, step_name: str) -> bool:
        if self._chapter_is_auxiliary_filtered(chapter):
            return False
        if step_name != "consistency_check":
            return True
        title = str((chapter.meta or {}).get("canonical_title") or (chapter.meta or {}).get("title") or "")
        return not self._is_meta_chapter_title(title)

    def _list_project_chapters(self, project_id: str) -> list[ChapterChunk]:
        chapters = list(
            self.db.scalars(
                select(ChapterChunk)
                .where(ChapterChunk.project_id == project_id)
                .order_by(ChapterChunk.chapter_index.asc(), ChapterChunk.chunk_index.asc())
            ).all()
        )
        return [chapter for chapter in chapters if not self._chapter_is_auxiliary_filtered(chapter)]

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
        chapter_text = self._chapter_story_bible_matching_text(chapter, frames, neighbor_frames)
        reference_characters = self._select_relevant_story_bible_entities(
            story_bible.get("characters"),
            chapter_text,
            chapter=chapter,
            limit=CONSISTENCY_REFERENCE_CHARACTER_LIMIT,
            allow_fallback=True,
        )
        reference_scenes = self._select_relevant_story_bible_entities(
            story_bible.get("scenes"),
            chapter_text,
            chapter=chapter,
            limit=CONSISTENCY_REFERENCE_SCENE_LIMIT,
            allow_fallback=False,
        )
        return {
            "project_id": project.id,
            "chapter_id": chapter.id if chapter else None,
            "chapter_title": str((chapter.meta or {}).get("title") or "") if chapter else "",
            "chapter_summary": str((chapter.meta or {}).get("summary") or "") if chapter else "",
            "frames": frames,
            "neighbor_frames": neighbor_frames,
            "story_bible": story_bible if isinstance(story_bible, dict) else {},
            "reference_characters": reference_characters,
            "reference_scenes": reference_scenes,
        }

    def _storyboard_frames_for_chapter(self, chapter: ChapterChunk | None) -> list[dict[str, Any]]:
        if chapter is None:
            return []
        gallery = self._load_storyboard_gallery(chapter)
        frames = gallery.get("frames")
        if not isinstance(frames, list):
            return []
        return [deepcopy(item) for item in frames if isinstance(item, dict)]

    def _active_storyboard_frames_for_chapter(self, project_id: str, chapter: ChapterChunk) -> dict[int, dict[str, Any]]:
        storyboard_step = self._get_storyboard_step(project_id)
        candidates: list[dict[str, Any]] = []
        active_version = self._get_active_storyboard_version(storyboard_step.id, chapter_id=chapter.id)
        if active_version and isinstance(active_version.output_snapshot, dict):
            candidates.append(deepcopy(active_version.output_snapshot))
        stage_output = deepcopy(self._chapter_stage_chain(chapter).get("storyboard_image") or {})
        if stage_output:
            candidates.append(stage_output)
        for candidate in candidates:
            artifact = deepcopy(candidate.get("artifact") or {})
            frames = artifact.get("frames")
            if isinstance(frames, list):
                return {
                    int(frame.get("shot_index") or index + 1): deepcopy(frame)
                    for index, frame in enumerate(frames)
                    if isinstance(frame, dict)
                }
            gallery = candidate.get("storyboard_gallery")
            if isinstance(gallery, dict) and isinstance(gallery.get("frames"), list):
                return {
                    int(frame.get("shot_index") or index + 1): deepcopy(frame)
                    for index, frame in enumerate(gallery["frames"])
                    if isinstance(frame, dict)
                }
        return {}

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

    def _build_consistency_revision_prompt(self, chapter: ChapterChunk) -> str:
        stage_output = deepcopy(self._chapter_stage_chain(chapter).get("consistency_check") or {})
        consistency = deepcopy(stage_output.get("consistency") or {})
        details = deepcopy(consistency.get("details") or {})
        low_frames = [item for item in details.get("low_frames", []) if isinstance(item, dict)]
        summary = str(details.get("summary") or consistency.get("summary") or "").strip()
        revision_lines = [
            "针对一致性校核的低分镜头进行定向修正，重新生成分镜时必须严格遵守 Story Bible 参考图与章节内既有镜头连续性。",
            "优先保持人物身份、服装、发型、年龄感、场景结构、光线色调、镜头语言与前后镜头一致。",
        ]
        if summary:
            revision_lines.append(f"本章校核摘要：{summary}")
        for item in low_frames[:8]:
            shot_index = int(item.get("shot_index") or 0)
            reason = self._normalize_consistency_low_frame_reason(item)
            if shot_index > 0:
                revision_lines.append(f"重点修正镜头 {shot_index}：{reason}")
        if len(revision_lines) <= 2:
            revision_lines.append("若原分镜与剧情不符，请严格回到当前章节文本与分镜细化描述重绘，不得偏离情节。")
        return "\n".join(revision_lines)

    def _normalize_consistency_low_frame_reason(self, low_frame: dict[str, Any]) -> str:
        explicit_reason = str(low_frame.get("reason") or "").strip()
        if explicit_reason:
            return explicit_reason
        character_anchors = [str(item).strip() for item in low_frame.get("character_anchors", []) if str(item).strip()]
        scene_anchors = [str(item).strip() for item in low_frame.get("scene_anchors", []) if str(item).strip()]
        if character_anchors and scene_anchors:
            return (
                f"保持人物 {', '.join(character_anchors[:3])} 的外观与服装一致，同时延续场景 "
                f"{', '.join(scene_anchors[:3])} 的空间关系和氛围。"
            )
        if character_anchors:
            return f"保持人物 {', '.join(character_anchors[:3])} 的身份、发型、服装和表情连续。"
        if scene_anchors:
            return f"保持场景 {', '.join(scene_anchors[:3])} 的地点、布景、光线与色调连续。"
        score = low_frame.get("score")
        if isinstance(score, (int, float)):
            return f"当前镜头一致性得分偏低（{int(score)}），请重点修正人物与场景连续性。"
        return "请重点修正人物身份、场景空间、光线色调与镜头风格连续性。"

    def _calibrate_consistency_report(
        self,
        report: Any,
        consistency_context: dict[str, Any],
        *,
        threshold: int,
    ) -> Any:
        details = deepcopy(report.details)
        low_frames = [item for item in details.get("low_frames", []) if isinstance(item, dict)]
        if not low_frames:
            return report
        soft_issues = 0
        hard_issues = 0
        for item in low_frames:
            reason = self._normalize_consistency_low_frame_reason(item)
            if any(token in reason for token in CONSISTENCY_HARD_IDENTITY_PATTERNS):
                hard_issues += 1
                continue
            if any(token in reason for token in CONSISTENCY_SOFT_MISMATCH_PATTERNS):
                soft_issues += 1
                continue
            if "场景" in reason and ("不符" in reason or "差异" in reason):
                soft_issues += 1
                continue
            hard_issues += 1
        if soft_issues <= 0 or hard_issues > 0:
            return report
        boost = min(15, soft_issues * 4 + (2 if soft_issues >= 3 else 0))
        calibrated_score = min(100, int(report.score) + boost)
        details["calibration"] = {
            "applied": True,
            "reason": "soft_reference_mismatch_only",
            "soft_issue_count": soft_issues,
            "score_boost": boost,
            "original_score": int(report.score),
            "calibrated_score": calibrated_score,
        }
        return type(report)(
            score=calibrated_score,
            dimensions=deepcopy(report.dimensions),
            should_rework=calibrated_score < threshold,
            details=details,
        )

    def _consistency_rework_target_shots(self, chapter: ChapterChunk) -> list[int]:
        stage_output = deepcopy(self._chapter_stage_chain(chapter).get("consistency_check") or {})
        details = deepcopy((stage_output.get("consistency") or {}).get("details") or {})
        low_frames = [item for item in details.get("low_frames", []) if isinstance(item, dict)]
        shot_indexes = {
            int(item.get("shot_index") or 0)
            for item in low_frames
            if isinstance(item.get("shot_index"), (int, float, str)) and str(item.get("shot_index")).strip()
        }
        return sorted(index for index in shot_indexes if index > 0)

    async def _score_storyboard_consistency_with_model(
        self,
        project: Project,
        step: PipelineStep,
        consistency_context: dict[str, Any],
        *,
        threshold: int,
    ) -> dict[str, Any]:
        fallback = score_consistency(consistency_context, threshold=threshold)
        provider, model = self._resolve_binding(project, "consistency_check", "consistency")
        if provider == "local":
            details = deepcopy(fallback.details)
            details["scoring_mode"] = "heuristic_fallback"
            result = type(fallback)(
                score=fallback.score,
                dimensions=fallback.dimensions,
                should_rework=fallback.should_rework,
                details=details,
            )
            return {
                "provider": "local",
                "model": "heuristic-consistency",
                "estimated_cost": 0.0,
                "report": result,
                "response": ProviderResponse(
                    output={
                        "summary": str(details.get("reason") or "heuristic consistency"),
                        "scoring_mode": "heuristic_fallback",
                    },
                    usage={},
                    raw={"scoring_mode": "heuristic_fallback"},
                ),
            }

        try:
            adapter = self.registry.resolve(provider)
            visual_inputs = self._consistency_visual_inputs(consistency_context)
            if not visual_inputs:
                details = deepcopy(fallback.details)
                details["scoring_mode"] = "heuristic_fallback"
                result = type(fallback)(
                    score=fallback.score,
                    dimensions=fallback.dimensions,
                    should_rework=fallback.should_rework,
                    details=details,
                )
                return {
                    "provider": provider,
                    "model": model,
                    "estimated_cost": 0.0,
                    "report": result,
                    "response": ProviderResponse(
                        output={
                            "summary": "no visual inputs; heuristic fallback",
                            "scoring_mode": "heuristic_fallback",
                        },
                        usage={},
                        raw={"scoring_mode": "heuristic_fallback", "reason": "no_visual_inputs"},
                    ),
                }
            prompt = self._build_consistency_review_prompt(consistency_context, threshold)
            model_candidates = [model]
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
                    result = type(fallback)(
                        score=score,
                        dimensions=normalized_dimensions,
                        should_rework=score < threshold,
                        details=details,
                    )
                    result = self._calibrate_consistency_report(result, consistency_context, threshold=threshold)
                    return {
                        "provider": provider,
                        "model": candidate_model,
                        "estimated_cost": await adapter.estimate_cost(req, response.usage),
                        "report": result,
                        "response": ProviderResponse(
                            output={
                                "summary": str(parsed.get("summary") or ""),
                                "parsed": parsed,
                            },
                            usage=response.usage,
                            raw=response.raw,
                        ),
                    }
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    continue
            raise last_error or ValueError("visual consistency scoring failed")
        except Exception as exc:  # noqa: BLE001
            details = deepcopy(fallback.details)
            details["scoring_mode"] = "heuristic_fallback"
            details["fallback_reason"] = str(exc)
            result = type(fallback)(
                score=fallback.score,
                dimensions=fallback.dimensions,
                should_rework=fallback.should_rework,
                details=details,
            )
            result = self._calibrate_consistency_report(result, consistency_context, threshold=threshold)
            return {
                "provider": provider,
                "model": model,
                "estimated_cost": 0.0,
                "report": result,
                "response": ProviderResponse(
                    output={
                        "summary": str(exc),
                        "scoring_mode": "heuristic_fallback",
                    },
                    usage={},
                    raw={"scoring_mode": "heuristic_fallback", "fallback_reason": str(exc)},
                ),
            }

    def _build_consistency_review_prompt(self, consistency_context: dict[str, Any], threshold: int) -> str:
        characters = consistency_context.get("reference_characters") or []
        scenes = consistency_context.get("reference_scenes") or []
        frame_descriptions = []
        for frame in self._sample_consistency_frames(
            consistency_context.get("frames") or [],
            CONSISTENCY_CURRENT_FRAME_LIMIT,
        ):
            if not isinstance(frame, dict):
                continue
            frame_descriptions.append(
                {
                    "shot_index": frame.get("shot_index"),
                    "visual": frame.get("visual"),
                    "action": frame.get("action"),
                    "dialogue": frame.get("dialogue"),
                    "expected_characters": frame.get("characters") or frame.get("character_names") or [],
                    "expected_scene": frame.get("scene") or frame.get("scene_hint") or frame.get("location") or "",
                }
            )
        neighbor_descriptions = []
        for frame in self._sample_consistency_frames(
            consistency_context.get("neighbor_frames") or [],
            CONSISTENCY_NEIGHBOR_FRAME_LIMIT,
        ):
            if not isinstance(frame, dict):
                continue
            neighbor_descriptions.append(
                {
                    "shot_index": frame.get("shot_index"),
                    "visual": frame.get("visual"),
                    "action": frame.get("action"),
                    "expected_characters": frame.get("characters") or frame.get("character_names") or [],
                    "expected_scene": frame.get("scene") or frame.get("scene_hint") or frame.get("location") or "",
                }
            )
        payload = {
            "chapter_title": consistency_context.get("chapter_title"),
            "chapter_summary": consistency_context.get("chapter_summary"),
            "threshold": threshold,
            "reference_characters": characters,
            "reference_scenes": scenes,
            "review_rules": [
                "只将镜头与语义相关的人物/场景参考图比较，不要拿无关参考图扣分。",
                "如果某个镜头对应的场景没有 Story Bible 参考图，不要因为它不像其他场景参考图而扣分。",
                "优先检查同一角色的脸型、年龄感、发型、服装层次是否稳定，再检查章节内镜头之间的光线和场景连续性。",
                "角色参考图首先用于识别人物身份与面部特征，不要把参考图中的具体服装当作全书所有章节都必须一致的硬约束。",
                "如果镜头发生在同一地点体系的子场景，例如学校内的网球场、走廊、办公室，仍应视为同一地点家族，不要因为建筑外观不同直接扣分。",
                "建立氛围的空镜、物件镜、电话镜头可以不出现清晰人脸；如果剧情本身不要求角色露面，不要因缺少正脸而重罚。",
            ],
            "current_frames": frame_descriptions,
            "neighbor_frames": neighbor_descriptions,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    def _consistency_visual_inputs(self, consistency_context: dict[str, Any]) -> list[dict[str, Any]]:
        images: list[dict[str, Any]] = []
        sampled_current_frames = self._sample_consistency_frames(
            consistency_context.get("frames") or [],
            CONSISTENCY_CURRENT_FRAME_LIMIT,
        )
        sampled_neighbor_frames = self._sample_consistency_frames(
            consistency_context.get("neighbor_frames") or [],
            CONSISTENCY_NEIGHBOR_FRAME_LIMIT,
        )
        frame_character_names = {
            str(item).strip()
            for frame in sampled_current_frames
            for item in (frame.get("characters") or frame.get("character_names") or [])
            if str(item).strip()
        }
        frame_scene_names: set[str] = set()
        for frame in sampled_current_frames:
            for raw_value in (frame.get("scene"), frame.get("scene_hint"), frame.get("location")):
                value = str(raw_value or "").strip()
                if not value:
                    continue
                for token in re.split(r"[\\/|]|\\s{2,}", value):
                    token = token.strip()
                    if token:
                        frame_scene_names.add(token)
        for serialized in consistency_context.get("reference_characters") or []:
            if not isinstance(serialized, str):
                continue
            name = serialized.split(":", 1)[0].strip()
            if frame_character_names and name not in frame_character_names:
                continue
            raw = self._story_bible_entity_by_name(consistency_context.get("story_bible"), "characters", name)
            if not raw:
                continue
            storage_key, image_url = self._story_bible_entity_reference_fields("characters", raw)
            url = self._reference_image_data_url(storage_key, image_url, variant="portrait")
            if url:
                images.append({"url": url, "label": f"character:{name}"})
        for serialized in consistency_context.get("reference_scenes") or []:
            if not isinstance(serialized, str):
                continue
            name = serialized.split(":", 1)[0].strip()
            if frame_scene_names and name not in frame_scene_names:
                continue
            raw = self._story_bible_entity_by_name(consistency_context.get("story_bible"), "scenes", name)
            if not raw:
                continue
            storage_key, image_url = self._story_bible_entity_reference_fields("scenes", raw)
            url = self._reference_image_data_url(storage_key, image_url)
            if url:
                images.append({"url": url, "label": f"scene:{name}"})
        for frame in sampled_current_frames:
            if not isinstance(frame, dict):
                continue
            url = self._reference_image_data_url(frame.get("storage_key"), frame.get("image_url"))
            if url:
                images.append({"url": url, "label": f"current-shot:{frame.get('shot_index')}"})
        for frame in sampled_neighbor_frames:
            if not isinstance(frame, dict):
                continue
            url = self._reference_image_data_url(frame.get("storage_key"), frame.get("image_url"))
            if url:
                images.append({"url": url, "label": f"neighbor-shot:{frame.get('shot_index')}"})
        return images

    def _sample_consistency_frames(self, frames: list[Any], limit: int) -> list[dict[str, Any]]:
        normalized = [item for item in frames if isinstance(item, dict)]
        if limit <= 0 or len(normalized) <= limit:
            return normalized
        indexes = sorted({round(index * (len(normalized) - 1) / max(limit - 1, 1)) for index in range(limit)})
        return [normalized[index] for index in indexes]

    def _reference_image_data_url(self, storage_key: Any, fallback_url: Any, *, variant: str = "full") -> str | None:
        if isinstance(storage_key, str) and storage_key and Path(storage_key).exists():
            return _cached_reference_image_variant_data_url(storage_key, variant)
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
        elif step.step_name == "stitch_subtitle_tts":
            manifest = self._build_final_cut_segment_manifest(project)
            plan = self._build_final_cut_narration_plan(project, manifest=manifest)
            artifact.setdefault("segment_manifest", manifest)
            artifact.setdefault("narration_text", plan["narration_text"])
            artifact.setdefault("subtitle_entries", plan["subtitle_entries"])
            artifact.setdefault("chapter_count", plan["chapter_count"])
            artifact.setdefault("segment_count", plan["segment_count"])
            artifact.setdefault("voice", plan["voice"])
            artifact.setdefault("summary", f"已整理 {plan['segment_count']} 个章节片段的成片合成方案。")
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
        characters = self._recount_story_bible_occurrences(characters, chapter_digest)
        scenes = self._recount_story_bible_occurrences(scenes, chapter_digest)
        characters = self._filter_story_bible_entities_by_occurrence(characters, kind="character", chapter_digest=chapter_digest)
        scenes = self._filter_story_bible_entities_by_occurrence(scenes, kind="scene", chapter_digest=chapter_digest)
        if not self._story_bible_entities_quality_ok(characters, kind="character") or not self._story_bible_entities_quality_ok(scenes, kind="scene"):
            fallback = self._build_local_story_bible_fallback(project, chapters, chapter_digest)
            if not self._story_bible_entities_quality_ok(characters, kind="character"):
                fallback_characters = self._normalize_story_bible_entities(fallback.get("characters"), kind="character")
                if fallback_characters:
                    characters = fallback_characters
                else:
                    characters = []
            if not self._story_bible_entities_quality_ok(scenes, kind="scene"):
                fallback_scenes = self._normalize_story_bible_entities(fallback.get("scenes"), kind="scene")
                if fallback_scenes:
                    scenes = fallback_scenes
                else:
                    scenes = []
        characters = self._recount_story_bible_occurrences(characters, chapter_digest)
        scenes = self._recount_story_bible_occurrences(scenes, chapter_digest)
        characters = self._filter_story_bible_entities_by_occurrence(characters, kind="character", chapter_digest=chapter_digest)
        scenes = self._filter_story_bible_entities_by_occurrence(scenes, kind="scene", chapter_digest=chapter_digest)
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
                "5) 忽略书名、作者、目录、前言等元信息。\n"
                "6) description/visual_anchor 要可用于后续分镜一致性控制。"
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
            "4) chapter_ids/chapter_titles 保留并集；occurrence_count 使用合并后总出现次数。\n"
            "5) 删除明显无效实体：书名、作者名、指代词（如“他说”）、元信息短语。"
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
            "事实上", "例如", "当然", "东西", "因为", "如果", "但是", "于是", "因此", "并且", "或者", "其实", "时间", "一样", "的话",
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

    def _should_skip_chapter_for_batch_step(self, chapter: ChapterChunk, step_name: str) -> bool:
        if step_name not in CHAPTER_SCOPED_STEPS:
            return False
        return not self._chapter_participates_in_step(chapter, step_name)

    def _estimate_action_unit_count(self, project_id: str, step_name: str, *, scope_mode: str, chapter_id: str | None) -> int:
        if step_name not in CHAPTER_SCOPED_STEPS:
            return 1
        chapters = self._list_project_chapters(project_id)
        if scope_mode == "single":
            return 1 if chapter_id else 1
        if scope_mode == "failed_chapters":
            return sum(
                1
                for chapter in chapters
                if not self._should_skip_chapter_for_batch_step(chapter, step_name)
                and self._chapter_step_status(chapter, step_name) == StepStatus.FAILED.value
                and self._chapter_dependency_satisfied(project_id, chapter, step_name)
            )
        if scope_mode == "all_chapters":
            return sum(
                1
                for chapter in chapters
                if not self._should_skip_chapter_for_batch_step(chapter, step_name)
                and self._chapter_dependency_satisfied(project_id, chapter, step_name)
            )
        return 1

    def _cost_run_matches_scope(self, run: ModelRun, *, chapter_id: str | None, scope_mode: str) -> bool:
        request_summary = run.request_summary if isinstance(run.request_summary, dict) else {}
        params = request_summary.get("params") if isinstance(request_summary.get("params"), dict) else {}
        run_chapter_id = params.get("chapter_id")
        if scope_mode == "single" and chapter_id:
            return run_chapter_id == chapter_id
        if scope_mode in {"all_chapters", "failed_chapters"}:
            return bool(run_chapter_id)
        return True

    def _is_fatal_batch_error(self, exc: Exception) -> bool:
        message = str(exc or "").lower()
        fatal_markers = (
            "insufficient credits",
            "402 from openrouter",
            "401 from openrouter",
            "403 from openrouter",
            "invalid api key",
            "authentication",
        )
        return any(marker in message for marker in fatal_markers)

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
            honorific_only = {"先生", "女士", "太太", "小姐", "老师", "医生", "警官", "典狱长"}
            if value in honorific_only:
                return False
            generic_character_noise = {
                "救赎", "作者", "电影", "小说", "故事", "作品", "事实上", "例如", "当然", "东西", "因为", "如果", "但是",
                "于是", "因此", "并且", "或者", "其实", "开始", "后来", "然后", "时间", "一样", "的话", "这些", "那些",
            }
            if any(term in value for term in generic_character_noise):
                return False
            if "的" in value and len(value) >= 4:
                return False
            invalid_suffix = ("说", "道", "问", "想", "看", "听", "笑", "哭", "喊", "答", "讲")
            if value.endswith(invalid_suffix) and len(value) <= 4:
                return False
            if re.match(r"^[我你他她它这那][知说想看问答道]$", value):
                return False
            if value[0] in {"我", "你", "他", "她", "它", "这", "那"} and len(value) <= 3:
                return False
            if re.fullmatch(r"[\u4e00-\u9fff]{2,6}", value):
                banned_chars = {"的", "了", "我", "你", "他", "她", "它", "这", "那", "有", "没", "不", "又", "都", "和", "在"}
                if any(char in banned_chars for char in value):
                    return False
        return True

    def _story_bible_entities_quality_ok(self, items: list[dict[str, Any]], *, kind: str) -> bool:
        if not items:
            return False
        strong = 0
        for item in items:
            occurrences = int(item.get("occurrence_count") or 0)
            chapter_span = len(item.get("chapter_ids") or [])
            if occurrences >= 2 or chapter_span >= 2:
                strong += 1
        if kind == "character":
            return strong >= 2
        return strong >= 1

    def _recount_story_bible_occurrences(
        self,
        items: list[dict[str, Any]],
        chapter_digest: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        recounted: list[dict[str, Any]] = []
        for raw in items:
            item = deepcopy(raw)
            aliases = [str(item.get("name") or "").strip(), *[str(alias).strip() for alias in item.get("aliases") or []]]
            aliases = [alias for alias in aliases if alias]
            chapter_ids = set(item.get("chapter_ids") or [])
            chapter_titles = set(item.get("chapter_titles") or [])
            occurrence_count = int(item.get("occurrence_count") or 0)
            for chapter in chapter_digest:
                chapter_text = f"{chapter.get('summary', '')}\n{chapter.get('context', chapter.get('excerpt', ''))}"
                if not chapter_text:
                    continue
                chapter_hit = False
                for alias in aliases:
                    hits = chapter_text.count(alias)
                    if hits > 0:
                        occurrence_count += hits
                        chapter_hit = True
                if chapter_hit:
                    chapter_id = str(chapter.get("chapter_id") or "").strip()
                    chapter_title = str(chapter.get("title") or "").strip()
                    if chapter_id:
                        chapter_ids.add(chapter_id)
                    if chapter_title:
                        chapter_titles.add(chapter_title)
            item["occurrence_count"] = max(occurrence_count, int(item.get("occurrence_count") or 0), 1)
            item["chapter_ids"] = list(chapter_ids)
            item["chapter_titles"] = list(chapter_titles)
            recounted.append(item)
        return recounted

    def _filter_story_bible_entities_by_occurrence(
        self,
        items: list[dict[str, Any]],
        *,
        kind: str,
        chapter_digest: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not items:
            return []
        if kind == "character":
            author_aliases = self._detect_author_aliases(chapter_digest)
            candidates = [
                item
                for item in items
                if int(item.get("occurrence_count") or 0) >= 2 or len(item.get("chapter_ids") or []) >= 2
            ]
            strong = [
                item
                for item in candidates
                if not self._is_likely_author_name(str(item.get("name") or ""), chapter_digest)
                and not self._is_author_alias(str(item.get("name") or ""), author_aliases)
            ]
            if strong:
                return strong
            return candidates if candidates else items
        return items

    def _is_likely_author_name(self, name: str, chapter_digest: list[dict[str, Any]]) -> bool:
        if not name:
            return False
        total_hits = 0
        author_hits = 0
        pattern_left = re.compile(rf"作者[^。\n]{{0,12}}{re.escape(name)}")
        pattern_right = re.compile(rf"{re.escape(name)}[^。\n]{{0,12}}作者")
        for chapter in chapter_digest:
            text = f"{chapter.get('summary', '')}\n{chapter.get('context', chapter.get('excerpt', ''))}"
            if not text:
                continue
            count = text.count(name)
            if count <= 0:
                continue
            total_hits += count
            author_hits += len(pattern_left.findall(text)) + len(pattern_right.findall(text))
        if total_hits <= 0:
            return False
        return (author_hits / total_hits) >= 0.35

    def _detect_author_aliases(self, chapter_digest: list[dict[str, Any]]) -> set[str]:
        aliases: set[str] = set()
        samples = chapter_digest[:3]
        for chapter in samples:
            text = f"{chapter.get('summary', '')}\n{chapter.get('context', chapter.get('excerpt', ''))}"
            for match in re.findall(r"作者[:：]\s*([^\n。；;，,]{2,24})", text):
                raw = str(match).strip().strip("《》\"'“”")
                if not raw:
                    continue
                aliases.add(raw)
                for part in re.split(r"[·\s,，/]+", raw):
                    token = part.strip()
                    if len(token) >= 2:
                        aliases.add(token)
        return aliases

    def _is_author_alias(self, name: str, aliases: set[str]) -> bool:
        if not name or not aliases:
            return False
        name_key = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", name.lower())
        if not name_key:
            return False
        for alias in aliases:
            alias_key = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", alias.lower())
            if not alias_key:
                continue
            if name_key == alias_key or name_key in alias_key or alias_key in name_key:
                return True
        return False

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
        speech_patterns = [
            r"([\u4e00-\u9fff]{2,4})(?=(?:说|道|问|答|喊|叫|想|看|笑|哭|告诉|回答))",
            r"(?:老|小)?([\u4e00-\u9fff]{2,3})(?:先生|女士|太太|医生|警官|典狱长)",
        ]
        for pattern in speech_patterns:
            for token in re.findall(pattern, source_text):
                name = str(token).strip()
                if self._is_valid_story_bible_entity_name(name, kind="character"):
                    character_counts[name] = character_counts.get(name, 0) + 4
        for token in re.findall(r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})?)\b", source_text):
            name = str(token).strip()
            if self._is_valid_story_bible_entity_name(name, kind="character"):
                character_counts[name] = character_counts.get(name, 0) + 2
        for item in chapter_digest:
            for name in item.get("name_candidates") or []:
                token = str(name).strip()
                if token and self._is_valid_story_bible_entity_name(token, kind="character"):
                    character_counts[token] = character_counts.get(token, 0) + 2
        for token in re.findall(r"[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})?|[\u4e00-\u9fff]{2,4}", source_text):
            if token in {"他们", "我们", "自己", "一个", "前置内容", "章节"}:
                continue
            if not self._is_valid_story_bible_entity_name(token, kind="character"):
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
            if count >= 3
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
            if not self._is_valid_story_bible_entity_name(name, kind=kind):
                continue
            aliases = self._story_bible_aliases(raw)
            aliases = [alias for alias in aliases if self._is_valid_story_bible_entity_name(alias, kind=kind)]
            if not aliases:
                aliases = [name]
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
            if not self._is_valid_story_bible_entity_name(name, kind=kind):
                continue
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
            reference_kind = "identity_portrait" if category == "characters" else "scene_board"
            prompt = self._build_story_bible_reference_prompt(project, category, item, reference_kind=reference_kind)
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
            reference_asset = self._materialize_story_bible_reference_asset(
                project,
                step,
                category,
                item["name"],
                index + 1,
                artifact,
                reference_kind=reference_kind,
            )
            if category == "characters":
                item["identity_reference_image_url"] = reference_asset["image_url"]
                item["identity_reference_storage_key"] = reference_asset["storage_key"]
                item["identity_reference_provider"] = artifact.get("provider") or provider
                item["identity_reference_model"] = artifact.get("model") or model
                item["reference_image_url"] = reference_asset["image_url"]
                item["reference_storage_key"] = reference_asset["storage_key"]
                item["reference_provider"] = item["identity_reference_provider"]
                item["reference_model"] = item["identity_reference_model"]
            else:
                item["scene_reference_image_url"] = reference_asset["image_url"]
                item["scene_reference_storage_key"] = reference_asset["storage_key"]
                item["scene_reference_provider"] = artifact.get("provider") or provider
                item["scene_reference_model"] = artifact.get("model") or model
                item["reference_image_url"] = reference_asset["image_url"]
                item["reference_storage_key"] = reference_asset["storage_key"]
                item["reference_provider"] = item["scene_reference_provider"]
                item["reference_model"] = item["scene_reference_model"]

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

    def _build_story_bible_reference_prompt(
        self,
        project: Project,
        category: str,
        item: dict[str, Any],
        *,
        reference_kind: str,
    ) -> str:
        story_bible = normalize_style_profile(project.style_profile).get("story_bible", {})
        visual_style = story_bible.get("visual_style", {}) if isinstance(story_bible, dict) else {}
        base_lines = [
            f"Project: {project.name}",
            f"Reference type: {reference_kind}",
            f"Name: {item.get('name')}",
            f"Description: {item.get('description')}",
            f"Visual anchor: {item.get('visual_anchor')}",
            f"Base style: {visual_style.get('preset_label') if isinstance(visual_style, dict) else '电影质感'}",
            f"Rendering: {visual_style.get('rendering') if isinstance(visual_style, dict) else '写实电影画面'}",
            f"Lighting: {visual_style.get('lighting') if isinstance(visual_style, dict) else ''}",
        ]
        if category == "characters":
            wardrobe_anchor = str(item.get("wardrobe_anchor") or "").strip()
            lines = base_lines + [
                f"Wardrobe anchor: {wardrobe_anchor or 'neutral, timeless, non-scene-specific wardrobe only'}",
                "Frame: chest-up or waist-up portrait, centered subject, neutral background, no environment storytelling.",
                "Pose: calm reference pose, looking slightly off-camera or front-facing, no dramatic action.",
                "Hard constraints: identity portrait only, stable face geometry, stable age impression, stable hairstyle, minimal neutral wardrobe, no props, no scene background, no dramatic lighting gimmicks, no text.",
            ]
        else:
            mood = str(item.get("mood") or "").strip()
            lines = base_lines + [
                f"Mood: {mood}",
                "Frame: wide environment board focused on architecture, layout, light, color and atmosphere.",
                "Hard constraints: scene reference only, no character close-up, no hero pose, emphasize set layout and lighting continuity, no text.",
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
        *,
        reference_kind: str,
    ) -> dict[str, Any]:
        file_path: Path | None = None
        mime_type = str(artifact.get("mime_type") or "image/png")
        image_data_url = artifact.get("image_data_url")
        image_base64 = artifact.get("image_base64")
        image_url = artifact.get("image_url") or artifact.get("thumbnail_url")
        prefix = f"{reference_kind}-{index:02d}-{sanitize_component(name)}"
        target_dir = project_category_dir(project.id, project.name, "references") / category / reference_kind
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
        story_bible = normalize_style_profile(project.style_profile).get("story_bible", {})
        shots: list[dict[str, Any]] = []
        for index in range(shot_count):
            source = sentences[index % len(sentences)] if sentences else content[:180]
            dialogue_match = re.findall(r"[“\"]([^”\"]{2,80})[”\"]", source)
            shot_text = source.lower()
            characters = self._extract_shot_entities(
                story_bible.get("characters") if isinstance(story_bible, dict) else [],
                shot_text,
                chapter=chapter,
                limit=3,
            )
            scenes = self._extract_shot_entities(
                story_bible.get("scenes") if isinstance(story_bible, dict) else [],
                shot_text,
                chapter=chapter,
                limit=2,
            )
            shots.append(
                {
                    "shot_index": index + 1,
                    "duration_sec": max(2.5, round(chapter_budget / shot_count, 1)),
                    "frame_type": "中景" if index % 3 else "远景",
                    "visual": source[:160],
                    "action": source[:120],
                    "dialogue": (dialogue_match[0] if dialogue_match else source[:90]),
                    "characters": characters,
                    "scene": scenes[0] if scenes else "",
                    "scene_hint": " / ".join(scenes),
                    "continuity_anchor": "保持同一人物外貌、服装和同一场景光线连续一致。",
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

    def _storyboard_version_count_for_chapter(self, project_id: str, chapter: ChapterChunk) -> int:
        storyboard_step = self._get_storyboard_step(project_id)
        return len(self.list_storyboard_versions(project_id, storyboard_step.id, chapter_id=chapter.id))

    def _dominant_storyboard_model_for_chapter(self, project_id: str, chapter: ChapterChunk) -> str | None:
        model_counts: dict[str, int] = {}
        for frame in self._active_storyboard_frames_for_chapter(project_id, chapter).values():
            model_name = str(frame.get("model") or "").strip()
            if not model_name:
                continue
            model_counts[model_name] = model_counts.get(model_name, 0) + 1
        if not model_counts:
            return None
        return max(model_counts.items(), key=lambda item: (item[1], item[0]))[0]

    def _available_storyboard_models(self, provider: str) -> list[str]:
        if provider != "openrouter":
            return []
        catalog = [item for item in self.registry.list_catalog() if item.provider == provider and item.step == "image"]
        return list(catalog[0].models) if catalog else []

    def _preferred_storyboard_model_for_run(
        self,
        project: Project,
        chapter: ChapterChunk,
        provider: str,
        primary_model: str,
        params: dict[str, Any],
    ) -> str:
        if provider != "openrouter":
            return primary_model
        available_list = self._available_storyboard_models(provider)
        available = set(available_list)
        if primary_model not in available and available_list:
            primary_model = available_list[0]
        is_targeted_rework = bool(params.get("auto_revision_prompt")) or bool(params.get("target_shot_indexes"))
        dominant = self._dominant_storyboard_model_for_chapter(project.id, chapter)
        version_count = self._storyboard_version_count_for_chapter(project.id, chapter)
        if is_targeted_rework and version_count >= 4 and "openai/gpt-5-image" in available:
            return "openai/gpt-5-image"
        if is_targeted_rework and dominant and dominant in available:
            return dominant
        return primary_model

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

        requested_targets = {
            int(item)
            for item in (params.get("target_shot_indexes") or [])
            if str(item).strip().isdigit() and int(item) > 0
        }
        existing_frames = self._active_storyboard_frames_for_chapter(project.id, chapter) if requested_targets else {}
        frames: list[dict[str, Any]] = []
        raw_outputs: list[dict[str, Any]] = []
        aggregated_usage: dict[str, Any] = {}
        total_estimated_cost = 0.0
        generated_frame_count = 0
        render_model = self._preferred_storyboard_model_for_run(project, chapter, provider, model, params)
        system = (
            f"{system_prompt}\n"
            "你现在是电影分镜美术师。每次只返回一张真实图片，不要返回 markdown、JSON、镜头列表或文字解释。"
        )
        for shot in shots:
            shot_index = max(1, int(shot.get("shot_index") or len(frames) + 1))
            if requested_targets and shot_index not in requested_targets and shot_index in existing_frames:
                reused_frame = deepcopy(existing_frames[shot_index])
                reused_frame["reused_from_previous_version"] = True
                frames.append(reused_frame)
                raw_outputs.append(
                    {
                        "shot_index": shot_index,
                        "reused": True,
                        "provider_output": {"storage_key": reused_frame.get("storage_key"), "model": reused_frame.get("model")},
                    }
                )
                continue
            image_prompt = self._build_storyboard_image_prompt(
                project,
                chapter,
                shot,
                task_prompt,
                style_directive,
                auto_revision_prompt=str(params.get("auto_revision_prompt") or "").strip() or None,
            )
            reference_images = self._story_bible_reference_images_for_shot(project, shot, chapter=chapter)
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
                primary_model=render_model,
                system_prompt=system,
                image_prompt=image_prompt,
                request_params=request_params,
                shot_index=shot_index,
            )
            render_model = used_model or render_model
            total_estimated_cost += cost
            aggregated_usage = self._merge_usage_metrics(aggregated_usage, frame_response.usage)
            generated_frame_count += 1
            frame_asset = self._materialize_storyboard_frame_asset(project.id, chapter, step, shot_index, frame_artifact)
            raw_outputs.append(
                {
                    "shot_index": shot_index,
                    "prompt": image_prompt,
                    "used_model": used_model,
                    "provider_output": frame_artifact,
                    "usage": frame_response.usage,
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
                    "characters": list(shot.get("characters") or []) if isinstance(shot.get("characters"), list) else [],
                    "scene": str(shot.get("scene") or ""),
                    "scene_hint": str(shot.get("scene_hint") or ""),
                    "continuity_anchor": str(shot.get("continuity_anchor") or ""),
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
            "model": render_model,
            "artifact_mode": "real_storyboard_frames",
            "summary": (
                f"已定向重绘 {generated_frame_count} 张低分镜头，并复用 {len(frames) - generated_frame_count} 张既有分镜图。"
                if requested_targets and generated_frame_count < len(frames)
                else f"已真实生成当前章节 {len(frames)} 张分镜图。"
            ),
            "frame_count": len(frames),
            "generated_frame_count": generated_frame_count,
            "reused_frame_count": len(frames) - generated_frame_count,
            "frames": frames,
            "image_url": frames[0]["image_url"],
            "thumbnail_url": frames[0]["thumbnail_url"],
            "cover_image_url": frames[0]["image_url"],
            "storage_key": frames[0]["storage_key"],
        }
        aggregated_usage["frame_count"] = len(frames)
        aggregated_usage["generated_frame_count"] = generated_frame_count
        aggregated_usage["reused_frame_count"] = len(frames) - generated_frame_count
        aggregated_usage["request_count"] = generated_frame_count
        return ProviderResponse(output=artifact, usage=aggregated_usage, raw={"frames": raw_outputs}), total_estimated_cost

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
        reference_images = self._story_bible_reference_images_for_chapter(project, chapter=chapter)
        story_bible = normalize_style_profile(project.style_profile).get("story_bible", {})
        reference_paths = [
            storage_key
            for group_name, group in (
                ("characters", story_bible.get("characters", []) if isinstance(story_bible, dict) else []),
                ("scenes", story_bible.get("scenes", []) if isinstance(story_bible, dict) else []),
            )
            for item in group
            if isinstance(item, dict)
            for storage_key, _ in [self._story_bible_entity_reference_fields(group_name, item)]
            if storage_key
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
        estimated_cost = await adapter.estimate_cost(req, response.usage)
        return response, estimated_cost

    async def _invoke_stitch_subtitle_tts_step(
        self,
        project: Project,
        step: PipelineStep,
        adapter: Any,
        provider: str,
        model: str,
        system_prompt: str,
        task_prompt: str,
        style_directive: str,
        params: dict[str, Any],
    ) -> tuple[ProviderResponse, float]:
        manifest = self._build_final_cut_segment_manifest(project)
        if not manifest:
            raise ValueError("no approved chapter video segments available for final cut")
        voice = str(params.get("voice") or "alloy")
        target_duration_sec = round(
            sum(max(float(item.get("duration_sec") or 0.0), 0.0) for item in manifest),
            3,
        )
        aggregated_usage: dict[str, Any] = {}
        total_estimated_cost = 0.0
        narration_generation_mode = "heuristic"
        narration_writer_provider = ""
        narration_writer_model = ""
        segment_lines: list[dict[str, Any]] | None = None
        try:
            (
                segment_lines,
                writer_usage,
                writer_cost,
                narration_writer_provider,
                narration_writer_model,
                narration_generation_mode,
            ) = (
                await self._generate_final_cut_narration_with_model(
                    project,
                    manifest=manifest,
                    task_prompt=task_prompt,
                    style_directive=style_directive,
                )
            )
            aggregated_usage = self._merge_usage_metrics(aggregated_usage, writer_usage)
            total_estimated_cost += writer_cost
        except Exception:
            segment_lines = None

        plan = self._build_final_cut_narration_plan(
            project,
            manifest=manifest,
            voice=voice,
            segment_lines=segment_lines,
        )
        narration_text = str(plan.get("narration_text") or "").strip()
        if not narration_text:
            raise ValueError("final cut narration plan is empty")

        async def invoke_tts(speed: float) -> tuple[ProviderResponse, float, float | None]:
            req = ProviderRequest(
                step="tts",
                model=model,
                input={
                    "tts_text": narration_text,
                    "segment_manifest": manifest,
                    "subtitle_entries": plan["subtitle_entries"],
                    "project_name": project.name,
                },
                prompt=(
                    f"{system_prompt}\n{task_prompt}\n{style_directive}\n"
                    "请只朗读旁白正文，使用自然、克制、电影预告片式的中文旁白语气，不要读出 JSON、路径或字段名。"
                ),
                params={
                    **params,
                    "voice": voice,
                    "speed": speed,
                    "tts_text": narration_text,
                    "format": params.get("format") or "mp3",
                    "instructions": params.get("instructions") or "Calm Mandarin narration for cinematic storytelling.",
                },
            )
            response = await adapter.invoke(req)
            estimated_cost = await adapter.estimate_cost(req, response.usage)
            duration = self._probe_audio_duration_from_artifact(deepcopy(response.output or {}))
            return response, estimated_cost, duration

        try:
            requested_speed = float(params.get("speed") or 1.0)
        except (TypeError, ValueError):
            requested_speed = 1.0
        response, estimated_cost, spoken_duration_sec = await invoke_tts(requested_speed)
        aggregated_usage = self._merge_usage_metrics(aggregated_usage, response.usage)
        total_estimated_cost += estimated_cost
        if target_duration_sec > 0 and spoken_duration_sec and abs(spoken_duration_sec - target_duration_sec) > max(6.0, target_duration_sec * 0.05):
            corrected_speed = max(0.55, min(1.35, requested_speed * (spoken_duration_sec / target_duration_sec)))
            if abs(corrected_speed - requested_speed) >= 0.03:
                retry_response, retry_cost, retry_duration = await invoke_tts(corrected_speed)
                response = retry_response
                spoken_duration_sec = retry_duration
                requested_speed = corrected_speed
                aggregated_usage = self._merge_usage_metrics(aggregated_usage, retry_response.usage)
                total_estimated_cost += retry_cost
        artifact = deepcopy(response.output or {})
        artifact.update(
            {
                "provider": artifact.get("provider") or provider,
                "step": artifact.get("step") or "tts",
                "model": artifact.get("model") or model,
                "segment_manifest": manifest,
                "narration_text": narration_text,
                "subtitle_entries": plan["subtitle_entries"],
                "subtitle_format": "srt",
                "chapter_count": plan["chapter_count"],
                "segment_count": plan["segment_count"],
                "voice": voice,
                "tts_speed": round(requested_speed, 3),
                "target_duration_sec": target_duration_sec,
                "spoken_audio_duration_sec": round(spoken_duration_sec, 3) if spoken_duration_sec else None,
                "narration_generation_mode": narration_generation_mode,
                "narration_writer_provider": narration_writer_provider,
                "narration_writer_model": narration_writer_model,
                "summary": str(artifact.get("summary") or f"已生成 {plan['segment_count']} 个章节片段的旁白与字幕方案。"),
            }
        )
        return (
            ProviderResponse(
                output=artifact,
                usage=aggregated_usage,
                raw={
                    "tts": response.raw,
                    "tts_speed": requested_speed,
                    "spoken_audio_duration_sec": spoken_duration_sec,
                    "target_duration_sec": target_duration_sec,
                    "narration_generation_mode": narration_generation_mode,
                    "narration_writer_provider": narration_writer_provider,
                    "narration_writer_model": narration_writer_model,
                },
            ),
            total_estimated_cost,
        )

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
                    raise ValueError(self._storyboard_missing_image_reason(response, artifact, shot_index))
                cost = await adapter.estimate_cost(req, response.usage)
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
            "openai/gpt-5-image-mini",
            "google/gemini-3.1-flash-image-preview",
            "google/gemini-3-pro-image-preview",
            "openai/gpt-5-image",
        ]
        candidates: list[str] = []
        for item in preferred:
            if item in available and item not in candidates:
                candidates.append(item)
        if not candidates:
            candidates.append(primary_model)
        return candidates[:STORYBOARD_IMAGE_FALLBACK_LIMIT]

    def _build_storyboard_image_prompt(
        self,
        project: Project,
        chapter: ChapterChunk,
        shot: dict[str, Any],
        task_prompt: str,
        style_directive: str,
        auto_revision_prompt: str | None = None,
    ) -> str:
        title = str((chapter.meta or {}).get("title") or f"章节 {chapter.chapter_index + 1}")
        summary = str((chapter.meta or {}).get("summary") or chapter.content[:160])
        story_bible = normalize_style_profile(project.style_profile).get("story_bible", {})
        visual_style = story_bible.get("visual_style", {}) if isinstance(story_bible, dict) else {}
        keywords = ", ".join(visual_style.get("keywords", [])) if isinstance(visual_style, dict) else ""
        palette = ", ".join(visual_style.get("palette", [])) if isinstance(visual_style, dict) else ""
        compact_style = ", ".join(
            part
            for part in [
                str(visual_style.get("rendering") or "").strip(),
                str(visual_style.get("lighting") or "").strip(),
                str(visual_style.get("camera_language") or "").strip(),
                keywords,
                palette,
                str(visual_style.get("custom_style") or "").strip(),
                str(visual_style.get("custom_directives") or "").strip(),
            ]
            if part
        )
        related_characters = self._story_bible_entities_for_prompt(
            story_bible.get("characters"),
            shot,
            chapter=chapter,
            allow_fallback=True,
        )
        related_scenes = self._story_bible_entities_for_prompt(
            story_bible.get("scenes"),
            shot,
            chapter=chapter,
            allow_fallback=False,
        )
        primary_characters = ", ".join(str(item) for item in (shot.get("characters") or []) if str(item).strip())
        primary_scene = str(shot.get("scene_hint") or shot.get("scene") or "").strip()
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
        safety_context = self._storyboard_image_safety_context(summary, shot)
        safe_summary = self._sanitize_storyboard_image_text(summary, field="summary")
        safe_visual = self._sanitize_storyboard_image_text(str(shot.get("visual") or summary), field="visual")
        safe_action = self._sanitize_storyboard_image_text(str(shot.get("action") or ""), field="action")
        safe_dialogue = self._sanitize_storyboard_image_text(str(shot.get("dialogue") or ""), field="dialogue")
        if safety_context["sexual_content_softened"]:
            constraints.extend(
                [
                    "PG-13 framing",
                    "fully clothed adult characters",
                    "implied intimacy only",
                    "no nudity",
                    "no lingerie emphasis",
                    "no explicit sexual content",
                ]
            )
        lines = [
            f"Chapter: {title}",
            f"Chapter summary: {safe_summary}",
            f"Shot {int(shot.get('shot_index') or 1)}",
            f"Visual style keywords: {keywords}",
            f"Palette: {palette}",
            f"Rendering: {visual_style.get('rendering') if isinstance(visual_style, dict) else '写实电影画面'}",
            f"Lighting: {visual_style.get('lighting') if isinstance(visual_style, dict) else ''}",
            f"Camera language: {visual_style.get('camera_language') if isinstance(visual_style, dict) else ''}",
            f"Scene description: {safe_visual}",
            f"Character action: {safe_action}",
            f"Dialogue context: {safe_dialogue}",
            f"Shot type: {shot.get('frame_type') or '中景'}",
            f"Primary characters in this shot: {primary_characters}",
            f"Primary scene in this shot: {primary_scene}",
            f"Character reference anchors: {related_characters}",
            f"Scene reference anchors: {related_scenes}",
            f"Continuity anchor: {shot.get('continuity_anchor') or ''}",
            f"User image directive: {task_prompt}",
            f"Consistency correction hint: {auto_revision_prompt or ''}",
            f"Compact style directive: {compact_style or style_directive}",
            f"Hard constraints: {', '.join(constraints)}",
        ]
        return "\n".join(line for line in lines if line and not line.endswith(": "))

    def _storyboard_image_safety_context(self, summary: str, shot: dict[str, Any]) -> dict[str, Any]:
        text = " ".join(
            [
                summary,
                str(shot.get("visual") or ""),
                str(shot.get("action") or ""),
                str(shot.get("dialogue") or ""),
            ]
        )
        return {
            "sexual_content_softened": self._has_storyboard_image_sexual_risk(text),
        }

    def _has_storyboard_image_sexual_risk(self, text: str) -> bool:
        normalized = text.strip().lower()
        if not normalized:
            return False
        return any(pattern.search(normalized) for pattern in STORYBOARD_IMAGE_SEXUAL_RISK_PATTERNS)

    def _sanitize_storyboard_image_text(self, text: str, *, field: str) -> str:
        value = " ".join(text.split())
        if not value:
            return ""
        if not self._has_storyboard_image_sexual_risk(value):
            return value

        has_children_context = any(token in value for token in ("孩子", "孩子们", "child", "children"))
        has_doorway_context = any(token in value for token in ("门口", "门前", "门廊", "door", "threshold"))
        has_embrace_context = any(token in value for token in ("搂", "拥抱", "抱住", "hug", "embrace"))

        if field == "summary":
            summary = "章节开场为成年角色在家庭空间中的亲密重逢，整体表达含蓄克制，采用 PG-13 电影化构图，避免裸露与内衣特写。"
            if has_children_context:
                summary += " 情节信息包含孩子暂由他人照看。"
            return summary
        if field == "visual":
            visual = "成年角色在"
            visual += "家门口" if has_doorway_context else "私密家庭空间"
            visual += "重逢，气氛亲密但克制，穿着完整居家服，不出现裸露或内衣焦点。"
            if has_embrace_context:
                visual += " 构图可表现轻微靠近或克制拥抱。"
            return visual
        if field == "action":
            action = "角色靠近并进行克制的亲密互动，以表情和姿态传达情绪，不表现裸露或明确性暗示。"
            if has_embrace_context:
                action += " 可保留轻拥动作。"
            return action
        if field == "dialogue":
            dialogue = "对话强调家庭关系与私人相处时段，保持含蓄表达。"
            if has_children_context:
                dialogue += " 可体现孩子暂由他人照看这一信息。"
            return dialogue
        return value

    def _storyboard_missing_image_reason(self, response: ProviderResponse, artifact: dict[str, Any], shot_index: int) -> str:
        artifact_error = artifact.get("error_message")
        if isinstance(artifact_error, str) and artifact_error.strip():
            return f"storyboard_image model rejected shot {shot_index}: {artifact_error.strip()}"
        artifact_text = artifact.get("text")
        if isinstance(artifact_text, str) and artifact_text.strip():
            return f"storyboard_image model did not return an image for shot {shot_index}: {artifact_text.strip()}"
        raw = response.raw if isinstance(response.raw, dict) else {}
        choices = raw.get("choices") if isinstance(raw, dict) else None
        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            choice_error = first.get("error")
            if isinstance(choice_error, dict):
                message = choice_error.get("message")
                if isinstance(message, str) and message.strip():
                    return f"storyboard_image model rejected shot {shot_index}: {message.strip()}"
            message = first.get("message")
            if isinstance(message, dict):
                refusal = message.get("refusal")
                if isinstance(refusal, str) and refusal.strip():
                    return f"storyboard_image model rejected shot {shot_index}: {refusal.strip()}"
        return f"storyboard_image did not return a real image for shot {shot_index}"

    def _story_bible_entities_for_prompt(
        self,
        items: Any,
        shot: dict[str, Any],
        *,
        chapter: ChapterChunk | None = None,
        allow_fallback: bool = False,
    ) -> str:
        if not isinstance(items, list):
            return ""
        shot_text = self._story_bible_matching_text(shot)
        chosen = self._select_relevant_story_bible_entities(
            items,
            shot_text,
            chapter=chapter,
            limit=3,
            allow_fallback=allow_fallback,
        )
        return " | ".join(chosen)

    def _anchor_tokens(self, text: str) -> list[str]:
        return [
            token
            for token in re.findall(r"[A-Za-z]{2,}|[\u4e00-\u9fff]{2,6}", text.lower())
            if token not in {"保持", "一致", "稳定", "角色", "场景", "镜头", "光线", "空间", "服装"}
        ][:8]

    def _story_bible_matching_text(self, payload: dict[str, Any]) -> str:
        parts: list[str] = [
            str(payload.get("visual") or ""),
            str(payload.get("action") or ""),
            str(payload.get("dialogue") or ""),
            str(payload.get("summary") or ""),
            str(payload.get("scene") or ""),
            str(payload.get("scene_hint") or ""),
            str(payload.get("location") or ""),
            str(payload.get("setting") or ""),
            str(payload.get("continuity_anchor") or ""),
        ]
        characters = payload.get("characters") or payload.get("character_names") or []
        if isinstance(characters, list):
            parts.extend(str(item) for item in characters if str(item).strip())
        return " ".join(part for part in parts if part).lower()

    def _chapter_story_bible_matching_text(
        self,
        chapter: ChapterChunk | None,
        frames: list[dict[str, Any]] | None = None,
        neighbor_frames: list[dict[str, Any]] | None = None,
    ) -> str:
        if chapter is None:
            return ""
        meta = chapter.meta or {}
        parts = [
            str(meta.get("title") or ""),
            str(meta.get("summary") or ""),
            self._chapter_body_text(chapter)[:1200],
        ]
        for frame in frames or []:
            parts.append(self._story_bible_matching_text(frame))
        for frame in neighbor_frames or []:
            parts.append(self._story_bible_matching_text(frame))
        return " ".join(part for part in parts if part).lower()

    def _story_bible_entity_label(self, raw: dict[str, Any]) -> str:
        name = str(raw.get("name") or "").strip()
        description = str(raw.get("visual_anchor") or raw.get("description") or "").strip()
        return f"{name}: {description}" if description else name

    def _story_bible_entity_match_score(
        self,
        raw: dict[str, Any],
        text: str,
        *,
        chapter: ChapterChunk | None = None,
        include_chapter_bonus: bool = True,
    ) -> int:
        name = str(raw.get("name") or "").strip().lower()
        if not name:
            return 0
        score = 0
        if chapter is not None and include_chapter_bonus:
            chapter_ids = [str(item) for item in raw.get("chapter_ids", []) if str(item).strip()]
            if chapter.id in chapter_ids:
                score += 12
            title = str((chapter.meta or {}).get("title") or "").strip()
            chapter_titles = [str(item).strip() for item in raw.get("chapter_titles", []) if str(item).strip()]
            if title and title in chapter_titles:
                score += 8
        if name in text:
            score += 10
        aliases = [str(item).strip().lower() for item in raw.get("aliases", []) if str(item).strip()]
        for alias in aliases:
            if alias and alias in text:
                score += 6
        searchable = " ".join(
            [
                str(raw.get("name") or ""),
                str(raw.get("description") or ""),
                str(raw.get("visual_anchor") or ""),
                " ".join(str(item) for item in raw.get("chapter_titles", []) if str(item).strip()),
            ]
        )
        for token in self._anchor_tokens(searchable):
            if token in text:
                score += 2
        occurrence = int(raw.get("occurrence_count") or 0)
        if score > 0:
            score += min(3, occurrence // 40)
        return score

    def _select_relevant_story_bible_entities(
        self,
        items: Any,
        text: str,
        *,
        chapter: ChapterChunk | None = None,
        limit: int,
        allow_fallback: bool,
        include_chapter_bonus: bool = True,
    ) -> list[str]:
        if not isinstance(items, list) or limit <= 0:
            return []
        matched: list[tuple[int, int, str]] = []
        fallback: list[tuple[int, str]] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            label = self._story_bible_entity_label(raw)
            if not label.strip():
                continue
            occurrence = int(raw.get("occurrence_count") or 0)
            score = self._story_bible_entity_match_score(raw, text, chapter=chapter, include_chapter_bonus=include_chapter_bonus)
            fallback.append((occurrence, label))
            if score > 0:
                matched.append((score, occurrence, label))
        if matched:
            matched.sort(key=lambda item: (-item[0], -item[1], item[2]))
            return [item[2] for item in matched[:limit]]
        if allow_fallback:
            fallback.sort(key=lambda item: (-item[0], item[1]))
            return [item[1] for item in fallback[:limit]]
        return []

    def _extract_shot_entities(
        self,
        items: Any,
        text: str,
        *,
        chapter: ChapterChunk | None = None,
        limit: int,
        include_chapter_bonus: bool = False,
    ) -> list[str]:
        if not isinstance(items, list):
            return []
        matches: list[tuple[int, int, str]] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            score = self._story_bible_entity_match_score(raw, text, chapter=chapter, include_chapter_bonus=include_chapter_bonus)
            if score <= 0:
                continue
            occurrence = int(raw.get("occurrence_count") or 0)
            matches.append((score, occurrence, name))
        matches.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return [item[2] for item in matches[:limit]]

    def _story_bible_entity_by_name(
        self,
        story_bible: Any,
        group: str,
        name: str,
    ) -> dict[str, Any] | None:
        if not isinstance(story_bible, dict) or not name:
            return None
        for raw in story_bible.get(group, []) or []:
            if not isinstance(raw, dict):
                continue
            if str(raw.get("name") or "").strip() == name:
                return raw
        return None

    def _story_bible_entity_reference_fields(self, group: str, raw: dict[str, Any]) -> tuple[str | None, str | None]:
        if group == "characters":
            storage_key = raw.get("identity_reference_storage_key") or raw.get("reference_storage_key")
            image_url = raw.get("identity_reference_image_url") or raw.get("reference_image_url")
            return (
                str(storage_key).strip() if isinstance(storage_key, str) and str(storage_key).strip() else None,
                str(image_url).strip() if isinstance(image_url, str) and str(image_url).strip() else None,
            )
        storage_key = raw.get("scene_reference_storage_key") or raw.get("reference_storage_key")
        image_url = raw.get("scene_reference_image_url") or raw.get("reference_image_url")
        return (
            str(storage_key).strip() if isinstance(storage_key, str) and str(storage_key).strip() else None,
            str(image_url).strip() if isinstance(image_url, str) and str(image_url).strip() else None,
        )

    def _story_bible_reference_images_for_shot(
        self,
        project: Project,
        shot: dict[str, Any],
        *,
        chapter: ChapterChunk | None = None,
    ) -> list[dict[str, Any]]:
        story_bible = normalize_style_profile(project.style_profile).get("story_bible", {})
        shot_text = self._story_bible_matching_text(shot)
        selected: list[dict[str, Any]] = []
        for group in ("characters", "scenes"):
            allow_fallback = False
            candidates = self._extract_shot_entities(
                story_bible.get(group, []) if isinstance(story_bible, dict) else [],
                shot_text,
                chapter=chapter,
                limit=STORYBOARD_REFERENCE_IMAGE_LIMIT,
            )
            for raw in (story_bible.get(group, []) if isinstance(story_bible, dict) else []):
                if not isinstance(raw, dict):
                    continue
                name = str(raw.get("name") or "").strip()
                if not name or name not in candidates:
                    continue
                storage_key, image_url = self._story_bible_entity_reference_fields(group, raw)
                data_url = self._reference_image_data_url(storage_key, image_url, variant="portrait" if group == "characters" else "full")
                if data_url:
                    selected.append({"url": data_url, "label": f"{group}:{name}"})
        if selected:
            return selected[:STORYBOARD_REFERENCE_IMAGE_LIMIT]
        return self._story_bible_reference_images_for_chapter(project, chapter=chapter)[:STORYBOARD_REFERENCE_IMAGE_LIMIT]

    def _story_bible_reference_images_for_chapter(
        self,
        project: Project,
        chapter: ChapterChunk | None = None,
    ) -> list[dict[str, Any]]:
        story_bible = normalize_style_profile(project.style_profile).get("story_bible", {})
        selected: list[dict[str, Any]] = []
        chapter_text = self._chapter_story_bible_matching_text(chapter)
        for group in ("characters", "scenes"):
            allow_fallback = group == "characters"
            chosen = set(
                self._extract_shot_entities(
                    story_bible.get(group, []) if isinstance(story_bible, dict) else [],
                    chapter_text,
                    chapter=chapter,
                    limit=STORYBOARD_REFERENCE_IMAGE_LIMIT,
                )
            )
            if not chosen and allow_fallback:
                chosen = {
                    item.split(":", 1)[0]
                    for item in self._select_relevant_story_bible_entities(
                        story_bible.get(group, []) if isinstance(story_bible, dict) else [],
                        chapter_text,
                        chapter=chapter,
                        limit=STORYBOARD_REFERENCE_IMAGE_LIMIT,
                        allow_fallback=True,
                    )
                }
            for raw in (story_bible.get(group, []) if isinstance(story_bible, dict) else []):
                if not isinstance(raw, dict):
                    continue
                name = str(raw.get("name") or "").strip()
                if chosen and name not in chosen:
                    continue
                storage_key, image_url = self._story_bible_entity_reference_fields(group, raw)
                data_url = self._reference_image_data_url(storage_key, image_url, variant="portrait" if group == "characters" else "full")
                if data_url:
                    selected.append({"url": data_url, "label": f"{group}:{name}"})
        return selected[:STORYBOARD_REFERENCE_IMAGE_LIMIT]

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
