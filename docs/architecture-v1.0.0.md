# FilmIt Pipeline 技术架构文档 v1.0.0

- 文档版本: `1.0.0`
- 生效日期: `2026-03-01`
- 适用范围: `macOS 本地部署 + 浏览器访问（Chrome/Safari）`
- 目标产物: 从 `PDF/TXT 小说` 自动生成 `可指定时长` 的成片视频，支持人工闭环和多模型自由组合。

## 1. 目标与设计原则

### 1.1 核心目标

1. 输入小说文件（`pdf` / `txt`），输出指定总时长的视频（`mp4`）。
2. 覆盖全流程：分段、剧情抽取、剧本分镜、文生图、图生视频、拼接、字幕、配音。
3. 每个步骤都支持人工干预，形成可审可改可回滚的闭环。
4. 每一步可切换不同 AI 模型组合，不绑定单一供应商。
5. 在 macOS 本地一键运行，通过浏览器进行配置、执行和审核。

### 1.2 关键设计原则

1. 一致性优先：角色、场景、动作、时间线在跨镜头和跨段落中保持连贯。
2. 人机协同：自动生成为默认路径，人工审核为强制关口。
3. 可插拔模型：统一适配层屏蔽不同厂商 API 差异。
4. 可追溯：每次生成、重跑、人工编辑都保留版本与差异。
5. 可运维：任务状态、失败原因、成本和时延可观测。

## 2. 业务流程与人工闭环

## 2.1 标准流程（8 步）

1. 小说导入与解析（PDF/TXT）。
2. 章节切分与上下文压缩。
3. 冲突场景/情节转折识别，生成剧本和分镜。
4. 分镜细化（人物形象、场景、动作、对白）。
5. 分镜文生图。
6. 一致性检测与回炉修复。
7. 图/文生视频，按分镜生成段落视频。
8. 段落拼接、字幕、配音、导出目标时长成片。

## 2.2 每步统一审核动作

每一步 UI 固定提供以下四个动作：

1. `通过`
2. `编辑后继续`
3. `修改提示词或设定后重新生成`
4. `切换模型重跑`

### 2.3 审核动作约束

1. 作用域：`当前镜头`、`当前章节`、`当前步骤全部待处理项`。
2. 提示词分层：
   - `系统基线提示词（锁定）`
   - `任务提示词（可编辑）`
3. 可编辑设定：模型参数（如 `seed`、风格、时长、运动强度、阈值、音色等）。
4. 每次重跑写入版本快照：`prompt diff + params diff + 结果版本`。

## 3. 系统架构

## 3.1 逻辑组件

1. Web 前端（Next.js）
   - 项目管理、模型配置、步骤审核、时间线编辑、导出。
2. API 服务（FastAPI）
   - 提供 REST API、权限校验、任务提交、状态查询。
3. 工作流编排器（Orchestrator）
   - 负责 DAG 步骤调度、状态流转、重试与依赖控制。
4. Worker 集群（Celery Workers）
   - 执行 LLM、图像、视频、TTS、FFmpeg 等耗时任务。
5. 模型适配层（Provider Adapters）
   - 统一调用主流模型 API，屏蔽请求/响应差异。
6. 一致性引擎（Consistency Engine）
   - 人物/场景/动作连续性评分，输出修复建议。
7. 存储层
   - PostgreSQL（元数据）
   - MinIO（素材与中间产物）
   - Redis（队列与缓存）

## 3.2 部署拓扑（本地）

```mermaid
flowchart LR
UI["Browser (Chrome/Safari)"] --> FE["Next.js Web"]
FE --> API["FastAPI API"]
API --> ORCH["Workflow Orchestrator"]
ORCH --> Q["Redis Queue"]
Q --> W1["Worker: LLM/Image/Video"]
Q --> W2["Worker: Consistency/TTS/FFmpeg"]
API --> DB["PostgreSQL"]
W1 --> OBJ["MinIO Object Storage"]
W2 --> OBJ
API --> OBJ
```

## 3.3 技术栈（v1.0.0）

1. 前端：`Next.js 15 + TypeScript + Tailwind + Zustand`
2. 后端：`FastAPI + Pydantic + SQLAlchemy`
3. 队列：`Celery + Redis`
4. 数据库：`PostgreSQL 16`
5. 对象存储：`MinIO`
6. 媒体处理：`FFmpeg`
7. 部署：`Docker Compose`（macOS 本地）

## 4. 工作流与状态机

## 4.1 顶层状态

`DRAFT -> RUNNING -> REVIEW_REQUIRED -> APPROVED -> RENDERING -> COMPLETED`

失败分支：`RUNNING/RENDERING -> FAILED`（可重试、可回滚、可切模型）

## 4.2 步骤状态

`PENDING -> GENERATING -> REVIEW_REQUIRED -> APPROVED`

异常/修复分支：

- `REVIEW_REQUIRED -> REWORK_REQUESTED -> GENERATING`
- `GENERATING -> FAILED -> RETRYING | BLOCKED`

