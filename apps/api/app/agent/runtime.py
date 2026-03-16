from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import settings
from app.db.models import AgentSession, Project

WRITE_KEYWORDS = (
    "重跑",
    "重生成",
    "重新生成",
    "切换模型",
    "修改",
    "改动",
    "执行",
    "运行",
    "审批",
    "通过",
    "重建",
    "修复",
    "保存",
)


@dataclass(slots=True)
class RuntimeReply:
    text: str
    run_status: str
    content_json: dict[str, Any] = field(default_factory=dict)
    suggested_next_actions: list[str] = field(default_factory=list)


class FilmItAgentRuntime:
    async def reply(
        self,
        *,
        project: Project,
        session: AgentSession,
        user_text: str,
        context: dict[str, Any],
    ) -> RuntimeReply:
        if self._is_write_intent(user_text):
            approval_request = self._build_approval_request(user_text, context)
            text = self._build_write_intent_response(context, approval_request)
            return RuntimeReply(
                text=text,
                run_status="WAITING_APPROVAL",
                content_json={"approval_request": approval_request, "sources": context.get("sources", [])},
                suggested_next_actions=[
                    "明确指出要影响的步骤、章节或项目范围",
                    "确认自己已知晓写操作会改变 FilmIt 项目状态",
                    "在下一阶段接入执行器后，再允许 Agent 实际落地",
                ],
            )

        live_text = await self._maybe_generate_live_response(project=project, session=session, user_text=user_text, context=context)
        text = live_text or self._build_fallback_response(context=context, user_text=user_text)
        suggested_next_actions = self._suggest_next_actions(context)
        return RuntimeReply(
            text=text,
            run_status="COMPLETED",
            content_json={
                "sources": context.get("sources", []),
                "retrieval_hits": context.get("retrieval_hits", []),
                "suggested_next_actions": suggested_next_actions,
            },
            suggested_next_actions=suggested_next_actions,
        )

    async def _maybe_generate_live_response(
        self,
        *,
        project: Project,
        session: AgentSession,
        user_text: str,
        context: dict[str, Any],
    ) -> str | None:
        if not settings.agent_live_model_enabled:
            return None
        if session.agent_provider != "openai" or not settings.openai_api_key:
            return None

        prompt = self._build_live_prompt(project=project, user_text=user_text, context=context)
        payload = {
            "model": session.agent_model_name,
            "instructions": (
                "你是 FilmIt 项目内置 Agent。只能基于给定上下文回答，不得臆造项目状态。"
                "如果用户要求写操作，但系统未明确给出已执行结果，必须说明写操作仍需明确授权确认。"
            ),
            "input": prompt,
            "max_output_tokens": settings.agent_max_output_tokens,
        }
        headers = {
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=settings.openai_timeout_sec) as client:
                response = await client.post(f"{settings.openai_base_url.rstrip('/')}/responses", headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()
            text = str(body.get("output_text") or self._collect_response_text(body)).strip()
            return text or None
        except Exception:
            return None

    def _build_live_prompt(self, *, project: Project, user_text: str, context: dict[str, Any]) -> str:
        compact_context = {
            "project_name": project.name,
            "overview": context.get("overview", {}),
            "page_context": context.get("page_context", {}),
            "story_bible_summary": context.get("story_bible_summary", {}),
            "retrieval_hits": context.get("retrieval_hits", [])[:4],
        }
        return (
            f"用户问题:\n{user_text}\n\n"
            f"FilmIt 项目上下文:\n{json.dumps(compact_context, ensure_ascii=False, indent=2)}\n\n"
            "请给出精炼、面向执行的回复；如果用户问的是诊断问题，请优先说明当前状态、阻塞点和建议动作。"
        )

    def _build_fallback_response(self, *, context: dict[str, Any], user_text: str) -> str:
        overview = context.get("overview", {})
        story_bible = context.get("story_bible_summary", {})
        failed = context.get("chapter_buckets", {}).get("failed", []) or []
        rework = context.get("chapter_buckets", {}).get("rework_requested", []) or []
        retrieval_hits = context.get("retrieval_hits", []) or []
        page_context = context.get("page_context", {})
        selected_step_name = page_context.get("selected_step_name") or "-"
        selected_chapter = page_context.get("selected_chapter") or {}
        current_step = overview.get("current_step", {})

        lines = [
            "已读取当前 FilmIt 项目状态。",
            "",
            "项目概览",
            f"- 项目状态: {overview.get('project_status', '-')}",
            f"- 当前推进步骤: {current_step.get('step_display_name') or current_step.get('step_name') or '-'}",
            f"- 章节数: {overview.get('chapter_count', 0)}",
            f"- 失败章节: {overview.get('failed_chapter_count', 0)}",
            f"- 待返工章节: {overview.get('rework_chapter_count', 0)}",
            f"- 待人工审核章节: {overview.get('review_required_chapter_count', 0)}",
            "",
            "页面焦点",
            f"- 当前步骤: {selected_step_name}",
            f"- 当前章节: {selected_chapter.get('title') or '-'}",
        ]

        if any(keyword in user_text.lower() for keyword in ("story bible", "角色", "场景", "圣经")):
            lines.extend(
                [
                    "",
                    "Story Bible 摘要",
                    f"- 角色锚点: {story_bible.get('character_count', 0)} 个",
                    f"- 场景锚点: {story_bible.get('scene_count', 0)} 个",
                    f"- 角色样本: {', '.join(story_bible.get('characters', [])[:5]) or '-'}",
                    f"- 场景样本: {', '.join(story_bible.get('scenes', [])[:5]) or '-'}",
                ]
            )

        if failed or rework:
            lines.append("")
            lines.append("阻塞与返工")
            for item in failed[:3]:
                lines.append(f"- 失败章节: {item.get('title') or item.get('chapter_index', '-')}")
            for item in rework[:3]:
                lines.append(f"- 待返工章节: {item.get('title') or item.get('chapter_index', '-')}")

        if retrieval_hits:
            lines.append("")
            lines.append("相关命中")
            for item in retrieval_hits[:4]:
                lines.append(f"- {item.get('title')}: {item.get('snippet')}")

        suggestions = self._suggest_next_actions(context)
        if suggestions:
            lines.append("")
            lines.append("建议下一步")
            for item in suggestions:
                lines.append(f"- {item}")
        return "\n".join(lines)

    def _build_approval_request(self, user_text: str, context: dict[str, Any]) -> dict[str, Any]:
        page_context = context.get("page_context", {})
        return {
            "status": "REQUIRES_USER_CONFIRMATION",
            "reason": "当前请求包含会改变 FilmIt 项目状态的写操作。",
            "requested_action": user_text.strip(),
            "scope_hint": {
                "selected_step_name": page_context.get("selected_step_name"),
                "selected_chapter_title": (page_context.get("selected_chapter") or {}).get("title"),
            },
            "policy": "所有 Agent 写操作都必须让用户充分知情并明确授权确认。",
        }

    def _build_write_intent_response(self, context: dict[str, Any], approval_request: dict[str, Any]) -> str:
        page_context = context.get("page_context", {})
        step_name = page_context.get("selected_step_name") or "-"
        chapter = (page_context.get("selected_chapter") or {}).get("title") or "-"
        return "\n".join(
            [
                "已识别到写操作请求。",
                "",
                "当前策略不会直接执行任何改动。",
                "- 写操作需要你充分知情并明确授权确认",
                f"- 当前页面步骤: {step_name}",
                f"- 当前页面章节: {chapter}",
                "",
                f"待确认动作: {approval_request['requested_action']}",
                "",
                "这轮后端已接入审批式 Agent 骨架；实际写操作执行器会在下一阶段把确认流与 FilmIt 工具调用打通。",
            ]
        )

    def _suggest_next_actions(self, context: dict[str, Any]) -> list[str]:
        overview = context.get("overview", {})
        suggestions: list[str] = []
        if int(overview.get("failed_chapter_count", 0)) > 0:
            suggestions.append("先定位失败章节的共同错误，再决定是否批量重跑当前阶段。")
        if int(overview.get("rework_chapter_count", 0)) > 0:
            suggestions.append("优先检查 `REWORK_REQUESTED` 章节的一致性失败原因。")
        current_step = overview.get("current_step", {})
        if current_step.get("status") == "REVIEW_REQUIRED":
            suggestions.append("当前步骤处于 `REVIEW_REQUIRED`，可先在左侧完成人工审核。")
        if not suggestions:
            suggestions.append("先确认当前步骤与章节焦点，再提出更具体的诊断或执行请求。")
        return suggestions[:3]

    def _is_write_intent(self, user_text: str) -> bool:
        lowered = user_text.lower()
        return any(keyword in user_text or keyword in lowered for keyword in WRITE_KEYWORDS)

    def _collect_response_text(self, response: dict[str, Any]) -> str:
        lines: list[str] = []
        for item in response.get("output", []):
            if item.get("type") != "message":
                continue
            for content in item.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    lines.append(str(content["text"]))
        return "\n".join(lines).strip()
