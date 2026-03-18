from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.context_builder import AgentContextBuilder
from app.agent.runtime import FilmItAgentRuntime
from app.agent.tool_registry import AgentToolRegistry, PlannedToolAction
from app.core.config import settings
from app.db.models import AgentMessage, AgentRun, AgentSession, AgentToolCall, Project
from app.services.pipeline_service import PipelineService


class AgentSessionService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.pipeline = PipelineService(db)
        self.context_builder = AgentContextBuilder(db)
        self.runtime = FilmItAgentRuntime()
        self.tool_registry = AgentToolRegistry(self.pipeline)

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
        planned_action = self.tool_registry.plan_write_action(project=project, user_text=message_text, page_context=page_context)
        self._record_tool_call(
            run=run,
            session=session,
            project=project,
            tool_name="get_project_overview",
            call_status="SUCCEEDED",
            args_json={},
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
                args_json={"query": message_text},
                result_summary=f"已从本地轻量索引命中 {len(context['retrieval_hits'])} 条相关内容。",
                result_json={"hits": context["retrieval_hits"]},
                approval_policy="auto_read_only",
            )

        reply = await self.runtime.reply(
            project=project,
            session=session,
            user_text=message_text,
            context=context,
            planned_action=planned_action.to_dict() if planned_action else None,
        )
        proposal_tool_call: AgentToolCall | None = None
        if reply.run_status == "WAITING_APPROVAL":
            proposal_tool_call = self._record_tool_call(
                run=run,
                session=session,
                project=project,
                tool_name="propose_write_action",
                call_status="REQUIRES_APPROVAL",
                args_json={
                    "requested_action": message_text,
                    "page_context": page_context,
                    "planned_action": planned_action.to_dict() if planned_action else {},
                },
                result_summary=self._proposal_summary(planned_action),
                result_json=reply.content_json,
                approval_policy="explicit_write_confirmation",
                requires_user_confirmation=True,
            )

        assistant_content_json = {
            **reply.content_json,
            "suggested_next_actions": reply.suggested_next_actions,
        }
        if proposal_tool_call is not None:
            assistant_content_json["pending_tool_call_id"] = proposal_tool_call.id

        assistant_message = AgentMessage(
            session_id=session.id,
            project_id=project.id,
            run_id=run.id,
            role="assistant",
            content_text=reply.text,
            content_json=assistant_content_json,
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

        return self._build_turn_payload(session=session, user_message=user_message, assistant_message=assistant_message, run=run)

    async def approve_tool_call(
        self,
        project: Project,
        tool_call_id: str,
        *,
        comment: str | None = None,
    ) -> dict[str, Any]:
        session = self.get_or_create_default_session(project)
        proposal = self._get_tool_call(session.id, project.id, tool_call_id)
        if proposal.call_status != "REQUIRES_APPROVAL":
            raise ValueError("tool call is not pending approval")

        proposal_args = dict(proposal.args_json or {})
        action = self._planned_action_from_payload(proposal_args.get("planned_action"))
        if action is None or not action.tool_name:
            raise ValueError("pending tool call does not contain an executable action")
        if not action.ready:
            raise ValueError(f"planned action is incomplete: {', '.join(action.missing_fields or ['missing_fields'])}")

        page_context = dict(proposal_args.get("page_context") or {})
        self._mark_proposal_decision(proposal, decision_status="APPROVED", comment=comment)

        user_message = AgentMessage(
            session_id=session.id,
            project_id=project.id,
            role="user",
            content_text=f"批准执行 Agent 动作: {action.display_name}",
            content_json={
                "approved_tool_call_id": proposal.id,
                "comment": comment,
                "planned_action": action.to_dict(),
            },
            token_estimate=self._token_estimate(action.display_name),
        )
        self.db.add(user_message)
        self.db.flush()

        now = datetime.now(timezone.utc)
        run = AgentRun(
            session_id=session.id,
            project_id=project.id,
            status="RUNNING",
            run_mode="approval_execute",
            input_message_id=user_message.id,
            agent_provider=session.agent_provider,
            agent_model_name=session.agent_model_name,
            meta={
                "source_tool_call_id": proposal.id,
                "page_context": page_context,
                "planned_action": action.to_dict(),
                "comment": comment,
            },
            started_at=now,
        )
        self.db.add(run)
        self.db.flush()

        execution_tool_call = AgentToolCall(
            run_id=run.id,
            session_id=session.id,
            project_id=project.id,
            tool_name=action.tool_name,
            call_status="RUNNING",
            args_json=action.to_dict(),
            result_json={},
            approval_policy="approved_write_action",
            requires_user_confirmation=False,
            started_at=now,
        )
        self.db.add(execution_tool_call)
        self.db.flush()

        try:
            self._update_proposal_execution_status(
                proposal,
                execution_status="RUNNING",
                execution_summary="已获批准，正在调用真实 FilmIt 工具执行。",
                execution_run_id=run.id,
            )
            execution = await self.tool_registry.execute(project=project, action=action)
            self.db.refresh(project)
            context = self.context_builder.build(project, execution["summary"], page_context=page_context)
            assistant_text = self._build_execution_response(action=action, execution=execution, context=context, comment=comment)
            assistant_content_json = {
                "sources": context.get("sources", []),
                "suggested_next_actions": self.runtime._suggest_next_actions(context),
                "executed_action": action.to_dict(),
                "execution_result": execution.get("result", {}),
            }
            execution_tool_call.call_status = "SUCCEEDED"
            execution_tool_call.result_summary = execution["summary"]
            execution_tool_call.result_json = {
                "action": action.to_dict(),
                "execution_result": execution.get("result", {}),
            }
            self._update_proposal_execution_status(
                proposal,
                execution_status="SUCCEEDED",
                execution_summary=execution["summary"],
                execution_run_id=run.id,
                execution_tool_call_id=execution_tool_call.id,
                execution_result=execution.get("result", {}),
            )
            run.status = "COMPLETED"
            run.error_message = None
        except Exception as exc:  # noqa: BLE001
            self.db.refresh(project)
            context = self.context_builder.build(project, str(exc), page_context=page_context)
            assistant_text = self._build_execution_failure_response(action=action, error=str(exc), context=context)
            assistant_content_json = {
                "sources": context.get("sources", []),
                "suggested_next_actions": [
                    "先检查当前步骤状态与前置依赖是否满足。",
                    "如果要重试，请重新发起一个新的待确认动作。",
                ],
                "executed_action": action.to_dict(),
                "error": str(exc),
            }
            execution_tool_call.call_status = "FAILED"
            execution_tool_call.result_summary = f"执行失败: {exc}"
            execution_tool_call.result_json = {
                "action": action.to_dict(),
                "error": str(exc),
            }
            self._update_proposal_execution_status(
                proposal,
                execution_status="FAILED",
                execution_summary=str(exc),
                execution_run_id=run.id,
                execution_tool_call_id=execution_tool_call.id,
                execution_result={"error": str(exc)},
            )
            run.status = "FAILED"
            run.error_message = str(exc)
        execution_tool_call.finished_at = datetime.now(timezone.utc)
        self.db.add(execution_tool_call)

        assistant_message = AgentMessage(
            session_id=session.id,
            project_id=project.id,
            run_id=run.id,
            role="assistant",
            content_text=assistant_text,
            content_json=assistant_content_json,
            token_estimate=self._token_estimate(assistant_text),
        )
        self.db.add(assistant_message)
        self.db.flush()

        run.output_message_id = assistant_message.id
        run.finished_at = datetime.now(timezone.utc)
        self.db.add(run)
        self.db.commit()
        self.db.refresh(user_message)
        self.db.refresh(assistant_message)
        self.db.refresh(run)
        self.db.refresh(session)

        return self._build_turn_payload(session=session, user_message=user_message, assistant_message=assistant_message, run=run)

    def list_action_queue(self, project: Project, *, history_limit: int = 12) -> dict[str, list[dict[str, Any]]]:
        session = self.get_or_create_default_session(project)
        proposals = list(
            self.db.scalars(
                select(AgentToolCall)
                .where(
                    AgentToolCall.project_id == project.id,
                    AgentToolCall.session_id == session.id,
                    AgentToolCall.tool_name == "propose_write_action",
                )
                .order_by(AgentToolCall.created_at.desc())
            ).all()
        )
        pending: list[dict[str, Any]] = []
        history: list[dict[str, Any]] = []
        for proposal in proposals:
            item = self._serialize_action_item(proposal)
            if proposal.call_status == "REQUIRES_APPROVAL":
                pending.append(item)
            elif len(history) < history_limit:
                history.append(item)
        return {"pending": pending, "history": history}

    def reject_tool_call(
        self,
        project: Project,
        tool_call_id: str,
        *,
        comment: str | None = None,
    ) -> dict[str, Any]:
        session = self.get_or_create_default_session(project)
        proposal = self._get_tool_call(session.id, project.id, tool_call_id)
        if proposal.call_status != "REQUIRES_APPROVAL":
            raise ValueError("tool call is not pending approval")

        proposal_args = dict(proposal.args_json or {})
        action = self._planned_action_from_payload(proposal_args.get("planned_action"))
        page_context = dict(proposal_args.get("page_context") or {})
        self._mark_proposal_decision(proposal, decision_status="REJECTED", comment=comment)

        user_message = AgentMessage(
            session_id=session.id,
            project_id=project.id,
            role="user",
            content_text=f"拒绝执行 Agent 动作: {action.display_name if action else '待确认动作'}",
            content_json={
                "rejected_tool_call_id": proposal.id,
                "comment": comment,
                "planned_action": action.to_dict() if action else {},
            },
            token_estimate=self._token_estimate(comment or "reject"),
        )
        self.db.add(user_message)
        self.db.flush()

        run = AgentRun(
            session_id=session.id,
            project_id=project.id,
            status="COMPLETED",
            run_mode="approval_reject",
            input_message_id=user_message.id,
            agent_provider=session.agent_provider,
            agent_model_name=session.agent_model_name,
            meta={
                "source_tool_call_id": proposal.id,
                "page_context": page_context,
                "planned_action": action.to_dict() if action else {},
                "comment": comment,
            },
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )
        self.db.add(run)
        self.db.flush()

        context = self.context_builder.build(project, "rejected agent write action", page_context=page_context)
        assistant_text = self._build_rejection_response(action=action, comment=comment, context=context)
        assistant_message = AgentMessage(
            session_id=session.id,
            project_id=project.id,
            run_id=run.id,
            role="assistant",
            content_text=assistant_text,
            content_json={
                "sources": context.get("sources", []),
                "suggested_next_actions": [
                    "如果你只是想了解风险，可以先继续问我影响范围。",
                    "如果要执行别的动作，请重新发起新的待确认操作。",
                ],
                "rejected_action": action.to_dict() if action else {},
            },
            token_estimate=self._token_estimate(assistant_text),
        )
        self.db.add(assistant_message)
        self.db.flush()

        run.output_message_id = assistant_message.id
        self.db.add(run)
        self.db.commit()
        self.db.refresh(user_message)
        self.db.refresh(assistant_message)
        self.db.refresh(run)
        self.db.refresh(session)

        return self._build_turn_payload(session=session, user_message=user_message, assistant_message=assistant_message, run=run)

    def _record_tool_call(
        self,
        *,
        run: AgentRun,
        session: AgentSession,
        project: Project,
        tool_name: str,
        call_status: str,
        args_json: dict[str, Any],
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
            args_json=args_json,
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

    def _build_turn_payload(
        self,
        *,
        session: AgentSession,
        user_message: AgentMessage,
        assistant_message: AgentMessage,
        run: AgentRun,
    ) -> dict[str, Any]:
        return {
            "session": session,
            "user_message": user_message,
            "assistant_message": assistant_message,
            "run": run,
        }

    def _serialize_action_item(self, proposal: AgentToolCall) -> dict[str, Any]:
        args_json = dict(proposal.args_json or {})
        result_json = dict(proposal.result_json or {})
        planned_action = self._planned_action_from_payload(args_json.get("planned_action"))
        approval_request = dict(result_json.get("approval_request") or {})
        requested_action = str(args_json.get("requested_action") or approval_request.get("requested_action") or "").strip()
        return {
            "tool_call_id": proposal.id,
            "call_status": proposal.call_status,
            "requested_action": requested_action,
            "display_name": planned_action.display_name if planned_action else approval_request.get("display_name"),
            "scope_summary": planned_action.scope_summary if planned_action else approval_request.get("scope_summary"),
            "ready": planned_action.ready if planned_action else bool(approval_request.get("ready")),
            "missing_fields": planned_action.missing_fields if planned_action else list(approval_request.get("missing_fields", [])),
            "user_visible_summary": planned_action.user_visible_summary if planned_action else approval_request.get("user_visible_summary"),
            "estimated_cost": planned_action.estimated_cost if planned_action else approval_request.get("estimated_cost"),
            "estimated_cost_summary": (
                planned_action.estimated_cost_summary if planned_action else approval_request.get("estimated_cost_summary")
            ),
            "cost_source": planned_action.cost_source if planned_action else approval_request.get("cost_source"),
            "prompt_preview": planned_action.prompt_preview if planned_action else approval_request.get("prompt_preview"),
            "feedback_summary": planned_action.feedback_summary if planned_action else approval_request.get("feedback_summary"),
            "decision_status": result_json.get("decision_status"),
            "decision_comment": result_json.get("decision_comment"),
            "execution_status": result_json.get("execution_status"),
            "execution_summary": result_json.get("execution_summary"),
            "execution_run_id": result_json.get("execution_run_id"),
            "execution_tool_call_id": result_json.get("execution_tool_call_id"),
            "created_at": proposal.created_at,
            "finished_at": proposal.finished_at,
        }

    def _proposal_summary(self, planned_action: PlannedToolAction | None) -> str:
        if planned_action is None:
            return "已识别写操作意图，但当前还未归一化为可执行动作。"
        if planned_action.ready:
            return f"已生成待确认动作: {planned_action.display_name}。"
        return f"已生成待确认动作，但仍缺少必要信息: {', '.join(planned_action.missing_fields or ['necessary_fields'])}。"

    def _get_tool_call(self, session_id: str, project_id: str, tool_call_id: str) -> AgentToolCall:
        tool_call = self.db.scalar(
            select(AgentToolCall).where(
                AgentToolCall.id == tool_call_id,
                AgentToolCall.session_id == session_id,
                AgentToolCall.project_id == project_id,
            )
        )
        if not tool_call:
            raise ValueError("tool call not found")
        return tool_call

    def _planned_action_from_payload(self, payload: Any) -> PlannedToolAction | None:
        if not isinstance(payload, dict):
            return None
        missing_fields = [str(item) for item in payload.get("missing_fields", []) if str(item).strip()]
        return PlannedToolAction(
            tool_name=str(payload.get("tool_name") or "").strip(),
            display_name=str(payload.get("display_name") or payload.get("tool_name") or "").strip(),
            args=dict(payload.get("args") or {}),
            scope_summary=str(payload.get("scope_summary") or "当前项目").strip(),
            ready=bool(payload.get("ready")),
            missing_fields=missing_fields,
            user_visible_summary=str(payload.get("user_visible_summary") or "").strip(),
            estimated_cost=float(payload["estimated_cost"]) if isinstance(payload.get("estimated_cost"), (int, float)) else None,
            estimated_cost_summary=str(payload.get("estimated_cost_summary") or "").strip() or None,
            cost_source=str(payload.get("cost_source") or "").strip() or None,
            prompt_preview=str(payload.get("prompt_preview") or "").strip() or None,
            feedback_summary=str(payload.get("feedback_summary") or "").strip() or None,
        )

    def _mark_proposal_decision(self, proposal: AgentToolCall, *, decision_status: str, comment: str | None) -> None:
        decided_at = datetime.now(timezone.utc)
        decision_label = {"APPROVED": "已批准", "REJECTED": "已拒绝"}.get(decision_status, decision_status)
        proposal.call_status = decision_status
        proposal.result_summary = f"{proposal.result_summary or '待确认动作'} {decision_label}。"
        proposal.finished_at = decided_at
        proposal.result_json = {
            **dict(proposal.result_json or {}),
            "decision_status": decision_status,
            "decision_comment": comment,
            "decided_at": decided_at.isoformat(),
        }
        self.db.add(proposal)

        run = self.db.scalar(select(AgentRun).where(AgentRun.id == proposal.run_id))
        if not run or not run.output_message_id:
            return
        assistant_message = self.db.scalar(select(AgentMessage).where(AgentMessage.id == run.output_message_id))
        if not assistant_message:
            return
        content_json = dict(assistant_message.content_json or {})
        approval_request = dict(content_json.get("approval_request") or {})
        approval_request["decision_status"] = decision_status
        approval_request["decision_comment"] = comment
        approval_request["decided_at"] = decided_at.isoformat()
        content_json["approval_request"] = approval_request
        assistant_message.content_json = content_json
        self.db.add(assistant_message)

    def _update_proposal_execution_status(
        self,
        proposal: AgentToolCall,
        *,
        execution_status: str,
        execution_summary: str,
        execution_run_id: str,
        execution_tool_call_id: str | None = None,
        execution_result: dict[str, Any] | None = None,
    ) -> None:
        result_json = dict(proposal.result_json or {})
        result_json["execution_status"] = execution_status
        result_json["execution_summary"] = execution_summary
        result_json["execution_run_id"] = execution_run_id
        result_json["execution_tool_call_id"] = execution_tool_call_id
        result_json["executed_at"] = datetime.now(timezone.utc).isoformat()
        if execution_result is not None:
            result_json["execution_result"] = execution_result
        proposal.result_json = result_json
        self.db.add(proposal)

    def _build_execution_response(
        self,
        *,
        action: PlannedToolAction,
        execution: dict[str, Any],
        context: dict[str, Any],
        comment: str | None,
    ) -> str:
        overview = context.get("overview", {})
        current_step = overview.get("current_step", {})
        lines = [
            "已执行已批准的 Agent 写操作。",
            "",
            f"- 动作: {action.display_name}",
            f"- 影响范围: {action.scope_summary}",
            f"- 执行结果: {execution.get('summary') or '-'}",
        ]
        if comment:
            lines.append(f"- 用户备注: {comment}")
        lines.extend(
            [
                "",
                "执行后状态",
                f"- 项目状态: {overview.get('project_status', '-')}",
                f"- 当前推进步骤: {current_step.get('step_display_name') or current_step.get('step_name') or '-'}",
                f"- 失败章节: {overview.get('failed_chapter_count', 0)}",
                f"- 待返工章节: {overview.get('rework_chapter_count', 0)}",
            ]
        )
        return "\n".join(lines)

    def _build_execution_failure_response(
        self,
        *,
        action: PlannedToolAction,
        error: str,
        context: dict[str, Any],
    ) -> str:
        overview = context.get("overview", {})
        current_step = overview.get("current_step", {})
        return "\n".join(
            [
                "已收到你的授权，但执行真实 FilmIt 工具时失败。",
                "",
                f"- 动作: {action.display_name}",
                f"- 影响范围: {action.scope_summary}",
                f"- 错误: {error}",
                "",
                "当前状态",
                f"- 项目状态: {overview.get('project_status', '-')}",
                f"- 当前推进步骤: {current_step.get('step_display_name') or current_step.get('step_name') or '-'}",
            ]
        )

    def _build_rejection_response(
        self,
        *,
        action: PlannedToolAction | None,
        comment: str | None,
        context: dict[str, Any],
    ) -> str:
        overview = context.get("overview", {})
        page_context = context.get("page_context", {})
        display_name = action.display_name if action else "待确认动作"
        scope_summary = action.scope_summary if action else "当前项目"
        lines = [
            "已取消该 Agent 写操作。",
            "",
            f"- 动作: {display_name}",
            f"- 影响范围: {scope_summary}",
            "- 结果: FilmIt 项目未发生写入改动。",
        ]
        if comment:
            lines.append(f"- 备注: {comment}")
        lines.extend(
            [
                "",
                "当前项目状态保持不变",
                f"- 项目状态: {overview.get('project_status', '-')}",
                f"- 当前步骤: {page_context.get('selected_step_name') or '-'}",
            ]
        )
        return "\n".join(lines)

    def _token_estimate(self, text: str) -> int:
        return max(1, len(str(text or "")) // 4)
