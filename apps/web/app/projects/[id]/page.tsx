"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

type Project = {
  id: string;
  name: string;
  status: string;
  target_duration_sec: number;
  style_profile: Record<string, unknown>;
  model_bindings: Record<string, Array<{ provider: string; model: string }>>;
  input_path: string | null;
  output_path: string | null;
  updated_at: string;
};

type Step = {
  id: string;
  step_name: string;
  step_display_name: string;
  step_order: number;
  status: string;
  model_provider: string | null;
  model_name: string | null;
  output_ref: Record<string, unknown>;
};

type SourceDocument = {
  id: string;
  file_name: string;
  file_type: string;
  storage_key: string | null;
  parse_status: string;
};

type Chapter = {
  id: string;
  chapter_index: number;
  chunk_index: number;
  title: string;
  summary: string;
  content_excerpt: string;
  stage_status: string;
  stage_map: Record<string, string>;
  consistency_score?: number | null;
  meta: Record<string, unknown>;
};

type ProviderCatalog = {
  provider: string;
  step: string;
  models: string[];
  model_pricing?: Record<string, Record<string, string | number | null>>;
};

type ProjectRunResponse = {
  project_id: string;
  status: string;
  current_step: Step | null;
};

type BatchStepRunResponse = {
  project_id: string;
  step_name: string;
  total: number;
  succeeded: number;
  failed: number;
  skipped: number;
  chapter_results: Array<{
    chapter_id: string;
    chapter_title: string;
    status: string;
    detail: string;
  }>;
  current_step: Step | null;
};

type ExportRead = {
  id: string;
  status: string;
  output_key: string | null;
  error_message: string | null;
};

type StoryboardVersion = {
  id: string;
  step_id: string;
  version_index: number;
  source_attempt: number;
  model_provider: string | null;
  model_name: string | null;
  output_snapshot: Record<string, unknown>;
  prompt_snapshot: Record<string, unknown>;
  consistency_score: number | null;
  consistency_report: Record<string, unknown>;
  rollback_reason: string | null;
  is_active: boolean;
  created_at: string;
};

type StylePreset = {
  id: string;
  label: string;
  description: string;
};

type PromptTemplate = {
  step_name: string;
  step_display_name: string;
  template_id: string;
  label: string;
  description: string;
  system_prompt: string;
  task_prompt: string;
};

type StoryBibleEntity = {
  name: string;
  description?: string;
  visual_anchor?: string;
  reference_image_url?: string;
  reference_storage_key?: string;
};

const apiBase = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";
const localOnlySteps = new Set(["ingest_parse", "chapter_chunking"]);
const textEditableSteps = new Set(["ingest_parse", "chapter_chunking", "story_scripting", "shot_detailing"]);
const chapterScopedSteps = new Set(["story_scripting", "shot_detailing", "storyboard_image", "consistency_check", "segment_video"]);
const mediaFocusedSteps = new Set(["storyboard_image", "consistency_check", "segment_video"]);

const stepTypeByName: Record<string, string> = {
  ingest_parse: "chunk",
  chapter_chunking: "chunk",
  story_scripting: "script",
  shot_detailing: "shot_detail",
  storyboard_image: "image",
  consistency_check: "consistency",
  segment_video: "video",
  stitch_subtitle_tts: "tts",
};

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function asString(value: unknown, fallback = "-"): string {
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return fallback;
}

function asNumber(value: unknown, fallback = 0): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