## 4.3 人工闭环门禁

每个步骤默认必须进入 `REVIEW_REQUIRED`，只有点击 `通过` 才可推进到下一步；支持配置“低风险步骤自动通过”，但 v1.0.0 默认关闭。

## 5. 一致性设计（重点）

## 5.1 Story Bible（故事圣经）

项目创建时生成并持续维护：

1. 角色卡：姓名、外貌锚点、服装道具、语气、禁忌。
2. 场景卡：地点、风格、时代、天气/光照、不可变元素。
3. 时间线：章节时间顺序、角色状态迁移。
4. 视觉基线：镜头语言、色彩基调、构图偏好。

## 5.2 Shot Graph（镜头图）

每个镜头记录：

1. 输入依赖：上一个镜头的角色状态和场景状态。
2. 目标状态：本镜头人物动作、情绪、关键道具。
3. 连续性约束：服装一致、空间一致、动作可达。

## 5.3 自动检测维度

1. 角色一致性：脸部特征、发型、服装、体态。
2. 场景一致性：背景结构、光照方向、主色调。
3. 动作一致性：动作起止合理、跨镜头衔接自然。
4. 叙事一致性：对白与剧情阶段不冲突。

## 5.4 评分与回炉

1. 每镜头输出一致性分数：`0~100`。
2. 阈值默认 `75`，低于阈值自动打回 `REWORK_REQUESTED`。
3. 回炉策略优先级：
   - 优先局部重生成（单镜头）
   - 再章节级重生成
   - 最后步骤级重跑

## 6. 多模型适配架构（重点）

## 6.1 统一 Provider 接口

```ts
export type StepType =
  | "chunk"
  | "script"
  | "shot_detail"
  | "image"
  | "consistency"
  | "video"
  | "subtitle"
  | "tts";

export interface ProviderRequest {
  step: StepType;
  model: string;
  input: Record<string, unknown>;
  prompt?: string;
  params?: Record<string, unknown>;
}

export interface ProviderResponse {
  output: Record<string, unknown>;
  usage?: { inputTokens?: number; outputTokens?: number; seconds?: number };
  raw?: unknown;
}

export interface ProviderAdapter {
  name(): string;
  supports(step: StepType, model: string): boolean;
  invoke(req: ProviderRequest): Promise<ProviderResponse>;
  estimateCost?(req: ProviderRequest): Promise<number>;
  healthCheck(): Promise<boolean>;
}
```

## 6.2 步骤到模型池映射（示例）

```yaml
step_model_pool:
  chunk:
    - gemini-2.5-flash-lite
    - deepseek-chat
  script:
    - claude-sonnet-4
    - gpt-5
    - gemini-2.5-pro
  image:
    - imagen-4
    - gpt-image-1
    - runway-gen4-image
  video:
    - veo-3.1
    - sora
    - runway-gen4-turbo
  tts:
    - gpt-4o-mini-tts
    - elevenlabs-multilingual-v2
    - azure-neural-tts
```

## 6.3 参数归一化

统一参数字典，适配层内部做映射：

1. 文本模型：`temperature`、`top_p`、`max_tokens`
2. 图像模型：`seed`、`style`、`aspect_ratio`、`negative_prompt`
3. 视频模型：`duration_sec`、`fps`、`camera_motion`、`strength`
4. 语音模型：`voice_id`、`speed`、`emotion`、`sample_rate`

## 7. 数据模型（PostgreSQL）

## 7.1 核心表

1. `projects`
   - 基本信息、目标时长、输入输出路径、全局风格。
2. `source_documents`
   - 原始文件、解析状态、页码映射。
3. `chapter_chunks`
   - 分段文本、边界信息、上下文重叠区。
4. `story_beats`
   - 冲突、转折、剧情节点。
5. `shots`
   - 分镜定义、镜头顺序、时长预算、角色场景引用。
6. `assets`
   - 中间产物索引（图像、视频、音频、字幕）。
7. `pipeline_steps`
   - 步骤执行状态、耗时、成本、错误信息。
8. `review_actions`
   - 人工操作日志（四类按钮动作 + 备注）。
9. `prompt_versions`
   - 系统基线与任务提示词版本、diff、回滚标记。
10. `model_runs`
    - 单次模型调用请求摘要、响应摘要、usage、费用估算。

## 7.2 建议字段（节选）

`pipeline_steps`：

- `id`、`project_id`、`step_name`、`status`
- `input_ref`、`output_ref`
- `model_provider`、`model_name`
- `attempt`、`started_at`、`finished_at`
- `error_code`、`error_message`

`review_actions`：

- `id`、`project_id`、`step_id`、`scope_type`
- `action_type`（`approve`/`edit_continue`/`edit_prompt_regen`/`switch_model_rerun`）
- `editor_payload`（JSON）
- `created_by`、`created_at`

`prompt_versions`：

