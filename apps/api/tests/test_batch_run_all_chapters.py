from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.helpers import fresh_app


def _prepare_project(client: TestClient, *, name: str, source_text: str) -> str:
    project_id = client.post("/api/v1/projects", json={"name": name, "target_duration_sec": 120}).json()["id"]
    client.post(
        f"/api/v1/projects/{project_id}/source-documents",
        files={"file": ("novel.txt", source_text.encode("utf-8"), "text/plain")},
    )

    ingest = client.post(f"/api/v1/projects/{project_id}/steps/ingest_parse/run", json={"force": True})
    assert ingest.status_code == 200
    approve_ingest = client.post(
        f"/api/v1/projects/{project_id}/steps/{ingest.json()['id']}/approve",
        json={"scope_type": "step", "created_by": "tester"},
    )
    assert approve_ingest.status_code == 200

    chunk = client.post(f"/api/v1/projects/{project_id}/steps/chapter_chunking/run", json={"force": True})
    assert chunk.status_code == 200
    approve_chunk = client.post(
        f"/api/v1/projects/{project_id}/steps/{chunk.json()['id']}/approve",
        json={"scope_type": "step", "created_by": "tester"},
    )
    assert approve_chunk.status_code == 200
    return project_id


def test_run_current_step_for_all_chapters() -> None:
    db_path = Path("./test_n2v_batch.db")
    if db_path.exists():
        db_path.unlink()

    app = fresh_app(database_url="sqlite:///./test_n2v_batch.db")
    source_text = (
        "一\n\n路易斯走进小镇，贾德在门口迎接他。墓地后方的树林始终阴沉。\n\n"
        "二\n\n瑞秋带着艾丽回家，公路另一侧的墓地让所有人都感到不安。"
    )

    with TestClient(app) as client:
        project_id = _prepare_project(client, name="batch-demo", source_text=source_text)
        batch = client.post(
            f"/api/v1/projects/{project_id}/steps/story_scripting/run-all-chapters",
            json={"force": True},
        )
        assert batch.status_code == 200
        payload = batch.json()
        assert payload["step_name"] == "story_scripting"
        assert payload["succeeded"] >= 2
        assert payload["failed"] == 0

    if db_path.exists():
        db_path.unlink()


def test_meta_chapters_only_skip_consistency_stage() -> None:
    db_path = Path("./test_n2v_batch_meta.db")
    if db_path.exists():
        db_path.unlink()

    app = fresh_app(database_url="sqlite:///./test_n2v_batch_meta.db")
    source_text = (
        "这是一本小说的版权页和前言内容，用来介绍作者、题记和出版信息。"
        "这些内容不应该进入真正的分镜出图和后续视觉阶段，否则只会浪费时间和额度。"
        "这里继续补充一些说明文字，确保它会被识别为前置内容。\n\n"
        "一\n\n路易斯走进小镇，贾德在门口迎接他。\n\n"
        "二\n\n瑞秋带着艾丽回家，公路另一侧的墓地让所有人都感到不安。"
    )

    with TestClient(app) as client:
        project_id = _prepare_project(client, name="batch-meta-demo", source_text=source_text)

        story = client.post(
            f"/api/v1/projects/{project_id}/steps/story_scripting/run-all-chapters",
            json={"force": True},
        )
        assert story.status_code == 200
        steps = client.get(f"/api/v1/projects/{project_id}/steps").json()
        story_step = next(item for item in steps if item["step_name"] == "story_scripting")
        approve_story = client.post(
            f"/api/v1/projects/{project_id}/steps/{story_step['id']}/approve-all-chapters",
            json={"scope_type": "chapter", "created_by": "tester"},
        )
        assert approve_story.status_code == 200

        batch = client.post(
            f"/api/v1/projects/{project_id}/steps/shot_detailing/run-all-chapters",
            json={"force": True},
        )
        assert batch.status_code == 200
        payload = batch.json()
        assert payload["step_name"] == "shot_detailing"
        assert payload["succeeded"] >= 3
        assert payload["skipped"] == 0

        shot_step = next(item for item in client.get(f"/api/v1/projects/{project_id}/steps").json() if item["step_name"] == "shot_detailing")
        approve_shot = client.post(
            f"/api/v1/projects/{project_id}/steps/{shot_step['id']}/approve-all-chapters",
            json={"scope_type": "chapter", "created_by": "tester"},
        )
        assert approve_shot.status_code == 200

        storyboard = client.post(
            f"/api/v1/projects/{project_id}/steps/storyboard_image/run-all-chapters",
            json={"force": True},
        )
        assert storyboard.status_code == 200
        storyboard_payload = storyboard.json()
        assert storyboard_payload["succeeded"] >= 3

        storyboard_step = next(item for item in client.get(f"/api/v1/projects/{project_id}/steps").json() if item["step_name"] == "storyboard_image")
        approve_storyboard = client.post(
            f"/api/v1/projects/{project_id}/steps/{storyboard_step['id']}/approve-all-chapters",
            json={"scope_type": "chapter", "created_by": "tester"},
        )
        assert approve_storyboard.status_code == 200

        consistency = client.post(
            f"/api/v1/projects/{project_id}/steps/consistency_check/run-all-chapters",
            json={"force": True},
        )
        assert consistency.status_code == 200
        consistency_payload = consistency.json()
        assert consistency_payload["skipped"] >= 1
        assert any("不参与" in item["detail"] or "前置/附录元内容" in item["detail"] for item in consistency_payload["chapter_results"])

        chapters = client.get(f"/api/v1/projects/{project_id}/chapters")
        assert chapters.status_code == 200
        meta_chapter = chapters.json()[0]
        assert meta_chapter["stage_map"]["consistency_check"] == "APPROVED"

    if db_path.exists():
        db_path.unlink()


