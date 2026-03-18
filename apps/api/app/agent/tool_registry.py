from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from workflow_engine import PIPELINE_STEPS

from app.db.models import Project
from app.services.pipeline_service import PipelineService

CHAPTER_SCOPED_STEPS = {
    "story_scripting",
    "shot_detailing",
    "storyboard_image",
    "consistency_check",
    "segment_video",
}

STEP_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("分镜图", "出图", "画面图", "storyboard image"), "storyboard_image"),
    (("章节剧本", "剧情节拍", "情节点", "beat"), "story_scripting"),
    (("分镜", "镜头", "shot", "镜头语言"), "shot_detailing"),
    (("一致性", "校核", "校对", "评分"), "consistency_check"),
    (("片段视频", "视频片段", "视频"), "segment_video"),
    (("章节切分", "切分章节", "切章节"), "chapter_chunking"),
    (("导入全文", "解析全文", "全文解析"), "ingest_parse"),
)

REBUILD_KEYWORDS = ("重建", "重做", "重新生成", "重新构建", "重刷", "修正")
EXECUTE_KEYWORDS = ("运行", "执行", "重跑", "提交重跑", "提交", "重生成", "重新生成", "rerun")
PROMPT_KEYWORDS = ("提示词", "prompt")
STORY_BIBLE_KEYWORDS = ("story bible", "故事圣经", "圣经", "角色卡", "场景卡")
MODEL_SWITCH_KEYWORDS = ("切换模型", "换模型", "改模型", "换成模型", "切到", "切换到")
ALL_CHAPTER_KEYWORDS = ("所有章节", "全部章节", "全章节", "整本", "批量", "所有镜头", "全部镜头")
FAILED_CHAPTER_KEYWORDS = ("失败章节", "出错章节", "失败的章节")
FEEDBACK_KEYWORDS = (
    "优化",
    "改进",
    "加强",
    "增强",
    "修正",
    "调整",
    "批评",
    "意见",
    "不足",
    "不够",
    "太",
    "过于",
    "偏",
    "不准",
    "不准确",
    "不一致",
    "单调",
    "拖沓",
    "平淡",
    "加强冲突",
    "加强节奏",
)
PROMPT_VALUE_MARKERS = ("改成", "修改为", "改为", "设为", "设成", "写成", "写为", "强调", "加入", "补充")

STEP_NAME_BY_LABEL: dict[str, str] = {}
STEP_LABEL_BY_NAME: dict[str, str] = {}
for step in PIPELINE_STEPS:
    STEP_NAME_BY_LABEL[step.step_name] = step.step_name
    STEP_NAME_BY_LABEL[step.step_name.lower()] = step.step_name
    STEP_NAME_BY_LABEL[step.display_name] = step.step_name
    STEP_NAME_BY_LABEL[step.display_name.lower()] = step.step_name
    STEP_LABEL_BY_NAME[step.step_name] = step.display_name


@dataclass(slots=True)
class PlannedToolAction:
    tool_name: str
    display_name: str
    args: dict[str, Any]
    scope_summary: str
    ready: bool
    missing_fields: list[str]
    user_visible_summary: str
    estimated_cost: float | None = None
    estimated_cost_summary: str | None = None
    cost_source: str | None = None
    prompt_preview: str | None = None
    feedback_summary: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "display_name": self.display_name,
            "args": self.args,
            "scope_summary": self.scope_summary,
            "ready": self.ready,
            "missing_fields": self.missing_fields,
            "user_visible_summary": self.user_visible_summary,
            "estimated_cost": self.estimated_cost,
            "estimated_cost_summary": self.estimated_cost_summary,
            "cost_source": self.cost_source,
            "prompt_preview": self.prompt_preview,
            "feedback_summary": self.feedback_summary,
        }