function asList(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function clipText(value: string, limit = 120): string {
  return value.length > limit ? `${value.slice(0, limit)}...` : value;
}

function dimensionLabel(key: string): string {
  const mapping: Record<string, string> = {
    chapter_internal_character: "章节内人物连续性",
    chapter_internal_scene: "章节内场景连续性",
    reference_adherence: "Story Bible 贴合度",
    cross_chapter_style: "跨章节风格连续性",
  };
  return mapping[key] ?? key;
}

function summarizeStoryboardSnapshot(snapshot: Record<string, unknown>) {
  const artifact = asRecord(snapshot.artifact);
  const prompt = asRecord(snapshot.prompt);
  const rollback = asRecord(snapshot.rollback_required);
  const consistency = asRecord(snapshot.consistency);

  return {
    artifactSummary: clipText(asString(artifact.summary, "暂无生成摘要"), 140),
    artifactId: asString(artifact.artifact_id),
    provider: asString(artifact.provider),
    model: asString(artifact.model),
    taskPrompt: clipText(asString(prompt.task, "暂无任务提示词"), 120),
    systemPrompt: clipText(asString(prompt.system, "暂无系统提示词"), 120),
    rollbackReason: clipText(asString(rollback.reason, ""), 180),
    selectedVersionId: asString(snapshot.selected_storyboard_version_id, ""),
    consistencyScore: typeof consistency.score === "number" ? consistency.score : null,
    thumbnailUrl: asString(artifact.thumbnail_url, ""),
    imageUrl: asString(artifact.image_url, ""),
  };
}

function flattenForDiff(value: unknown, prefix = "", depth = 0, acc: Record<string, string> = {}) {
  if (depth > 2) {
    acc[prefix] = clipText(asString(value), 80);
    return acc;
  }
  if (Array.isArray(value)) {
    acc[prefix || "items"] = clipText(value.map((item) => asString(item)).join(", "), 80);
    return acc;
  }
  if (value && typeof value === "object") {
    Object.entries(value as Record<string, unknown>).forEach(([key, nested]) => {
      const nextPrefix = prefix ? `${prefix}.${key}` : key;
      flattenForDiff(nested, nextPrefix, depth + 1, acc);
    });
    return acc;
  }
  acc[prefix || "value"] = clipText(asString(value), 80);
  return acc;
}

function buildDiffSummary(base: Record<string, unknown>, candidate: Record<string, unknown>): string[] {
  const left = flattenForDiff(base);
  const right = flattenForDiff(candidate);
  const keys = Array.from(new Set([...Object.keys(left), ...Object.keys(right)])).sort();
  const changes = keys
    .filter((key) => left[key] !== right[key])
    .map((key) => `${key}: ${left[key] ?? "-"} -> ${right[key] ?? "-"}`);
  return changes.slice(0, 6);
}

function resolveMediaUrl(url: string): string {
  if (!url) return "";
  if (url.startsWith("http://") || url.startsWith("https://") || url.startsWith("data:")) return url;
  if (url.startsWith("/")) return `${apiBase}${url}`;
  return `${apiBase}/${url}`;
}

function resolveDownloadUrl(url: string): string {
  const resolved = resolveMediaUrl(url);
  if (!resolved || resolved.startsWith("data:")) return resolved;
  if (resolved.includes("/api/v1/local-files/")) {
    return resolved.includes("?") ? `${resolved}&download=1` : `${resolved}?download=1`;
  }
  return resolved;
}

function modelPricingLabel(
  provider: string,
  model: string,
  catalogs: ProviderCatalog[]
): string {
  if (provider !== "openrouter") return model;
  const catalog = catalogs.find((item) => item.provider === provider && item.models.includes(model));
  const pricing = asRecord(catalog?.model_pricing?.[model]);
  const formatUsd = (amount: number, maxFraction = 4): string =>
    amount.toLocaleString("en-US", {
      minimumFractionDigits: 0,
      maximumFractionDigits: maxFraction,
    });
  const formatTokenPrice = (value: unknown): string => {
    const amount = Number(value);
    if (!Number.isFinite(amount) || amount <= 0) return "";
    const perMillion = amount * 1_000_000;
    return `${formatUsd(perMillion, perMillion >= 1 ? 2 : 4)}美元/百万token`;
  };
  const formatUnitPrice = (value: unknown): string => {
    const amount = Number(value);
    if (!Number.isFinite(amount) || amount <= 0) return "";
    return `${formatUsd(amount, 6)}美元/次`;
  };
  const input = formatTokenPrice(pricing.input);
  const output = formatTokenPrice(pricing.output);
  const image = formatUnitPrice(pricing.image);
  const request = formatUnitPrice(pricing.request);
  const segments: string[] = [];
  if (input) segments.push(`输入 ${input}`);
  if (output) segments.push(`输出 ${output}`);
  if (image) segments.push(`图片 ${image}`);
  if (request) segments.push(`请求 ${request}`);
  return segments.length > 0 ? `${model} (${segments.join(" / ")})` : model;
}

function extractStoryboardGallery(output: Record<string, unknown>): Record<string, unknown> {
  const gallery = asRecord(output.storyboard_gallery);
  if (Object.keys(gallery).length > 0) return gallery;
  const artifact = asRecord(output.artifact);
  if (Array.isArray(artifact.frames)) {
    return {
      frame_count: artifact.frame_count,
      frames: artifact.frames,
      contact_sheet_url: artifact.thumbnail_url,
      gallery_export_url: artifact.gallery_export_url,
      cover_image_url: artifact.cover_image_url,
    };
  }
  return {};
}

function extractStoryboardFrames(output: Record<string, unknown>): Record<string, unknown>[] {
  const gallery = extractStoryboardGallery(output);
  const frames = gallery.frames ?? asRecord(output.artifact).frames;
  return asList(frames).map((item) => asRecord(item)).filter((item) => Object.keys(item).length > 0);
}

function chapterStageOutput(chapter: Chapter | null, stepName: string | undefined): Record<string, unknown> {
  if (!chapter || !stepName) return {};
  const meta = asRecord(chapter.meta);
  const stages = asRecord(meta.stages);
  const stage = asRecord(stages[stepName]);
  return asRecord(stage.output);
}

function stepExecutionStats(step: Step, chapter: Chapter | null): Record<string, unknown> {
  if (chapterScopedSteps.has(step.step_name)) {
    return asRecord(chapterStageOutput(chapter, step.step_name).execution_stats);
  }
  return asRecord(asRecord(step.output_ref).execution_stats);
}

function asStoryBibleEntityList(value: unknown): StoryBibleEntity[] {
  return asList(value)
    .map((item) => asRecord(item))
    .filter((item) => asString(item.name, "") !== "")
    .map((item) => ({
      name: asString(item.name, ""),
      description: asString(item.description, ""),
      visual_anchor: asString(item.visual_anchor, ""),
      reference_image_url: asString(item.reference_image_url, ""),
      reference_storage_key: asString(item.reference_storage_key, ""),
    }));
}

export default function ProjectPage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;
  const [project, setProject] = useState<Project | null>(null);
  const [steps, setSteps] = useState<Step[]>([]);
  const [chapters, setChapters] = useState<Chapter[]>([]);
  const [docs, setDocs] = useState<SourceDocument[]>([]);
  const [catalog, setCatalog] = useState<ProviderCatalog[]>([]);
  const [stylePresets, setStylePresets] = useState<StylePreset[]>([]);
  const [promptTemplates, setPromptTemplates] = useState<PromptTemplate[]>([]);
  const [selectedStepId, setSelectedStepId] = useState<string | null>(null);
  const [selectedChapterId, setSelectedChapterId] = useState<string | null>(null);
  const [systemPrompt, setSystemPrompt] = useState("你是 AI 工作流助手。");
  const [taskPrompt, setTaskPrompt] = useState("请加强人物一致性与场景衔接。");
  const [templateId, setTemplateId] = useState("");
  const [provider, setProvider] = useState("openai");
  const [modelName, setModelName] = useState("gpt-5");
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [stylePresetId, setStylePresetId] = useState("cinematic");
  const [customStyle, setCustomStyle] = useState("");
  const [customDirectives, setCustomDirectives] = useState("");
  const [latestExport, setLatestExport] = useState<ExportRead | null>(null);
  const [storyboardVersions, setStoryboardVersions] = useState<StoryboardVersion[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [actionProgress, setActionProgress] = useState(0);

  const selected = useMemo(
    () => steps.find((step) => step.id === selectedStepId) ?? steps[0] ?? null,
    [selectedStepId, steps]
  );
  const selectedChapter = useMemo(
    () => chapters.find((chapter) => chapter.id === selectedChapterId) ?? chapters[0] ?? null,
    [chapters, selectedChapterId]
  );
  const storyBible = useMemo(
    () => asRecord(asRecord(project?.style_profile).story_bible),
    [project?.style_profile]
  );
  const storyBibleCharacters = useMemo(() => asStoryBibleEntityList(storyBible.characters), [storyBible]);
  const storyBibleScenes = useMemo(() => asStoryBibleEntityList(storyBible.scenes), [storyBible]);

  const selectedOutput = useMemo(() => {
    if (!selected) return {};
    if (chapterScopedSteps.has(selected.step_name)) {
      const chapterOutput = chapterStageOutput(selectedChapter, selected.step_name);
      if (Object.keys(chapterOutput).length > 0) return chapterOutput;
    }
    return asRecord(selected.output_ref);
  }, [selected, selectedChapter]);
  const currentStoryboardSummary = useMemo(() => summarizeStoryboardSnapshot(selectedOutput), [selectedOutput]);
  const activeStoryboardVersion = useMemo(
    () => storyboardVersions.find((version) => version.is_active) ?? storyboardVersions[0] ?? null,
    [storyboardVersions]
  );
  const selectedArtifact = useMemo(() => asRecord(selectedOutput.artifact), [selectedOutput]);
  const selectedStoryboardGallery = useMemo(() => extractStoryboardGallery(selectedOutput), [selectedOutput]);
  const selectedStoryboardFrames = useMemo(() => extractStoryboardFrames(selectedOutput), [selectedOutput]);
  const consistencyPayload = useMemo(() => asRecord(selectedOutput.consistency), [selectedOutput]);
  const consistencyDetails = useMemo(() => asRecord(consistencyPayload.details), [consistencyPayload]);
  const videoConsistency = useMemo(() => asRecord(selectedOutput.video_consistency), [selectedOutput]);
  const mediaHeroUrl = useMemo(
    () =>
      asString(
        selectedStoryboardGallery.contact_sheet_url,
        asString(selectedArtifact.thumbnail_url, asString(selectedArtifact.image_url, ""))
      ),
    [selectedArtifact, selectedStoryboardGallery]
  );
  const galleryExportUrl = useMemo(
    () => asString(selectedStoryboardGallery.gallery_export_url, asString(selectedArtifact.gallery_export_url, "")),
    [selectedArtifact, selectedStoryboardGallery]
  );
  const coverImageUrl = useMemo(
    () => asString(selectedStoryboardGallery.cover_image_url, asString(selectedArtifact.cover_image_url, "")),
    [selectedArtifact, selectedStoryboardGallery]
  );
  const chapterVideoPreviewUrl = useMemo(
    () => asString(selectedArtifact.preview_url, asString(selectedArtifact.export_url, "")),
    [selectedArtifact]
  );
  const chapterConsistencyScores = useMemo(
    () => asList(selectedOutput.chapter_consistency_scores).map((item) => asRecord(item)),
    [selectedOutput]
  );
  const executionStats = useMemo(() => asRecord(selectedOutput.execution_stats), [selectedOutput]);

  const stepModelOptions = useMemo(() => {
    if (!selected) return [];
    const stepType = stepTypeByName[selected.step_name];
    return catalog.filter((item) => item.step === stepType);
  }, [catalog, selected]);

  const allModelsForSelectedStep = useMemo(() => {
    const merged: Array<{ provider: string; model: string }> = [];
    stepModelOptions.forEach((group) =>
      group.models.forEach((model) => merged.push({ provider: group.provider, model }))
    );
    return merged;
  }, [stepModelOptions]);

  const providerOptions = useMemo(
    () => Array.from(new Set(stepModelOptions.map((item) => item.provider))),
    [stepModelOptions]
  );

  const modelOptions = useMemo(() => {
    const selectedProviderGroup = stepModelOptions.find((item) => item.provider === provider);
    return selectedProviderGroup?.models ?? [];
  }, [provider, stepModelOptions]);

  const suggestedModelPreview = useMemo(() => {
    if (allModelsForSelectedStep.length === 0) return "暂无";
    const preview = allModelsForSelectedStep
      .slice(0, 10)
      .map((item) => `${item.provider}/${item.model}`)
      .join(" | ");
    const remaining = allModelsForSelectedStep.length - 10;
    return remaining > 0 ? `${preview} | ...共 ${allModelsForSelectedStep.length} 个模型` : preview;
  }, [allModelsForSelectedStep]);

  const selectedStepTemplates = useMemo(
    () => promptTemplates.filter((item) => item.step_name === selected?.step_name),
    [promptTemplates, selected?.step_name]
  );
  const overallProgress = useMemo(() => {
    if (steps.length === 0) return 0;
    const approved = steps.filter((step) => step.status === "APPROVED").length;
    const reviewing = steps.filter((step) => step.status === "REVIEW_REQUIRED").length;
    const generating = steps.filter((step) => step.status === "GENERATING").length;
    const weighted = approved + reviewing * 0.75 + generating * 0.35;
    return Math.round((weighted / steps.length) * 100);
  }, [steps]);

  async function loadProject() {
    if (!projectId) return;
    const res = await fetch(`${apiBase}/api/v1/projects/${projectId}`, { cache: "no-store" });
    if (!res.ok) throw new Error("加载项目信息失败");
    const data = (await res.json()) as Project;
    setProject(data);
  }

  async function loadSteps() {
    if (!projectId) return;
    const res = await fetch(`${apiBase}/api/v1/projects/${projectId}/steps`, { cache: "no-store" });
    if (!res.ok) throw new Error("加载步骤失败");
    const data = (await res.json()) as Step[];
    setSteps(data);
    if (!selectedStepId && data.length > 0) {
      setSelectedStepId(data[0].id);
      setProvider(data[0].model_provider ?? "openai");
      setModelName(data[0].model_name ?? "gpt-5");
    }
  }

  async function loadDocuments() {
    if (!projectId) return;
    const res = await fetch(`${apiBase}/api/v1/projects/${projectId}/source-documents`, { cache: "no-store" });
    if (!res.ok) throw new Error("加载源文件失败");
    const data = (await res.json()) as SourceDocument[];
    setDocs(data);
  }

  async function loadChapters() {
    if (!projectId) return;
    const res = await fetch(`${apiBase}/api/v1/projects/${projectId}/chapters`, { cache: "no-store" });
    if (!res.ok) throw new Error("加载章节失败");
    const data = (await res.json()) as Chapter[];
    setChapters(data);
    if (!selectedChapterId && data.length > 0) {
      setSelectedChapterId(data[0].id);
    }
  }

  async function loadCatalog() {
    const res = await fetch(`${apiBase}/api/v1/providers/models`, { cache: "no-store" });
    if (!res.ok) throw new Error("加载模型目录失败");
    const data = (await res.json()) as ProviderCatalog[];
    setCatalog(data);
  }

  async function loadStylePresets() {
    const res = await fetch(`${apiBase}/api/v1/style-presets`, { cache: "no-store" });
    if (!res.ok) throw new Error("加载风格预设失败");
    const data = (await res.json()) as StylePreset[];
    setStylePresets(data);
  }

  async function loadPromptTemplates() {
    const res = await fetch(`${apiBase}/api/v1/prompt-templates`, { cache: "no-store" });
    if (!res.ok) throw new Error("加载提示词模板失败");
    const data = (await res.json()) as PromptTemplate[];
    setPromptTemplates(data);
  }

  async function loadStoryboardVersions(step: Step | null) {
    if (!projectId || !step || step.step_name !== "storyboard_image") {
      setStoryboardVersions([]);
      return;
    }
    const suffix = selectedChapter ? `?chapter_id=${selectedChapter.id}` : "";
    const res = await fetch(`${apiBase}/api/v1/projects/${projectId}/steps/${step.id}/storyboard-versions${suffix}`, {
      cache: "no-store",
    });
    if (!res.ok) throw new Error("加载分镜版本失败");
    const data = (await res.json()) as StoryboardVersion[];
    setStoryboardVersions(data);
  }

  async function refreshAll() {
    await Promise.all([loadProject(), loadSteps(), loadChapters(), loadDocuments(), loadCatalog(), loadStylePresets(), loadPromptTemplates()]);
  }

  function syncSelectionFromResponse(data: unknown) {
    if (!data || typeof data !== "object") return;
    const record = data as Record<string, unknown>;
    if (typeof record.id === "string") {
      setSelectedStepId(record.id);
      return;
    }
    const currentStep = record.current_step;
    if (currentStep && typeof currentStep === "object" && typeof (currentStep as Record<string, unknown>).id === "string") {
      setSelectedStepId((currentStep as Record<string, unknown>).id as string);
    }
  }

  async function runProject() {
    if (!projectId || !selected) return;
    await runCurrentStep(true);
  }

  async function runCurrentStep(force = true) {
    if (!projectId || !selected) return;
    setPendingAction(`正在运行：${selected.step_display_name}`);
    setBusy(true);
    setError(null);
    setActionMessage(null);
    try {
      const res = await fetch(`${apiBase}/api/v1/projects/${projectId}/steps/${selected.step_name}/run`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          force,
          chapter_id: chapterScopedSteps.has(selected.step_name) ? selectedChapter?.id ?? null : null,
          params: chapterScopedSteps.has(selected.step_name) && selectedChapter ? { chapter_id: selectedChapter.id } : {},
        }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "步骤运行失败");
      }
      const data = await res.json();
      syncSelectionFromResponse(data);
      await refreshAll();
      setActionProgress(100);
      setActionMessage(`已完成：${data.step_display_name ?? selected.step_display_name}`);
    } catch (err) {
      setActionProgress(0);
      setError(err instanceof Error ? err.message : "步骤运行失败");
    } finally {
      setPendingAction(null);
      setBusy(false);
    }
  }

  async function runCurrentStepForAllChapters() {
    if (!projectId || !selected || !chapterScopedSteps.has(selected.step_name)) return;
    setPendingAction(`正在批量运行：${selected.step_display_name}`);
    setBusy(true);
    setError(null);
    setActionMessage(null);
    try {
      const res = await fetch(`${apiBase}/api/v1/projects/${projectId}/steps/${selected.step_name}/run-all-chapters`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force: true, params: {} }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "批量运行失败");
      }
      const data = (await res.json()) as BatchStepRunResponse;
      syncSelectionFromResponse(data);
      await refreshAll();
      setActionProgress(100);
      setActionMessage(
        `批量运行完成：成功 ${data.succeeded} 章，失败 ${data.failed} 章，跳过 ${data.skipped} 章。`
      );
    } catch (err) {
      setActionProgress(0);
      setError(err instanceof Error ? err.message : "批量运行失败");
    } finally {
      setPendingAction(null);
      setBusy(false);
    }
  }

  async function uploadSourceDocument() {
    if (!projectId || !uploadFile) return;
    setPendingAction("正在上传源文件...");
    setBusy(true);
    setError(null);
    setActionMessage(null);
    try {
      const form = new FormData();
      form.append("file", uploadFile);
      const res = await fetch(`${apiBase}/api/v1/projects/${projectId}/source-documents`, {
        method: "POST",
        body: form,
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "上传失败");
      }
      setUploadFile(null);
      await refreshAll();
      setActionProgress(100);
      setActionMessage("源文件已上传并登记");
    } catch (err) {
      setActionProgress(0);
      setError(err instanceof Error ? err.message : "上传失败");
    } finally {
      setPendingAction(null);
      setBusy(false);
    }
  }

  async function bindModelForCurrentStep() {
    if (!projectId || !selected) return;
    setPendingAction(`正在绑定模型：${provider}/${modelName}`);
    setBusy(true);
    setError(null);
    setActionMessage(null);
    try {
      const payload = {
        bindings: {
          [selected.step_name]: [{ provider, model: modelName }],
        },
      };
      const res = await fetch(`${apiBase}/api/v1/projects/${projectId}/model-bindings`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "绑定失败");
      }
      await refreshAll();
      setActionProgress(100);
      setActionMessage(`已绑定模型：${provider}/${modelName}`);
    } catch (err) {
      setActionProgress(0);
      setError(err instanceof Error ? err.message : "绑定失败");
    } finally {
      setPendingAction(null);
      setBusy(false);
    }
  }

  async function saveStyleProfile() {
    if (!projectId) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${apiBase}/api/v1/projects/${projectId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          style_profile: {
            preset_id: stylePresetId,
            custom_style: customStyle,
            custom_directives: customDirectives,
          },
        }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "保存风格失败");
      }
      await refreshAll();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存风格失败");
    } finally {
      setBusy(false);
    }
  }

  async function rebuildStoryBibleReferences() {
    if (!projectId) return;
    setPendingAction("正在重建 Story Bible 人物/场景参考图...");
    setBusy(true);
    setError(null);
    setActionMessage(null);
    try {
      const res = await fetch(`${apiBase}/api/v1/projects/${projectId}/story-bible/rebuild`, {
        method: "POST",
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "重建 Story Bible 失败");
      }
      await refreshAll();
      setActionProgress(100);
      setActionMessage("已重建 Story Bible 人物/场景参考图。");
    } catch (err) {
      setActionProgress(0);
      setError(err instanceof Error ? err.message : "重建 Story Bible 失败");
    } finally {
      setPendingAction(null);
      setBusy(false);
    }
  }

  async function postAction(path: string, body: Record<string, unknown>) {
    if (!selected || !projectId) return;
    const actionLabel =
      path.includes("/approve") ? "正在审批通过..." :
      path.includes("/edit-continue") ? "正在保存人工编辑..." :
      path.includes("/edit-prompt-regenerate") ? "正在按提示词重生成..." :
      path.includes("/switch-model-rerun") ? "正在切换模型重跑..." :
      "正在处理操作...";
    setPendingAction(actionLabel);
    setBusy(true);
    setError(null);
    setActionMessage(null);
    try {
      const res = await fetch(`${apiBase}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...body,
          chapter_id: chapterScopedSteps.has(selected.step_name) ? selectedChapter?.id ?? null : null,
        }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "动作执行失败");
      }
      const data = await res.json();
      syncSelectionFromResponse(data);
      await refreshAll();
      setActionProgress(100);
      if (path.includes("/approve") || path.includes("/edit-continue")) {
        const nextStep = (data as ProjectRunResponse).current_step;
        if (nextStep && selected && nextStep.id === selected.id && selectedChapter) {
          setActionMessage(`已通过当前章节，等待继续处理下一章节或重新选择章节。`);
        } else {
          setActionMessage(nextStep ? `已通过，下一阶段：${nextStep.step_display_name}` : "已通过，流程已到末尾");
        }
      } else if (path.includes("/switch-model-rerun")) {
        setActionMessage(`已切换模型并重跑：${provider}/${modelName}`);
      } else if (path.includes("/edit-prompt-regenerate")) {
        setActionMessage("已按新提示词重新生成");
      } else {
        setActionMessage("操作已完成");
      }
    } catch (err) {
      setActionProgress(0);
      setError(err instanceof Error ? err.message : "动作失败");
    } finally {
      setPendingAction(null);
      setBusy(false);
    }
  }

  async function postBatchAction(path: string, body: Record<string, unknown>, successMessage: string) {
    if (!selected || !projectId) return;
    setPendingAction("正在批量处理当前章节动作...");
    setBusy(true);
    setError(null);
    setActionMessage(null);
    try {
      const res = await fetch(`${apiBase}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "批量动作执行失败");
      }
      const data = (await res.json()) as BatchStepRunResponse;
      syncSelectionFromResponse(data);
      await refreshAll();
      setActionProgress(100);
      setActionMessage(`${successMessage} 成功 ${data.succeeded} 章，失败 ${data.failed} 章，跳过 ${data.skipped} 章。`);
    } catch (err) {
      setActionProgress(0);
      setError(err instanceof Error ? err.message : "批量动作执行失败");
    } finally {
      setPendingAction(null);
      setBusy(false);
    }
  }

  async function renderFinal() {
    if (!projectId) return;
    setPendingAction("正在导出成片...");
    setBusy(true);
    setError(null);
    setActionMessage(null);
    try {
      const res = await fetch(`${apiBase}/api/v1/projects/${projectId}/render/final`, { method: "POST" });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "导出失败");
      }
      const data = (await res.json()) as ExportRead;
      setLatestExport(data);
      await refreshAll();
      setActionProgress(100);
      setActionMessage("成片导出任务已创建");
    } catch (err) {
      setActionProgress(0);
      setError(err instanceof Error ? err.message : "导出失败");
    } finally {
      setPendingAction(null);
      setBusy(false);
    }
  }

  async function selectStoryboardVersion(versionId: string) {
    if (!projectId || !selected) return;
    setPendingAction("正在切换分镜版本...");
    setBusy(true);
    setError(null);
    setActionMessage(null);
    try {
      const res = await fetch(
        `${apiBase}/api/v1/projects/${projectId}/steps/${selected.id}/storyboard-versions/${versionId}/select`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            created_by: "ui-reviewer",
            scope_type: "step",
            chapter_id: selectedChapter?.id ?? null,
          }),
        }
      );
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "选用分镜版本失败");
      }
      const data = await res.json();
      syncSelectionFromResponse(data);
      await refreshAll();
      setActionProgress(100);
      setActionMessage("已切换到所选分镜版本");
    } catch (err) {
      setActionProgress(0);
      setError(err instanceof Error ? err.message : "选用分镜版本失败");
    } finally {
      setPendingAction(null);
      setBusy(false);
    }
  }

  useEffect(() => {
    if (!projectId) return;
    refreshAll().catch((err) => setError(err instanceof Error ? err.message : "加载失败"));
  }, [projectId]);

  useEffect(() => {
    const styleProfile = asRecord(project?.style_profile);
    setStylePresetId(asString(styleProfile.preset_id, "cinematic"));
    setCustomStyle(asString(styleProfile.custom_style, ""));
    setCustomDirectives(asString(styleProfile.custom_directives, ""));
  }, [project?.id, project?.updated_at]);

  useEffect(() => {
    if (!selected) return;
    const promptPayload = asRecord(selectedOutput.prompt);
    const defaultTemplate = promptTemplates.find((item) => item.step_name === selected.step_name);
    setSystemPrompt(asString(promptPayload.system, defaultTemplate?.system_prompt ?? "你是 AI 工作流助手。"));
    setTaskPrompt(asString(promptPayload.task, defaultTemplate?.task_prompt ?? "请输出结构化结果。"));
    setTemplateId("");
    if (localOnlySteps.has(selected.step_name)) {
      setProvider(selected.model_provider ?? "local");
      setModelName(selected.model_name ?? "builtin-parser");
      return;
    }
    const nextProvider =
      (selected.model_provider && providerOptions.includes(selected.model_provider) ? selected.model_provider : null) ??
      providerOptions[0] ??
      selected.model_provider ??
      "openrouter";
    const nextModelOptions = stepModelOptions.find((item) => item.provider === nextProvider)?.models ?? [];
    const nextModel =
      (selected.model_name && nextModelOptions.includes(selected.model_name) ? selected.model_name : null) ??
      nextModelOptions[0] ??
      selected.model_name ??
      "";
    setProvider(nextProvider);
    setModelName(nextModel);
  }, [selected?.id, selectedChapter?.id, promptTemplates, selectedOutput, providerOptions, stepModelOptions]);

  useEffect(() => {
    if (!selected || localOnlySteps.has(selected.step_name)) return;
    if (modelOptions.length === 0) return;
    if (!modelOptions.includes(modelName)) {
      setModelName(modelOptions[0]);
    }
  }, [selected?.id, provider, modelOptions, modelName]);

  useEffect(() => {
    loadStoryboardVersions(selected).catch((err) =>
      setError(err instanceof Error ? err.message : "加载分镜版本失败")
    );
  }, [projectId, selected?.id, selected?.step_name, selectedChapter?.id]);

  useEffect(() => {
    if (!pendingAction) {
      if (actionProgress >= 100) {
        const timer = window.setTimeout(() => setActionProgress(0), 500);
        return () => window.clearTimeout(timer);
      }
      return;
    }
    setActionProgress((current) => (current > 5 ? current : 6));
    const timer = window.setInterval(() => {
      setActionProgress((current) => {
        const next = current + (current < 40 ? 10 : current < 75 ? 5 : 2);
        return next > 92 ? 92 : next;
      });
    }, 600);
    return () => window.clearInterval(timer);
  }, [pendingAction, actionProgress]);

  function renderStoryboardFrameList(frames: Record<string, unknown>[]) {
    if (frames.length === 0) {
      return <p className="muted">当前章节还没有可预览的分镜图。</p>;
    }
    return (
      <div className="storyboardGrid">
        {frames.map((frame, index) => {
          const shotIndex = asNumber(frame.shot_index, index + 1);
          const imageUrl = asString(frame.image_url, asString(frame.thumbnail_url, ""));
          const exportUrl = asString(frame.export_url, imageUrl);
          return (
            <article key={`${shotIndex}-${imageUrl || index}`} className="frameCard">
              <div className="frameCardHeader">
                <strong>镜头 {shotIndex.toString().padStart(2, "0")}</strong>
                <div className="row">
                  <span className="pill">{asString(frame.frame_type, "镜头")}</span>
                  <span className="pill">{asNumber(frame.duration_sec, 0).toFixed(1)}s</span>
                </div>
              </div>
              {imageUrl ? (
                <img
                  src={resolveMediaUrl(imageUrl)}
                  alt={`镜头 ${shotIndex}`}
                  className="framePreview"
                />
              ) : (
                <div className="framePreview framePreviewEmpty">暂无图像</div>
              )}
              <div className="frameMeta">
                <p><strong>画面</strong> {asString(frame.visual, asString(frame.summary, "暂无描述"))}</p>
                <p><strong>动作</strong> {asString(frame.action, "暂无动作描述")}</p>
                {asString(frame.dialogue, "") !== "" ? <p><strong>对白</strong> {asString(frame.dialogue, "")}</p> : null}
              </div>
              <div className="frameCardFooter">
                {exportUrl ? (
                  <a className="downloadLink" href={resolveDownloadUrl(exportUrl)}>
                    导出图片
                  </a>
                ) : null}
              </div>
            </article>
          );
        })}
      </div>
    );
  }

  function renderExecutionStatsBlock() {
    if (Object.keys(executionStats).length === 0) return null;
    const tokenUsage = asRecord(executionStats.token_usage);
    return (
      <section className="card" style={{ marginTop: 12 }}>
        <div className="mediaSectionHeader">
          <div>
            <p className="eyebrow">本阶段执行统计</p>
            <h4>用时与资源消耗</h4>
          </div>
        </div>
        <div className="metricRow">
          <div className="metric">
            <span className="metricLabel">执行耗时</span>
            <strong>{asNumber(executionStats.elapsed_sec, 0).toFixed(2)}s</strong>
          </div>
          <div className="metric">
            <span className="metricLabel">输入 Token</span>
            <strong>{asNumber(tokenUsage.input_tokens, 0)}</strong>
          </div>
          <div className="metric">
            <span className="metricLabel">输出 Token</span>
            <strong>{asNumber(tokenUsage.output_tokens, 0)}</strong>
          </div>
          <div className="metric">
            <span className="metricLabel">总 Token</span>
            <strong>{asNumber(tokenUsage.total_tokens, 0)}</strong>
          </div>
          <div className="metric">
            <span className="metricLabel">估算成本</span>
            <strong>{asNumber(executionStats.estimated_cost, 0).toFixed(6)}</strong>
          </div>
        </div>
      </section>
    );
  }

  function renderMediaPreview() {
    if (!selected) return <p className="muted">请选择步骤</p>;

    if (selected.step_name === "storyboard_image") {
      return (
        <div className="mediaWorkspace">
          <section className="mediaHeroCard">
            <div className="mediaHeroHeader">
              <div>
                <p className="eyebrow">当前章节分镜总览</p>
                <h3>{selectedChapter?.title ?? "未选择章节"}</h3>
              </div>
              <div className="actionsRow">
                {mediaHeroUrl ? (
                  <a className="downloadLink" href={resolveDownloadUrl(mediaHeroUrl)}>
                    导出总览图
                  </a>
                ) : null}
                {galleryExportUrl ? (
                  <a className="downloadLink" href={resolveDownloadUrl(galleryExportUrl)}>
                    导出全部分镜图
                  </a>
                ) : null}
                {coverImageUrl ? (
                  <a className="downloadLink subtle" href={resolveDownloadUrl(coverImageUrl)}>
                    导出参考封面
                  </a>
                ) : null}
              </div>
            </div>
            {mediaHeroUrl ? (
              <img src={resolveMediaUrl(mediaHeroUrl)} alt="当前章节分镜总览" className="mediaHeroImage" />
            ) : (
              <div className="mediaHeroImage mediaHeroEmpty">暂无总览图</div>
            )}
            <div className="metricRow">
              <div className="metric">
                <span className="metricLabel">分镜版本</span>
                <strong>{asString(selectedOutput.storyboard_version_index, "-")}</strong>
              </div>
              <div className="metric">
                <span className="metricLabel">分镜数量</span>
                <strong>{selectedStoryboardFrames.length}</strong>
              </div>
              <div className="metric">
                <span className="metricLabel">当前模型</span>
                <strong style={{ fontSize: 16 }}>{selected.model_provider}/{selected.model_name}</strong>
              </div>
            </div>
            {storyboardVersions.length > 0 ? (
              <div style={{ marginTop: 14 }}>
                <label>快捷切换版本</label>
                <select
                  value={activeStoryboardVersion?.id ?? ""}
                  onChange={(e) => {
                    if (e.target.value) {
                      void selectStoryboardVersion(e.target.value);
                    }
                  }}
                  disabled={busy}
                  style={{ width: "100%", marginTop: 8 }}
                >
                  {storyboardVersions.map((version) => (
                    <option key={version.id} value={version.id}>
                      版本 #{version.version_index} · {version.model_provider}/{version.model_name} · {version.is_active ? "当前使用" : "历史版本"}
                    </option>
                  ))}
                </select>
              </div>
            ) : null}
          </section>
          <section className="card mediaGalleryCard">
            <div className="mediaSectionHeader">
              <div>
                <p className="eyebrow">完整分镜图列表</p>
                <h4>逐镜头预览与导出</h4>
              </div>
              <span className="pill">共 {selectedStoryboardFrames.length} 张</span>
            </div>
            {renderStoryboardFrameList(selectedStoryboardFrames)}
          </section>
          <details className="card mediaDetails">
            <summary>查看原始 JSON</summary>
            <pre style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(selected.output_ref, null, 2)}</pre>
          </details>
          {renderExecutionStatsBlock()}
        </div>
      );
    }

    if (selected.step_name === "consistency_check") {
      const dimensions = asRecord(consistencyPayload.dimensions);
      return (
        <div className="mediaWorkspace">
          <section className="mediaHeroCard">
            <div className="mediaHeroHeader">
              <div>
                <p className="eyebrow">章节分镜一致性校核</p>
                <h3>{selectedChapter?.title ?? "未选择章节"}</h3>
              </div>
              <div className="actionsRow">
                {mediaHeroUrl ? (
                  <a className="downloadLink" href={resolveDownloadUrl(mediaHeroUrl)}>
                    导出总览图
                  </a>
                ) : null}
                {galleryExportUrl ? (
                  <a className="downloadLink" href={resolveDownloadUrl(galleryExportUrl)}>
                    导出全部分镜图
                  </a>
                ) : null}
              </div>
            </div>
            <div className="metricRow">
              <div className="metric">
                <span className="metricLabel">一致性分数</span>
                <strong>{asNumber(consistencyPayload.score, 0)}</strong>
              </div>
              <div className="metric">
                <span className="metricLabel">阈值</span>
                <strong>{asNumber(consistencyPayload.threshold, 0)}</strong>
              </div>
              <div className="metric">
                <span className="metricLabel">分镜数量</span>
                <strong>{selectedStoryboardFrames.length}</strong>
              </div>
              <div className="metric">
                <span className="metricLabel">评分模式</span>
                <strong style={{ fontSize: 16 }}>{asString(consistencyDetails.scoring_mode, "heuristic")}</strong>
              </div>
            </div>
            {Object.keys(dimensions).length > 0 ? (
              <div className="diffList">
                {Object.entries(dimensions).map(([key, value]) => (
                  <div key={key} className="diffItem">
                    {dimensionLabel(key)}: {asString(value)}
                  </div>
                ))}
              </div>
            ) : null}
            {selectedOutput?.rollback_required ? (
              <p className="muted" style={{ marginTop: 12 }}>
                {asString(asRecord(selectedOutput.rollback_required).reason, "一致性未通过，需返回分镜出图步骤选择新版本。")}
              </p>
            ) : null}
            {asList(consistencyDetails.low_frames).length > 0 ? (
              <div style={{ marginTop: 16 }}>
                <p className="eyebrow">低分镜头</p>
                <div className="diffList">
                  {asList(consistencyDetails.low_frames).map((item, index) => {
                    const row = asRecord(item);
                    return (
                      <div key={`${asString(row.shot_index, String(index))}-${index}`} className="diffItem">
                        镜头 {asString(row.shot_index)}: {asString(row.reason, asString(row.character_anchors, "需要人工复核"))}
                      </div>
                    );
                  })}
                </div>
              </div>
            ) : null}
            {chapterConsistencyScores.length > 0 ? (
              <div style={{ marginTop: 16 }}>
                <p className="eyebrow">全书章节一致性分数</p>
                <div className="diffList">
                  {chapterConsistencyScores.map((item) => {
                    const chapterId = asString(item.chapter_id, "");
                    return (
                      <div key={chapterId} className="diffItem" style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
                        <span>{asString(item.chapter_title)}</span>
                        <span>{asString(item.score)} 分</span>
                        <button
                          onClick={() => setSelectedChapterId(chapterId)}
                          style={{ minWidth: 96 }}
                        >
                          查看该章节
                        </button>
                      </div>
                    );
                  })}
                </div>
              </div>
            ) : null}
          </section>
          <section className="card mediaGalleryCard">
            <div className="mediaSectionHeader">
              <div>
                <p className="eyebrow">当前章节分镜</p>
                <h4>用于校核的完整分镜列表</h4>
              </div>
              <span className="pill">共 {selectedStoryboardFrames.length} 张</span>
            </div>
            {renderStoryboardFrameList(selectedStoryboardFrames)}
          </section>
          {renderExecutionStatsBlock()}
        </div>
      );
    }

    if (selected.step_name === "segment_video") {
      const dimensions = asRecord(videoConsistency.dimensions);
      return (
        <div className="mediaWorkspace">
          <section className="mediaHeroCard">
            <div className="mediaHeroHeader">
              <div>
                <p className="eyebrow">当前章节视频片段</p>
                <h3>{selectedChapter?.title ?? "未选择章节"}</h3>
              </div>
              <div className="actionsRow">
                {chapterVideoPreviewUrl ? (
                  <a className="downloadLink" href={resolveDownloadUrl(chapterVideoPreviewUrl)}>
                    导出当前章节片段
                  </a>
                ) : null}
              </div>
            </div>
            {chapterVideoPreviewUrl ? (
              <video className="chapterVideoPlayer" controls preload="metadata" src={resolveMediaUrl(chapterVideoPreviewUrl)} />
            ) : (
              <div className="mediaHeroImage mediaHeroEmpty">当前章节还没有可播放片段</div>
            )}
            <div className="metricRow">
              <div className="metric">
                <span className="metricLabel">片段状态</span>
                <strong style={{ fontSize: 16 }}>{asString(selected.status)}</strong>
              </div>
              <div className="metric">
                <span className="metricLabel">一致性分数</span>
                <strong>{asNumber(videoConsistency.score, 0)}</strong>
              </div>
              <div className="metric">
                <span className="metricLabel">来源模型</span>
                <strong style={{ fontSize: 16 }}>{selected.model_provider}/{selected.model_name}</strong>
              </div>
            </div>
            {Object.keys(dimensions).length > 0 ? (
              <div className="diffList">
                {Object.entries(dimensions).map(([key, value]) => (
                  <div key={key} className="diffItem">
                    {dimensionLabel(key)}: {asString(value)}
                  </div>
                ))}
              </div>
            ) : null}
          </section>
          <section className="card mediaGalleryCard">
            <div className="mediaSectionHeader">
              <div>
                <p className="eyebrow">参考分镜</p>
                <h4>生成该章节片段使用的分镜图列表</h4>
              </div>
              {galleryExportUrl ? (
                <a className="downloadLink" href={resolveDownloadUrl(galleryExportUrl)}>
                  导出当前章节分镜图
                </a>
              ) : null}
            </div>
            {renderStoryboardFrameList(selectedStoryboardFrames)}
          </section>
          {renderExecutionStatsBlock()}
        </div>
      );
    }

    return (
      <>
        {selectedChapter ? (
          <div className="demoCard" style={{ marginBottom: 12 }}>
            <p className="eyebrow">当前章节</p>
            <h4 style={{ marginTop: 0 }}>{selectedChapter.title}</h4>
            <p className="muted">当前阶段状态: {selected ? selectedChapter.stage_map[selected.step_name] ?? selectedChapter.stage_status : selectedChapter.stage_status}</p>
            <p className="muted" style={{ marginBottom: 0 }}>{selectedChapter.content_excerpt}</p>
          </div>
        ) : null}
        <p className="muted">
          当前模型: {selected.model_provider}/{selected.model_name}
        </p>
        {selected.step_name === "ingest_parse" ? (
          <textarea
            readOnly
            rows={18}
            value={asString(asRecord(selectedOutput.artifact).full_text, "")}
            style={{ width: "100%", marginBottom: 12 }}
          />
        ) : null}
        {selected.step_name === "chapter_chunking" ? (
          <div className="demoCard" style={{ marginBottom: 12 }}>
            <p className="eyebrow">章节切分结果</p>
            <p>章节数: {asString(asRecord(selectedOutput.artifact).chapter_count, "0")}</p>
            <pre style={{ whiteSpace: "pre-wrap", marginBottom: 0 }}>
              {JSON.stringify(selectedOutput.chapters ?? asRecord(selectedOutput.artifact).chapter_titles ?? [], null, 2)}
            </pre>
          </div>
        ) : null}
        {Object.keys(selectedOutput).length === 0 ? <p className="muted">当前章节尚未生成该阶段产物。</p> : null}
        {renderExecutionStatsBlock()}
        <pre style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(selected.output_ref, null, 2)}</pre>
      </>
    );
  }

  return (
    <main className="shell">
      <section className="card row" style={{ justifyContent: "space-between" }}>
        <div>
          <h1>{project?.name ?? "项目审核台"}</h1>
          <p className="muted">状态: {project?.status ?? "-"} | 目标时长: {project?.target_duration_sec ?? "-"}s</p>
        </div>
        <div className="row">
          <Link href="/">返回项目列表</Link>
          <button onClick={renderFinal} disabled={busy}>
            导出成片
          </button>
        </div>
      </section>

      <section className="card">
        <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
          <strong>流程总体进度</strong>
          <span>{overallProgress}%</span>
        </div>
        <div style={{ width: "100%", height: 10, borderRadius: 999, background: "#2f3f5c", marginTop: 10, overflow: "hidden" }}>
          <div
            style={{
              width: `${Math.max(0, Math.min(100, overallProgress))}%`,
              height: "100%",
              background: "linear-gradient(90deg,#4ea7ff,#75d5f5)",
              transition: "width 280ms ease",
            }}
          />
        </div>
      </section>

      <section className="card">
        <h3>输入源文件（PDF/TXT）</h3>
        <div className="row">
          <input
            type="file"
            accept=".pdf,.txt"
            onChange={(e) => setUploadFile(e.target.files?.[0] ?? null)}
          />
          <button onClick={uploadSourceDocument} disabled={busy || !uploadFile}>
            上传并登记
          </button>
        </div>
        {docs.length === 0 ? <p className="muted">暂无源文件</p> : null}
        {docs.map((doc) => (
          <div key={doc.id} className="row" style={{ justifyContent: "space-between", marginTop: 8 }}>
            <span>{doc.file_name}</span>
            <span className="pill">{doc.file_type.toUpperCase()}</span>
            <span className="pill">{doc.parse_status}</span>
          </div>
        ))}
      </section>

      {error ? <section className="card muted">{error}</section> : null}
      {(pendingAction || actionProgress > 0) ? (
        <section className="card muted">
          <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
            <strong>{pendingAction ?? "执行完成"}</strong>
            <span>{Math.round(actionProgress)}%</span>
          </div>
          <div style={{ width: "100%", height: 10, borderRadius: 999, background: "#2f3f5c", marginTop: 10, overflow: "hidden" }}>
            <div
              style={{
                width: `${Math.max(0, Math.min(100, actionProgress))}%`,
                height: "100%",
                background: "linear-gradient(90deg,#d95f23,#f3a84a)",
                transition: "width 300ms ease",
              }}
            />
          </div>
        </section>
      ) : null}
      {actionMessage ? <section className="card">{actionMessage}</section> : null}
      {latestExport ? (
        <section className="card">
          <h3>最新导出</h3>
          <p>状态: {latestExport.status}</p>
          <p className="muted">输出: {latestExport.output_key ?? "-"}</p>
        </section>
      ) : null}

      <section className="card">
        <h3>Story Bible 风格设定</h3>
        <div className="row">
          <select value={stylePresetId} onChange={(e) => setStylePresetId(e.target.value)}>
            {stylePresets.map((preset) => (
              <option key={preset.id} value={preset.id}>
                {preset.label}
              </option>
            ))}
          </select>
          <button onClick={saveStyleProfile} disabled={busy}>
            保存风格设定
          </button>
          <button onClick={rebuildStoryBibleReferences} disabled={busy}>
            重建人物/场景参考图
          </button>
        </div>
        <textarea
          rows={2}
          value={customStyle}
          onChange={(e) => setCustomStyle(e.target.value)}
          placeholder="自定义风格名，例如“工业宗教+潮湿金属质感”"
          style={{ width: "100%", marginTop: 10 }}
        />
        <textarea
          rows={3}
          value={customDirectives}
          onChange={(e) => setCustomDirectives(e.target.value)}
          placeholder="补充风格约束：镜头语言、配色、材质、人物设计、运动节奏"
          style={{ width: "100%", marginTop: 10 }}
        />
        <p className="muted" style={{ marginBottom: 0 }}>
          预设风格会被注入 Story Bible，并自动进入剧本、分镜图、视频生成阶段的提示词。
        </p>
        {(storyBibleCharacters.length > 0 || storyBibleScenes.length > 0) ? (
          <div style={{ marginTop: 18 }}>
            <div className="mediaSectionHeader">
              <div>
                <p className="eyebrow">全局锚点</p>
                <h4>人物与场景参考图</h4>
              </div>
            </div>
            {storyBibleCharacters.length > 0 ? (
              <div style={{ marginBottom: 16 }}>
                <p className="eyebrow">角色锚点</p>
                <div className="storyboardGrid">
                  {storyBibleCharacters.map((item) => (
                    <article key={`character-${item.name}`} className="frameCard">
                      {item.reference_image_url ? (
                        <img src={resolveMediaUrl(item.reference_image_url)} alt={item.name} className="framePreview" />
                      ) : (
                        <div className="framePreview framePreviewEmpty">暂无参考图</div>
                      )}
                      <div className="frameMeta">
                        <p><strong>{item.name}</strong></p>
                        <p>{asString(item.description, item.visual_anchor ?? "暂无描述")}</p>
                      </div>
                      {item.reference_image_url ? (
                        <div className="frameCardFooter">
                          <a className="downloadLink" href={resolveDownloadUrl(item.reference_image_url)}>导出参考图</a>
                        </div>
                      ) : null}
                    </article>
                  ))}
                </div>
              </div>
            ) : null}
            {storyBibleScenes.length > 0 ? (
              <div>
                <p className="eyebrow">场景锚点</p>
                <div className="storyboardGrid">
                  {storyBibleScenes.map((item) => (
                    <article key={`scene-${item.name}`} className="frameCard">
                      {item.reference_image_url ? (
                        <img src={resolveMediaUrl(item.reference_image_url)} alt={item.name} className="framePreview" />
                      ) : (
                        <div className="framePreview framePreviewEmpty">暂无参考图</div>
                      )}
                      <div className="frameMeta">
                        <p><strong>{item.name}</strong></p>
                        <p>{asString(item.description, item.visual_anchor ?? "暂无描述")}</p>
                      </div>
                      {item.reference_image_url ? (
                        <div className="frameCardFooter">
                          <a className="downloadLink" href={resolveDownloadUrl(item.reference_image_url)}>导出参考图</a>
                        </div>
                      ) : null}
                    </article>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        ) : null}
      </section>

      <section className="card">
        <h3>阶段选择</h3>
        <div className="row">
          {steps.map((step) => {
            const isRunning = step.status === "GENERATING" || (busy && selected?.id === step.id && !!pendingAction);
            const stats = stepExecutionStats(step, selectedChapter);
            const tokenUsage = asRecord(stats.token_usage);
            const elapsedSec = asNumber(stats.elapsed_sec, 0);
            const totalTokens = asNumber(tokenUsage.total_tokens, 0);
            return (
              <button
                key={step.id}
                onClick={() => setSelectedStepId(step.id)}
                style={{
                  borderColor: isRunning ? "#f3a84a" : selected?.id === step.id ? "#d95f23" : undefined,
                  background: isRunning ? "rgba(243,168,74,0.16)" : undefined,
                }}
              >
                {step.step_order}. {step.step_display_name}{isRunning ? " · 执行中" : ""}
                {elapsedSec > 0 ? ` · ${elapsedSec.toFixed(1)}s` : ""}
                {totalTokens > 0 ? ` · ${totalTokens} tok` : ""}
              </button>
            );
          })}
        </div>
      </section>

      <section className="card">
        <h3>章节状态</h3>
        {chapters.length === 0 ? <p className="muted">完成“章节切分”后会生成章节列表。</p> : null}
        <div className="row">
          {chapters.map((chapter) => (
            <div key={chapter.id} className="pill">
              {chapter.title}: {selected ? (chapter.stage_map[selected.step_name] ?? chapter.stage_status) : chapter.stage_status}
              {typeof chapter.consistency_score === "number" ? ` · ${chapter.consistency_score}分` : ""}
            </div>
          ))}
        </div>
        {selectedChapter ? (
          <div className="row" style={{ marginTop: 12 }}>
            {Object.entries(selectedChapter.stage_map).map(([stepName, status]) => (
              <div key={stepName} className="pill">
                {steps.find((item) => item.step_name === stepName)?.step_display_name ?? stepName}: {status}
              </div>
            ))}
          </div>
        ) : null}
      </section>

      <section className="grid">
        <aside className="card">
          <h3>章节列表</h3>
          {chapters.length === 0 ? <p className="muted">暂无章节</p> : null}
          {chapters.map((chapter) => (
            <button
              key={chapter.id}
              onClick={() => setSelectedChapterId(chapter.id)}
              style={{
                width: "100%",
                textAlign: "left",
                marginBottom: 6,
                borderColor: selectedChapter?.id === chapter.id ? "#d95f23" : undefined,
              }}
            >
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span>
                  {chapter.chapter_index + 1}. {chapter.title}
                </span>
                <span className="pill">
                  {chapter.stage_status}
                  {typeof chapter.consistency_score === "number" ? ` · ${chapter.consistency_score}` : ""}
                </span>
              </div>
              <p className="muted" style={{ margin: "6px 0 0" }}>{clipText(chapter.summary, 40)}</p>
            </button>
          ))}
        </aside>

        <section className={`card ${selected && mediaFocusedSteps.has(selected.step_name) ? "mediaReviewCard" : ""}`}>
          <h3>产物预览</h3>
          {renderMediaPreview()}
        </section>

        <section className="card">
          <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
            <h3 style={{ margin: 0 }}>人工闭环动作</h3>
            <div className="row">
              {selected && chapterScopedSteps.has(selected.step_name) ? (
                <button onClick={runCurrentStepForAllChapters} disabled={busy || !selected}>
                  {busy && pendingAction?.includes("批量运行") ? "批量运行中..." : "对当前所有章节运行当前阶段"}
                </button>
              ) : null}
              <button onClick={() => runCurrentStep(true)} disabled={busy || !selected}>
                {busy && pendingAction?.includes("正在运行") ? pendingAction : "运行当前阶段"}
              </button>
            </div>
          </div>
          <label>当前步骤模型绑定</label>
          {selected && localOnlySteps.has(selected.step_name) ? (
            <div className="card" style={{ padding: 12, marginBottom: 12 }}>
              <p style={{ margin: 0 }}>
                该步骤为本地固定步骤：<strong>{provider}/{modelName}</strong>
              </p>
            </div>
          ) : (
            <>
              <div className="modelSelectorStack">
                <select className="fullWidthControl" value={provider} onChange={(e) => setProvider(e.target.value)} disabled={busy || !selected}>
                  {providerOptions.length === 0 ? <option value="">暂无可用 provider</option> : null}
                  {providerOptions.map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
                <select
                  className="fullWidthControl"
                  value={modelName}
                  onChange={(e) => setModelName(e.target.value)}
                  disabled={busy || !selected || modelOptions.length === 0}
                >
                  {modelOptions.length === 0 ? <option value="">暂无可用模型</option> : null}
                  {modelOptions.map((item) => (
                    <option key={item} value={item}>
                      {modelPricingLabel(provider, item, stepModelOptions)}
                    </option>
                  ))}
                </select>
              </div>
              <p className="muted" style={{ marginTop: 8, marginBottom: 8 }}>
                当前 provider 下共 {modelOptions.length} 个候选模型
              </p>
              {modelName ? (
                <p className="muted modelSelectionHint" style={{ marginTop: 0, marginBottom: 8 }}>
                  当前模型价格：{modelPricingLabel(provider, modelName, stepModelOptions)}
                </p>
              ) : null}
              <button
                style={{ width: "100%", marginTop: 8, marginBottom: 12 }}
                onClick={bindModelForCurrentStep}
                disabled={busy || !selected || !provider || !modelName}
              >
                {busy && pendingAction?.includes("绑定模型") ? pendingAction : "绑定当前步骤模型"}
              </button>
            </>
          )}

          <div className="row" style={{ marginBottom: 8 }}>
            <button
              onClick={() =>
                selected &&
                postAction(`/api/v1/projects/${projectId}/steps/${selected.id}/approve`, {
                  scope_type: "step",
                  created_by: "ui-reviewer",
                })
              }
              disabled={busy || !selected}
            >
              {busy && pendingAction?.includes("审批通过") ? "审批中..." : "通过"}
            </button>
            {selected && chapterScopedSteps.has(selected.step_name) ? (
              <button
                onClick={() =>
                  selected &&
                  postBatchAction(
                    `/api/v1/projects/${projectId}/steps/${selected.id}/approve-all-chapters`,
                    {
                      scope_type: "chapter",
                      created_by: "ui-reviewer",
                    },
                    "已批量通过当前阶段。"
                  )
                }
                disabled={busy || !selected}
              >
                对当前所有章节通过
              </button>
            ) : null}
            {selected && textEditableSteps.has(selected.step_name) ? (
              <>
                <button
                  onClick={() =>
                    selected &&
                    postAction(`/api/v1/projects/${projectId}/steps/${selected.id}/edit-continue`, {
                      scope_type: "step",
                      created_by: "ui-reviewer",
                      editor_payload: { note: "manual adjustment applied" },
                    })
                  }
                  disabled={busy || !selected}
                >
                  {busy && pendingAction?.includes("人工编辑") ? "保存中..." : "编辑后继续"}
                </button>
                {chapterScopedSteps.has(selected.step_name) ? (
                  <button
                    onClick={() =>
                      selected &&
                      postBatchAction(
                        `/api/v1/projects/${projectId}/steps/${selected.id}/edit-continue-all-chapters`,
                        {
                          scope_type: "chapter",
                          created_by: "ui-reviewer",
                          editor_payload: { note: "manual adjustment applied" },
                        },
                        "已批量保存人工编辑并继续。"
                      )
                    }
                    disabled={busy || !selected}
                  >
                    对当前所有章节编辑后继续
                  </button>
                ) : null}
              </>
            ) : null}
          </div>

          <label>提示词模板</label>
          <div className="row" style={{ marginBottom: 8 }}>
            <select value={templateId} onChange={(e) => setTemplateId(e.target.value)}>
              <option value="">选择模板</option>
              {selectedStepTemplates.map((item) => (
                <option key={item.template_id} value={item.template_id}>
                  {item.label}
                </option>
              ))}
            </select>
            <button
              onClick={() => {
                const template = selectedStepTemplates.find((item) => item.template_id === templateId);
                if (!template) return;
                setSystemPrompt(template.system_prompt);
                setTaskPrompt(template.task_prompt);
              }}
              disabled={busy || !selected || !templateId}
            >
              套用模板
            </button>
          </div>
          {templateId ? (
            <p className="muted" style={{ marginTop: 0 }}>
              {selectedStepTemplates.find((item) => item.template_id === templateId)?.description ?? ""}
            </p>
          ) : null}

          <label>系统提示词</label>
          <textarea
            rows={4}
            value={systemPrompt}
            onChange={(e) => setSystemPrompt(e.target.value)}
            style={{ width: "100%", marginBottom: 8 }}
          />
          <label>任务提示词</label>
          <textarea
            rows={5}
            value={taskPrompt}
            onChange={(e) => setTaskPrompt(e.target.value)}
            style={{ width: "100%", marginBottom: 8 }}
          />
          <button
            style={{ width: "100%", marginBottom: 12 }}
            onClick={() =>
                selected &&
                postAction(`/api/v1/projects/${projectId}/steps/${selected.id}/edit-prompt-regenerate`, {
                  scope_type: "step",
                  created_by: "ui-reviewer",
                  task_prompt: taskPrompt,
                  system_prompt: systemPrompt,
                  params: chapterScopedSteps.has(selected.step_name) && selectedChapter ? { chapter_id: selectedChapter.id } : {},
                })
            }
            disabled={busy || !selected}
          >
            {busy && pendingAction?.includes("提示词重生成") ? "重生成中..." : "修改提示词或设定后重新生成"}
          </button>
          {selected && chapterScopedSteps.has(selected.step_name) ? (
            <button
              style={{ width: "100%", marginBottom: 12 }}
              onClick={() =>
                selected &&
                postBatchAction(
                  `/api/v1/projects/${projectId}/steps/${selected.id}/edit-prompt-regenerate-all-chapters`,
                  {
                    scope_type: "chapter",
                    created_by: "ui-reviewer",
                    task_prompt: taskPrompt,
                    system_prompt: systemPrompt,
                    params: {},
                  },
                  "已对当前所有章节按新提示词重新生成。"
                )
              }
              disabled={busy || !selected}
            >
              对当前所有章节修改提示词或设定后重新生成
            </button>
          ) : null}
          <button
            style={{ width: "100%", marginTop: 8 }}
            onClick={() =>
              selected &&
              postAction(`/api/v1/projects/${projectId}/steps/${selected.id}/switch-model-rerun`, {
                scope_type: "step",
                created_by: "ui-reviewer",
                provider,
                model_name: modelName,
                params: chapterScopedSteps.has(selected.step_name) && selectedChapter ? { chapter_id: selectedChapter.id } : {},
              })
            }
            disabled={busy || !selected}
          >
            {busy && pendingAction?.includes("切换模型重跑") ? "重跑中..." : "切换模型重跑"}
          </button>
          {selected && chapterScopedSteps.has(selected.step_name) ? (
            <button
              style={{ width: "100%", marginTop: 8 }}
              onClick={() =>
                selected &&
                postBatchAction(
                  `/api/v1/projects/${projectId}/steps/${selected.id}/switch-model-rerun-all-chapters`,
                  {
                    scope_type: "chapter",
                    created_by: "ui-reviewer",
                    provider,
                    model_name: modelName,
                    params: {},
                  },
                  "已对当前所有章节切换模型重跑。"
                )
              }
              disabled={busy || !selected}
            >
              对当前所有章节切换模型重跑
            </button>
          ) : null}

          <p className="muted" style={{ marginTop: 12 }}>
            推荐模型:
            {" "}
            {suggestedModelPreview}
          </p>

          {selected?.step_name === "storyboard_image" ? (
            <section style={{ marginTop: 16 }}>
              <h4>分镜版本对比</h4>
              {selectedOutput?.rollback_required ? (
                <div className="card" style={{ padding: 12, marginBottom: 12 }}>
                  <strong>一致性检查未通过，已回退到当前步骤</strong>
                  <pre style={{ whiteSpace: "pre-wrap" }}>
                    {JSON.stringify(selectedOutput.rollback_required, null, 2)}
                  </pre>
                </div>
              ) : null}
              {storyboardVersions.length === 0 ? <p className="muted">当前还没有可对比的分镜版本</p> : null}
              {storyboardVersions.map((version) => (
                <div
                  key={version.id}
                  className="demoCard"
                  style={{
                    marginBottom: 12,
                    borderColor: version.is_active ? "#d95f23" : undefined,
                  }}
                >
                  {(() => {
                    const summary = summarizeStoryboardSnapshot(version.output_snapshot);
                    const diffSummary = activeStoryboardVersion
                      ? buildDiffSummary(activeStoryboardVersion.output_snapshot, version.output_snapshot)
                      : [];
                    return (
                      <>
                  <div className="row" style={{ justifyContent: "space-between" }}>
                    <strong>版本 #{version.version_index}</strong>
                    <span className="pill">{version.is_active ? "当前使用" : "历史版本"}</span>
                  </div>
                  <p className="muted">
                    来源尝试: {version.source_attempt} | 模型: {version.model_provider}/{version.model_name}
                  </p>
                  <p className="muted">
                    一致性分数: {version.consistency_score ?? "-"} | 创建时间: {new Date(version.created_at).toLocaleString()}
                  </p>
                  {summary.thumbnailUrl ? (
                    <img
                      src={resolveMediaUrl(summary.thumbnailUrl)}
                      alt={`版本 ${version.version_index} 分镜缩略图`}
                      className="storyboardPreview"
                    />
                  ) : null}
                  <h4 style={{ marginBottom: 8 }}>{summary.artifactSummary}</h4>
                  <div className="metricRow">
                    <div className="metric">
                      <span className="metricLabel">Artifact ID</span>
                      <strong style={{ fontSize: 14 }}>{summary.artifactId}</strong>
                    </div>
                    <div className="metric">
                      <span className="metricLabel">任务提示词</span>
                      <strong style={{ fontSize: 14 }}>{summary.taskPrompt}</strong>
                    </div>
                  </div>
                  {version.rollback_reason ? <p className="muted">{version.rollback_reason}</p> : null}
                  {diffSummary.length > 0 ? (
                    <>
                      <p className="eyebrow" style={{ marginTop: 12 }}>差异摘要</p>
                      {version.is_active ? (
                        <p className="muted">这是当前基准版本，其它候选会与它进行比较。</p>
                      ) : (
                        <div className="diffList">
                          {diffSummary.map((line) => (
                            <div key={line} className="diffItem">
                              {line}
                            </div>
                          ))}
                        </div>
                      )}
                    </>
                  ) : null}
                  <details style={{ marginTop: 10 }}>
                    <summary>查看版本详情</summary>
                    <pre style={{ whiteSpace: "pre-wrap", maxHeight: 220, overflow: "auto" }}>
                      {JSON.stringify(version.output_snapshot, null, 2)}
                    </pre>
                  </details>
                  <button onClick={() => selectStoryboardVersion(version.id)} disabled={busy}>
                    选用该版本
                  </button>
                      </>
                    );
                  })()}
                </div>
              ))}
            </section>
          ) : null}
        </section>
      </section>
    </main>
  );
}
