"use client";

import { FormEvent, useEffect, useMemo, useRef, useState } from "react";

type AgentPanelProps = {
  projectId: string;
  projectName: string;
  projectStatus: string;
  targetDurationSec: number;
  selectedStepName: string | null;
  selectedChapterId: string | null;
  selectedChapterTitle: string | null;
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
  result_summary: string | null;
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
  selectedStepName,
  selectedChapterId,
  selectedChapterTitle,
}: AgentPanelProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [session, setSession] = useState<AgentSession | null>(null);
  const [messages, setMessages] = useState<AgentMessage[]>([]);
  const [busy, setBusy] = useState(false);
  const [input, setInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [latestRun, setLatestRun] = useState<AgentRun | null>(null);
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
    const [sessionRes, messagesRes] = await Promise.all([
      fetch(`${apiBase}/api/v1/projects/${projectId}/agent/sessions/default`, { cache: "no-store" }),
      fetch(`${apiBase}/api/v1/projects/${projectId}/agent/sessions/default/messages`, { cache: "no-store" }),
    ]);
    if (!sessionRes.ok) {
      throw new Error("加载 Agent 会话失败");
    }
    if (!messagesRes.ok) {
      throw new Error("加载 Agent 消息失败");
    }
    const sessionData = (await sessionRes.json()) as AgentSession;
    const messageData = (await messagesRes.json()) as AgentMessage[];
    setSession(sessionData);
    setMessages(messageData.filter((item) => item.visibility !== "hidden"));
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
            selected_step_name: selectedStepName,
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
      setSession(data.session);
      setLatestRun(data.run);
      setMessages((current) => [...current, data.user_message, data.assistant_message]);
      setInput("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "发送消息失败");
    } finally {
      setBusy(false);
    }
  }

  function renderAssistantMeta(message: AgentMessage) {
    const content = asRecord(message.content_json);
    const sources = asList(content.sources);
    const nextActions = asList(content.suggested_next_actions);
    const approval = asRecord(content.approval_request);

    return (
      <>
        {approval.status ? (
          <div className="agentApprovalCard">
            <strong>{asString(approval.status)}</strong>
            <p>{asString(approval.reason, "当前请求涉及写操作。")}</p>
            <p className="muted" style={{ marginBottom: 0 }}>
              {asString(approval.policy, "请在充分知情后再给予明确授权。")}
            </p>
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
                当前页面焦点: {selectedStepName ?? "-"} / {selectedChapterTitle ?? "-"}
              </div>
            </div>
          </section>

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
                placeholder="例如：当前项目卡在哪一步？ / 帮我总结一下 Story Bible / 哪些章节处于返工状态？"
                data-testid="project-agent-input"
              />
              <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
                <span className="muted">当前版本先以读侧诊断和审批式写操作骨架为主。</span>
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