class AgentToolRegistry:
    def __init__(self, pipeline: PipelineService) -> None:
        self.pipeline = pipeline

    def plan_write_action(self, *, project: Project, user_text: str, page_context: dict[str, Any]) -> PlannedToolAction | None:
        text = str(user_text or "").strip()
        lowered = text.lower()
        if not text:
            return None

        selected_step_name = self._resolve_step_name(page_context)
        explicit_step_name = self._resolve_step_name_from_text(text)
        step_name = explicit_step_name or selected_step_name
        step_label = STEP_LABEL_BY_NAME.get(step_name or "", str(page_context.get("selected_step_name") or step_name or "-"))
        scope_mode = self._resolve_scope_mode(text)
        chapter = self._resolve_chapter(project.id, text=text, page_context=page_context)
        chapter_id = str(chapter.get("id") or "").strip() or None
        chapter_title = str(chapter.get("title") or page_context.get("selected_chapter_title") or "").strip() or None
        scope_summary = self._scope_summary(step_label, chapter_title, scope_mode=scope_mode)
        shot_index = self._extract_shot_index(text)
        prompt_chapter_title = chapter_title if scope_mode == "single" else None
        prompt_chapter_summary = str(chapter.get("summary") or "") if scope_mode == "single" else ""

        if any(keyword in lowered for keyword in STORY_BIBLE_KEYWORDS) and any(keyword in text for keyword in REBUILD_KEYWORDS):
            return PlannedToolAction(
                tool_name="rebuild_story_bible",
                display_name="重建 Story Bible",
                args={},
                scope_summary="影响当前项目的 Story Bible 引用与角色/场景参考图。",
                ready=True,
                missing_fields=[],
                user_visible_summary="将重新抽取并刷新当前项目的 Story Bible 参考信息。",
                estimated_cost=0.0,
                estimated_cost_summary="当前动作为项目级整理与引用刷新，预计不直接新增模型费用。",
                cost_source="project_story_bible_refresh",
            )

        if any(keyword in text for keyword in MODEL_SWITCH_KEYWORDS):
            provider, model_name = self._extract_model_target(text, step_name=step_name)
            missing_fields: list[str] = []
            if not step_name:
                missing_fields.append("step_name")
            if not provider:
                missing_fields.append("provider")
            if not model_name:
                missing_fields.append("model_name")
            ready = not missing_fields
            estimation = self._estimate_cost(
                project,
                step_name,
                scope_mode=scope_mode,
                chapter_id=chapter_id,
                provider=provider,
                model_name=model_name,
            )
            tool_name = "switch_model_rerun"
            display_name = "切换模型并重跑"
            if scope_mode == "all_chapters":
                tool_name = "switch_model_rerun_all_chapters"
                display_name = "切换模型并批量重跑"
            if scope_mode == "failed_chapters":
                tool_name = "switch_model_rerun_failed_chapters"
                display_name = "切换模型并重跑失败章节"
            return PlannedToolAction(
                tool_name=tool_name,
                display_name=display_name,
                args={
                    "step_name": step_name,
                    "provider": provider,
                    "model_name": model_name,
                    "params": self._build_params(step_name, chapter_id, include_chapter=scope_mode == "single"),
                    "chapter_id": chapter_id,
                },
                scope_summary=scope_summary,
                ready=ready,
                missing_fields=missing_fields,
                user_visible_summary=(
                    f"将把 {scope_summary} 切换到 {provider}/{model_name} 后重新运行。"
                    if ready
                    else "已识别为“切换模型并重跑”，但还缺少目标步骤或模型信息。"
                ),
                estimated_cost=estimation.get("estimated_cost"),
                estimated_cost_summary=estimation.get("summary"),
                cost_source=estimation.get("source"),
            )

        if self._is_feedback_intent(text, step_name=step_name):
            feedback_summary = self._extract_feedback_summary(text)
            prompt_snapshot = self.pipeline.get_active_prompt_snapshot(project.id, step_name) if step_name else {}
            task_prompt = self._compose_improved_prompt(
                step_name=step_name,
                base_prompt=str(prompt_snapshot.get("task_prompt") or ""),
                feedback_summary=feedback_summary,
                chapter_title=prompt_chapter_title,
                chapter_summary=prompt_chapter_summary,
                shot_index=shot_index,
            )
            missing_fields = [] if step_name else ["step_name"]
            if not task_prompt:
                missing_fields.append("task_prompt")
            ready = not missing_fields
            estimation = self._estimate_cost(project, step_name, scope_mode=scope_mode, chapter_id=chapter_id)
            tool_name = {
                "all_chapters": "refine_prompt_rerun_all_chapters",
                "failed_chapters": "refine_prompt_rerun_failed_chapters",
            }.get(scope_mode, "refine_prompt_rerun")
            return PlannedToolAction(
                tool_name=tool_name,
                display_name="根据批评意见改写提示词并重跑",
                args={
                    "step_name": step_name,
                    "task_prompt": task_prompt,
                    "system_prompt": prompt_snapshot.get("system_prompt"),
                    "params": self._build_params(step_name, chapter_id, include_chapter=scope_mode == "single"),
                    "chapter_id": chapter_id,
                    "feedback_summary": feedback_summary,
                    "shot_index": shot_index,
                    "scope_type": "chapter" if chapter_id else "step",
                },
                scope_summary=scope_summary,
                ready=ready,
                missing_fields=missing_fields,
                user_visible_summary=(
                    f"将基于你的批评意见自动补强 {scope_summary} 的提示词，再提交重跑。"
                    if ready
                    else "已识别为“根据批评意见重跑”，但还缺少目标步骤。"
                ),
                estimated_cost=estimation.get("estimated_cost"),
                estimated_cost_summary=estimation.get("summary"),
                cost_source=estimation.get("source"),
                prompt_preview=task_prompt[:280] if task_prompt else None,
                feedback_summary=feedback_summary,
            )

        if any(keyword in lowered for keyword in PROMPT_KEYWORDS):
            task_prompt = self._extract_task_prompt(text)
            missing_fields: list[str] = []
            if not step_name:
                missing_fields.append("step_name")
            if not task_prompt:
                missing_fields.append("task_prompt")
            ready = not missing_fields
            prompt_snapshot = self.pipeline.get_active_prompt_snapshot(project.id, step_name) if step_name else {}
            estimation = self._estimate_cost(project, step_name, scope_mode=scope_mode, chapter_id=chapter_id)
            tool_name = {
                "all_chapters": "refine_prompt_rerun_all_chapters",
                "failed_chapters": "refine_prompt_rerun_failed_chapters",
            }.get(scope_mode, "edit_prompt_regenerate")
            display_name = {
                "all_chapters": "修改提示词并批量重生成",
                "failed_chapters": "修改提示词并重跑失败章节",
            }.get(scope_mode, "修改提示词并重生成")
            return PlannedToolAction(
                tool_name=tool_name,
                display_name=display_name,
                args={
                    "step_name": step_name,
                    "task_prompt": task_prompt,
                    "system_prompt": prompt_snapshot.get("system_prompt"),
                    "params": self._build_params(step_name, chapter_id, include_chapter=scope_mode == "single"),
                    "chapter_id": chapter_id,
                    "scope_type": "chapter" if chapter_id else "step",
                },
                scope_summary=scope_summary,
                ready=ready,
                missing_fields=missing_fields,
                user_visible_summary=(
                    f"将对 {scope_summary} 更新提示词后重新运行。"
                    if ready
                    else "已识别为“修改提示词并重生成”，但还缺少明确的新提示词或目标步骤。"
                ),
                estimated_cost=estimation.get("estimated_cost"),
                estimated_cost_summary=estimation.get("summary"),
                cost_source=estimation.get("source"),
                prompt_preview=task_prompt[:280] if task_prompt else None,
            )

        if any(keyword in text for keyword in EXECUTE_KEYWORDS):
            missing_fields = [] if step_name else ["step_name"]
            ready = not missing_fields
            estimation = self._estimate_cost(project, step_name, scope_mode=scope_mode, chapter_id=chapter_id)
            tool_name = {
                "all_chapters": "run_step_all_chapters",
                "failed_chapters": "run_step_failed_chapters",
            }.get(scope_mode, "run_step")
            display_name = {
                "all_chapters": "批量运行步骤",
                "failed_chapters": "运行失败章节",
            }.get(scope_mode, "运行步骤")
            return PlannedToolAction(
                tool_name=tool_name,
                display_name=display_name,
                args={
                    "step_name": step_name,
                    "force": True,
                    "params": self._build_params(step_name, chapter_id, include_chapter=scope_mode == "single"),
                    "chapter_id": chapter_id,
                },
                scope_summary=scope_summary,
                ready=ready,
                missing_fields=missing_fields,
                user_visible_summary=(
                    f"将运行 {scope_summary}。"
                    if ready
                    else "已识别为“运行步骤”，但还缺少明确的目标步骤。"
                ),
                estimated_cost=estimation.get("estimated_cost"),
                estimated_cost_summary=estimation.get("summary"),
                cost_source=estimation.get("source"),
            )

        return None

    async def execute(self, *, project: Project, action: PlannedToolAction) -> dict[str, Any]:
        tool_name = action.tool_name
        args = dict(action.args)

        if tool_name == "rebuild_story_bible":
            updated = await self.pipeline.rebuild_story_bible_references(project)
            story_bible = ((updated.style_profile or {}).get("story_bible") or {}) if isinstance(updated.style_profile, dict) else {}
            characters = story_bible.get("characters", []) if isinstance(story_bible, dict) else []
            scenes = story_bible.get("scenes", []) if isinstance(story_bible, dict) else []
            return {
                "summary": f"已重建 Story Bible，当前角色锚点 {len(characters)} 个，场景锚点 {len(scenes)} 个。",
                "result": {
                    "project_status": updated.status,
                    "character_count": len(characters),
                    "scene_count": len(scenes),
                },
            }

        if tool_name == "run_step":
            step = await self.pipeline.run_specific_step(
                project,
                self._require_step_name(args),
                force=bool(args.get("force", True)),
                params=self._build_execution_params(args),
            )
            return self._step_result("已触发步骤", step)

        if tool_name == "run_step_all_chapters":
            result = await self.pipeline.run_step_for_all_chapters(
                project,
                self._require_step_name(args),
                force=bool(args.get("force", True)),
                params=dict(args.get("params") or {}),
            )
            return self._batch_result("已批量运行当前阶段。", result)

        if tool_name == "run_step_failed_chapters":
            result = await self.pipeline.run_step_for_failed_chapters(
                project,
                self._require_step_name(args),
                force=bool(args.get("force", True)),
                params=dict(args.get("params") or {}),
            )
            return self._batch_result("已对失败章节批量重跑当前阶段。", result)

        if tool_name == "switch_model_rerun":
            step = self._resolve_step(project.id, self._require_step_name(args))
            rerun = await self.pipeline.switch_model_rerun(
                project,
                step.id,
                {
                    "scope_type": "chapter" if args.get("chapter_id") else "step",
                    "created_by": "filmit-agent",
                    "comment": "approved by user via FilmIt Agent",
                    "provider": str(args.get("provider") or "").strip(),
                    "model_name": str(args.get("model_name") or "").strip(),
                    "params": self._build_execution_params(args),
                },
            )
            return self._step_result("已切换模型并重跑", rerun)

        if tool_name == "switch_model_rerun_all_chapters":
            step = self._resolve_step(project.id, self._require_step_name(args))
            result = await self.pipeline.switch_model_rerun_for_all_chapters(
                project,
                step.id,
                {
                    "scope_type": "chapter",
                    "created_by": "filmit-agent",
                    "comment": "approved by user via FilmIt Agent",
                    "provider": str(args.get("provider") or "").strip(),
                    "model_name": str(args.get("model_name") or "").strip(),
                    "params": dict(args.get("params") or {}),
                },
            )
            return self._batch_result("已切换模型并对所有章节重跑。", result)

        if tool_name == "switch_model_rerun_failed_chapters":
            step = self._resolve_step(project.id, self._require_step_name(args))
            result = await self.pipeline.switch_model_rerun_failed_chapters(
                project,
                step.id,
                {
                    "scope_type": "chapter",
                    "created_by": "filmit-agent",
                    "comment": "approved by user via FilmIt Agent",
                    "provider": str(args.get("provider") or "").strip(),
                    "model_name": str(args.get("model_name") or "").strip(),
                    "params": dict(args.get("params") or {}),
                },
            )
            return self._batch_result("已切换模型并对失败章节重跑。", result)

        if tool_name == "edit_prompt_regenerate":
            step = await self.pipeline.rerun_with_prompt_update(
                project,
                self._require_step_name(args),
                {
                    "scope_type": str(args.get("scope_type") or ("chapter" if args.get("chapter_id") else "step")),
                    "created_by": "filmit-agent",
                    "comment": "approved by user via FilmIt Agent",
                    "action_type": "agent_edit_prompt_regenerate",
                    "task_prompt": str(args.get("task_prompt") or "").strip(),
                    "system_prompt": args.get("system_prompt"),
                    "params": self._build_execution_params(args),
                    "chapter_id": args.get("chapter_id"),
                },
            )
            return self._step_result("已按新提示词重新运行", step, extra={"task_prompt": str(args.get("task_prompt") or "").strip()})

        if tool_name == "refine_prompt_rerun":
            step = await self.pipeline.rerun_with_prompt_update(
                project,
                self._require_step_name(args),
                {
                    "scope_type": str(args.get("scope_type") or ("chapter" if args.get("chapter_id") else "step")),
                    "created_by": "filmit-agent",
                    "comment": "approved by user via FilmIt Agent",
                    "action_type": "agent_prompt_refine_rerun",
                    "task_prompt": str(args.get("task_prompt") or "").strip(),
                    "system_prompt": args.get("system_prompt"),
                    "params": self._build_execution_params(args),
                    "chapter_id": args.get("chapter_id"),
                },
            )
            return self._step_result(
                "已根据批评意见改写提示词并重跑",
                step,
                extra={
                    "task_prompt": str(args.get("task_prompt") or "").strip(),
                    "feedback_summary": str(args.get("feedback_summary") or "").strip(),
                    "shot_index": args.get("shot_index"),
                },
            )

        if tool_name == "refine_prompt_rerun_all_chapters":
            result = await self.pipeline.rerun_all_chapters_with_prompt_update(
                project,
                self._require_step_name(args),
                {
                    "scope_type": "chapter",
                    "created_by": "filmit-agent",
                    "comment": "approved by user via FilmIt Agent",
                    "action_type": "agent_prompt_refine_rerun_all",
                    "task_prompt": str(args.get("task_prompt") or "").strip(),
                    "system_prompt": args.get("system_prompt"),
                    "params": dict(args.get("params") or {}),
                },
            )
            return self._batch_result("已根据批评意见批量改写提示词并重跑。", result)

        if tool_name == "refine_prompt_rerun_failed_chapters":
            result = await self.pipeline.rerun_failed_chapters_with_prompt_update(
                project,
                self._require_step_name(args),
                {
                    "scope_type": "chapter",
                    "created_by": "filmit-agent",
                    "comment": "approved by user via FilmIt Agent",
                    "action_type": "agent_prompt_refine_rerun_failed",
                    "task_prompt": str(args.get("task_prompt") or "").strip(),
                    "system_prompt": args.get("system_prompt"),
                    "params": dict(args.get("params") or {}),
                },
            )
            return self._batch_result("已根据批评意见对失败章节批量改写提示词并重跑。", result)

        raise ValueError(f"unsupported tool: {tool_name}")

    def _step_result(self, summary_prefix: str, step: Any, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {
            "step_id": step.id,
            "step_name": step.step_name,
            "step_display_name": step.step_display_name,
            "status": step.status,
        }
        if extra:
            payload.update(extra)
        return {
            "summary": f"{summary_prefix} {step.step_display_name}，当前状态 {step.status}。",
            "result": payload,
        }

    def _batch_result(self, summary_prefix: str, result: dict[str, Any]) -> dict[str, Any]:
        current_step = result.get("current_step")
        payload = {
            "project_id": result.get("project_id"),
            "step_name": result.get("step_name"),
            "total": result.get("total", 0),
            "succeeded": result.get("succeeded", 0),
            "failed": result.get("failed", 0),
            "skipped": result.get("skipped", 0),
        }
        if current_step is not None:
            payload["current_step_status"] = getattr(current_step, "status", None)
            payload["current_step_name"] = getattr(current_step, "step_name", None)
        return {
            "summary": (
                f"{summary_prefix} 成功 {payload['succeeded']} 项，失败 {payload['failed']} 项，跳过 {payload['skipped']} 项。"
            ),
            "result": payload,
        }

    def _resolve_step(self, project_id: str, step_name: str):
        for step in self.pipeline.list_steps(project_id):
            if step.step_name == step_name:
                return step
        raise ValueError(f"step not found: {step_name}")

    def _resolve_step_name(self, page_context: dict[str, Any]) -> str | None:
        for key in ("selected_step_key", "selected_step_name"):
            raw = str(page_context.get(key) or "").strip()
            if not raw:
                continue
            mapped = STEP_NAME_BY_LABEL.get(raw) or STEP_NAME_BY_LABEL.get(raw.lower())
            if mapped:
                return mapped
        return None

    def _resolve_step_name_from_text(self, text: str) -> str | None:
        lowered = text.lower()
        for keywords, step_name in STEP_HINTS:
            if any(keyword.lower() in lowered for keyword in keywords):
                return step_name
        candidates = sorted(STEP_NAME_BY_LABEL.items(), key=lambda item: len(item[0]), reverse=True)
        for label, step_name in candidates:
            if label and label.lower() in lowered:
                return step_name
        return None

    def _resolve_scope_mode(self, text: str) -> str:
        if any(keyword in text for keyword in FAILED_CHAPTER_KEYWORDS):
            return "failed_chapters"
        if any(keyword in text for keyword in ALL_CHAPTER_KEYWORDS):
            return "all_chapters"
        return "single"

    def _resolve_chapter(self, project_id: str, *, text: str, page_context: dict[str, Any]) -> dict[str, Any] | None:
        chapters = self.pipeline.list_chapters(project_id)
        match = re.search(r"第\s*(\d+)\s*章", text)
        if match:
            chapter_index = max(int(match.group(1)) - 1, 0)
            current = next((chapter for chapter in chapters if int(chapter.get("chapter_index", -1)) == chapter_index), None)
            if current:
                return current

        lowered = text.lower()
        for chapter in chapters:
            title = str(chapter.get("title") or "").strip()
            if title and title.lower() in lowered:
                return chapter

        selected_chapter_id = str(page_context.get("selected_chapter_id") or "").strip()
        if selected_chapter_id:
            current = next((chapter for chapter in chapters if chapter.get("id") == selected_chapter_id), None)
            if current:
                return current
        return None

    def _extract_shot_index(self, text: str) -> int | None:
        match = re.search(r"(?:第\s*)?(\d+)\s*(?:个)?(?:分镜|镜头|shot)", text, flags=re.I)
        if not match:
            return None
        return max(1, int(match.group(1)))

    def _extract_model_target(self, text: str, *, step_name: str | None) -> tuple[str | None, str | None]:
        lowered = text.lower()
        step_type = self.pipeline.step_def_map.get(step_name).step_type if step_name and step_name in self.pipeline.step_def_map else None
        catalog = self.pipeline.list_provider_catalog()
        model_candidates: list[tuple[str, str]] = []
        for item in catalog:
            if step_type and item["step"] != step_type:
                continue
            provider = str(item["provider"])
            for model_name in item.get("models", []):
                if str(model_name).lower() in lowered:
                    model_candidates.append((provider, str(model_name)))
        if model_candidates:
            return model_candidates[0]
        provider = "openrouter" if "openrouter" in lowered else ("openai" if "openai" in lowered else None)
        return provider, None

    def _extract_task_prompt(self, text: str) -> str | None:
        normalized = re.sub(r"\s+", " ", text).strip()
        for marker in PROMPT_VALUE_MARKERS:
            idx = normalized.find(marker)
            if idx == -1:
                continue
            candidate = normalized[idx + len(marker) :].strip(" ：:，,。！？!?'\"“”")
            if candidate:
                return candidate
        if "prompt" in normalized.lower():
            parts = re.split(r"prompt", normalized, flags=re.I)
            if len(parts) > 1:
                candidate = parts[-1].strip(" ：:，,。！？!?'\"“”")
                if candidate:
                    return candidate
        return None

    def _is_feedback_intent(self, text: str, *, step_name: str | None) -> bool:
        lowered = text.lower()
        if any(keyword in text for keyword in FEEDBACK_KEYWORDS):
            return bool(step_name or any(keyword in lowered for keywords, _step in STEP_HINTS for keyword in keywords))
        return False

    def _extract_feedback_summary(self, text: str) -> str:
        cleaned = re.sub(r"^(请|帮我|麻烦|直接|现在|把|将|针对)\s*", "", text).strip()
        return cleaned.rstrip("。！？!?") or text.strip()

    def _compose_improved_prompt(
        self,
        *,
        step_name: str | None,
        base_prompt: str,
        feedback_summary: str,
        chapter_title: str | None,
        chapter_summary: str,
        shot_index: int | None,
    ) -> str:
        if not step_name:
            return ""
        base_prompt = base_prompt.strip()
        focus_guidance = {
            "story_scripting": "请优先修正章节戏剧冲突、人物动机、转折力度和对白推进。",
            "shot_detailing": "请优先修正分镜节奏、镜头语言、动作连续性、视觉焦点和情绪推进。",
            "storyboard_image": "请优先修正画面构图、光线氛围、人物一致性和场景质感。",
            "consistency_check": "请优先修正校核目标、低分原因和返工判断标准。",
            "segment_video": "请优先修正镜头运动、时长节奏、转场和画面连续性。",
        }.get(step_name, "请根据反馈修正当前步骤输出质量。")
        scope_bits: list[str] = []
        if chapter_title:
            scope_bits.append(f"目标章节：{chapter_title}")
        if chapter_summary:
            scope_bits.append(f"章节摘要：{chapter_summary[:160]}")
        if shot_index:
            scope_bits.append(f"重点镜头：第 {shot_index} 个分镜/镜头。")
        scope_bits.append(f"用户反馈：{feedback_summary}")
        scope_bits.append(focus_guidance)
        scope_bits.append("保留既有故事事实、角色身份、世界观和 Story Bible 约束，只针对反馈点做定向增强。")
        refinement_block = "\n".join(f"- {item}" for item in scope_bits if item)
        if not base_prompt:
            base_prompt = "请基于项目上下文生成当前步骤的高质量输出。"
        return f"{base_prompt}\n\n[Agent Feedback Refinement]\n{refinement_block}"

    def _build_params(self, step_name: str | None, chapter_id: str | None, *, include_chapter: bool) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if include_chapter and chapter_id and step_name in CHAPTER_SCOPED_STEPS:
            params["chapter_id"] = chapter_id
        return params

    def _build_execution_params(self, args: dict[str, Any]) -> dict[str, Any]:
        params = dict(args.get("params") or {})
        if args.get("chapter_id") and self._require_step_name(args) in CHAPTER_SCOPED_STEPS:
            params["chapter_id"] = args["chapter_id"]
        return params

    def _estimate_cost(
        self,
        project: Project,
        step_name: str | None,
        *,
        scope_mode: str,
        chapter_id: str | None,
        provider: str | None = None,
        model_name: str | None = None,
    ) -> dict[str, Any]:
        if not step_name:
            return {"estimated_cost": None, "summary": "当前还缺少目标步骤，暂时无法估算费用。", "source": "missing_step"}
        try:
            return self.pipeline.estimate_step_action_cost(
                project,
                step_name,
                scope_mode=scope_mode,
                chapter_id=chapter_id,
                provider=provider,
                model_name=model_name,
            )
        except Exception:
            return {"estimated_cost": None, "summary": "当前无法可靠估算本次动作费用。", "source": "estimate_failed"}

    def _scope_summary(self, step_label: str, chapter_title: str | None, *, scope_mode: str) -> str:
        if scope_mode == "all_chapters":
            return f"{step_label} / 全部章节"
        if scope_mode == "failed_chapters":
            return f"{step_label} / 失败章节"
        if chapter_title:
            return f"{step_label} / {chapter_title}"
        return step_label or "当前项目"

    def _require_step_name(self, args: dict[str, Any]) -> str:
        step_name = str(args.get("step_name") or "").strip()
        if not step_name:
            raise ValueError("step_name is required")
        return step_name
