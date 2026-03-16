# FilmIt Pipeline

本项目当前以 `docs/architecture-v1.1.0.md` 作为技术架构基线：

- `PDF/TXT` 小说输入
- 8 步流水线处理
- 四按钮人工闭环
- 步骤级模型切换与重跑
- macOS 本地部署，浏览器访问工作台

## 目录

```text
apps/
  api/     FastAPI 后端
  web/     Next.js 前端
workers/   Celery worker
libs/      Provider/Workflow/Consistency 共享库
infra/     部署与基础设施配置
docs/      技术文档
```

## 快速启动

1. 复制环境变量：

```bash
cp .env.example .env
```

2. 启动服务：

```bash
docker compose up -d --build
```

如果你所在网络访问 `auth.docker.io` 超时，本项目默认已改为镜像前缀 `docker.m.daocloud.io`。如需切换，可修改 `.env` 中：

```bash
N2V_REGISTRY_MIRROR=...
N2V_PYTHON_BASE_IMAGE=...
N2V_NODE_BASE_IMAGE=...
```

如需在浏览器中强制验证“第 5 步一致性失败后自动回退到第 4 步分镜出图并进行版本比较”的流程，可临时设置：

```bash
N2V_CONSISTENCY_THRESHOLD=101
docker compose up -d --build api web llm-worker media-worker
```

恢复默认行为时改回：

```bash
N2V_CONSISTENCY_THRESHOLD=75
docker compose up -d --build api web llm-worker media-worker
```

3. 访问地址：

- Web: `http://localhost:3000`
- API: `http://localhost:8000`
- API Docs: `http://localhost:8000/docs`

## 安全说明

- 不要提交 `.env`、本地数据库、浏览器缓存或任何包含真实 API key 的文件。
- `README.md`、`.env.example` 与代码示例中只使用占位符，例如 `sk-...` 或 `N2V_OPENROUTER_API_KEY=...`。
- 当前仓库默认忽略 `.env`、`apps/api/generated/`、`.playwright-cli/`、`*.egg-info/` 等本地生成内容。

## Playwright 浏览器测试

后续页面维护、交互回归和新需求验收默认使用 `Playwright`。

首次安装浏览器：

```bash
cd apps/web
npm run playwright:install
```

执行快速冒烟测试：

```bash
cd apps/web
npm run playwright:test
```

可视化执行：

```bash
cd apps/web
npm run playwright:test:headed
```

执行现有的整条工作流自动化脚本：

```bash
cd apps/web
npm run playwright:demo-workflow -- --headed --stop-after-step chapter_chunking
```

说明：

- Playwright 默认访问 `http://127.0.0.1:3000`
- API 默认访问 `http://127.0.0.1:8000`
- 产物输出到 `output/playwright/`

## 文件落盘

当前所有项目相关文件默认统一落盘到：

- `/Users/wyj/proj/novel-to-video-demo-cases`

目录结构按“项目 + 文件类别”组织，例如：

- `sources/` 原始 `PDF/TXT`
- `texts/` 全文解析结果
- `chapters/` 章节切分结果
- `scripts/` 章节剧本
- `shots/` 分镜细化结果
- `storyboards/` 分镜图
- `videos/` 章节视频片段
- `audio/` 配音与音频相关产物
- `exports/` 最终成片

最终导出的 `output_key` 现在返回真实的绝对路径，而不是逻辑占位键。

## 当前实现说明

当前已实现内容：

1. 核心数据库模型（项目、步骤、镜头、素材、审计、版本）
2. 流程状态机（`PENDING -> GENERATING -> REVIEW_REQUIRED -> APPROVED`）
3. 四按钮人工闭环 API
4. 可切换模型绑定与重跑
5. 一致性评分引擎（基础版本）
6. 最小 Web 工作台（项目创建、步骤审核、动作触发）
7. 分镜版本回滚、版本选用、卡片化版本对比
8. 分镜缩略图预览与本地生成资产访问路由
9. OpenAI 首批真实模型接入准备（`gpt-5/gpt-image-1.5/gpt-4o-mini-tts/sora-2`）
10. `Story Bible` 风格预设与自定义风格注入
11. `segment_video` 任务轮询与本地视频片段落盘
12. 八步中文显示名与每步提示词模板预设