- `id`、`project_id`、`step_name`
- `system_prompt`、`task_prompt`
- `parent_version_id`、`diff_patch`
- `is_active`、`created_at`

## 8. API 设计（FastAPI）

## 8.1 项目与配置

1. `POST /api/v1/projects`
2. `GET /api/v1/projects/{project_id}`
3. `PATCH /api/v1/projects/{project_id}`
4. `POST /api/v1/projects/{project_id}/model-bindings`
5. `GET /api/v1/providers/models`

## 8.2 任务执行

1. `POST /api/v1/projects/{project_id}/run`
2. `POST /api/v1/projects/{project_id}/steps/{step_name}/run`
3. `GET /api/v1/projects/{project_id}/steps`
4. `GET /api/v1/projects/{project_id}/timeline`

## 8.3 人工闭环

1. `POST /api/v1/projects/{project_id}/steps/{step_id}/approve`
2. `POST /api/v1/projects/{project_id}/steps/{step_id}/edit-continue`
3. `POST /api/v1/projects/{project_id}/steps/{step_id}/edit-prompt-regenerate`
4. `POST /api/v1/projects/{project_id}/steps/{step_id}/switch-model-rerun`

## 8.4 产物与导出

1. `GET /api/v1/projects/{project_id}/assets`
2. `POST /api/v1/projects/{project_id}/render/final`
3. `GET /api/v1/projects/{project_id}/exports/{export_id}`

## 9. 前端信息架构

## 9.1 页面结构

1. 项目列表页
2. 项目工作台
3. 模型配置页
4. 步骤审核页
5. 时间线编辑页
6. 导出页

## 9.2 步骤审核页关键组件

1. 左侧：步骤队列和状态标签。
2. 中央：当前步骤产物预览（文本/图像/视频）。
3. 右侧：可编辑提示词和参数面板。
4. 底部：四个统一动作按钮。
5. 版本对比弹窗：A/B 结果与 diff 对照。

## 10. 媒体处理规范

## 10.1 时间预算

1. 目标总时长 `T`。
2. 根据剧情权重将 `T` 分配到章节和镜头。
3. 镜头最小时长建议 `2.5s`，最大建议 `12s`。

## 10.2 编码参数（默认）

1. 分辨率：`1920x1080`
2. 帧率：`24 fps`
3. 编码：`H.264 + AAC`
4. 容器：`mp4`

## 10.3 字幕与配音

1. 字幕格式：`srt`（同时可导出 `vtt`）。
2. 字幕来源：对白脚本 + 强制时间轴对齐。
3. 配音策略：旁白轨、角色轨分离后混音。

## 11. 错误处理与重试策略

1. 网络错误：指数退避重试（`1s/3s/9s`）。
2. 限流错误：降速 + 排队，必要时切换备用模型。
3. 结果不合规：进入 `REWORK_REQUESTED`，不自动推进。
4. 媒体处理失败：保留中间产物，支持从失败节点续跑。

## 12. 安全与密钥管理

1. API Key 加密存储（建议 `AES-GCM`）。
2. 密钥只在后端使用，前端永不直连第三方模型。
3. 敏感日志脱敏（key、用户隐私文本）。
4. 本地部署默认单用户，保留扩展到多用户 RBAC 的接口字段。

## 13. 可观测性

1. 指标：
   - 每步耗时、成功率、平均重跑次数、人工介入率。
2. 成本：
   - 按 `project/step/model` 统计 token、秒数、估算费用。
3. 质量：
   - 一致性评分趋势、打回率、最终导出通过率。
4. 日志：
   - 请求链路 ID + 步骤 ID + 版本 ID 可追踪。

## 14. v1.0.0 交付边界

### 14.1 In Scope

1. PDF/TXT 导入与解析。
2. 8 步全流程可运行。
3. 四按钮人工闭环。
4. 多模型切换与重跑。
5. 本地浏览器管理界面。

### 14.2 Out of Scope

1. 云端多租户 SaaS。
2. 实时协作编辑。
3. 移动端适配优化。

## 15. 推荐目录结构（仓库）

```text
filmit-pipeline/
  apps/
    web/                # Next.js 前端
    api/                # FastAPI 后端
  workers/
    llm_worker/
    media_worker/
  libs/
    provider_adapters/
    workflow_engine/
    consistency_engine/
    prompt_templates/
  infra/
    docker/
    compose/
  docs/
    architecture-v1.0.0.md
```

## 16. 验收标准（v1.0.0）

1. 给定任意合法 `pdf/txt`，可生成完整视频文件。
2. 任意步骤可执行四按钮动作，并正确记录审计日志。
3. 任意步骤可切换至少 2 个不同模型并成功重跑。
4. 一致性评分低于阈值的镜头会被自动拦截，不允许直接导出。
5. 在 macOS 本地通过浏览器完成项目创建、执行、审核、导出的全流程。

---

本版本文档为工程落地基线；后续如进入 `v1.1.x`，建议优先扩展“自动镜头节奏优化”和“基于参考图的角色锁定增强”。