@pytest.mark.usefixtures("monkeypatch")
def test_run_all_chapters_stops_early_on_fatal_provider_error(monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = Path("./test_n2v_batch_fatal.db")
    if db_path.exists():
        db_path.unlink()

    app = fresh_app(database_url="sqlite:///./test_n2v_batch_fatal.db")
    source_text = (
        "一\n\n路易斯走进小镇，贾德在门口迎接他。\n\n"
        "二\n\n瑞秋带着艾丽回家，公路另一侧的墓地让所有人都感到不安。\n\n"
        "三\n\n夜里卡车灯光穿过窗帘，墓地后的树林变得更黑。"
    )

    from app.services.pipeline_service import PipelineService

    calls = {"count": 0}
    original = PipelineService.run_specific_step

    async def fake_run_specific_step(self, project, step_name, force=False, params=None):
        if step_name != "story_scripting":
            return await original(self, project, step_name, force=force, params=params)
        params = params or {}
        chapter = self._resolve_target_chapter(project.id, step_name, params.get("chapter_id"), force=True)
        step = next(item for item in self._list_steps(project.id) if item.step_name == step_name)
        calls["count"] += 1
        if calls["count"] == 1:
            self._set_chapter_stage_state(
                chapter,
                step_name,
                status="REVIEW_REQUIRED",
                output={"artifact": {"summary": "ok"}},
                attempt=1,
                provider="openrouter",
                model="google/gemini-2.5-flash-image",
            )
            self.db.add(step)
            self.db.commit()
            return step
        raise RuntimeError('402 from OpenRouter: {"error":{"message":"Insufficient credits","code":402}}')

    monkeypatch.setattr(PipelineService, "run_specific_step", fake_run_specific_step)

    with TestClient(app) as client:
        project_id = _prepare_project(client, name="batch-fatal-demo", source_text=source_text)
        batch = client.post(
            f"/api/v1/projects/{project_id}/steps/story_scripting/run-all-chapters",
            json={"force": True},
        )
        assert batch.status_code == 200
        payload = batch.json()
        assert calls["count"] == 2
        assert payload["succeeded"] == 1
        assert payload["failed"] == 1
        assert payload["skipped"] == 1
        assert any("批量运行已中止" in item["detail"] for item in payload["chapter_results"])

    if db_path.exists():
        db_path.unlink()


def test_run_failed_chapters_only() -> None:
    db_path = Path("./test_n2v_batch_failed_only.db")
    if db_path.exists():
        db_path.unlink()

    app = fresh_app(database_url="sqlite:///./test_n2v_batch_failed_only.db")
    source_text = (
        "一\n\n路易斯走进小镇。\n\n"
        "二\n\n瑞秋带着艾丽回家。\n\n"
        "三\n\n夜里卡车灯光穿过窗帘。"
    )

    with TestClient(app) as client:
        project_id = _prepare_project(client, name="batch-failed-only-demo", source_text=source_text)

        from app.db.models import ChapterChunk, PipelineStep, Project
        from app.db.session import SessionLocal
        from app.services.pipeline_service import PipelineService

        with SessionLocal() as db:
            svc = PipelineService(db)
            project = db.scalar(select(Project).where(Project.id == project_id))
            step = db.scalar(select(PipelineStep).where(PipelineStep.project_id == project_id, PipelineStep.step_name == "story_scripting"))
            chapters = list(
                db.scalars(
                    select(ChapterChunk)
                    .where(ChapterChunk.project_id == project_id)
                    .order_by(ChapterChunk.chapter_index.asc(), ChapterChunk.chunk_index.asc())
                ).all()
            )
            assert project is not None
            assert step is not None
            chapter_count = len(chapters)
            for chapter in chapters[:2]:
                svc._set_chapter_stage_state(
                    chapter,
                    "story_scripting",
                    status="FAILED",
                    output={"error_message": "mock fail"},
                    attempt=1,
                    provider="openrouter",
                    model="openai/gpt-5-mini",
                )
            svc._sync_global_chapter_scoped_step(project, step)

        batch = client.post(
            f"/api/v1/projects/{project_id}/steps/story_scripting/run-failed-chapters",
            json={"force": True},
        )
        assert batch.status_code == 200
        payload = batch.json()
        assert payload["succeeded"] == 2
        assert payload["failed"] == 0
        assert payload["skipped"] == max(chapter_count - 2, 0)
        if chapter_count > 2:
            assert any("不是失败章节" in item["detail"] for item in payload["chapter_results"])

        chapters = client.get(f"/api/v1/projects/{project_id}/chapters").json()
        statuses = [chapter["stage_map"]["story_scripting"] for chapter in chapters]
        assert statuses.count("REVIEW_REQUIRED") == 2
        assert statuses.count("PENDING") == max(chapter_count - 2, 0)

    if db_path.exists():
        db_path.unlink()


def test_approve_failed_chapters_only() -> None:
    db_path = Path("./test_n2v_batch_approve_failed.db")
    if db_path.exists():
        db_path.unlink()

    app = fresh_app(database_url="sqlite:///./test_n2v_batch_approve_failed.db")
    source_text = (
        "一\n\n路易斯走进小镇。\n\n"
        "二\n\n瑞秋带着艾丽回家。\n\n"
        "三\n\n夜里卡车灯光穿过窗帘。"
    )

    with TestClient(app) as client:
        project_id = _prepare_project(client, name="batch-approve-failed-demo", source_text=source_text)

        from app.db.models import ChapterChunk, PipelineStep, Project
        from app.db.session import SessionLocal
        from app.services.pipeline_service import PipelineService

        with SessionLocal() as db:
            svc = PipelineService(db)
            project = db.scalar(select(Project).where(Project.id == project_id))
            step = db.scalar(select(PipelineStep).where(PipelineStep.project_id == project_id, PipelineStep.step_name == "story_scripting"))
            chapters = list(
                db.scalars(
                    select(ChapterChunk)
                    .where(ChapterChunk.project_id == project_id)
                    .order_by(ChapterChunk.chapter_index.asc(), ChapterChunk.chunk_index.asc())
                ).all()
            )
            assert project is not None
            assert step is not None
            chapter_count = len(chapters)
            for chapter in chapters[:2]:
                svc._set_chapter_stage_state(
                    chapter,
                    "story_scripting",
                    status="FAILED",
                    output={"error_message": "mock fail"},
                    attempt=1,
                    provider="openrouter",
                    model="openai/gpt-5-mini",
                )
            svc._sync_global_chapter_scoped_step(project, step)
            step_id = step.id

        batch = client.post(
            f"/api/v1/projects/{project_id}/steps/{step_id}/approve-failed-chapters",
            json={"scope_type": "chapter", "created_by": "tester"},
        )
        assert batch.status_code == 200
        payload = batch.json()
        assert payload["succeeded"] == 2
        assert payload["failed"] == 0
        assert payload["skipped"] == max(chapter_count - 2, 0)
        if chapter_count > 2:
            assert any("不是失败章节" in item["detail"] for item in payload["chapter_results"])

        chapters = client.get(f"/api/v1/projects/{project_id}/chapters").json()
        statuses = [chapter["stage_map"]["story_scripting"] for chapter in chapters]
        assert statuses.count("APPROVED") == 2
        assert statuses.count("PENDING") == max(chapter_count - 2, 0)

    if db_path.exists():
        db_path.unlink()