后续建议按 `docs/architecture-v1.1.0.md` 继续扩展 Story Bible 资产拆分、真实图生视频与高级时间线编辑。

## 真实模型测试

当前已优先接入 `OpenAI` 首批真实 provider 能力：

- 文本步骤：`chunk/script/shot_detail/consistency`
- 图片步骤：`storyboard_image`
- 配音步骤：`stitch_subtitle_tts`
- 视频步骤：`segment_video` 已接入创建接口，但完整异步轮询与拼接仍待补完

启用方式：

```bash
cp .env.example .env
```

在 `.env` 中设置：

```bash
N2V_OPENAI_API_KEY=sk-...
```

然后重建：

```bash
docker compose up -d --build api web
```

说明：

- 若未配置 `N2V_OPENAI_API_KEY`，系统会自动回退到 mock adapter。
- 分镜图、视频片段、章节脚本等都会写入 `/Users/wyj/proj/novel-to-video-demo-cases`，并通过 `/api/v1/local-files/...` 提供浏览器预览。
- 视频步骤当前已实现“创建任务 -> 轮询状态 -> 下载片段”的主干闭环，轮询参数可通过以下环境变量调整：

```bash
N2V_VIDEO_POLL_INTERVAL_SEC=8
N2V_VIDEO_POLL_MAX_ATTEMPTS=15
```

## OpenRouter 统一接口

当前已新增 `openrouter` provider，接入 `chat/completions` 统一文本接口。

适用范围：

- `chunk`
- `script`
- `shot_detail`
- `consistency`

也可用于 `image/video/tts` 的提示词包生成，但这些步骤如果只走 `openrouter`，当前产物将是“提示词/结构化文本”，不会直接生成真实二进制媒体。

环境变量：

```bash
N2V_OPENROUTER_API_KEY=...
N2V_OPENROUTER_API_URL=https://openrouter.ai/api/v1/chat/completions
N2V_OPENROUTER_SITE_URL=http://localhost:3000
N2V_OPENROUTER_APP_NAME=FilmIt Pipeline
N2V_OPENROUTER_TIMEOUT_SEC=180
```

默认策略：

- 文本推理与提示词阶段优先建议 `openrouter`
- 真实图片/视频/音频生成仍优先建议专用媒体 provider

## 风格预设

当前内置可选风格：

- `电影质感`
- `赛博朋克`
- `哥特式`
- `阴郁黑色`
- `Q版`
- `写实`
- `平面插画`
- `三维风格化`
- `动画番剧`
- `水墨`

支持用户在创建项目或项目页中继续补充：

- 自定义风格名
- 自定义风格约束

这些内容会被标准化写入 `style_profile.story_bible`，并自动注入剧本、分镜图、视频生成等步骤的提示词。

## 提示词模板

当前八个步骤均已内置多组提示词模板，可在项目审核页直接选择并套用：

- 导入全文
- 切分章节
- 章节剧本
- 分镜细化
- 分镜出图
- 分镜校核
- 视频片段
- 成片输出

模板会同时覆盖：

- 系统提示词
- 任务提示词

## 本地演示

如需用本机准备好的 `1408` 文本跑一次最小演示：

1. 将演示输入放到 `demo_data/night_shift_demo/source_story.txt`
2. 可直接在 Web 首页点击“一键导入 1408 Demo”
3. 或运行：

```bash
python3 scripts/run_demo_1408.py
```

脚本会：

- 创建一个本地演示项目
- 上传 `1408` 文本
- 运行首个步骤 `ingest_parse`
- 将结果写入 `demo_runs/1408/<timestamp>/summary.json`
