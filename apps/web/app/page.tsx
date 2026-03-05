"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

type Project = {
  id: string;
  name: string;
  status: string;
  target_duration_sec: number;
  created_at: string;
};

type DemoCase = {
  id: string;
  title: string;
  description: string;
  file_name: string;
  recommended_project_name: string;
  target_duration_sec: number;
  available: boolean;
  char_count: number | null;
  line_count: number | null;
};

type StylePreset = {
  id: string;
  label: string;
  description: string;
};

const apiBase = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

export default function HomePage() {
  const router = useRouter();
  const [projects, setProjects] = useState<Project[]>([]);
  const [demoCase, setDemoCase] = useState<DemoCase | null>(null);
  const [demoLoading, setDemoLoading] = useState(true);
  const [name, setName] = useState("新建小说项目");
  const [duration, setDuration] = useState(120);
  const [stylePresets, setStylePresets] = useState<StylePreset[]>([]);
  const [presetId, setPresetId] = useState("cinematic");
  const [customStyle, setCustomStyle] = useState("");
  const [customDirectives, setCustomDirectives] = useState("");
  const [busyAction, setBusyAction] = useState<"create" | "import" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const sortedProjects = useMemo(
    () => [...projects].sort((a, b) => +new Date(b.created_at) - +new Date(a.created_at)),
    [projects]
  );

  async function loadProjects() {
    const res = await fetch(`${apiBase}/api/v1/projects`, { cache: "no-store" });
    if (!res.ok) {
      throw new Error("加载项目失败");
    }
    const data = (await res.json()) as Project[];
    setProjects(data);
  }

  async function loadDemoCase() {
    setDemoLoading(true);
    try {
      const res = await fetch(`${apiBase}/api/v1/demo-cases`, { cache: "no-store" });
      if (!res.ok) {
        throw new Error("加载演示案例失败");
      }
      const data = (await res.json()) as DemoCase[];
      setDemoCase(data.find((item) => item.id === "1408") ?? null);
    } finally {
      setDemoLoading(false);
    }
  }

  async function loadStylePresets() {
    const res = await fetch(`${apiBase}/api/v1/style-presets`, { cache: "no-store" });
    if (!res.ok) {
      throw new Error("加载风格预设失败");
    }
    const data = (await res.json()) as StylePreset[];
    setStylePresets(data);
  }

  async function createProject() {
    setBusyAction("create");
    setError(null);
    try {
      const res = await fetch(`${apiBase}/api/v1/projects`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name,
          target_duration_sec: duration,
          style_profile: {
            preset_id: presetId,
            custom_style: customStyle,
            custom_directives: customDirectives,
          },
        }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "创建失败");
      }
      await loadProjects();
    } catch (err) {
      setError(err instanceof Error ? err.message : "未知错误");
    } finally {
      setBusyAction(null);
    }
  }

  async function importDemoProject() {
    setBusyAction("import");
    setError(null);
    try {
      const res = await fetch(`${apiBase}/api/v1/demo-cases/1408/import`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: demoCase?.recommended_project_name ?? "1408 Demo",
          target_duration_sec: demoCase?.target_duration_sec ?? 90,
        }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "导入失败");
      }
      const project = (await res.json()) as Project;
      router.push(`/projects/${project.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "导入失败");
    } finally {
      setBusyAction(null);
    }
  }

  useEffect(() => {
    Promise.all([loadProjects(), loadDemoCase(), loadStylePresets()]).catch((err) =>
      setError(err instanceof Error ? err.message : "加载失败")
    );
  }, []);

  return (
    <main className="shell">
      <section className="card showcase">
        <div className="showcaseGrid">
          <div>
            <p className="eyebrow">One-Click Demo</p>
            <h1>Novel-to-Video 工作台 v1.0.0</h1>
            <p className="muted">
              支持分步审核、提示词重生成、模型切换重跑。现在可以直接从首页导入本机准备好的《1408》演示文本。
            </p>
            <div className="actionsRow">
              <button
                className="primary"
                onClick={importDemoProject}
                disabled={busyAction !== null || demoLoading || !demoCase?.available}
              >
                {busyAction === "import" ? "导入中..." : "一键导入 1408 Demo"}
              </button>
              <span className="pill">
                {demoLoading
                  ? "正在检查本机演示素材"
                  : demoCase?.available
                    ? "本机演示素材已就绪"
                    : "未检测到本机演示素材"}
              </span>
            </div>
          </div>

          <article className="demoCard">
            <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
              <div>
                <h2 style={{ marginBottom: 6 }}>{demoCase?.title ?? "1408"}</h2>
                <p className="muted" style={{ marginTop: 0 }}>
                  {demoCase?.description ?? "酒店单场景惊悚短篇，适合做导入与审核演示。"}
                </p>
              </div>
              <span className="pill">{demoCase?.file_name ?? "1408.txt"}</span>
            </div>
            <div className="metricRow">
              <div className="metric">
                <span className="metricLabel">目标时长</span>
                <strong>{demoCase?.target_duration_sec ?? 90}s</strong>
              </div>
              <div className="metric">
                <span className="metricLabel">字符数</span>
                <strong>{demoLoading ? "..." : demoCase?.char_count ?? "-"}</strong>
              </div>
              <div className="metric">
                <span className="metricLabel">行数</span>
                <strong>{demoLoading ? "..." : demoCase?.line_count ?? "-"}</strong>
              </div>
            </div>
            <p className="muted" style={{ marginBottom: 0 }}>
              导入动作会自动创建项目、登记 `1408.txt` 源文件，并跳转到该项目的审核页。
            </p>
          </article>
        </div>
      </section>

      <section className="card">
        <h2>手动创建项目</h2>
        <p className="muted">也可以继续手动创建空项目，再自行上传 PDF/TXT 文本。</p>
        <div className="row">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="项目名称" />
          <input
            type="number"
            min={15}
            max={7200}
            value={duration}
            onChange={(e) => setDuration(Number(e.target.value))}
          />
          <select value={presetId} onChange={(e) => setPresetId(e.target.value)}>
            {stylePresets.map((preset) => (
              <option key={preset.id} value={preset.id}>
                {preset.label}
              </option>
            ))}
          </select>
          <button className="primary" onClick={createProject} disabled={busyAction !== null}>
            {busyAction === "create" ? "创建中..." : "创建项目"}
          </button>
        </div>
        <textarea
          rows={2}
          value={customStyle}
          onChange={(e) => setCustomStyle(e.target.value)}
          placeholder="可选：自定义风格名，例如“废土宗教机械感”"
          style={{ width: "100%", marginTop: 10 }}
        />
        <textarea
          rows={3}
          value={customDirectives}
          onChange={(e) => setCustomDirectives(e.target.value)}
          placeholder="可选：补充风格约束，例如镜头语言、配色、材质、光线、动势"
          style={{ width: "100%", marginTop: 10 }}
        />
        {error ? <p className="muted">{error}</p> : null}
      </section>

      <section className="card">
        <h2>项目列表</h2>
        {sortedProjects.length === 0 ? <p className="muted">暂无项目</p> : null}
        {sortedProjects.map((project) => (
          <div key={project.id} className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
            <Link href={`/projects/${project.id}`}>
              <strong>{project.name}</strong>
            </Link>
            <span className="pill">{project.status}</span>
            <span className="muted">{project.target_duration_sec}s</span>
          </div>
        ))}
      </section>
    </main>
  );
}
