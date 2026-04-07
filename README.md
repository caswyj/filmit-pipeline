# FilmIt Pipeline

FilmIt Pipeline 是一套给小说影视化和 AI 漫剧制作使用的工作流系统。它把 `PDF/TXT -> 切章 -> 剧本 -> 分镜 -> 出图 -> 视频 -> 配音/字幕 -> 导出` 拆成 8 个可审阅、可回滚、可换模型的步骤，而不是一次性丢给模型跑完。

这个项目真正有价值的地方不是“接了多少模型”，而是把生产流程拆开了：

- 每一步都能看输入、看输出、单独重跑，不是黑盒。
- 可以人工审核、改 prompt、换 provider，只修出问题的那一步。
- 图片、视频、字幕、音频在同一个工作台里，不是几段零散脚本。
- 中间产物全部落盘，方便继续精修、回滚和复用。

## 看效果

《辛巴达航海》第 4 章分镜示例：

![辛巴达航海分镜示例](docs/assets/sinbad-storyboard-contact-sheet.png)

真实视频片段示例：

- [辛巴达航海 · 第 4 章单镜头 Demo（MP4）](docs/assets/sinbad-shot-demo.mp4)

## 最短启动命令

如果你只想先把项目跑起来，直接执行：

```bash
make up
```

如果你不想用 `make`：

```bash
./scripts/bootstrap.sh --up
```

这个脚本会做三件事：

- 没有 `.env` 时，从 `.env.example` 自动创建
- 自动生成本地 `N2V_GENERATED_DIR`
- 自动创建输出目录，然后启动 `docker compose`

默认生成目录是仓库内的 `output/generated`，不会再写死作者机器路径。

启动后访问：

- Web: `http://localhost:3000`
- API: `http://localhost:8000`
- API Docs: `http://localhost:8000/docs`

## 两条使用路径

### 路径 A：本地开发 / 流程调试

适合你现在想做这些事：

- 先看界面和流程是否顺
- 先做前端、审核流、状态机开发
- 不想一开始就烧 API 费用

做法：

```bash
./scripts/bootstrap.sh
docker compose up -d --build
```

然后保持这些 key 为空：

- `N2V_OPENAI_API_KEY`
- `N2V_OPENROUTER_API_KEY`
- `N2V_VOLCENGINE_LAS_API_KEY`

这时系统会尽量回退到 mock provider。你依然可以验证：

- 新建项目
- 导入 `TXT` / `PDF`
- 跑文本链路
- 改 prompt
- 切模型
- 单步重跑
- 审核和回滚

如果你的目标是开发工作台，而不是测试真实媒体质量，这条路径就够了。

### 路径 B：真实模型接入

适合你现在想测真实图片、视频和配音。

先初始化：

```bash
./scripts/bootstrap.sh
```

然后编辑 `.env`，按你的目标加 key：

- 只想先把文本链路跑顺：`N2V_OPENROUTER_API_KEY`
- 想测图片、视频、TTS：`N2V_OPENAI_API_KEY`
- 想测 `Seedance` 视频链路：`N2V_VOLCENGINE_LAS_API_KEY`

再启动：

```bash
docker compose up -d --build
```

当前代码里已经接入这些方向：

- 文本推理：`OpenRouter`、`OpenAI`
- 图片生成：`OpenAI`
- 视频生成：`OpenAI / Sora`、`Volcengine / Seedance`
- 配音：`OpenAI TTS`、`Edge TTS`

## 第一次进入后建议怎么验

推荐按这个顺序：

1. 先创建一个空项目，确认前后端都正常。
2. 导入一个本地 `TXT` 或 `PDF`，先看切章和剧本链路。
3. 如果配了真实 key，再去跑图片或视频步骤。
4. 在项目页试一次“改 prompt / 切模型 / 只重跑某一步”。

第 4 步就是这个项目和普通生成 demo 的差别所在。

## 常用命令

```bash
make bootstrap
make up
make down
```

不用 `make` 也可以：

```bash
./scripts/bootstrap.sh
./scripts/bootstrap.sh --up
docker compose down
```

浏览器回归测试：

```bash
cd apps/web
npm run playwright:install
npm run playwright:test
```

## 项目结构

```text
apps/
  api/     FastAPI 后端
  web/     Next.js 前端
workers/   Celery worker
libs/      Provider / Workflow / Consistency 共享库
docs/      架构和演示材料
```

所有生成文件都会落到 `N2V_GENERATED_DIR`，常见目录包括：

- `sources/`
- `chapters/`
- `scripts/`
- `storyboards/`
- `videos/`
- `audio/`
- `exports/`

FilmIt 的目标不是“一次生成结束”，而是把每一步产物保留下来，方便继续修。

## 常见问题

`docker compose` 起不来：

- 先检查 `.env` 里的 `N2V_GENERATED_DIR` 是否存在
- 再检查这个目录是否有写权限
- 如果拉镜像很慢，再调整 `N2V_REGISTRY_MIRROR`

页面能打开，但媒体预览失败：

- 大多数情况是 `N2V_GENERATED_DIR` 没设对
- 现在默认会走 `output/generated`，并通过脚本自动创建

只想做前端或流程开发，不想真调模型：

- 把 provider key 留空即可
- 先用 mock 跑通界面、状态流转和审核操作

## 相关文档

- 架构基线：[docs/architecture-v1.1.0.md](docs/architecture-v1.1.0.md)
- Agent 化规划：[docs/filmit-agentization-implementation-plan.md](docs/filmit-agentization-implementation-plan.md)
