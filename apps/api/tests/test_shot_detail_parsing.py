from __future__ import annotations

from types import SimpleNamespace

from tests.helpers import fresh_app


def test_parse_shot_detail_text_prefers_structured_model_output(tmp_path) -> None:
    fresh_app(database_url=f"sqlite:///{tmp_path / 'test_shot_detail_parser.db'}")

    from app.services.pipeline_service import PipelineService

    service = PipelineService.__new__(PipelineService)
    project = SimpleNamespace(
        id="project-1",
        name="辛巴达",
        target_duration_sec=240,
        style_profile={
            "story_bible": {
                "characters": [
                    {"name": "辛巴达", "description": "中年男性", "occurrence_count": 12},
                    {"name": "国王", "description": "国王", "occurrence_count": 8},
                ],
                "scenes": [
                    {"name": "岛上", "description": "海难后的岛屿", "occurrence_count": 4},
                    {"name": "海上", "description": "海面与船只", "occurrence_count": 10},
                ],
                "props": [
                    {"name": "珠宝", "description": "珍贵珠宝", "occurrence_count": 6},
                ],
            }
        },
    )
    chapter = SimpleNamespace(
        id="chapter-31",
        project_id="project-1",
        chapter_index=31,
        chunk_index=0,
        content="辛巴达把珠宝献给国王，然后启程返航。",
        meta={"title": "章节 32", "summary": "辛巴达受到国王礼遇后决定返航。"},
    )
    text = """
## 剧本段落

### 场景：岛上 - 国王的宫殿内

**镜头草案:**
1.  **人物**: 辛巴达 (恭敬), 国王 (赞许)
    **场景**: 岛上 - 国王的宫殿内 (珠宝在光线下闪耀)
    **动作**: 辛巴达从怀中取出珠宝，恭敬地献给国王。
    **对白**: 辛巴达：“我把自己带来的珠宝拿出一部分献给国王。”
    **构图**: 珠宝位于画面中心，国王和辛巴达的手势形成视觉引导。
    **景别**: 近景
    **时长**: 7秒

### 场景：海上

**镜头草案:**
1.  **人物**: 辛巴达 (坚定)
    **场景**: 海上 (阳光洒在海面上)
    **动作**: 船只驶离港口，辛巴达站在甲板上眺望远方。
    **对白**: 辛巴达（OS）：“第六次航海旅行，开始了！”
    **构图**: 新大船在画面中央，海面延伸到远方。
    **景别**: 远景
    **时长**: 8秒
"""

    parsed = service._parse_shot_detail_text(project, chapter, text)

    assert parsed["shot_count"] == 2
    assert parsed["shots"][0]["scene_hint"].startswith("岛上 - 国王的宫殿内")
    assert parsed["shots"][0]["characters"] == ["辛巴达", "国王"]
    assert parsed["shots"][0]["props"] == ["珠宝"]
    assert parsed["shots"][0]["frame_type"] == "近景"
    assert parsed["shots"][1]["scene"] == "海上"
    assert parsed["shots"][1]["frame_type"] == "远景"


def test_shot_payloads_need_reparse_when_scene_context_is_missing(tmp_path) -> None:
    fresh_app(database_url=f"sqlite:///{tmp_path / 'test_shot_detail_reparse.db'}")

    from app.services.pipeline_service import PipelineService

    service = PipelineService.__new__(PipelineService)

    assert service._shot_payloads_need_reparse([]) is True
    assert service._shot_payloads_need_reparse(
        [
            {
                "shot_index": 1,
                "visual": "整段章节原文被直接塞进 visual",
                "action": "整段章节原文被直接塞进 action",
                "scene": "",
                "scene_hint": "",
            }
        ]
    ) is True
    assert service._shot_payloads_need_reparse(
        [
            {
                "shot_index": 1,
                "visual": "海上远景，船只驶离港口。",
                "action": "辛巴达站在甲板上眺望远方。",
                "scene": "海上",
                "scene_hint": "海上 / 港口",
            },
            {
                "shot_index": 2,
                "visual": "岛上宫殿内，辛巴达献上珠宝。",
                "action": "国王接过珠宝。",
                "scene": "岛上",
                "scene_hint": "岛上 / 国王的宫殿内",
            },
        ]
    ) is False
