from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.context_builder import AgentContextBuilder
from app.agent.runtime import FilmItAgentRuntime
from app.core.config import settings
from app.db.models import AgentMessage, AgentRun, AgentSession, AgentToolCall, Project


class AgentSessionService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.context_builder = AgentContextBuilder(db)
        self.runtime = FilmItAgentRuntime()

    def get_or_create_default_session(self, project: Project) -> AgentSession:
        session = self.db.scalar(
            select(AgentSession)
            .where(AgentSession.project_id == project.id, AgentSession.is_default.is_(True))
            .order_by(AgentSession.created_at.asc())
        )
        if session:
            return session

        session = AgentSession(
            project_id=project.id,
            title=f"{project.name} Agent",
            status="ACTIVE",
            session_kind="PROJECT_DEFAULT",
            is_default=True,
            agent_provider=settings.agent_provider,
            agent_model_name=settings.agent_model_name,
            approval_mode="explicit_write_confirmation",
            retrieval_mode="local_lightweight_index",
            meta={
                "session_mode": "single",
                "multi_session_ready": True,
                "write_actions_require_confirmation": True,
            },
        )
        self.db.add(session)
        self.db.flush()

        greeting = AgentMessage(
            session_id=session.id,
            project_id=project.id,
            role="assistant",
            content_text=(
                "FilmIt Agent 已就绪。\n\n"
                "当前是首个可运行切片:\n"
                "- 单项目单对话\n"
                "- 轻量本地检索\n"
                "- 右侧面板可随时开启或关闭\n"
                "- 写操作默认必须让你充分知情并明确授权确认\n"
                "- Agent 模型与流水线模型分离\n\n"
                "你可以先问我当前项目状态、阻塞点、Story Bible 摘要或下一步建议。"
            ),
            content_json={
                "sources": [
                    {"kind": "session_policy", "label": "会话模式", "snippet": "当前为单项目单对话模式"},
                    {"kind": "approval_policy", "label": "写操作策略", "snippet": "所有写操作都需要明确授权确认"},
                ],
                "suggested_next_actions": [
                    "问我当前项目卡在哪一步",
                    "问我当前 Story Bible 摘要",
                    "问我有哪些失败或返工章节",
                ],
            },
            token_estimate=self._token_estimate("FilmIt Agent 已就绪。"),
        )
        self.db.add(greeting)
        self.db.commit()
        self.db.refresh(session)
        return session

    def list_messages(self, project: Project) -> list[AgentMessage]:
        session = self.get_or_create_default_session(project)
        return list(
            self.db.scalars(
                select(AgentMessage)
                .where(AgentMessage.project_id == project.id, AgentMessage.session_id == session.id)
                .order_by(AgentMessage.created_at.asc())
            ).all()
        )

    async def send_message(self, project: Project, message_text: str, page_context: dict[str, Any] | None = None) -> dict[str, Any]:
        session = self.get_or_create_default_session(project)
        page_context = page_context or {}
        user_message = AgentMessage(
            session_id=session.id,
            project_id=project.id,
            role="user",
            content_text=message_text,
            content_json={"page_context": page_context},
            token_estimate=self._token_estimate(message_text),
        )
        self.db.add(user_message)
        self.db.flush()

        run = AgentRun(
            session_id=session.id,
            project_id=project.id,
            status="RUNNING",
            run_mode="chat",
            input_message_id=user_message.id,
            agent_provider=session.agent_provider,
            agent_model_name=session.agent_model_name,
            meta={"page_context": page_context},
            started_at=datetime.now(timezone.utc),
        )
        self.db.add(run)
        self.db.flush()

        context = self.context_builder.build(project, message_text, page_context=page_context)
        self._record_tool_call(
            run=run,
            session=session,
            project=project,
            tool_name="get_project_overview",
            call_status="SUCCEEDED",
            result_summary="已读取项目状态、步骤进度与章节聚合信息。",
            result_json={"overview": context.get("overview", {})},
            approval_policy="auto_read_only",
        )
        if context.get("retrieval_hits"):
            self._record_tool_call(
                run=run,
                session=session,
                project=project,
                tool_name="search_project_knowledge",
                call_status="SUCCEEDED",
                result_summary=f"已从本地轻量索引命中 {len(context['retrieval_hits'])} 条相关内容。",
                result_json={"hits": context["retrieval_hits"]},
                approval_policy="auto_read_only",
            )

        reply = await self.runtime.reply(project=project, session=session, user_text=message_text, context=context)
        if reply.run_status == "WAITING_APPROVAL":
            self._record_tool_call(
                run=run,
                session=session,
                project=project,
                tool_name="propose_write_action",
                call_status="REQUIRES_APPROVAL",
                result_summary="已识别写操作意图，当前进入明确授权确认前的说明阶段。",
                result_json=reply.content_json,
                approval_policy="explicit_write_confirmation",
                requires_user_confirmation=True,
            )

        assistant_message = AgentMessage(
            session_id=session.id,
            project_id=project.id,
            run_id=run.id,
            role="assistant",
            content_text=reply.text,
            content_json={
                **reply.content_json,
                "suggested_next_actions": reply.suggested_next_actions,
            },
            token_estimate=self._token_estimate(reply.text),
        )
        self.db.add(assistant_message)
        self.db.flush()

        run.status = reply.run_status
        run.output_message_id = assistant_message.id
        run.finished_at = datetime.now(timezone.utc)
        self.db.add(run)
        self.db.commit()
        self.db.refresh(user_message)
        self.db.refresh(assistant_message)
        self.db.refresh(run)
        self.db.refresh(session)

        return {
            "session": session,
            "user_message": user_message,
            "assistant_message": assistant_message,
            "run": run,
        }

    def _record_tool_call(
        self,
        *,
        run: AgentRun,
        session: AgentSession,
        project: Project,
        tool_name: str,
        call_status: str,
        result_summary: str,
        result_json: dict[str, Any],
        approval_policy: str,
        requires_user_confirmation: bool = False,
    ) -> AgentToolCall:
        now = datetime.now(timezone.utc)
        tool_call = AgentToolCall(
            run_id=run.id,
            session_id=session.id,
            project_id=project.id,
            tool_name=tool_name,
            call_status=call_status,
            args_json={},
            result_summary=result_summary,
            result_json=result_json,
            approval_policy=approval_policy,
            requires_user_confirmation=requires_user_confirmation,
            started_at=now,
            finished_at=now,
        )
        self.db.add(tool_call)
        self.db.flush()
        return tool_call

    def _token_estimate(self, text: str) -> int:
        return max(1, len(str(text or "")) // 4)
