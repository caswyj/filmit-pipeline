"use client";

import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

type AgentPanelProps = {
  projectId: string;
  projectName: string;
  projectStatus: string;
  targetDurationSec: number;
  selectedStepKey: string | null;
  selectedStepLabel: string | null;
  selectedChapterId: string | null;
  selectedChapterTitle: string | null;
  onAgentMutation?: (() => Promise<void>) | (() => void);
};

type AgentSession = {
  id: string;
  project_id: string;
  title: string;
  status: string;
  session_kind: string;
  is_default: boolean;
  agent_provider: string;
  agent_model_name: string;
  approval_mode: string;
  retrieval_mode: string;
  meta: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

type AgentMessage = {
  id: string;
  session_id: string;
  project_id: string;
  run_id: string | null;
  role: string;
  content_text: string;
  content_json: Record<string, unknown>;
  visibility: string;
  token_estimate: number;
  created_at: string;
};

type AgentToolCall = {
  id: string;
  tool_name: string;
  call_status: string;
  args_json: Record<string, unknown>;
  result_summary: string | null;
  result_json: Record<string, unknown>;
  approval_policy: string;
  requires_user_confirmation: boolean;
};

type AgentRun = {
  id: string;
  status: string;
  run_mode: string;
  agent_provider: string;
  agent_model_name: string;
  tool_calls: AgentToolCall[];
};

type AgentTurn = {
  session: AgentSession;
  user_message: AgentMessage;
  assistant_message: AgentMessage;
  run: AgentRun;
};

type AgentActionItem = {
  tool_call_id: string;
  call_status: string;
  requested_action: string;
  display_name: string | null;
  scope_summary: string | null;
  ready: boolean;
  missing_fields: string[];
  user_visible_summary: string | null;
  estimated_cost: number | null;
  estimated_cost_summary: string | null;
  cost_source: string | null;
  prompt_preview: string | null;
  feedback_summary: string | null;
  decision_status: string | null;
  decision_comment: string | null;
  execution_status: string | null;
  execution_summary: string | null;
  execution_run_id: string | null;
  execution_tool_call_id: string | null;
  created_at: string;
  finished_at: string | null;
};

type AgentActionQueue = {
  pending: AgentActionItem[];
  history: AgentActionItem[];
};

const apiBase = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function asList(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function asString(value: unknown, fallback = ""): string {
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}

export function AgentPanel({
  projectId,
  projectName,
  projectStatus,
  targetDurationSec,
  selectedStepKey,
  selectedStepLabel,
  selectedChapterId,
  selectedChapterTitle,
  onAgentMutation,
}: AgentPanelProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [session, setSession] = useState<AgentSession | null>(null);
  const [messages, setMessages] = useState<AgentMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const [decisionBusyId, setDecisionBusyId] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [latestRun, setLatestRun] = useState<AgentRun | null>(null);
  const [actionQueue, setActionQueue] = useState<AgentActionQueue>({ pending: [], history: [] });
  const messageEndRef = useRef<HTMLDivElement | null>(null);

  const storageKey = useMemo(() => `filmit-agent-panel-open:${projectId}`, [projectId]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const saved = window.localStorage.getItem(storageKey);
    setIsOpen(saved === "true");
  }, [storageKey]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(storageKey, isOpen ? "true" : "false");
  }, [isOpen, storageKey]);

  useEffect(() => {
    if (!isOpen) return;
    loadSessionAndMessages().catch((err) =>
      setError(err instanceof Error ? err.message : "加载 Agent 上下文失败")
    );
  }, [isOpen, projectId]);

  useEffect(() => {
    messageEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, latestRun, isOpen]);

  async function loadSessionAndMessages() {
    setError(null);
    setLatestRun(null);
    const [sessionRes, messagesRes, actionsRes] = await Promise.all([
      fetch(`${apiBase}/api/v1/projects/${projectId}/agent/sessions/default`, { cache: "no-store" }),
      fetch(`${apiBase}/api/v1/projects/${projectId}/agent/sessions/default/messages`, { cache: "no-store" }),
      fetch(`${apiBase}/api/v1/projects/${projectId}/agent/actions`, { cache: "no-store" }),
    ]);
    if (!sessionRes.ok) {
      throw new Error("加载 Agent 会话失败");
    }
    if (!messagesRes.ok) {
      throw new Error("加载 Agent 消息失败");
    }
    if (!actionsRes.ok) {
      throw new Error("加载 Agent 动作列表失败");
    }
    const sessionData = (await sessionRes.json()) as AgentSession;
    const messageData = (await messagesRes.json()) as AgentMessage[];
    const actionData = (await actionsRes.json()) as AgentActionQueue;
    setSession(sessionData);
    setMessages(messageData.filter((item) => item.visibility !== "hidden"));
    setActionQueue(actionData);
  }

  async function sendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = input.trim();
    if (!trimmed) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${apiBase}/api/v1/projects/${projectId}/agent/sessions/default/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: trimmed,
          page_context: {
            selected_step_key: selectedStepKey,
            selected_step_name: selectedStepLabel,
            selected_chapter_id: selectedChapterId,
            selected_chapter_title: selectedChapterTitle,
          },
        }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "发送消息失败");
      }
      const data = (await res.json()) as AgentTurn;
      await loadSessionAndMessages();
      setSession(data.session);
      setLatestRun(data.run);
      setInput("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "发送消息失败");
    } finally {
      setBusy(false);
    }
  }

  async function decideToolCall(toolCallId: string, decision: "approve" | "reject") {
    setDecisionBusyId(toolCallId);
    setError(null);
    try {
      const res = await fetch(`${apiBase}/api/v1/projects/${projectId}/agent/tool-calls/${toolCallId}/${decision}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `Agent ${decision} 失败`);
      }
      const data = (await res.json()) as AgentTurn;
      await loadSessionAndMessages();
      setLatestRun(data.run);
      if (decision === "approve") {
        await Promise.resolve(onAgentMutation?.());
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : `Agent ${decision} 失败`);
    } finally {
      setDecisionBusyId(null);
    }
  }

  function renderAssistantMeta(message: AgentMessage) {
    const content = asRecord(message.content_json);
    const sources = asList(content.sources);
    const nextActions = asList(content.suggested_next_actions);
    const approval = asRecord(content.approval_request);
    const pendingToolCallId = asString(content.pending_tool_call_id);
    const decisionStatus = asString(approval.decision_status);
    const isPendingApproval =
      asString(approval.status) === "REQUIRES_USER_CONFIRMATION" && !decisionStatus && Boolean(pendingToolCallId);
    const ready = approval.ready === true;
    const canDecide = isPendingApproval && ready;

    return (
      <>
        {approval.status ? (
          <div className="agentApprovalCard">
            <strong>{asString(approval.status)}</strong>
            <p>{asString(approval.reason, "当前请求涉及写操作。")}</p>
            {approval.display_name ? <p>动作: {asString(approval.display_name)}</p> : null}
            {approval.scope_summary ? <p>范围: {asString(approval.scope_summary)}</p> : null}
            {approval.user_visible_summary ? <p>摘要: {asString(approval.user_visible_summary)}</p> : null}
            {approval.feedback_summary ? <p>反馈: {asString(approval.feedback_summary)}</p> : null}
            {approval.estimated_cost_summary ? <p>费用: {asString(approval.estimated_cost_summary)}</p> : null}
            <p className="muted" style={{ marginBottom: 0 }}>
              {asString(approval.policy, "请在充分知情后再给予明确授权。")}
            </p>
            {approval.prompt_preview ? (
              <details style={{ marginTop: 8 }}>
                <summary>查看提示词修订预览</summary>
                <pre style={{ whiteSpace: "pre-wrap", marginTop: 8 }}>{asString(approval.prompt_preview)}</pre>
              </details>
            ) : null}
            {decisionStatus ? (
              <p className="muted" style={{ marginTop: 8, marginBottom: 0 }}>
                当前状态: {decisionStatus}
              </p>
            ) : null}
            {canDecide ? (
              <div className="row" style={{ gap: 8, marginTop: 12 }}>
                <button
                  className="primary"
                  type="button"
                  disabled={busy || decisionBusyId === pendingToolCallId}
                  onClick={() => decideToolCall(pendingToolCallId, "approve")}
                >
                  {decisionBusyId === pendingToolCallId ? "执行中..." : "批准执行"}
                </button>
                <button
                  type="button"
                  disabled={busy || decisionBusyId === pendingToolCallId}
                  onClick={() => decideToolCall(pendingToolCallId, "reject")}
                >
                  拒绝执行
                </button>
              </div>
            ) : null}
          </div>
        ) : null}
        {sources.length > 0 ? (
          <div className="agentSourceList">
            {sources.slice(0, 4).map((item, index) => {
              const source = asRecord(item);
              return (
                <div key={`${message.id}-source-${index}`} className="agentSourceItem">
                  <strong>{asString(source.label, "来源")}</strong>
                  <p>{asString(source.snippet, "-")}</p>
                </div>
              );
            })}
          </div>
        ) : null}
        {nextActions.length > 0 ? (
          <div className="agentNextActions">
            {nextActions.slice(0, 3).map((item, index) => (
              <span key={`${message.id}-next-${index}`} className="pill">
                {asString(item, "-")}
              </span>
            ))}
          </div>
        ) : null}
      </>
    );
  }

  return (
    <div className="agentPanelShell" data-testid="project-agent-panel">
      <section className="card agentToggleCard" data-testid="project-agent-toggle-card">
        <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
          <div>
            <p className="eyebrow">Project Agent</p>
            <h3 style={{ margin: 0 }}>右侧 Agent 工作台</h3>
          </div>
          <span className="pill">{isOpen ? "已开启" : "已关闭"}</span>
        </div>
        <p className="muted" style={{ marginBottom: 12 }}>
          你可以随时开启或关闭 Agent。当前策略下，所有会改变 FilmIt 项目状态的写操作都必须先让你充分知情并明确授权确认。
        </p>
        <button
          className={isOpen ? "" : "primary"}
          onClick={() => setIsOpen((current) => !current)}
          data-testid="project-agent-toggle-button"
        >
          {isOpen ? "关闭 Agent 面板" : "开启 Agent 面板"}
        </button>
      </section>

      {isOpen ? (
        <>
          <section className="card agentStatusCard" data-testid="project-agent-status-card">
            <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
              <strong>{session?.title ?? "FilmIt Agent"}</strong>
              <span className="pill">单对话模式</span>
            </div>
            <p className="muted" style={{ marginBottom: 8 }}>
              项目: {projectName} | 状态: {projectStatus} | 目标时长: {targetDurationSec}s
            </p>
            <div className="diffList">
              <div className="diffItem">
                Agent 模型: {session?.agent_provider ?? "openai"}/{session?.agent_model_name ?? "gpt-5-mini"}
              </div>
              <div className="diffItem">
                检索模式: {session?.retrieval_mode ?? "local_lightweight_index"}
              </div>
              <div className="diffItem">
                当前页面焦点: {selectedStepLabel ?? "-"} / {selectedChapterTitle ?? "-"}
              </div>
            </div>
          </section>

          {(actionQueue.pending.length > 0 || actionQueue.history.length > 0) ? (
            <section className="card agentActionQueueCard" data-testid="project-agent-action-queue-card">
              <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
                <strong>审批工作台</strong>
                <span className="pill">{actionQueue.pending.length} 待处理</span>
              </div>
              {actionQueue.pending.length > 0 ? (
                <div className="agentActionQueueList">
                  {actionQueue.pending.map((item) => (
                    <div key={item.tool_call_id} className="agentActionQueueItem">
                      <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
                        <strong>{item.display_name ?? "待确认动作"}</strong>
                        <span className="pill">{item.call_status}</span>
                      </div>
                      <p>{item.requested_action}</p>
                      {item.scope_summary ? <p className="muted">范围: {item.scope_summary}</p> : null}
                      {item.user_visible_summary ? <p className="muted">摘要: {item.user_visible_summary}</p> : null}
                      {item.feedback_summary ? <p className="muted">反馈: {item.feedback_summary}</p> : null}
                      {item.estimated_cost_summary ? <p className="muted">费用: {item.estimated_cost_summary}</p> : null}
                      {item.prompt_preview ? (
                        <details>
                          <summary>查看提示词修订预览</summary>
                          <pre style={{ whiteSpace: "pre-wrap", marginTop: 8 }}>{item.prompt_preview}</pre>
                        </details>
                      ) : null}
                      {!item.ready ? (
                        <p className="muted">缺少字段: {item.missing_fields.join(", ") || "必要参数"}</p>
                      ) : (
                        <div className="row" style={{ gap: 8 }}>
                          <button
                            className="primary"
                            type="button"
                            disabled={busy || decisionBusyId === item.tool_call_id}
                            onClick={() => decideToolCall(item.tool_call_id, "approve")}
                          >
                            {decisionBusyId === item.tool_call_id ? "执行中..." : "批准执行"}
                          </button>
                          <button
                            type="button"
                            disabled={busy || decisionBusyId === item.tool_call_id}
                            onClick={() => decideToolCall(item.tool_call_id, "reject")}
                          >
                            拒绝执行
                          </button>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              ) : (
                <p className="muted" style={{ marginBottom: 0 }}>当前没有待处理的审批动作。</p>
              )}
              {actionQueue.history.length > 0 ? (
                <>
                  <p className="eyebrow" style={{ marginTop: 12 }}>最近审批历史</p>
                  <div className="agentActionHistoryList">
                    {actionQueue.history.slice(0, 8).map((item) => (
                      <div key={item.tool_call_id} className="agentActionHistoryItem">
                        <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
                          <strong>{item.display_name ?? "历史动作"}</strong>
                          <span className="pill">{item.decision_status ?? item.call_status}</span>
                        </div>
                        <p>{item.requested_action}</p>
                        {item.execution_summary ? <p className="muted">执行结果: {item.execution_summary}</p> : null}
                      </div>
                    ))}
                  </div>
                </>
              ) : null}
            </section>
          ) : null}

          <section className="card agentConversationCard" data-testid="project-agent-conversation-card">
            <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
              <strong>Agent 对话</strong>
              {latestRun ? <span className="pill">{latestRun.status}</span> : null}
            </div>
            <div className="agentMessages">
              {messages.map((message) => (
                <article
                  key={message.id}
                  className={`agentMessage ${message.role === "user" ? "user" : "assistant"}`}
                  data-role={message.role}
                >
                  <div className="agentMessageMeta">
                    <strong>{message.role === "user" ? "你" : "Agent"}</strong>
                    <span className="muted">{new Date(message.created_at).toLocaleTimeString()}</span>
                  </div>
                  <p style={{ whiteSpace: "pre-wrap", marginBottom: 0 }}>{message.content_text}</p>
                  {message.role === "assistant" ? renderAssistantMeta(message) : null}
                </article>
              ))}
              {busy ? (
                <article className="agentMessage assistant">
                  <div className="agentMessageMeta">
                    <strong>Agent</strong>
                    <span className="muted">处理中</span>
                  </div>
                  <p style={{ marginBottom: 0 }}>正在读取项目状态、轻量检索本地上下文并整理回复...</p>
                </article>
              ) : null}
              <div ref={messageEndRef} />
            </div>
            {latestRun?.tool_calls?.length ? (
              <div className="agentToolCallList">
                {latestRun.tool_calls.map((toolCall) => (
                  <div key={toolCall.id} className="diffItem">
                    <strong>{toolCall.tool_name}</strong>
                    <p style={{ margin: "6px 0 0" }}>
                      {toolCall.result_summary ?? `${toolCall.call_status} / ${toolCall.approval_policy}`}
                    </p>
                  </div>
                ))}
              </div>
            ) : null}
            <form onSubmit={sendMessage} className="agentInputForm">
              <textarea
                rows={4}
                value={input}
                onChange={(event) => setInput(event.target.value)}
                placeholder="例如：当前项目卡在哪一步？ / 请重建 Story Bible / 对当前章节分镜提出优化意见并自动改提示词后重跑 / 请把失败章节切到某个模型后批量重跑"
                data-testid="project-agent-input"
              />
              <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
                <span className="muted">当前已接通读侧诊断，以及经你批准后的步骤运行、Story Bible 重建和提示词改写重生成。</span>
                <button className="primary" type="submit" disabled={busy || !input.trim()} data-testid="project-agent-send-button">
                  {busy ? "发送中..." : "发送给 Agent"}
                </button>
              </div>
            </form>
            {error ? <p className="muted">{error}</p> : null}
          </section>
        </>
      ) : null}
    </div>
  );
}
