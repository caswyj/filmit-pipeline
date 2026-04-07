"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { AgentPanel } from "./components/agent-panel";

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
  total_estimated_cost: number;
  chapter_results: Array<{
    chapter_id: string;
    chapter_title: string;
    status: string;
    detail: string;
    estimated_cost?: number | null;
  }>;
  current_step: Step | null;
};

type StepPromptPreview = {
  project_id: string;
  step_name: string;
  chapter_id?: string | null;
  provider?: string | null;
  model?: string | null;
  prompt_profile?: string | null;
  prompt_profile_label?: string | null;
  chapter_prompt?: string | null;
  shot_prompts: Array<{
    shot_index: number;
    title: string;
    motion_directive?: string | null;
    reference_image_url?: string | null;
    prompt: string;
  }>;
  prompt_summary: Record<string, unknown>;
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

type StoryBibleAssetView = {
  view_key: string;
  view_label: string;
  image_url?: string;
  thumbnail_url?: string;
  export_url?: string;
  storage_key?: string;
  provider?: string;
  model?: string;
};

type StoryBibleEntity = {
  name: string;
  description?: string;
  visual_anchor?: string;
  reference_display_description?: string;
  reference_image_url?: string;
  reference_storage_key?: string;
  identity_reference_image_url?: string;
  identity_reference_storage_key?: string;
  scene_reference_image_url?: string;
  scene_reference_storage_key?: string;
  prop_reference_image_url?: string;
  prop_reference_storage_key?: string;
  reference_generation_status?: string;
  reference_generation_error?: string;
  reference_variant_count?: number;
  reference_variant_expected?: number;
  reference_hard_constraints?: string[];
  identity_reference_views: StoryBibleAssetView[];
  scene_reference_variants: StoryBibleAssetView[];
  prop_reference_views: StoryBibleAssetView[];
};

const apiBase = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";
const generatedRoots = [
  process.env.NEXT_PUBLIC_GENERATED_DIR,
  "/workspace/output/generated",
  "/Users/wyj/proj/novel-to-video-demo-cases",
  "/Users/wyj/proj/novel-to-video-pipeline/output/generated",
]
  .filter((root): root is string => Boolean(root))
  .map((root) => root.replace(/\/+$/, "") + "/");
const localOnlySteps = new Set(["ingest_parse", "chapter_chunking"]);
const textEditableSteps = new Set(["ingest_parse", "chapter_chunking", "story_scripting", "shot_detailing"]);
const chapterScopedSteps = new Set(["story_scripting", "shot_detailing", "storyboard_image", "consistency_check", "segment_video"]);
const mediaFocusedSteps = new Set(["storyboard_image", "consistency_check", "segment_video"]);
const directorStyleOptions = [
  "现代商业电影",
  "希区柯克式悬疑",
  "大卫·芬奇式冷静控制",
  "王家卫式情绪化",
  "黑泽明式横向调度",
];
const realLightSourceOptions = [
  "真实光源驱动，优先场景内实景光源与动机光",
  "高对比戏剧化光源",
  "低照度氛围光",
  "霓虹/广告牌动机光",
  "烛光/钨丝暖光",
];
const skinTextureOptions = [
  "保留真实肌肤纹理，不过度磨皮",
  "轻度柔化但保留毛孔细节",
  "商业级干净肤质",
  "粗粝写实肤质",
];
const shotDistanceOptions = [
  "中景为主，情绪峰值切近景，场景转换使用远景建立",
  "近景主导，强调表演和情绪",
  "远景建立后回到中景叙事",
  "近中远景均衡切换",
];
const lensPackageOptions = [
  "35mm / 50mm / 85mm 电影镜头组",
  "24mm / 35mm / 50mm 广角到标准",
  "50mm / 85mm / 135mm 偏长焦镜头组",
  "变焦混合镜头组",
];
const cameraMotionOptions = [
  "克制推进与稳定跟拍，必要时使用导演风格化运镜",
  "稳定跟拍与缓慢摇移",
  "手持纪实与近距离跟随",
  "希区柯克式变焦",
  "库布里克式对称推进",
  "黑泽明式横向调度",
];
const storyboardQualityOptions = [
  {
    id: "draft",
    label: "草稿",
    description: "最低成本，单模型、低分辨率、默认批量预算 5 美元",
    defaultSize: "1024x576",
    defaultBudget: 5,
  },
  {
    id: "balanced",
    label: "均衡",
    description: "适中质量，单模型、720p、默认批量预算 8 美元",
    defaultSize: "1280x720",
    defaultBudget: 8,
  },
  {
    id: "quality",
    label: "精修",
    description: "高质量，允许有限 fallback，默认批量预算 15 美元",
    defaultSize: "1536x1024",
    defaultBudget: 15,
  },
] as const;
const storyboardImageSizeOptions = [
  { value: "1024x576", label: "1024 x 576（推荐草稿）" },
  { value: "1280x720", label: "1280 x 720（推荐均衡）" },
  { value: "1536x1024", label: "1536 x 1024（高成本）" },
];
const videoPromptProfileOptions = [
  { id: "generic", label: "通用视频模型", description: "适合未明确锁定 provider 的中性提示词结构" },
  { id: "seedance1_5_pro", label: "Seedance 1.5 Pro", description: "适合火山 Seedance 1.5 Pro，强调 first_frame reference 与单镜头真实运动" },
  { id: "seedance2", label: "Seedance 2", description: "更短、更具体，强调 reference 起始帧和真实运动" },
  { id: "sora2", label: "Sora 2", description: "强调时空连续、物理合理和电影镜头语言" },
  { id: "runway_gen4", label: "Runway Gen-4", description: "强调 reference 构图锚点、镜头节奏和 blocking" },
  { id: "veo3_1", label: "Veo 3.1", description: "强调写实环境运动、光照变化和结尾帧承接" },
] as const;
const videoMotionIntensityOptions = [
  { id: "low", label: "低", description: "轻动作、克制位移，适合表演和情绪" },
  { id: "medium", label: "中", description: "中等动作幅度，兼顾主体位移和轻度运镜" },
  { id: "high", label: "高", description: "强动作或明显运镜，但仍要求主体稳定" },
] as const;
const videoAudioModeOptions = [
  { id: "demo_native_audio", label: "Demo 模式", description: "第 7 步直接生成有声视频片段，第 8 步只保留原生音轨并整理字幕" },
  { id: "formal_tts", label: "正式成片模式", description: "第 7 步生成静音视频片段，第 8 步统一生成旁白、字幕和配音" },
] as const;

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

function asBoolean(value: unknown, fallback = false): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase();
    if (["true", "1", "yes", "on"].includes(normalized)) return true;
    if (["false", "0", "no", "off"].includes(normalized)) return false;
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
    motion_dynamicity: "视频运动有效性",
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

function resolveGeneratedPathUrl(pathOrUrl: string): string {
  if (!pathOrUrl) return "";
  if (pathOrUrl.startsWith("http://") || pathOrUrl.startsWith("https://") || pathOrUrl.startsWith("data:")) {
    return pathOrUrl;
  }
  if (pathOrUrl.includes("/api/v1/local-files/")) return resolveMediaUrl(pathOrUrl);
  const matchedRoot = generatedRoots.find((root) => pathOrUrl.startsWith(root));
  if (!matchedRoot) return resolveMediaUrl(pathOrUrl);
  const relative = pathOrUrl
    .slice(matchedRoot.length)
    .replace(/^\/+/, "")
    .split("/")
    .map((segment) => encodeURIComponent(segment))
    .join("/");
  return `${apiBase}/api/v1/local-files/${relative}`;
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

function executionCostLabel(stats: Record<string, unknown>): string {
  const rawUsage = asRecord(stats.raw_usage);
  if (Number.isFinite(Number(rawUsage.cost))) return "实际成本";
  const source = asString(stats.cost_source, "");
  if (source === "provider_reported") return "实际成本";
  if (source === "openrouter_catalog_estimated") return "估算成本";
  if (source === "local") return "本地成本";
  return "估算成本";
}

function executionCostValue(stats: Record<string, unknown>): number {
  const rawUsage = asRecord(stats.raw_usage);
  const providerCost = Number(rawUsage.cost);
  if (Number.isFinite(providerCost) && providerCost >= 0) return providerCost;
  return asNumber(stats.estimated_cost, 0);
}

function buildVideoPromptEnhancementBlock(options: {
  profile: string;
  includeReferenceImage: boolean;
  includeDialogue: boolean;
  includeNarration: boolean;
  motionIntensity: string;
  audioMode: string;
  generateAudio: boolean;
}): string {
  const profileLabel =
    videoPromptProfileOptions.find((item) => item.id === options.profile)?.label ?? "通用视频模型";
  const motionLabel =
    videoMotionIntensityOptions.find((item) => item.id === options.motionIntensity)?.label ?? "中";
  const audioModeLabel =
    videoAudioModeOptions.find((item) => item.id === options.audioMode)?.label ?? "正式成片模式";
  const lines = [
    "[Video Model Enhancement]",
    `- Target profile: ${profileLabel}`,
    `- Use storyboard frame as reference image input: ${options.includeReferenceImage ? "yes" : "no"}`,
    `- Include character dialogue basis: ${options.includeDialogue ? "yes" : "no"}`,
    `- Include narration basis: ${options.includeNarration ? "yes" : "no"}`,
    `- Motion intensity: ${motionLabel}`,
    `- Audio mode: ${audioModeLabel}`,
    `- Generate native model audio: ${options.generateAudio ? "yes" : "no"}`,
    "- Build the video prompt around character identity, scene, action beat, camera movement, light source logic and continuity anchor.",
    "- Keep the storyboard frame as the opening composition and identity anchor when reference is enabled.",
    "- Generate a real moving shot, not a static hold-frame slideshow.",
    "- No readable subtitles, captions, logos or watermarks.",
  ];
  return lines.join("\n");
}

function extractStoryboardGallery(output: Record<string, unknown>): Record<string, unknown> {
  const gallery = asRecord(output.storyboard_gallery);
  if (Object.keys(gallery).length > 0) return gallery;
  const artifact = asRecord(output.artifact);
  if (Array.isArray(artifact.frames)) {
    return {
      frame_count: artifact.frame_count,
      frames: artifact.frames,
      contact_sheet_url: artifact.thumbnail_url || artifact.image_url || artifact.export_url,
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

function chapterMediaPrefix(chapter: Chapter | null): string {
  if (!chapter) return "";
  const chapterIndex = Math.max(0, Number(chapter.chapter_index ?? 0)) + 1;
  const chunkIndex = Math.max(0, Number(chapter.chunk_index ?? 0)) + 1;
  return `chapter-${String(chapterIndex).padStart(3, "0")}-chunk-${String(chunkIndex).padStart(2, "0")}`;
}

function outputBelongsToChapter(output: Record<string, unknown>, chapter: Chapter | null, stepName: string | undefined): boolean {
  if (!chapter || !stepName) return true;
  const prefix = chapterMediaPrefix(chapter);
  if (!prefix) return true;
  const artifact = asRecord(output.artifact);
  const gallery = extractStoryboardGallery(output);
  const candidates = [
    asString(artifact.storage_key, ""),
    asString(artifact.preview_url, ""),
    asString(artifact.export_url, ""),
    asString(gallery.contact_sheet_url, ""),
    asString(gallery.gallery_export_url, ""),
  ].filter(Boolean);
  if (candidates.length === 0) return true;
  return candidates.some((item) => item.includes(prefix));
}

function chapterHasRenderableOutput(chapter: Chapter, stepName: string | undefined): boolean {
  if (!stepName) return false;
  const output = chapterStageOutput(chapter, stepName);
  if (Object.keys(output).length === 0) return false;
  if (stepName === "storyboard_image" || stepName === "consistency_check") {
    if (extractStoryboardFrames(output).length > 0) return true;
    const gallery = extractStoryboardGallery(output);
    return asString(gallery.contact_sheet_url, "") !== "";
  }
  if (stepName === "segment_video") {
    const artifact = asRecord(output.artifact);
    return asString(artifact.preview_url, asString(artifact.export_url, "")) !== "";
  }
  const artifact = asRecord(output.artifact);
  return Object.keys(artifact).length > 0 || asString(output.summary, "") !== "";
}

function pickPreferredChapter(
  chapters: Chapter[],
  stepName: string | undefined,
  selectedChapterId: string | null
): Chapter | null {
  if (chapters.length === 0) return null;
  const selectedChapter = selectedChapterId ? chapters.find((chapter) => chapter.id === selectedChapterId) ?? null : null;
  if (selectedChapter) return selectedChapter;

  if (stepName && chapterScopedSteps.has(stepName)) {
    const withRenderableOutput = chapters.find((chapter) => chapterHasRenderableOutput(chapter, stepName));
    if (withRenderableOutput) return withRenderableOutput;
    const readyForReview = chapters.find((chapter) => {
      const status = asString(chapter.stage_map[stepName], "");
      return status === "REVIEW_REQUIRED" || status === "APPROVED";
    });
    if (readyForReview) return readyForReview;
    const inProgress = chapters.find((chapter) => {
      const status = asString(chapter.stage_map[stepName], "");
      return status === "GENERATING" || status === "PENDING";
    });
    if (inProgress) return inProgress;
  }

  return chapters[0];
}

function chapterStageRecord(chapter: Chapter | null, stepName: string | undefined): Record<string, unknown> {
  if (!chapter || !stepName) return {};
  const meta = asRecord(chapter.meta);
  const stages = asRecord(meta.stages);
  return asRecord(stages[stepName]);
}

function stepExecutionStats(step: Step, chapter: Chapter | null): Record<string, unknown> {
  if (chapterScopedSteps.has(step.step_name)) {
    return asRecord(chapterStageOutput(chapter, step.step_name).execution_stats);
  }
  return asRecord(asRecord(step.output_ref).execution_stats);
}

function asStoryBibleAssetViewList(value: unknown): StoryBibleAssetView[] {
  return asList(value)
    .map((item) => asRecord(item))
    .filter((item) => asString(item.view_key, "") !== "" || asString(item.image_url, "") !== "")
    .map((item) => ({
      view_key: asString(item.view_key, ""),
      view_label: asString(item.view_label, asString(item.view_key, "参考图")),
      image_url: asString(item.image_url, ""),
      thumbnail_url: asString(item.thumbnail_url, ""),
      export_url: asString(item.export_url, ""),
      storage_key: asString(item.storage_key, ""),
      provider: asString(item.provider, ""),
      model: asString(item.model, ""),
    }));
}

function asStoryBibleEntityList(value: unknown): StoryBibleEntity[] {
  return asList(value)
    .map((item) => asRecord(item))
    .filter((item) => asString(item.name, "") !== "")
    .map((item) => ({
      name: asString(item.name, ""),
      description: asString(item.description, ""),
      visual_anchor: asString(item.visual_anchor, ""),
      reference_display_description: asString(item.reference_display_description, ""),
      reference_image_url: asString(item.reference_image_url, ""),
      reference_storage_key: asString(item.reference_storage_key, ""),
      identity_reference_image_url: asString(item.identity_reference_image_url, ""),
      identity_reference_storage_key: asString(item.identity_reference_storage_key, ""),
      scene_reference_image_url: asString(item.scene_reference_image_url, ""),
      scene_reference_storage_key: asString(item.scene_reference_storage_key, ""),
      prop_reference_image_url: asString(item.prop_reference_image_url, ""),
      prop_reference_storage_key: asString(item.prop_reference_storage_key, ""),
      reference_generation_status: asString(item.reference_generation_status, ""),
      reference_generation_error: asString(item.reference_generation_error, ""),
      reference_variant_count: asNumber(item.reference_variant_count, 0),
      reference_variant_expected: asNumber(item.reference_variant_expected, 0),
      reference_hard_constraints: asList(item.reference_hard_constraints).map((entry) => asString(entry, "")).filter(Boolean),
      identity_reference_views: asStoryBibleAssetViewList(item.identity_reference_views),
      scene_reference_variants: asStoryBibleAssetViewList(item.scene_reference_variants),
      prop_reference_views: asStoryBibleAssetViewList(item.prop_reference_views),
    }));
}

function storyBiblePrimaryUrl(
  item: StoryBibleEntity,
  kind: "characters" | "scenes" | "props"
): string {
  if (kind === "characters") return item.identity_reference_image_url || item.reference_image_url || "";
  if (kind === "scenes") return item.scene_reference_image_url || item.reference_image_url || "";
  return item.prop_reference_image_url || item.reference_image_url || "";
}

function storyBibleVariants(
  item: StoryBibleEntity,
  kind: "characters" | "scenes" | "props"
): StoryBibleAssetView[] {
  if (kind === "characters") return item.identity_reference_views;
  if (kind === "scenes") return item.scene_reference_variants;
  return item.prop_reference_views;
}

function storyBibleStatus(
  item: StoryBibleEntity,
  kind: "characters" | "scenes" | "props"
): string {
  const variants = storyBibleVariants(item, kind);
  return item.reference_generation_status || (variants.length > 0 ? "SUCCEEDED" : "MISSING");
}

function storyBibleStatusLabel(status: string): string {
  if (status === "MISSING") return "未生成";
  if (status === "PARTIAL") return "部分缺失";
  if (status === "FAILED") return "生成失败";
  if (status === "SUCCEEDED") return "已就绪";
  return status || "未知";
}

export default function ProjectPage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;
  const [project, setProject] = useState<Project | null>(null);
  const [steps, setSteps] = useState<Step[]>([]);
  const [chapters, setChapters] = useState<Chapter[]>([]);
  const [chaptersLoading, setChaptersLoading] = useState(false);
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
  const [directorStyle, setDirectorStyle] = useState(directorStyleOptions[0]);
  const [realLightSourceStrategy, setRealLightSourceStrategy] = useState(realLightSourceOptions[0]);
  const [skinTextureLevel, setSkinTextureLevel] = useState(skinTextureOptions[0]);
  const [shotDistanceProfile, setShotDistanceProfile] = useState(shotDistanceOptions[0]);
  const [lensPackage, setLensPackage] = useState(lensPackageOptions[0]);
  const [cameraMovementStyle, setCameraMovementStyle] = useState(cameraMotionOptions[0]);
  const [firstLastFrameBridge, setFirstLastFrameBridge] = useState(true);
  const [forbidReadableText, setForbidReadableText] = useState(true);
  const [storyboardQuality, setStoryboardQuality] = useState<(typeof storyboardQualityOptions)[number]["id"]>("draft");
  const [storyboardImageSize, setStoryboardImageSize] = useState("1024x576");
  const [storyboardBudgetUsd, setStoryboardBudgetUsd] = useState("5");
  const [videoPromptProfile, setVideoPromptProfile] = useState<(typeof videoPromptProfileOptions)[number]["id"]>("generic");
  const [videoUseReferenceImage, setVideoUseReferenceImage] = useState(true);
  const [videoIncludeDialogue, setVideoIncludeDialogue] = useState(true);
  const [videoIncludeNarration, setVideoIncludeNarration] = useState(true);
  const [videoMotionIntensity, setVideoMotionIntensity] = useState<(typeof videoMotionIntensityOptions)[number]["id"]>("medium");
  const [videoAudioMode, setVideoAudioMode] = useState<(typeof videoAudioModeOptions)[number]["id"]>("formal_tts");
  const [videoGenerateAudio, setVideoGenerateAudio] = useState(false);
  const [videoPromptPreview, setVideoPromptPreview] = useState<StepPromptPreview | null>(null);
  const [latestExport, setLatestExport] = useState<ExportRead | null>(null);
  const [storyboardVersions, setStoryboardVersions] = useState<StoryboardVersion[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [actionProgress, setActionProgress] = useState(0);
  const sourceFileInputRef = useRef<HTMLInputElement | null>(null);

  const selected = useMemo(
    () => steps.find((step) => step.id === selectedStepId) ?? steps[0] ?? null,
    [selectedStepId, steps]
  );
  const selectedChapter = useMemo(
    () => pickPreferredChapter(chapters, selected?.step_name, selectedChapterId),
    [chapters, selected?.step_name, selectedChapterId]
  );
  const storyBible = useMemo(
    () => asRecord(asRecord(project?.style_profile).story_bible),
    [project?.style_profile]
  );
  const storyBibleVisualStyle = useMemo(() => asRecord(storyBible.visual_style), [storyBible]);
  const storyBibleCharacters = useMemo(() => asStoryBibleEntityList(storyBible.characters), [storyBible]);
  const storyBibleScenes = useMemo(() => asStoryBibleEntityList(storyBible.scenes), [storyBible]);
  const storyBibleProps = useMemo(() => asStoryBibleEntityList(storyBible.props), [storyBible]);
  const storyBibleAssetSummary = useMemo(() => {
    const summarize = (items: StoryBibleEntity[], kind: "characters" | "scenes" | "props") => {
      const ready = items.filter((item) => {
        const variants = storyBibleVariants(item, kind);
        return storyBibleStatus(item, kind) === "SUCCEEDED" && variants.length > 0;
      }).length;
      return {
        total: items.length,
        ready,
        attention: Math.max(items.length - ready, 0),
      };
    };
    const characters = summarize(storyBibleCharacters, "characters");
    const scenes = summarize(storyBibleScenes, "scenes");
    const props = summarize(storyBibleProps, "props");
    return {
      characters,
      scenes,
      props,
      total: characters.total + scenes.total + props.total,
      ready: characters.ready + scenes.ready + props.ready,
      attention: characters.attention + scenes.attention + props.attention,
    };
  }, [storyBibleCharacters, storyBibleScenes, storyBibleProps]);

  const selectedChapterOutput = useMemo(() => {
    if (!selected || !chapterScopedSteps.has(selected.step_name)) return {};
    const output = chapterStageOutput(selectedChapter, selected.step_name);
    if (!outputBelongsToChapter(output, selectedChapter, selected.step_name)) {
      return {};
    }
    return output;
  }, [selected, selectedChapter]);

  const selectedOutput = useMemo(() => {
    if (!selected) return {};
    if (chapterScopedSteps.has(selected.step_name) && selectedChapter) {
      return selectedChapterOutput;
    }
    return asRecord(selected.output_ref);
  }, [selected, selectedChapter, selectedChapterOutput]);
  const currentStoryboardSummary = useMemo(() => summarizeStoryboardSnapshot(selectedOutput), [selectedOutput]);
  const activeStoryboardVersion = useMemo(
    () => storyboardVersions.find((version) => version.is_active) ?? storyboardVersions[0] ?? null,
    [storyboardVersions]
  );
  const selectedArtifact = useMemo(() => asRecord(selectedOutput.artifact), [selectedOutput]);
  const finalCutSummary = useMemo(() => asRecord(selectedOutput.final_cut), [selectedOutput]);
  const selectedStoryboardGallery = useMemo(() => extractStoryboardGallery(selectedOutput), [selectedOutput]);
  const selectedStoryboardFrames = useMemo(() => extractStoryboardFrames(selectedOutput), [selectedOutput]);
  const consistencyPayload = useMemo(() => asRecord(selectedOutput.consistency), [selectedOutput]);
  const selectedStoryboardParams = useMemo(() => asRecord(selectedOutput.params), [selectedOutput]);
  const storyboardQualityPreset = useMemo(
    () => storyboardQualityOptions.find((item) => item.id === storyboardQuality) ?? storyboardQualityOptions[0],
    [storyboardQuality]
  );
  const selectedVideoPromptProfile = useMemo(
    () => videoPromptProfileOptions.find((item) => item.id === videoPromptProfile) ?? videoPromptProfileOptions[0],
    [videoPromptProfile]
  );
  const selectedVideoMotionOption = useMemo(
    () => videoMotionIntensityOptions.find((item) => item.id === videoMotionIntensity) ?? videoMotionIntensityOptions[1],
    [videoMotionIntensity]
  );
  const selectedVideoAudioMode = useMemo(
    () => videoAudioModeOptions.find((item) => item.id === videoAudioMode) ?? videoAudioModeOptions[1],
    [videoAudioMode]
  );
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
  const motionValidation = useMemo(() => asRecord(selectedArtifact.motion_validation), [selectedArtifact]);
  const segmentClipManifest = useMemo(
    () => asList(selectedArtifact.clip_manifest).map((item) => asRecord(item)),
    [selectedArtifact]
  );
  const chapterConsistencyScores = useMemo(
    () => asList(selectedOutput.chapter_consistency_scores).map((item) => asRecord(item)),
    [selectedOutput]
  );
  const executionStats = useMemo(() => asRecord(selectedOutput.execution_stats), [selectedOutput]);
  const finalCutManifest = useMemo(
    () => asList(selectedArtifact.segment_manifest).map((item) => asRecord(item)),
    [selectedArtifact]
  );
  const finalCutSubtitleEntries = useMemo(
    () => asList(selectedArtifact.subtitle_entries).map((item) => asRecord(item)),
    [selectedArtifact]
  );
  const finalCutAudioUrl = useMemo(
    () => asString(selectedArtifact.audio_url, asString(selectedArtifact.export_url, "")),
    [selectedArtifact]
  );
  const finalCutSubtitleUrl = useMemo(
    () => asString(selectedArtifact.subtitle_url, asString(selectedArtifact.subtitle_export_url, "")),
    [selectedArtifact]
  );
  const latestExportUrl = useMemo(
    () => resolveGeneratedPathUrl(asString(latestExport?.output_key, asString(project?.output_path, ""))),
    [latestExport?.output_key, project?.output_path]
  );
  const failedChapterItems = useMemo(() => {
    if (!selected || !chapterScopedSteps.has(selected.step_name)) return [];
    return chapters
      .map((chapter) => {
        const stage = chapterStageRecord(chapter, selected.step_name);
        const status = asString(stage.status, chapter.stage_map[selected.step_name] ?? "");
        const output = asRecord(stage.output);
        const detail = asString(output.error_message, asString(stage.error_message, ""));
        return {
          id: chapter.id,
          title: chapter.title,
          status,
          detail,
        };
      })
      .filter((item) => item.status === "FAILED");
  }, [chapters, selected]);
  const reviewRequiredChapterItems = useMemo(() => {
    if (!selected || selected.step_name !== "consistency_check") return [];
    return chapters
      .map((chapter) => {
        const stage = chapterStageRecord(chapter, selected.step_name);
        const status = asString(stage.status, chapter.stage_map[selected.step_name] ?? "");
        return {
          id: chapter.id,
          title: chapter.title,
          status,
        };
      })
      .filter((item) => item.status === "REVIEW_REQUIRED");
  }, [chapters, selected]);
  const reworkRequestedChapterItems = useMemo(() => {
    if (!selected || selected.step_name !== "consistency_check") return [];
    return chapters
      .map((chapter) => {
        const stage = chapterStageRecord(chapter, selected.step_name);
        const status = asString(stage.status, chapter.stage_map[selected.step_name] ?? "");
        const output = asRecord(stage.output);
        const consistency = asRecord(output.consistency);
        const details = asRecord(consistency.details);
        const lowFrames = asList(details.low_frames)
          .map((item) => asRecord(item))
          .slice(0, 3)
          .map((item) => `镜头${asString(item.shot_index, "?")}:${asString(item.reason, "需修正连续性")}`)
          .join("；");
        return {
          id: chapter.id,
          title: chapter.title,
          status,
          detail: lowFrames,
        };
      })
      .filter((item) => item.status === "REWORK_REQUESTED");
  }, [chapters, selected]);
  const pendingConsistencyChapterItems = useMemo(() => {
    if (!selected || selected.step_name !== "consistency_check") return [];
    return chapters
      .map((chapter) => {
        const stage = chapterStageRecord(chapter, selected.step_name);
        const status = asString(stage.status, chapter.stage_map[selected.step_name] ?? "");
        const storyboardStage = chapterStageRecord(chapter, "storyboard_image");
        const storyboardStatus = asString(storyboardStage.status, chapter.stage_map.storyboard_image ?? "");
        return {
          id: chapter.id,
          title: chapter.title,
          status,
          storyboardStatus,
        };
      })
      .filter(
        (item) =>
          item.status === "PENDING" &&
          (item.storyboardStatus === "APPROVED" || item.storyboardStatus === "REVIEW_REQUIRED")
      );
  }, [chapters, selected]);

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
    setChaptersLoading(true);
    try {
      const res = await fetch(`${apiBase}/api/v1/projects/${projectId}/chapters`, { cache: "no-store" });
      if (!res.ok) throw new Error("加载章节失败");
      const data = (await res.json()) as Chapter[];
      setChapters(data);
      if (!selectedChapterId && data.length > 0) {
        setSelectedChapterId(pickPreferredChapter(data, selected?.step_name, null)?.id ?? data[0].id);
      }
    } finally {
      setChaptersLoading(false);
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

  async function refreshWorkflowData(targetStep: Step | null = selected, includeDocuments = true) {
    const tasks = [loadProject(), loadSteps(), loadChapters()];
    if (includeDocuments) {
      tasks.push(loadDocuments());
    }
    await Promise.all(tasks);
    if (targetStep?.step_name === "storyboard_image") {
      await loadStoryboardVersions(targetStep);
    }
  }

  async function loadStaticData() {
    await Promise.all([loadCatalog(), loadStylePresets(), loadPromptTemplates()]);
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

  function applyUpdatedStep(updatedStep: Step, chapterId: string | null = null) {
    setSteps((current) => {
      const exists = current.some((step) => step.id === updatedStep.id);
      if (!exists) {
        return [...current, updatedStep];
      }
      return current.map((step) => (step.id === updatedStep.id ? updatedStep : step));
    });
    if (!chapterId || !chapterScopedSteps.has(updatedStep.step_name)) {
      return;
    }
    setChapters((current) =>
      current.map((chapter) => {
        if (chapter.id !== chapterId) return chapter;
        const meta = asRecord(chapter.meta);
        const stages = asRecord(meta.stages);
        const stage = asRecord(stages[updatedStep.step_name]);
        return {
          ...chapter,
          stage_map: {
            ...chapter.stage_map,
            [updatedStep.step_name]: updatedStep.status,
          },
          meta: {
            ...meta,
            stages: {
              ...stages,
              [updatedStep.step_name]: {
                ...stage,
                status: updatedStep.status,
                output: updatedStep.output_ref,
                error_code: null,
                error_message: null,
                provider: updatedStep.model_provider,
                model: updatedStep.model_name,
              },
            },
          },
        };
      })
    );
  }

  function applyBatchChapterStatuses(stepName: string, items: BatchStepRunResponse["chapter_results"], stepMeta: Step | null) {
    const statusMap = new Map(
      items
        .filter((item) => item.status !== "SKIPPED")
        .map((item) => [item.chapter_id, item.status])
    );
    if (statusMap.size === 0) return;
    setChapters((current) =>
      current.map((chapter) => {
        const nextStatus = statusMap.get(chapter.id);
        if (!nextStatus) return chapter;
        const meta = asRecord(chapter.meta);
        const stages = asRecord(meta.stages);
        const stage = asRecord(stages[stepName]);
        return {
          ...chapter,
          stage_map: {
            ...chapter.stage_map,
            [stepName]: nextStatus,
          },
          meta: {
            ...meta,
            stages: {
              ...stages,
              [stepName]: {
                ...stage,
                status: nextStatus,
                provider: stepMeta?.model_provider ?? asString(stage.provider, ""),
                model: stepMeta?.model_name ?? asString(stage.model, ""),
              },
            },
          },
        };
      })
    );
  }

  async function runProject() {
    if (!projectId || !selected) return;
    await runCurrentStep(true);
  }

  function buildStoryboardRunParams() {
    const budget = Number(storyboardBudgetUsd);
    return {
      storyboard_quality: storyboardQuality,
      size: storyboardImageSize,
      forbid_readable_text: forbidReadableText,
      ...(Number.isFinite(budget) && budget > 0 ? { max_total_cost_usd: budget } : {}),
    };
  }

  function buildSegmentVideoRunParams() {
    const generateNativeAudio = videoAudioMode === "demo_native_audio";
    return {
      video_prompt_profile: videoPromptProfile,
      video_use_reference_image: videoUseReferenceImage,
      video_include_dialogue: videoIncludeDialogue,
      video_include_narration: videoIncludeNarration,
      video_motion_intensity: videoMotionIntensity,
      video_audio_mode: videoAudioMode,
      generate_audio: generateNativeAudio,
    };
  }

  function buildFinalCutRunParams() {
    return {
      audio_mode: videoAudioMode,
    };
  }

  function buildStepParams(options?: { includeChapterId?: boolean }) {
    if (!selected) return {};
    const includeChapterId = options?.includeChapterId ?? true;
    const params: Record<string, unknown> = {};
    if (chapterScopedSteps.has(selected.step_name) && includeChapterId && selectedChapter) {
      params.chapter_id = selectedChapter.id;
    }
    if (selected.step_name === "storyboard_image") {
      Object.assign(params, buildStoryboardRunParams());
    }
    if (selected.step_name === "segment_video") {
      Object.assign(params, buildSegmentVideoRunParams());
    }
    if (selected.step_name === "stitch_subtitle_tts") {
      Object.assign(params, buildFinalCutRunParams());
    }
    return params;
  }

  function applyVideoPromptEnhancement() {
    const enhancement = buildVideoPromptEnhancementBlock({
      profile: videoPromptProfile,
      includeReferenceImage: videoUseReferenceImage,
      includeDialogue: videoIncludeDialogue,
      includeNarration: videoIncludeNarration,
      motionIntensity: videoMotionIntensity,
      audioMode: videoAudioMode,
      generateAudio: videoAudioMode === "demo_native_audio",
    });
    const nextPrompt = taskPrompt.includes("[Video Model Enhancement]")
      ? taskPrompt.replace(/\[Video Model Enhancement\][\s\S]*$/m, enhancement)
      : `${taskPrompt.trim()}\n\n${enhancement}`.trim();
    setTaskPrompt(nextPrompt);
    setActionMessage("已套用视频模型增强提示词模板。");
  }

  async function previewSegmentVideoPrompt() {
    if (!projectId || !selected || selected.step_name !== "segment_video" || !selectedChapter) return;
    setPendingAction("正在生成视频 Prompt 预览...");
    setBusy(true);
    setError(null);
    setActionMessage(null);
    try {
      const res = await fetch(`${apiBase}/api/v1/projects/${projectId}/steps/${selected.step_name}/prompt-preview`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          chapter_id: selectedChapter.id,
          system_prompt: systemPrompt,
          task_prompt: taskPrompt,
          params: buildSegmentVideoRunParams(),
        }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "生成视频 Prompt 预览失败");
      }
      const data = (await res.json()) as StepPromptPreview;
      setVideoPromptPreview(data);
      setActionProgress(100);
      setActionMessage(`已生成 ${data.shot_prompts.length} 个镜头的视频 Prompt 预览。`);
    } catch (err) {
      setActionProgress(0);
      setError(err instanceof Error ? err.message : "生成视频 Prompt 预览失败");
    } finally {
      setPendingAction(null);
      setBusy(false);
    }
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
          params: buildStepParams(),
        }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "步骤运行失败");
      }
      const data = await res.json();
      syncSelectionFromResponse(data);
      await refreshWorkflowData();
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
        body: JSON.stringify({ force: true, params: buildStepParams({ includeChapterId: false }) }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "批量运行失败");
      }
      const data = (await res.json()) as BatchStepRunResponse;
      syncSelectionFromResponse(data);
      await refreshWorkflowData(selected);
      setActionProgress(100);
      setActionMessage(
        `批量运行完成：成功 ${data.succeeded} 章，失败 ${data.failed} 章，跳过 ${data.skipped} 章。${
          asNumber(data.total_estimated_cost, 0) > 0 ? ` 本次预计消耗 $${asNumber(data.total_estimated_cost, 0).toFixed(4)}。` : ""
        }`
      );
    } catch (err) {
      setActionProgress(0);
      setError(err instanceof Error ? err.message : "批量运行失败");
    } finally {
      setPendingAction(null);
      setBusy(false);
    }
  }

  async function runCurrentStepForFailedChapters() {
    if (!projectId || !selected || !chapterScopedSteps.has(selected.step_name)) return;
    setPendingAction(`正在重跑失败章节：${selected.step_display_name}`);
    setBusy(true);
    setError(null);
    setActionMessage(null);
    try {
      const res = await fetch(`${apiBase}/api/v1/projects/${projectId}/steps/${selected.step_name}/run-failed-chapters`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force: true, params: buildStepParams({ includeChapterId: false }) }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "失败章节批量重跑失败");
      }
      const data = (await res.json()) as BatchStepRunResponse;
      syncSelectionFromResponse(data);
      await refreshWorkflowData(selected);
      setActionProgress(100);
      setActionMessage(
        `失败章节重跑完成：成功 ${data.succeeded} 章，失败 ${data.failed} 章，跳过 ${data.skipped} 章。${
          asNumber(data.total_estimated_cost, 0) > 0 ? ` 本次预计消耗 $${asNumber(data.total_estimated_cost, 0).toFixed(4)}。` : ""
        }`
      );
    } catch (err) {
      setActionProgress(0);
      setError(err instanceof Error ? err.message : "失败章节批量重跑失败");
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
      if (sourceFileInputRef.current) {
        sourceFileInputRef.current.value = "";
      }
      await refreshWorkflowData();
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

  function openSourceFilePicker() {
    if (busy) return;
    sourceFileInputRef.current?.click();
  }

  function handleSourceFileChange(file: File | null) {
    setUploadFile(file);
    setError(null);
    if (file) {
      setActionMessage(`已选择文件：${file.name}`);
    }
  }

  function clearSelectedSourceFile() {
    setUploadFile(null);
    if (sourceFileInputRef.current) {
      sourceFileInputRef.current.value = "";
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
      await refreshWorkflowData(selected, false);
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
            director_style: directorStyle,
            real_light_source_strategy: realLightSourceStrategy,
            skin_texture_level: skinTextureLevel,
            shot_distance_profile: shotDistanceProfile,
            lens_package: lensPackage,
            camera_movement_style: cameraMovementStyle,
            first_last_frame_bridge: firstLastFrameBridge,
            continuity_method: firstLastFrameBridge ? "story_bible + first_last_frame" : "story_bible_only",
            forbid_readable_text: forbidReadableText,
          },
        }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "保存风格失败");
      }
      await refreshWorkflowData(selected, false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存风格失败");
    } finally {
      setBusy(false);
    }
  }

  async function rebuildStoryBibleReferences() {
    if (!projectId) return;
    setPendingAction("正在重建 Story Bible 人物/场景/物品参考图库...");
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
      await refreshWorkflowData(selected, false);
      setActionProgress(100);
      setActionMessage("已重建 Story Bible 人物/场景/物品参考图库。");
    } catch (err) {
      setActionProgress(0);
      setError(err instanceof Error ? err.message : "重建 Story Bible 失败");
    } finally {
      setPendingAction(null);
      setBusy(false);
    }
  }

  async function regenerateStoryBibleEntity(kind: "characters" | "scenes" | "props", name: string) {
    if (!projectId) return;
    const label = kind === "characters" ? "人物" : kind === "scenes" ? "场景" : "道具";
    setPendingAction(`正在重新生成${label}参考：${name}...`);
    setBusy(true);
    setError(null);
    setActionMessage(null);
    try {
      const res = await fetch(`${apiBase}/api/v1/projects/${projectId}/story-bible/regenerate-item`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind, name }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || `重新生成${label}参考失败`);
      }
      const data = await res.json();
      setProject(data);
      await refreshWorkflowData(selected, false);
      setActionProgress(100);
      setActionMessage(`已重新生成${label}参考：${name}`);
    } catch (err) {
      setActionProgress(0);
      setError(err instanceof Error ? err.message : `重新生成${label}参考失败`);
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
      const mergedParams = {
        ...asRecord(body.params),
        ...buildStepParams(),
      };
      const res = await fetch(`${apiBase}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...body,
          chapter_id: chapterScopedSteps.has(selected.step_name) ? selectedChapter?.id ?? null : null,
          params: mergedParams,
        }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "动作执行失败");
      }
      const data = await res.json();
      syncSelectionFromResponse(data);
      await refreshWorkflowData();
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
    const lightweightApproval =
      path.includes("/approve-review-required-chapters") ||
      path.includes("/approve-all-chapters") ||
      path.includes("/approve-failed-chapters");
    const actionLabel =
      path.includes("/rework-regenerate-rescore-chapters") ? "正在自动修正返工章节并重新校核..." :
      path.includes("/rerun-pending-chapters") ? "正在重新评分待校核章节..." :
      path.includes("/approve-review-required-chapters") ? "正在批量通过已完成校核章节..." :
      path.includes("/approve-failed-chapters") ? "正在批量通过失败章节..." :
      path.includes("/approve-all-chapters") ? "正在批量通过当前所有章节..." :
      path.includes("/edit-continue-all-chapters") ? "正在批量保存人工编辑..." :
      path.includes("/edit-prompt-regenerate-all-chapters") ? "正在批量按新提示词重生成..." :
      path.includes("/switch-model-rerun-all-chapters") ? "正在批量切换模型重跑..." :
      path.includes("/run-failed-chapters") ? "正在批量重跑失败章节..." :
      path.includes("/run-all-chapters") ? "正在批量运行当前阶段..." :
      "正在批量处理当前章节动作...";
    setPendingAction(actionLabel);
    setBusy(true);
    setError(null);
    setActionMessage(null);
    try {
      const mergedParams = {
        ...asRecord(body.params),
        ...buildStepParams({ includeChapterId: false }),
      };
      const res = await fetch(`${apiBase}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...body,
          params: mergedParams,
        }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "批量动作执行失败");
      }
      const data = (await res.json()) as BatchStepRunResponse;
      syncSelectionFromResponse(data);
      if (data.current_step && typeof data.current_step === "object") {
        applyUpdatedStep(data.current_step as Step);
      }
      applyBatchChapterStatuses(selected.step_name, data.chapter_results, data.current_step as Step | null);
      setActionProgress(100);
      const batchCost = asNumber(data.total_estimated_cost, 0);
      setActionMessage(
        `${successMessage} 成功 ${data.succeeded} 章，失败 ${data.failed} 章，跳过 ${data.skipped} 章。${
          batchCost > 0 ? ` 本次预计消耗 $${batchCost.toFixed(4)}。` : ""
        }`
      );
      if (lightweightApproval) {
        void refreshWorkflowData(selected, false).catch((err) =>
          setError(err instanceof Error ? err.message : "刷新流程数据失败")
        );
      } else {
        await refreshWorkflowData(selected, false);
      }
    } catch (err) {
      setActionProgress(0);
      setError(err instanceof Error ? err.message : "批量动作执行失败");
    } finally {
      setPendingAction(null);
      setBusy(false);
    }
  }

  async function generateFinalCut() {
    if (!projectId) return;
    setPendingAction("正在生成最终成片...");
    setBusy(true);
    setError(null);
    setActionMessage(null);
    try {
      const res = await fetch(`${apiBase}/api/v1/projects/${projectId}/final-cut`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force: true, params: buildFinalCutRunParams() }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "生成成片失败");
      }
      const data = (await res.json()) as ExportRead;
      setLatestExport(data);
      await refreshWorkflowData(selected, false);
      setActionProgress(100);
      setActionMessage("已完成成片合成，可直接预览或导出。");
    } catch (err) {
      setActionProgress(0);
      setError(err instanceof Error ? err.message : "生成成片失败");
    } finally {
      setPendingAction(null);
      setBusy(false);
    }
  }

  async function selectStoryboardVersion(versionId: string) {
    if (!projectId || !selected) return;
    if (activeStoryboardVersion?.id === versionId) {
      setActionMessage("当前已经在使用这个分镜版本");
      return;
    }
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
      const data = (await res.json()) as Step;
      syncSelectionFromResponse(data);
      applyUpdatedStep(data, selectedChapter?.id ?? null);
      setStoryboardVersions((current) =>
        current.map((version) => ({
          ...version,
          is_active: version.id === versionId,
        }))
      );
      setActionProgress(100);
      setActionMessage("已切换到所选分镜版本");
      void refreshWorkflowData(data).catch((err) =>
        setError(err instanceof Error ? err.message : "刷新分镜版本失败")
      );
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
    Promise.all([refreshWorkflowData(null), loadStaticData()]).catch((err) =>
      setError(err instanceof Error ? err.message : "加载失败")
    );
  }, [projectId]);

  useEffect(() => {
    const styleProfile = asRecord(project?.style_profile);
    const visualStyle = asRecord(asRecord(styleProfile.story_bible).visual_style);
    setStylePresetId(asString(styleProfile.preset_id, "cinematic"));
    setCustomStyle(asString(styleProfile.custom_style, ""));
    setCustomDirectives(asString(styleProfile.custom_directives, ""));
    setDirectorStyle(asString(visualStyle.director_style, directorStyleOptions[0]));
    setRealLightSourceStrategy(asString(visualStyle.real_light_source_strategy, realLightSourceOptions[0]));
    setSkinTextureLevel(asString(visualStyle.skin_texture_level, skinTextureOptions[0]));
    setShotDistanceProfile(asString(visualStyle.shot_distance_profile, shotDistanceOptions[0]));
    setLensPackage(asString(visualStyle.lens_package, lensPackageOptions[0]));
    setCameraMovementStyle(asString(visualStyle.camera_movement_style, cameraMotionOptions[0]));
    setFirstLastFrameBridge(asBoolean(visualStyle.first_last_frame_bridge, true));
    setForbidReadableText(asBoolean(visualStyle.forbid_readable_text, true));
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
    if (selected?.step_name !== "storyboard_image") return;
    const nextQuality = asString(selectedStoryboardParams.storyboard_quality, "draft");
    const preset = storyboardQualityOptions.find((item) => item.id === nextQuality) ?? storyboardQualityOptions[0];
    setStoryboardQuality(preset.id);
    setStoryboardImageSize(asString(selectedStoryboardParams.size, preset.defaultSize));
    const nextBudget = asNumber(selectedStoryboardParams.max_total_cost_usd, preset.defaultBudget);
    setStoryboardBudgetUsd(String(nextBudget));
  }, [selected?.id, selected?.step_name, selectedChapter?.id, selectedStoryboardParams]);

  useEffect(() => {
    if (selected?.step_name !== "segment_video") return;
    const nextProfile = asString(selectedStoryboardParams.video_prompt_profile, "generic");
    const profile =
      videoPromptProfileOptions.find((item) => item.id === nextProfile) ?? videoPromptProfileOptions[0];
    const nextMotion = asString(selectedStoryboardParams.video_motion_intensity, "medium");
    const motion =
      videoMotionIntensityOptions.find((item) => item.id === nextMotion) ?? videoMotionIntensityOptions[1];
    setVideoPromptProfile(profile.id);
    setVideoUseReferenceImage(asBoolean(selectedStoryboardParams.video_use_reference_image, true));
    setVideoIncludeDialogue(asBoolean(selectedStoryboardParams.video_include_dialogue, true));
    setVideoIncludeNarration(asBoolean(selectedStoryboardParams.video_include_narration, true));
    setVideoMotionIntensity(motion.id);
    const nextAudioMode = asString(
      selectedStoryboardParams.video_audio_mode,
      asBoolean(selectedStoryboardParams.generate_audio, false) ? "demo_native_audio" : "formal_tts"
    );
    const audioMode =
      videoAudioModeOptions.find((item) => item.id === nextAudioMode) ?? videoAudioModeOptions[1];
    setVideoAudioMode(audioMode.id);
    setVideoGenerateAudio(audioMode.id === "demo_native_audio");
  }, [selected?.id, selected?.step_name, selectedChapter?.id, selectedStoryboardParams]);

  useEffect(() => {
    if (selected?.step_name !== "stitch_subtitle_tts") return;
    const nextAudioMode = asString(
      selectedStoryboardParams.audio_mode,
      asString(finalCutSummary.audio_mode, "formal_tts")
    );
    const audioMode =
      videoAudioModeOptions.find((item) => item.id === nextAudioMode) ?? videoAudioModeOptions[1];
    setVideoAudioMode(audioMode.id);
    setVideoGenerateAudio(audioMode.id === "demo_native_audio");
  }, [selected?.id, selected?.step_name, selectedStoryboardParams, finalCutSummary]);

  useEffect(() => {
    setVideoGenerateAudio(videoAudioMode === "demo_native_audio");
  }, [videoAudioMode]);

  useEffect(() => {
    loadStoryboardVersions(selected).catch((err) =>
      setError(err instanceof Error ? err.message : "加载分镜版本失败")
    );
  }, [projectId, selected?.id, selected?.step_name, selectedChapter?.id]);

  useEffect(() => {
    setVideoPromptPreview(null);
  }, [selected?.id, selectedChapter?.id]);

  useEffect(() => {
    if (selected?.step_name !== "segment_video") return;
    setVideoPromptPreview(null);
  }, [selected?.step_name, systemPrompt, taskPrompt, videoPromptProfile, videoUseReferenceImage, videoIncludeDialogue, videoIncludeNarration, videoMotionIntensity, videoAudioMode, videoGenerateAudio]);

  useEffect(() => {
    if (!selected || !chapterScopedSteps.has(selected.step_name) || chapters.length === 0) return;
    const current = selectedChapterId ? chapters.find((chapter) => chapter.id === selectedChapterId) ?? null : null;
    if (current) return;
    const preferred = pickPreferredChapter(chapters, selected.step_name, null);
    if (preferred && preferred.id !== selectedChapterId) {
      setSelectedChapterId(preferred.id);
    }
  }, [chapters, selected?.step_name, selectedChapterId]);

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


  function renderStoryBibleVariantGrid(variants: StoryBibleAssetView[]) {
    if (variants.length === 0) return null;
    return (
      <div className="storyBibleVariantGrid">
        {variants.map((variant, index) => {
          const imageUrl = variant.image_url || variant.thumbnail_url || "";
          const exportUrl = variant.export_url || imageUrl;
          return (
            <article key={`${variant.view_key || variant.view_label}-${index}`} className="storyBibleVariantCard">
              {imageUrl ? (
                <img
                  src={resolveMediaUrl(imageUrl)}
                  alt={variant.view_label || `参考图 ${index + 1}`}
                  className="storyBibleVariantPreview"
                />
              ) : (
                <div className="storyBibleVariantPreview framePreviewEmpty">暂无变体</div>
              )}
              <div className="storyBibleVariantMeta">
                <strong>{variant.view_label || variant.view_key || `参考图 ${index + 1}`}</strong>
                {(variant.provider || variant.model) ? (
                  <p className="muted" style={{ margin: "6px 0 0" }}>
                    {clipText(`${variant.provider || ""}${variant.model ? `/${variant.model}` : ""}`.replace(/^\//, ""), 64)}
                  </p>
                ) : null}
              </div>
              {exportUrl ? (
                <div className="frameCardFooter">
                  <a className="downloadLink subtle" href={resolveDownloadUrl(exportUrl)}>
                    导出该参考图
                  </a>
                </div>
              ) : null}
            </article>
          );
        })}
      </div>
    );
  }

  function renderStoryBibleEntitySection(
    title: string,
    eyebrow: string,
    items: StoryBibleEntity[],
    kind: "characters" | "scenes" | "props"
  ) {
    if (items.length === 0) return null;
    const readyCount = items.filter((item) => {
      const variants = storyBibleVariants(item, kind);
      return storyBibleStatus(item, kind) === "SUCCEEDED" && variants.length > 0;
    }).length;
    const attentionCount = Math.max(items.length - readyCount, 0);
    return (
      <details className="storyBibleShelf" open={kind === "characters"}>
        <summary className="storyBibleShelfSummary">
          <div>
            <p className="eyebrow">{eyebrow}</p>
            <strong>{title}</strong>
          </div>
          <div className="storyBibleShelfMeta">
            <span className="pill">{items.length} 项</span>
            <span className="pill">已就绪 {readyCount}</span>
            {attentionCount > 0 ? <span className="pill">{attentionCount} 项待处理</span> : null}
          </div>
        </summary>
        <div className="storyboardGrid storyBibleShelfBody">
          {items.map((item) => {
            const primaryUrl = storyBiblePrimaryUrl(item, kind);
            const primaryExportUrl = primaryUrl || "";
            const variants = storyBibleVariants(item, kind);
            const variantLabel = kind === "characters" ? "身份四视图" : kind === "scenes" ? "多角度/多光照参考" : "物品多视图";
            const exportLabel = kind === "characters" ? "导出主参考图" : kind === "scenes" ? "导出主场景图" : "导出主物品图";
            const variantSummary = variants.length > 0 ? `${variantLabel} · ${variants.length}` : `${variantLabel} · 待重建`;
            const status = storyBibleStatus(item, kind);
            const needsAttention = status !== "SUCCEEDED" || variants.length === 0;
            const description = item.reference_display_description || item.description || item.visual_anchor || "暂无描述";
            const actionLabel =
              kind === "characters"
                ? needsAttention ? "一键重新生成该人物" : "重新生成该人物参考"
                : kind === "scenes"
                ? needsAttention ? "一键重新生成该场景" : "重新生成该场景参考"
                : needsAttention ? "一键重新生成该道具" : "重新生成该道具参考";
            return (
              <article key={`${kind}-${item.name}`} className="frameCard">
                {primaryUrl ? (
                  <img src={resolveMediaUrl(primaryUrl)} alt={item.name} className="framePreview" />
                ) : (
                  <div className="framePreview framePreviewEmpty">暂无参考图</div>
                )}
                <div className="frameMeta">
                  <div className="frameCardHeader" style={{ padding: 0, marginBottom: 10 }}>
                    <strong>{item.name}</strong>
                    <span className="pill">{variantSummary}</span>
                  </div>
                  <p className="eyebrow" style={{ margin: "4px 0" }}>{title}</p>
                  <p>{description}</p>
                  {item.reference_hard_constraints && item.reference_hard_constraints.length > 0 ? (
                    <p className="muted" style={{ marginBottom: 8 }}>
                      硬约束：{item.reference_hard_constraints.join(" / ")}
                    </p>
                  ) : null}
                  {needsAttention ? (
                    <p className="muted" style={{ marginBottom: 8 }}>
                      当前状态：{storyBibleStatusLabel(status)}
                    </p>
                  ) : null}
                  {item.reference_generation_error ? (
                    <p className="muted" style={{ marginBottom: 8 }}>
                      失败原因：{item.reference_generation_error}
                    </p>
                  ) : null}
                  {variants.length === 0 && primaryUrl ? (
                    <p className="muted" style={{ marginBottom: 0 }}>
                      当前项目仍在使用旧版单图参考资产；点击“重建参考图库”后会生成完整多视图资产。
                    </p>
                  ) : null}
                </div>
                {renderStoryBibleVariantGrid(variants)}
                {primaryExportUrl ? (
                  <div className="frameCardFooter">
                    <a className="downloadLink" href={resolveDownloadUrl(primaryExportUrl)}>
                      {exportLabel}
                    </a>
                    <button onClick={() => regenerateStoryBibleEntity(kind, item.name)} disabled={busy}>
                      {busy && pendingAction?.includes(item.name) ? "处理中..." : actionLabel}
                    </button>
                  </div>
                ) : (
                  <div className="frameCardFooter">
                    <button onClick={() => regenerateStoryBibleEntity(kind, item.name)} disabled={busy}>
                      {busy && pendingAction?.includes(item.name) ? "处理中..." : actionLabel}
                    </button>
                  </div>
                )}
              </article>
            );
          })}
        </div>
      </details>
    );
  }

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
          const textDetection = asRecord(frame.text_detection);
          const detectedTokens = asList(textDetection.tokens).map((item) => asString(item, "")).filter(Boolean);
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
                {detectedTokens.length > 0 ? (
                  <p className="accentText">
                    <strong>文字检测</strong> 命中 {detectedTokens.join(", ")}
                  </p>
                ) : null}
                {textDetection.enabled === false ? (
                  <p className="muted"><strong>文字检测</strong> 当前环境未启用 OCR，仍以提示词硬约束为主。</p>
                ) : null}
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
    const costLabel = executionCostLabel(executionStats);
    const costValue = executionCostValue(executionStats);
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
            <span className="metricLabel">{costLabel}</span>
            <strong>{costValue.toFixed(6)}</strong>
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
            <div className="card calloutCard" style={{ marginBottom: 12, padding: 12 }}>
              <strong>硬约束</strong>
              <p style={{ margin: "6px 0 0" }}>
                当前分镜图必须无字幕、无文字、无 Logo、无水印；若 OCR 检出可读文字，将直接判定失败并要求重生。
              </p>
            </div>
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
            <div className="card calloutCard" style={{ marginBottom: 12, padding: 12 }}>
              <strong>校核重点</strong>
              <p style={{ margin: "6px 0 0" }}>
                除章节内部人物/场景连续性外，分镜校核会继续收口章节边界的首尾帧衔接；不相关的文学辅助章节默认跳过。
              </p>
            </div>
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
            <div className="card calloutCard" style={{ marginBottom: 12, padding: 12 }}>
              <strong>硬约束</strong>
              <p style={{ margin: "6px 0 0" }}>
                视频片段必须输出真实运动，而不是静态停留；当前已启用首尾帧桥接提示词，优先约束章节边界连续性。
              </p>
            </div>
            <div className="card" style={{ marginBottom: 12, padding: 12 }}>
              <strong>章节级生成</strong>
              <p style={{ margin: "6px 0 0" }}>
                第 7 步现在按章节生成视频片段。先在左侧选择章节，再点击上方“生成当前章节视频片段”；也可以批量对多个章节生成演示片段。
              </p>
            </div>
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
                <span className="metricLabel">生成模式</span>
                <strong style={{ fontSize: 16 }}>{asString(selectedArtifact.artifact_mode, "-")}</strong>
              </div>
              <div className="metric">
                <span className="metricLabel">提示词档案</span>
                <strong style={{ fontSize: 16 }}>{asString(selectedArtifact.prompt_profile_label, asString(selectedArtifact.prompt_profile, "-"))}</strong>
              </div>
              <div className="metric">
                <span className="metricLabel">音频策略</span>
                <strong style={{ fontSize: 16 }}>{asString(selectedArtifact.audio_mode_label, selectedVideoAudioMode.label)}</strong>
              </div>
              <div className="metric">
                <span className="metricLabel">来源模型</span>
                <strong style={{ fontSize: 16 }}>{selected.model_provider}/{selected.model_name}</strong>
              </div>
              <div className="metric">
                <span className="metricLabel">运动校验</span>
                <strong style={{ fontSize: 16 }}>{asBoolean(motionValidation.passed, false) ? "通过" : "未通过"}</strong>
              </div>
            </div>
            {(() => {
              const continuityPackage = asRecord(selectedArtifact.continuity_package);
              if (!asBoolean(continuityPackage.enabled, false)) return null;
              const currentFirst = asRecord(continuityPackage.current_first_frame);
              const previousLast = asRecord(continuityPackage.previous_last_frame);
              return (
                <div className="diffList" style={{ marginTop: 12 }}>
                  <div className="diffItem">
                    连续性方法：{asString(continuityPackage.method, "story_bible + first_last_frame")}
                  </div>
                  <div className="diffItem">
                    当前章节首帧：镜头 {asString(currentFirst.shot_index, "-")} · {asString(currentFirst.summary, "暂无摘要")}
                  </div>
                  {asString(previousLast.chapter_title, "") ? (
                    <div className="diffItem">
                      上一章节末帧：{asString(previousLast.chapter_title)} · 镜头 {asString(previousLast.shot_index, "-")}
                    </div>
                  ) : null}
                </div>
              );
            })()}
            {Object.keys(dimensions).length > 0 ? (
              <div className="diffList">
                {Object.entries(dimensions).map(([key, value]) => (
                  <div key={key} className="diffItem">
                    {dimensionLabel(key)}: {asString(value)}
                  </div>
                ))}
              </div>
            ) : null}
            {Object.keys(motionValidation).length > 0 ? (
              <div className="diffList" style={{ marginTop: 12 }}>
                <div className="diffItem">平均帧差：{asNumber(motionValidation.mean_frame_delta, 0).toFixed(2)}</div>
                <div className="diffItem">最大帧差：{asNumber(motionValidation.max_frame_delta, 0).toFixed(2)}</div>
                <div className="diffItem">采样帧数：{asString(motionValidation.sample_count, "-")}</div>
                <div className="diffItem">阈值：{asNumber(motionValidation.threshold, 0).toFixed(2)}</div>
                <div className="diffItem">reference 输入：{asBoolean(selectedArtifact.reference_image_used, true) ? "已启用" : "未启用"}</div>
                <div className="diffItem">旁白提示词：{asBoolean(selectedArtifact.narration_basis_included, true) ? "已纳入" : "未纳入"}</div>
                <div className="diffItem">对话提示词：{asBoolean(selectedArtifact.dialogue_basis_included, true) ? "已纳入" : "未纳入"}</div>
                <div className="diffItem">原生音轨：{asBoolean(selectedArtifact.native_audio_present, false) ? "已生成" : "未生成"}</div>
                {!asBoolean(motionValidation.passed, false) ? (
                  <div className="diffItem">告警：当前片段运动幅度过低，建议重跑视频生成。</div>
                ) : null}
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
          {segmentClipManifest.length > 0 ? (
            <section className="card mediaGalleryCard">
              <div className="mediaSectionHeader">
                <div>
                  <p className="eyebrow">镜头片段清单</p>
                  <h4>章节视频由这些镜头片段组成</h4>
                </div>
                <span className="pill">共 {segmentClipManifest.length} 段</span>
              </div>
              <div className="diffList">
                {segmentClipManifest.map((clip, index) => (
                  <div key={`${asString(clip.shot_index, String(index))}-${index}`} className="diffItem" style={{ display: "grid", gap: 4 }}>
                    <strong>镜头 {asString(clip.shot_index, String(index + 1))}</strong>
                    <span>模式：{asString(clip.mode, "-")}</span>
                    <span>时长：{asString(clip.duration_sec, "-")}s</span>
                    {asString(clip.motion_directive, "") ? <span>运镜：{asString(clip.motion_directive)}</span> : null}
                    {asString(clip.fallback_reason, "") ? <span>回退原因：{clipText(asString(clip.fallback_reason), 140)}</span> : null}
                    {asString(clip.preview_url, "") ? (
                      <a className="downloadLink subtle" href={resolveDownloadUrl(asString(clip.preview_url))}>导出该镜头片段</a>
                    ) : null}
                  </div>
                ))}
              </div>
            </section>
          ) : null}
          {renderExecutionStatsBlock()}
        </div>
      );
    }

    if (selected.step_name === "stitch_subtitle_tts") {
      return (
        <div className="mediaWorkspace">
          <section className="mediaHeroCard">
            <div className="mediaHeroHeader">
              <div>
                <p className="eyebrow">成片总装</p>
                <h3>将所有章节片段合并为完整成片</h3>
              </div>
              <div className="actionsRow">
                {latestExportUrl ? (
                  <a className="downloadLink" href={resolveDownloadUrl(latestExportUrl)}>
                    导出最新成片
                  </a>
                ) : null}
                {finalCutAudioUrl ? (
                  <a className="downloadLink" href={resolveDownloadUrl(finalCutAudioUrl)}>
                    导出旁白音轨
                  </a>
                ) : null}
                {finalCutSubtitleUrl ? (
                  <a className="downloadLink" href={resolveDownloadUrl(finalCutSubtitleUrl)}>
                    导出字幕文件
                  </a>
                ) : null}
              </div>
            </div>
            {latestExportUrl ? (
              <video className="chapterVideoPlayer" controls preload="metadata" src={resolveMediaUrl(latestExportUrl)} />
            ) : (
              <div className="mediaHeroImage mediaHeroEmpty">尚未生成最终成片，点击右侧“一键生成成片”后将在此处预览。</div>
            )}
            <div className="metricRow">
              <div className="metric">
                <span className="metricLabel">章节片段数</span>
                <strong>{asNumber(finalCutSummary.segment_count, finalCutManifest.length)}</strong>
              </div>
              <div className="metric">
                <span className="metricLabel">字幕条数</span>
                <strong>{asNumber(finalCutSummary.subtitle_count, finalCutSubtitleEntries.length)}</strong>
              </div>
              <div className="metric">
                <span className="metricLabel">旁白音轨</span>
                <strong style={{ fontSize: 16 }}>
                  {finalCutAudioUrl ? "已生成" : videoAudioMode === "demo_native_audio" ? "已跳过" : "未生成"}
                </strong>
              </div>
              <div className="metric">
                <span className="metricLabel">音频策略</span>
                <strong style={{ fontSize: 16 }}>{asString(finalCutSummary.audio_mode_label, selectedVideoAudioMode.label)}</strong>
              </div>
              <div className="metric">
                <span className="metricLabel">当前模型</span>
                <strong style={{ fontSize: 16 }}>{selected.model_provider}/{selected.model_name}</strong>
              </div>
            </div>
            <div className="card" style={{ padding: 12, marginTop: 12 }}>
              <strong>成片音频模式</strong>
              <p className="muted" style={{ marginTop: 8, marginBottom: 10 }}>
                Demo 模式会直接保留第 7 步原生音轨并跳过 TTS；正式成片模式会统一生成旁白、字幕和配音。
              </p>
              <label style={{ display: "block" }}>
                成片音频策略
                <select
                  className="fullWidthControl"
                  value={videoAudioMode}
                  onChange={(e) => setVideoAudioMode(e.target.value as (typeof videoAudioModeOptions)[number]["id"])}
                  disabled={busy}
                  data-testid="final-cut-audio-mode-select"
                >
                  {videoAudioModeOptions.map((option) => (
                    <option key={option.id} value={option.id}>
                      {option.label}：{option.description}
                    </option>
                  ))}
                </select>
              </label>
              <p className="muted" style={{ marginTop: 8, marginBottom: 0 }}>
                {asString(finalCutSummary.audio_mode_reason, videoAudioMode === "demo_native_audio"
                  ? "当前会尝试直接复用第 7 步原生音轨。"
                  : "当前会在第 8 步统一生成旁白和字幕。")}
              </p>
            </div>
            {asString(finalCutSummary.narration_writer_model, "") ? (
              <p className="muted" style={{ marginTop: 12, marginBottom: 0 }}>
                旁白脚本：{asString(finalCutSummary.narration_generation_mode, "model")} · {asString(finalCutSummary.narration_writer_provider, "-")}/{asString(finalCutSummary.narration_writer_model, "-")}
              </p>
            ) : null}
            <p className="muted" style={{ marginTop: 12, marginBottom: 0 }}>
              一键生成会按照当前音频策略自动执行本阶段：Demo 模式只整理字幕并保留原生音轨；正式成片模式会统一生成旁白、字幕与配音，再合成为最终 MP4。
            </p>
          </section>
          <section className="card mediaGalleryCard">
            <div className="mediaSectionHeader">
              <div>
                <p className="eyebrow">章节片段清单</p>
                <h4>参与成片合并的视频段落</h4>
              </div>
              <span className="pill">共 {finalCutManifest.length} 段</span>
            </div>
            {finalCutManifest.length === 0 ? (
              <p className="muted">当前还没有可合成的章节视频片段。</p>
            ) : (
              <div className="diffList">
                {finalCutManifest.map((item, index) => (
                  <div key={`${asString(item.chapter_id, String(index))}-${index}`} className="diffItem">
                    {asString(item.title, `片段 ${index + 1}`)} · {asNumber(item.duration_sec, 0).toFixed(1)}s
                  </div>
                ))}
              </div>
            )}
          </section>
          <section className="card mediaGalleryCard">
            <div className="mediaSectionHeader">
              <div>
                <p className="eyebrow">旁白与字幕预览</p>
                <h4>生成前可快速核对的后期文案</h4>
              </div>
              <span className="pill">{finalCutSubtitleEntries.length} 条字幕</span>
            </div>
            <textarea
              readOnly
              rows={8}
              value={asString(selectedArtifact.narration_text, "")}
              style={{ width: "100%", marginBottom: 12 }}
            />
            <div className="diffList">
              {finalCutSubtitleEntries.slice(0, 8).map((item, index) => (
                <div key={`${asString(item.index, String(index))}-${index}`} className="diffItem">
                  {asNumber(item.start_sec, 0).toFixed(1)}s - {asNumber(item.end_sec, 0).toFixed(1)}s：{asString(item.text, "")}
                </div>
              ))}
            </div>
            {finalCutSubtitleEntries.length > 8 ? (
              <p className="muted" style={{ marginBottom: 0 }}>仅预览前 8 条字幕，完整内容可导出 `.srt` 查看。</p>
            ) : null}
          </section>
          {renderExecutionStatsBlock()}
        </div>
      );
    }

    return (
      <>
        {selectedChapter && chapterScopedSteps.has(selected.step_name) ? (
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
    <main className="shell" data-testid="project-page">
      <div className="projectWorkspace">
        <div className="projectMain">
          <section className="card row" style={{ justifyContent: "space-between" }} data-testid="project-header">
        <div>
          <h1 data-testid="project-title">{project?.name ?? "项目审核台"}</h1>
          <p className="muted">状态: {project?.status ?? "-"} | 目标时长: {project?.target_duration_sec ?? "-"}s</p>
        </div>
        <div className="row">
          <Link href="/">返回项目列表</Link>
          <button onClick={generateFinalCut} disabled={busy} data-testid="render-final-button">
            一键生成成片
          </button>
        </div>
      </section>

      <section className="card">
        <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
          <strong>流程总体进度</strong>
          <span>{overallProgress}%</span>
        </div>
        <div className="progressTrack">
          <div
            className="progressFill"
            style={{
              width: `${Math.max(0, Math.min(100, overallProgress))}%`,
            }}
          />
        </div>
      </section>

      <section className="card" data-testid="source-documents-section">
        <h3>输入源文件（PDF/TXT）</h3>
        <input
          ref={sourceFileInputRef}
          type="file"
          accept=".pdf,.txt"
          onChange={(e) => handleSourceFileChange(e.target.files?.[0] ?? null)}
          data-testid="source-document-file-input"
          style={{ display: "none" }}
        />
        <div
          className="uploadPicker"
          role="button"
          tabIndex={0}
          onClick={openSourceFilePicker}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              openSourceFilePicker();
            }
          }}
          onDragOver={(event) => {
            event.preventDefault();
          }}
          onDrop={(event) => {
            event.preventDefault();
            handleSourceFileChange(event.dataTransfer.files?.[0] ?? null);
          }}
          data-testid="source-document-picker"
          aria-label="选择或拖入 PDF/TXT 文件"
        >
          <strong>{uploadFile ? "已选择源文件" : "选择文件或拖入这里"}</strong>
          <span className="muted">
            {uploadFile ? uploadFile.name : "支持 PDF、TXT；点击打开系统文件选择器"}
          </span>
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          <input
            value={uploadFile?.name ?? ""}
            readOnly
            placeholder="尚未选择文件"
            aria-label="当前选择的源文件"
            style={{ flex: "1 1 320px" }}
          />
          <button type="button" onClick={openSourceFilePicker} disabled={busy} data-testid="source-document-open-button">
            选择文件
          </button>
          <button type="button" onClick={clearSelectedSourceFile} disabled={busy || !uploadFile}>
            清除
          </button>
          <button onClick={uploadSourceDocument} disabled={busy || !uploadFile} data-testid="source-document-upload-button">
            上传并登记
          </button>
        </div>
        {docs.length === 0 ? <p className="muted">暂无源文件</p> : null}
        {docs.map((doc) => (
          <div
            key={doc.id}
            className="row"
            style={{ justifyContent: "space-between", marginTop: 8 }}
            data-testid={`source-document-row-${doc.id}`}
          >
            <span>{doc.file_name}</span>
            <span className="pill">{doc.file_type.toUpperCase()}</span>
            <span className="pill">{doc.parse_status}</span>
          </div>
        ))}
      </section>

      {error ? <section className="card muted" data-testid="workflow-error-message">{error}</section> : null}
      {(pendingAction || actionProgress > 0) ? (
        <section className="card muted" data-testid="workflow-pending-action">
          <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
            <strong>{pendingAction ?? "执行完成"}</strong>
            <span>{Math.round(actionProgress)}%</span>
          </div>
          <div className="progressTrack">
            <div
              className="progressFill progressFillAccent"
              style={{
                width: `${Math.max(0, Math.min(100, actionProgress))}%`,
              }}
            />
          </div>
        </section>
      ) : null}
      {actionMessage ? <section className="card" data-testid="workflow-action-message">{actionMessage}</section> : null}
      {latestExport || project?.output_path ? (
        <section className="card" data-testid="latest-export-section">
          <h3>最新导出</h3>
          <p>状态: {latestExport?.status ?? (project?.status === "COMPLETED" ? "COMPLETED" : "-")}</p>
          <p className="muted">输出: {latestExport?.output_key ?? project?.output_path ?? "-"}</p>
          {latestExportUrl ? (
            <div style={{ marginTop: 12 }}>
              <video className="chapterVideoPlayer" controls preload="metadata" src={resolveMediaUrl(latestExportUrl)} />
              <div className="row" style={{ marginTop: 8 }}>
                <a className="downloadLink" href={resolveDownloadUrl(latestExportUrl)}>导出成片文件</a>
              </div>
            </div>
          ) : null}
        </section>
      ) : null}

      <section className="card storyBibleSection" data-testid="story-bible-section">
        <div className="storyBibleHeader">
          <div className="storyBibleHeaderText">
            <p className="eyebrow">Story Bible</p>
            <h3>影视基准与参考资产</h3>
            <p className="muted storyBibleLead">
              先收口风格与硬约束，再按需展开资产库。默认减少页面噪音，但保留人物、场景、物品的完整人工闭环。
            </p>
          </div>
          <div className="storyBibleHeaderActions">
            <button onClick={saveStyleProfile} disabled={busy}>
              保存风格设定
            </button>
            <button onClick={rebuildStoryBibleReferences} disabled={busy}>
              重建参考图库
            </button>
          </div>
        </div>

        <div className="storyBibleCountRow">
          <span className="storyBibleCountPill">总资产 {storyBibleAssetSummary.total}</span>
          <span className="storyBibleCountPill">已就绪 {storyBibleAssetSummary.ready}</span>
          {storyBibleAssetSummary.attention > 0 ? (
            <span className="storyBibleCountPill attention">待处理 {storyBibleAssetSummary.attention}</span>
          ) : null}
          <span className="storyBibleCountPill">人物 {storyBibleAssetSummary.characters.total}</span>
          <span className="storyBibleCountPill">场景 {storyBibleAssetSummary.scenes.total}</span>
          <span className="storyBibleCountPill">物品 {storyBibleAssetSummary.props.total}</span>
        </div>

        <div className="storyBibleSummaryGrid">
          <article className="storyBibleSummaryCard">
            <p className="eyebrow">当前影视控制摘要</p>
            <p>导演风格：{asString(storyBibleVisualStyle.director_style, directorStyle)}</p>
            <p>真实光源管理：{asString(storyBibleVisualStyle.real_light_source_strategy, realLightSourceStrategy)}</p>
            <p>肌肤纹理：{asString(storyBibleVisualStyle.skin_texture_level, skinTextureLevel)}</p>
            <p>景别策略：{asString(storyBibleVisualStyle.shot_distance_profile, shotDistanceProfile)}</p>
            <p>镜头组：{asString(storyBibleVisualStyle.lens_package, lensPackage)}</p>
            <p>运镜风格：{asString(storyBibleVisualStyle.camera_movement_style, cameraMovementStyle)}</p>
            <p style={{ marginBottom: 0 }}>
              连续性方法：{asString(storyBibleVisualStyle.continuity_method, firstLastFrameBridge ? "story_bible + first_last_frame" : "story_bible_only")}
            </p>
          </article>
          <article className="storyBibleSummaryCard">
            <p className="eyebrow">当前资产状态</p>
            <p>人物身份肖像：{storyBibleAssetSummary.characters.ready}/{storyBibleAssetSummary.characters.total}</p>
            <p>场景空间参考：{storyBibleAssetSummary.scenes.ready}/{storyBibleAssetSummary.scenes.total}</p>
            <p>关键物品参考：{storyBibleAssetSummary.props.ready}/{storyBibleAssetSummary.props.total}</p>
            <p style={{ marginBottom: 0 }}>
              当前项目默认启用首尾帧桥接与分镜禁字硬约束。
            </p>
          </article>
          {(() => {
            const safety = asRecord(storyBible.safety_preprocess);
            const changedCount = asNumber(safety.changed_count, 0);
            if (!changedCount) {
              return (
                <article className="storyBibleSummaryCard">
                  <p className="eyebrow">安全预处理</p>
                  <p style={{ marginBottom: 0 }}>
                    当前未命中需要改写的敏感视觉描述，参考图将直接按 Story Bible 基准生成。
                  </p>
                </article>
              );
            }
            return (
              <article className="storyBibleSummaryCard attention">
                <p className="eyebrow">前置安全改写</p>
                <p>{asString(safety.summary, "已执行参考图安全预处理。")}</p>
                <p style={{ marginBottom: 0 }}>
                  已改写 {changedCount} 个参考项，避免人物、场景、物品参考图因敏感视觉表述直接生成失败。
                </p>
              </article>
            );
          })()}
        </div>

        <details className="storyBibleDetails" open>
          <summary>
            <div>
              <strong>风格控制与生成约束</strong>
              <p className="muted">这里保留项目级创作控制；资产库单独折叠，避免主页面过于拥挤。</p>
            </div>
            <span className="pill">项目级控制</span>
          </summary>
          <div className="storyBibleDetailsBody">
            <div className="row">
              <select value={stylePresetId} onChange={(e) => setStylePresetId(e.target.value)}>
                {stylePresets.map((preset) => (
                  <option key={preset.id} value={preset.id}>
                    {preset.label}
                  </option>
                ))}
              </select>
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
            <div className="storyBibleControlGrid">
              <label>
                导演风格
                <select value={directorStyle} onChange={(e) => setDirectorStyle(e.target.value)} style={{ width: "100%", marginTop: 6 }}>
                  {directorStyleOptions.map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                真实光源管理
                <select
                  value={realLightSourceStrategy}
                  onChange={(e) => setRealLightSourceStrategy(e.target.value)}
                  style={{ width: "100%", marginTop: 6 }}
                >
                  {realLightSourceOptions.map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                肌肤纹理
                <select value={skinTextureLevel} onChange={(e) => setSkinTextureLevel(e.target.value)} style={{ width: "100%", marginTop: 6 }}>
                  {skinTextureOptions.map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                景别策略
                <select value={shotDistanceProfile} onChange={(e) => setShotDistanceProfile(e.target.value)} style={{ width: "100%", marginTop: 6 }}>
                  {shotDistanceOptions.map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                镜头组
                <select value={lensPackage} onChange={(e) => setLensPackage(e.target.value)} style={{ width: "100%", marginTop: 6 }}>
                  {lensPackageOptions.map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                运镜风格
                <select
                  value={cameraMovementStyle}
                  onChange={(e) => setCameraMovementStyle(e.target.value)}
                  style={{ width: "100%", marginTop: 6 }}
                >
                  {cameraMotionOptions.map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div className="storyBibleCheckboxRow">
              <label>
                <input
                  type="checkbox"
                  checked={firstLastFrameBridge}
                  onChange={(e) => setFirstLastFrameBridge(e.target.checked)}
                />
                启用首尾帧桥接
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={forbidReadableText}
                  onChange={(e) => setForbidReadableText(e.target.checked)}
                />
                强制分镜禁字
              </label>
            </div>
            <p className="muted" style={{ marginBottom: 0 }}>
              预设风格会被注入 Story Bible，并自动进入剧本、分镜图、视频生成阶段的提示词。
            </p>
          </div>
        </details>

        {(storyBibleCharacters.length > 0 || storyBibleScenes.length > 0 || storyBibleProps.length > 0) ? (
          <details className="storyBibleDetails" data-testid="story-bible-assets-toggle">
            <summary>
              <div>
                <strong>参考资产库</strong>
                <p className="muted">默认折叠，避免整页都被人物、场景、物品缩略图占满；需要时再展开精查。</p>
              </div>
              <span className="pill">
                {storyBibleAssetSummary.ready}/{storyBibleAssetSummary.total} 已就绪
              </span>
            </summary>
            <div className="storyBibleDetailsBody">
              {renderStoryBibleEntitySection("身份肖像参考", "角色身份肖像锚点", storyBibleCharacters, "characters")}
              {renderStoryBibleEntitySection("场景空间参考", "场景锚点", storyBibleScenes, "scenes")}
              {renderStoryBibleEntitySection("关键物品参考", "重要物品图库", storyBibleProps, "props")}
            </div>
          </details>
        ) : null}
      </section>

      <section className="card" data-testid="step-selection">
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
                data-testid={`step-button-${step.step_name}`}
                data-step-name={step.step_name}
                data-step-status={step.status}
                data-selected={selected?.id === step.id ? "true" : "false"}
                style={{
                  borderColor: isRunning ? "var(--warning)" : selected?.id === step.id ? "var(--accent)" : undefined,
                  background: isRunning ? "var(--warning-soft)" : undefined,
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

      <section className="card" data-testid="chapter-status-section">
        <h3>章节状态</h3>
        {chaptersLoading ? <p className="muted">章节加载中...</p> : null}
        {!chaptersLoading && chapters.length === 0 ? <p className="muted">完成“章节切分”后会生成章节列表。</p> : null}
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
        <aside className="card chapterRail" data-testid="chapter-list">
          <div className="chapterRailHeader">
            <div>
              <h3>章节导航</h3>
              <p className="muted">仅保留章节定位与阶段状态。</p>
            </div>
            <span className="pill">{chapters.length}</span>
          </div>
          {chaptersLoading ? <p className="muted">章节加载中...</p> : null}
          {!chaptersLoading && chapters.length === 0 ? <p className="muted">暂无章节</p> : null}
          {chapters.map((chapter) => (
            <button
              key={chapter.id}
              onClick={() => setSelectedChapterId(chapter.id)}
              className="chapterNavItem"
              data-testid={`chapter-button-${chapter.id}`}
              data-chapter-id={chapter.id}
              data-selected={selectedChapter?.id === chapter.id ? "true" : "false"}
            >
              <div className="chapterNavTop">
                <span className="chapterNavIndex">{chapter.chapter_index + 1}</span>
                <span className="pill chapterNavStatus">
                  {chapter.stage_status}
                  {typeof chapter.consistency_score === "number" ? ` · ${chapter.consistency_score}` : ""}
                </span>
              </div>
              <strong className="chapterNavTitle">{chapter.title}</strong>
            </button>
          ))}
        </aside>

        <section
          className={`card ${selected && mediaFocusedSteps.has(selected.step_name) ? "mediaReviewCard" : ""}`}
          data-testid="artifact-preview"
        >
          <h3>产物预览</h3>
          {renderMediaPreview()}
        </section>

        <section className="card" data-testid="workflow-action-panel">
          <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
            <h3 style={{ margin: 0 }}>人工闭环动作</h3>
            <div className="row">
              {selected && chapterScopedSteps.has(selected.step_name) ? (
                <button
                  onClick={runCurrentStepForAllChapters}
                  disabled={busy || !selected}
                  data-testid="run-current-step-all-chapters-button"
                >
                  {busy && pendingAction?.includes("批量运行")
                    ? "批量运行中..."
                    : selected.step_name === "storyboard_image"
                    ? "对当前所有章节运行当前阶段（按成本设定）"
                    : selected.step_name === "segment_video"
                    ? "对当前所有章节生成视频片段"
                    : "对当前所有章节运行当前阶段"}
                </button>
              ) : null}
              {selected && chapterScopedSteps.has(selected.step_name) && failedChapterItems.length > 0 ? (
                <button
                  onClick={runCurrentStepForFailedChapters}
                  disabled={busy || !selected}
                  data-testid="run-current-step-failed-chapters-button"
                >
                  {busy && pendingAction?.includes("失败章节")
                    ? "重跑中..."
                    : selected.step_name === "segment_video"
                    ? `对失败章节重生成视频片段（${failedChapterItems.length}）`
                    : `对失败章节运行当前阶段（${failedChapterItems.length}）${selected.step_name === "storyboard_image" ? " · 按成本设定" : ""}`}
                </button>
              ) : null}
              {selected?.step_name === "stitch_subtitle_tts" ? (
                <button onClick={generateFinalCut} disabled={busy || !selected} data-testid="run-current-step-button">
                  {busy && pendingAction?.includes("成片") ? pendingAction : "一键生成成片"}
                </button>
              ) : (
                <button onClick={() => runCurrentStep(true)} disabled={busy || !selected} data-testid="run-current-step-button">
                  {busy && pendingAction?.includes("正在运行")
                    ? pendingAction
                    : selected?.step_name === "storyboard_image"
                    ? "运行当前阶段（按成本设定）"
                    : selected?.step_name === "segment_video"
                    ? "生成当前章节视频片段"
                    : "运行当前阶段"}
                </button>
              )}
            </div>
          </div>
          {selected?.step_name === "storyboard_image" ? (
            <div className="card" style={{ padding: 12, marginBottom: 12 }} data-testid="storyboard-cost-panel">
              <strong>分镜出图成本控制</strong>
              <p className="muted" style={{ marginTop: 8, marginBottom: 10 }}>
                当前默认推荐使用“草稿”模式。批量运行达到预算上限后会自动停止，避免继续烧钱。
              </p>
              <div className="modelSelectorStack">
                <label>
                  成本模式
                  <select
                    className="fullWidthControl"
                    value={storyboardQuality}
                    onChange={(e) => {
                      const nextPreset =
                        storyboardQualityOptions.find((item) => item.id === e.target.value) ?? storyboardQualityOptions[0];
                      setStoryboardQuality(nextPreset.id);
                      setStoryboardImageSize(nextPreset.defaultSize);
                      setStoryboardBudgetUsd(String(nextPreset.defaultBudget));
                    }}
                    disabled={busy}
                    data-testid="storyboard-quality-select"
                  >
                    {storyboardQualityOptions.map((option) => (
                      <option key={option.id} value={option.id}>
                        {option.label}：{option.description}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  输出尺寸
                  <select
                    className="fullWidthControl"
                    value={storyboardImageSize}
                    onChange={(e) => setStoryboardImageSize(e.target.value)}
                    disabled={busy}
                    data-testid="storyboard-size-select"
                  >
                    {storyboardImageSizeOptions.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  批量预算上限（美元）
                  <input
                    className="fullWidthControl"
                    type="number"
                    min="1"
                    step="0.5"
                    value={storyboardBudgetUsd}
                    onChange={(e) => setStoryboardBudgetUsd(e.target.value)}
                    disabled={busy}
                    data-testid="storyboard-budget-input"
                  />
                </label>
              </div>
              <p className="muted modelSelectionHint" style={{ marginTop: 8, marginBottom: 6 }}>
                当前配置：{storyboardQualityPreset.label} / {storyboardImageSize} / 批量预算 ${asNumber(storyboardBudgetUsd, storyboardQualityPreset.defaultBudget).toFixed(1)}
              </p>
              {/(gpt-5-image|gemini-3)/i.test(modelName) ? (
                <p className="accentText" style={{ margin: 0 }}>
                  当前模型偏贵。若只是先看剧情和构图，建议改用 `google/gemini-2.5-flash-image`。
                </p>
              ) : (
                <p className="muted" style={{ margin: 0 }}>
                  {storyboardQualityPreset.id === "draft"
                    ? "草稿模式会限制为低分辨率、少参考图、单模型 fallback，优先控制成本。"
                    : storyboardQualityPreset.id === "balanced"
                    ? "均衡模式会使用 720p 和单模型策略，在画质与成本之间折中。"
                    : "精修模式会放宽分辨率与 fallback，适合少量精选章节，不适合大批量连跑。"}
                </p>
              )}
            </div>
          ) : null}
          {selected?.step_name === "segment_video" ? (
            <div className="card" style={{ padding: 12, marginBottom: 12 }} data-testid="segment-video-prompt-panel">
              <strong>视频提示词增强</strong>
              <p className="muted" style={{ marginTop: 8, marginBottom: 10 }}>
                将当前分镜图作为 reference 输入，并把人物、动作、旁白、镜头运动和首尾帧桥接一起编入视频模型提示词。
              </p>
              <div className="modelSelectorStack">
                <label>
                  视频模型提示词档案
                  <select
                    className="fullWidthControl"
                    value={videoPromptProfile}
                    onChange={(e) => setVideoPromptProfile(e.target.value as (typeof videoPromptProfileOptions)[number]["id"])}
                    disabled={busy}
                    data-testid="segment-video-profile-select"
                  >
                    {videoPromptProfileOptions.map((option) => (
                      <option key={option.id} value={option.id}>
                        {option.label}：{option.description}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  运动强度
                  <select
                    className="fullWidthControl"
                    value={videoMotionIntensity}
                    onChange={(e) => setVideoMotionIntensity(e.target.value as (typeof videoMotionIntensityOptions)[number]["id"])}
                    disabled={busy}
                    data-testid="segment-video-motion-select"
                  >
                    {videoMotionIntensityOptions.map((option) => (
                      <option key={option.id} value={option.id}>
                        {option.label}：{option.description}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  音频策略
                  <select
                    className="fullWidthControl"
                    value={videoAudioMode}
                    onChange={(e) => setVideoAudioMode(e.target.value as (typeof videoAudioModeOptions)[number]["id"])}
                    disabled={busy}
                    data-testid="segment-video-audio-mode-select"
                  >
                    {videoAudioModeOptions.map((option) => (
                      <option key={option.id} value={option.id}>
                        {option.label}：{option.description}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <div className="row" style={{ gap: 18, flexWrap: "wrap", marginTop: 8 }}>
                <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <input
                    type="checkbox"
                    checked={videoUseReferenceImage}
                    onChange={(e) => setVideoUseReferenceImage(e.target.checked)}
                    disabled={busy}
                    data-testid="segment-video-use-reference-checkbox"
                  />
                  将分镜图作为 reference image 输入
                </label>
                <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <input
                    type="checkbox"
                    checked={videoIncludeDialogue}
                    onChange={(e) => setVideoIncludeDialogue(e.target.checked)}
                    disabled={busy}
                    data-testid="segment-video-include-dialogue-checkbox"
                  />
                  将对话纳入视频提示词
                </label>
                <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <input
                    type="checkbox"
                    checked={videoIncludeNarration}
                    onChange={(e) => setVideoIncludeNarration(e.target.checked)}
                    disabled={busy}
                    data-testid="segment-video-include-narration-checkbox"
                  />
                  将旁白纳入视频提示词
                </label>
                <span className="pill" data-testid="segment-video-generate-audio-indicator">
                  {videoAudioMode === "demo_native_audio" ? "第 7 步将直接生成有声音轨" : "第 7 步将保持静音片段"}
                </span>
              </div>
              <div className="row" style={{ marginTop: 10 }}>
                <button onClick={applyVideoPromptEnhancement} disabled={busy} data-testid="segment-video-apply-enhancement-button">
                  套用视频模型增强模板
                </button>
                <button
                  onClick={previewSegmentVideoPrompt}
                  disabled={busy || !selectedChapter}
                  data-testid="segment-video-preview-prompt-button"
                >
                  {busy && pendingAction?.includes("视频 Prompt 预览") ? "预览生成中..." : "预览当前章节视频 Prompt"}
                </button>
              </div>
              <p className="muted modelSelectionHint" style={{ marginTop: 8, marginBottom: 0 }}>
                当前配置：{selectedVideoPromptProfile.label} / 运动强度 {selectedVideoMotionOption.label} / 音频策略 {selectedVideoAudioMode.label} / reference {videoUseReferenceImage ? "开启" : "关闭"} / 原生音轨 {videoAudioMode === "demo_native_audio" ? "开启" : "关闭"}
              </p>
              <p className="muted" style={{ marginTop: 6, marginBottom: 0 }}>
                {videoAudioMode === "demo_native_audio"
                  ? "Demo 模式：第 7 步直接生成有声章节片段，第 8 步只整理字幕并保留这些原生音轨。"
                  : "正式成片模式：第 7 步生成静音章节片段，第 8 步统一生成旁白、字幕和配音。"}
              </p>
              {videoPromptPreview ? (
                <div className="card" style={{ padding: 12, marginTop: 12 }} data-testid="segment-video-prompt-preview">
                  <div className="row" style={{ justifyContent: "space-between", alignItems: "center" }}>
                    <strong>当前章节视频 Prompt 预览</strong>
                    <span className="pill">
                      {asString(videoPromptPreview.prompt_profile_label, asString(videoPromptPreview.prompt_profile, "未命名档案"))}
                    </span>
                  </div>
                  <p className="muted" style={{ marginTop: 8 }}>
                    Provider/Model：{asString(videoPromptPreview.provider, "-")}/{asString(videoPromptPreview.model, "-")}
                  </p>
                  <details open style={{ marginTop: 8 }}>
                    <summary>章节级 Prompt</summary>
                    <pre style={{ whiteSpace: "pre-wrap", maxHeight: 240, overflow: "auto" }}>
                      {asString(videoPromptPreview.chapter_prompt, "暂无章节级 Prompt")}
                    </pre>
                  </details>
                  <div className="diffList" style={{ marginTop: 10 }}>
                    {videoPromptPreview.shot_prompts.slice(0, 6).map((shot) => (
                      <div key={`${shot.shot_index}-${shot.title}`} className="diffItem" style={{ display: "grid", gap: 6 }}>
                        <strong>镜头 {shot.shot_index} · {shot.title}</strong>
                        {asString(shot.motion_directive, "") ? <span>运镜：{asString(shot.motion_directive)}</span> : null}
                        {asString(shot.reference_image_url, "") ? (
                          <a className="downloadLink subtle" href={resolveDownloadUrl(asString(shot.reference_image_url, ""))}>
                            查看该镜头 reference
                          </a>
                        ) : null}
                        <pre style={{ whiteSpace: "pre-wrap", maxHeight: 180, overflow: "auto", margin: 0 }}>
                          {shot.prompt}
                        </pre>
                      </div>
                    ))}
                  </div>
                  {videoPromptPreview.shot_prompts.length > 6 ? (
                    <p className="muted" style={{ marginBottom: 0 }}>
                      已展示前 6 个镜头 Prompt，其余镜头可后续按需要继续扩展预览。
                    </p>
                  ) : null}
                </div>
              ) : null}
            </div>
          ) : null}
          {selected && chapterScopedSteps.has(selected.step_name) && failedChapterItems.length > 0 ? (
            <div className="card" style={{ padding: 12, marginBottom: 12 }}>
              <strong>当前阶段有 {failedChapterItems.length} 个失败章节</strong>
              <div className="diffList" style={{ marginTop: 10 }}>
                {failedChapterItems.slice(0, 6).map((item) => (
                  <div key={item.id} className="diffItem">
                    {item.title}：{clipText(item.detail || "暂无失败详情", 120)}
                  </div>
                ))}
              </div>
              {failedChapterItems.length > 6 ? (
                <p className="muted" style={{ marginBottom: 0 }}>
                  仅展示前 6 个失败章节，其余可在章节列表中继续查看。
                </p>
              ) : null}
            </div>
          ) : null}
          {selected?.step_name === "consistency_check" &&
          (reviewRequiredChapterItems.length > 0 || reworkRequestedChapterItems.length > 0 || pendingConsistencyChapterItems.length > 0) ? (
            <div className="card" style={{ padding: 12, marginBottom: 12 }}>
              <strong>分镜校核批量处理概览</strong>
              <div className="diffList" style={{ marginTop: 10 }}>
                <div className="diffItem">已完成校核待通过：{reviewRequiredChapterItems.length} 章</div>
                <div className="diffItem">需返工自动修正：{reworkRequestedChapterItems.length} 章</div>
                <div className="diffItem">待重新评分：{pendingConsistencyChapterItems.length} 章</div>
              </div>
              {reworkRequestedChapterItems.length > 0 ? (
                <div className="diffList" style={{ marginTop: 10 }}>
                  {reworkRequestedChapterItems.slice(0, 4).map((item) => (
                    <div key={item.id} className="diffItem">
                      {item.title}：{clipText(item.detail || "将自动根据低分镜头原因补充修正提示词。", 120)}
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          ) : null}
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
              data-testid="approve-current-step-button"
            >
              {busy && pendingAction?.includes("审批通过") ? "审批中..." : "通过"}
            </button>
            {selected && chapterScopedSteps.has(selected.step_name) ? (
              <button
                onClick={() =>
                  selected &&
                  postBatchAction(
                    `/api/v1/projects/${projectId}/steps/${selected.id}/${selected.step_name === "consistency_check" ? "approve-review-required-chapters" : "approve-all-chapters"}`,
                    {
                      scope_type: "chapter",
                      created_by: "ui-reviewer",
                    },
                    selected.step_name === "consistency_check" ? "已批量通过所有已完成校核章节。" : "已批量通过当前阶段。"
                  )
                }
                disabled={busy || !selected}
                data-testid="approve-current-step-all-chapters-button"
              >
                {selected.step_name === "consistency_check"
                  ? `对已完成校核章节通过（${reviewRequiredChapterItems.length}）`
                  : "对当前所有章节通过"}
              </button>
            ) : null}
            {selected && chapterScopedSteps.has(selected.step_name) && failedChapterItems.length > 0 ? (
              <button
                onClick={() =>
                  selected &&
                  postBatchAction(
                    `/api/v1/projects/${projectId}/steps/${selected.id}/approve-failed-chapters`,
                    {
                      scope_type: "chapter",
                      created_by: "ui-reviewer",
                    },
                    "已批量通过失败章节。"
                  )
                }
                disabled={busy || !selected}
                data-testid="approve-current-step-failed-chapters-button"
              >
                对失败章节通过
              </button>
            ) : null}
            {selected?.step_name === "consistency_check" ? (
              <button
                onClick={() =>
                  selected &&
                  postBatchAction(
                    `/api/v1/projects/${projectId}/steps/${selected.id}/rework-regenerate-rescore-chapters`,
                    {
                      scope_type: "chapter",
                      created_by: "ui-reviewer",
                    },
                    "已完成返工章节自动修正、重新出图与重新校核。"
                  )
                }
                disabled={busy || !selected || reworkRequestedChapterItems.length === 0}
                data-testid="consistency-rework-regenerate-rescore-button"
              >
                对返工章节自动修正重跑（{reworkRequestedChapterItems.length}）
              </button>
            ) : null}
            {selected?.step_name === "consistency_check" ? (
              <button
                onClick={() =>
                  selected &&
                  postBatchAction(
                    `/api/v1/projects/${projectId}/steps/${selected.id}/rerun-pending-chapters`,
                    {
                      scope_type: "chapter",
                      created_by: "ui-reviewer",
                    },
                    "已重新对待评分章节执行分镜校核。"
                  )
                }
                disabled={busy || !selected || pendingConsistencyChapterItems.length === 0}
                data-testid="consistency-rerun-pending-chapters-button"
              >
                对待评分章节重新打分（{pendingConsistencyChapterItems.length}）
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
                    borderColor: version.is_active ? "var(--accent)" : undefined,
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
                  <button onClick={() => selectStoryboardVersion(version.id)} disabled={busy || version.is_active}>
                    {version.is_active ? "当前使用" : "选用该版本"}
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
	        </div>
	        <aside className="agentSidebar">
          <AgentPanel
            projectId={projectId}
            projectName={project?.name ?? "项目审核台"}
            projectStatus={project?.status ?? "-"}
            targetDurationSec={project?.target_duration_sec ?? 0}
            selectedStepKey={selected?.step_name ?? null}
            selectedStepLabel={selected?.step_display_name ?? null}
            selectedChapterId={selectedChapter?.id ?? null}
            selectedChapterTitle={selectedChapter?.title ?? null}
            onAgentMutation={() => refreshWorkflowData(selected, true)}
          />
        </aside>
      </div>
    </main>
  );
}
