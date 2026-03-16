from __future__ import annotations

import base64
import asyncio
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image

from tests.helpers import fresh_app


def test_ingest_generates_story_bible_reference_images() -> None:
    db_path = Path("./test_n2v_story_bible.db")
    if db_path.exists():
        db_path.unlink()

    app = fresh_app(database_url="sqlite:///./test_n2v_story_bible.db")
    source_text = (
        "一\n\n路易斯和瑞琪儿搬到小镇边缘的新家。贾德站在门廊迎接路易斯，身后是通往墓地的树林。"
        "艾丽抱着猫从厨房跑向门口，路易斯看见墓地石碑在雾里。\n\n"
        "二\n\n艾丽再次提到那片墓地，路易斯和贾德一起穿过树林的小径。"
        "瑞琪儿在厨房等他们回来，墓地石碑与树林反复出现。\n\n"
        "三\n\n贾德带着路易斯靠近墓地后的更深处，艾丽也在门廊远远看着。"
        "瑞琪儿回到厨房，树林、墓地和门廊在同一夜里反复出现。"
    )

    with TestClient(app) as client:
        created = client.post("/api/v1/projects", json={"name": "story-bible-demo", "target_duration_sec": 120})
        assert created.status_code == 201
        project_id = created.json()["id"]

        upload = client.post(
            f"/api/v1/projects/{project_id}/source-documents",
            files={"file": ("novel.txt", source_text.encode("utf-8"), "text/plain")},
        )
        assert upload.status_code == 201

        ingest = client.post(f"/api/v1/projects/{project_id}/steps/ingest_parse/run", json={"force": True})
        assert ingest.status_code == 200
        approve_ingest = client.post(
            f"/api/v1/projects/{project_id}/steps/{ingest.json()['id']}/approve",
            json={"scope_type": "step", "created_by": "tester"},
        )
        assert approve_ingest.status_code == 200

        chunk = client.post(f"/api/v1/projects/{project_id}/steps/chapter_chunking/run", json={"force": True})
        assert chunk.status_code == 200
        artifact = chunk.json()["output_ref"]["artifact"]
        story_bible = artifact["story_bible"]
        assert len(story_bible["characters"]) >= 1
        assert len(story_bible["scenes"]) >= 1
        assert any(item.get("reference_image_url") for item in story_bible["characters"])
        assert any(item.get("reference_image_url") for item in story_bible["scenes"])
        assert len(story_bible["props"]) >= 1
        assert any(item.get("prop_reference_image_url") for item in story_bible["props"])
        assert any(len(item.get("identity_reference_views") or []) >= 1 for item in story_bible["characters"])
        assert any(len(item.get("scene_reference_variants") or []) >= 1 for item in story_bible["scenes"])
        assert any(len(item.get("prop_reference_views") or []) >= 1 for item in story_bible["props"])
        assert "safety_preprocess" in story_bible

        project = client.get(f"/api/v1/projects/{project_id}")
        assert project.status_code == 200
        project_story_bible = project.json()["style_profile"]["story_bible"]
        assert len(project_story_bible["characters"]) >= 1
        assert len(project_story_bible["scenes"]) >= 1
        assert len(project_story_bible["props"]) >= 1


def test_story_bible_reference_generation_preprocesses_sensitive_visual_terms(tmp_path) -> None:
    db_path = tmp_path / "test_story_bible_safety.db"
    app = fresh_app(database_url=f"sqlite:///{db_path}")

    with TestClient(app) as client:
        created = client.post("/api/v1/projects", json={"name": "story-bible-safety-demo", "target_duration_sec": 60})
        assert created.status_code == 201
        project_id = created.json()["id"]

        upload = client.post(
            f"/api/v1/projects/{project_id}/source-documents",
            files={
                "file": (
                    "novel.txt",
                    (
                        "第一章\n\n"
                        "路易斯看到一个赤裸上身、血肉模糊的怪异身影从墓地边缘走过，随后又看清那只是需要被中性化处理的视觉误导。"
                        "旧墓地位于树林深处，石碑和潮湿泥地构成压抑氛围。"
                    ).encode("utf-8"),
                    "text/plain",
                )
            },
        )
        assert upload.status_code == 201

        ingest = client.post(f"/api/v1/projects/{project_id}/steps/ingest_parse/run", json={"force": True})
        assert ingest.status_code == 200
        approve_ingest = client.post(
            f"/api/v1/projects/{project_id}/steps/{ingest.json()['id']}/approve",
            json={"scope_type": "step", "created_by": "tester"},
        )
        assert approve_ingest.status_code == 200

        chunk = client.post(f"/api/v1/projects/{project_id}/steps/chapter_chunking/run", json={"force": True})
        assert chunk.status_code == 200
        story_bible = chunk.json()["output_ref"]["artifact"]["story_bible"]
        safety = story_bible["safety_preprocess"]
        assert safety["changed_count"] >= 1
        combined_text = " ".join(
            str(item.get("reference_safe_description") or "")
            for item in story_bible["characters"] + story_bible["scenes"] + story_bible["props"]
        )
        assert "赤裸" not in combined_text
        assert "血肉模糊" not in combined_text

    if db_path.exists():
        db_path.unlink()


def test_story_bible_reference_canonicalizes_character_identity_and_filters_food_props(tmp_path) -> None:
    db_path = tmp_path / "test_story_bible_canonical.db"
    app = fresh_app(database_url=f"sqlite:///{db_path}")

    with TestClient(app) as client:
        created = client.post("/api/v1/projects", json={"name": "story-bible-canonical-demo", "target_duration_sec": 60})
        assert created.status_code == 201
        project_id = created.json()["id"]

        upload = client.post(
            f"/api/v1/projects/{project_id}/source-documents",
            files={
                "file": (
                    "novel.txt",
                    (
                        "第一章\n\n"
                        "盖基是约两岁的男童，早餐时把燕麦粥抹得到处都是。艾丽是他的姐姐，边吃边偷笑。"
                        "路易斯是中年男性医生，也是盖基的父亲。瑞琪儿是母亲，递给路易斯鸡蛋。"
                        "路德楼镇在早春显得安静，早餐桌被晨光照亮。"
                    ).encode("utf-8"),
                    "text/plain",
                )
            },
        )
        assert upload.status_code == 201

        ingest = client.post(f"/api/v1/projects/{project_id}/steps/ingest_parse/run", json={"force": True})
        assert ingest.status_code == 200
        approve_ingest = client.post(
            f"/api/v1/projects/{project_id}/steps/{ingest.json()['id']}/approve",
            json={"scope_type": "step", "created_by": "tester"},
        )
        assert approve_ingest.status_code == 200

        chunk = client.post(f"/api/v1/projects/{project_id}/steps/chapter_chunking/run", json={"force": True})
        assert chunk.status_code == 200
        story_bible = chunk.json()["output_ref"]["artifact"]["story_bible"]
        first_character = story_bible["characters"][0]
        assert "燕麦粥" not in first_character["description"]
        assert any(token in first_character["description"] for token in ("两岁", "幼儿", "男童", "男孩"))
        assert "统一浅色中性日常服" in first_character["wardrobe_anchor"]
        assert "统一浅色中性日常服" in " ".join(first_character.get("reference_hard_constraints") or [])
        assert all(item["name"] != "燕麦粥" for item in story_bible["props"])


def test_story_bible_reference_keeps_child_age_detail_and_moves_vehicle_scene_to_prop(tmp_path) -> None:
    fresh_app(database_url=f"sqlite:///{tmp_path / 'test_story_bible_profile.db'}")

    from app.services.pipeline_service import PipelineService

    service = PipelineService.__new__(PipelineService)
    character = service._canonicalize_story_bible_reference_item(
        {
            "name": "盖基",
            "description": "路易斯的幼子，金发幼儿",
            "visual_anchor": "幼儿体型，金发",
            "wardrobe_anchor": "保持服装、发型和年龄感稳定一致。",
        },
        kind="character",
    )
    assert "幼子" in character["description"]
    assert any(token in character["description"] for token in ("幼儿", "男童"))
    assert "金发" in character["description"]
    assert character["reference_age_bucket"] == "child"
    assert any("绝不能生成成年人" in item for item in character["reference_hard_constraints"])

    characters, scenes, props = service._canonicalize_story_bible_reference_entities(
        [],
        [
            {
                "name": "旅行轿车",
                "description": "四缸旅行轿车，家庭迁徙交通工具",
                "visual_anchor": "旅行轿车外形，后座空间",
                "mood": "紧张",
            }
        ],
        [],
    )
    assert not scenes
    assert any(item["name"] == "旅行轿车" for item in props)


def test_story_bible_reference_moves_place_like_prop_to_scene(tmp_path) -> None:
    fresh_app(database_url=f"sqlite:///{tmp_path / 'test_story_bible_prop_scene.db'}")

    from app.services.pipeline_service import PipelineService

    service = PipelineService.__new__(PipelineService)
    characters, scenes, props = service._canonicalize_story_bible_reference_entities(
        [],
        [],
        [
            {
                "name": "巴士拉",
                "description": "一个港口城市，停泊着船只，有市集和建筑",
                "visual_anchor": "繁忙港口，船只，市集和建筑",
                "usage_context": "辛巴达在此寻找新船出海。",
            }
        ],
    )
    assert not props
    assert any(item["name"] == "巴士拉" for item in scenes)


def test_get_project_restores_scene_and_prop_reference_metadata_from_disk(tmp_path) -> None:
    db_path = tmp_path / "test_story_bible_restore.db"
    app = fresh_app(database_url=f"sqlite:///{db_path}")

    with TestClient(app) as client:
        created = client.post("/api/v1/projects", json={"name": "sinbad-demo", "target_duration_sec": 60})
        assert created.status_code == 201
        project = created.json()
        project_id = project["id"]

        generated_root = db_path.parent / "generated" / f"sinbad-demo-{project_id}" / "references"
        scene_dir = generated_root / "scenes" / "scene-establishing_day"
        prop_dir = generated_root / "props" / "prop-front"
        scene_dir.mkdir(parents=True, exist_ok=True)
        prop_dir.mkdir(parents=True, exist_ok=True)

        for path in (
            scene_dir / "scene-establishing_day-01-海上-establishing_day.png",
            prop_dir / "prop-front-01-金币-front.png",
        ):
            Image.new("RGB", (32, 32), (245, 245, 245)).save(path)

        updated = client.patch(
            f"/api/v1/projects/{project_id}",
            json={
                "style_profile": {
                    "story_bible": {
                        "characters": [],
                        "scenes": [{"name": "海上", "description": "海上的建立镜"}],
                        "props": [{"name": "金币", "description": "关键金币道具"}],
                    }
                }
            },
        )
        assert updated.status_code == 200

        hydrated = client.get(f"/api/v1/projects/{project_id}")
        assert hydrated.status_code == 200
        story_bible = hydrated.json()["style_profile"]["story_bible"]
        scene = story_bible["scenes"][0]
        prop = story_bible["props"][0]
        assert scene["reference_generation_status"] == "PARTIAL"
        assert len(scene["scene_reference_variants"]) == 1
        assert scene["scene_reference_variants"][0]["image_url"].startswith("/api/v1/local-files/")
        assert prop["reference_generation_status"] == "PARTIAL"
        assert len(prop["prop_reference_views"]) == 1
        assert prop["prop_reference_views"][0]["image_url"].startswith("/api/v1/local-files/")


def test_gallery_payload_uses_export_url_when_contact_sheet_thumbnail_missing(tmp_path) -> None:
    fresh_app(database_url=f"sqlite:///{tmp_path / 'test_storyboard_gallery_fallback.db'}")

    from app.services.pipeline_service import PipelineService

    service = PipelineService.__new__(PipelineService)
    payload = service._gallery_payload_from_artifact(
        {
            "frames": [{"shot_index": 1, "image_url": "/api/v1/local-files/demo/frame-001.png"}],
            "export_url": "/api/v1/local-files/demo/contact-sheet.png",
            "gallery_export_url": "/api/v1/local-files/demo/storyboards.zip",
            "cover_image_url": "/api/v1/local-files/demo/frame-001.png",
        }
    )
    assert payload["contact_sheet_url"] == "/api/v1/local-files/demo/contact-sheet.png"


def test_story_bible_refine_character_from_source_corrects_gender_and_role(tmp_path) -> None:
    fresh_app(database_url=f"sqlite:///{tmp_path / 'test_story_bible_refine.db'}")

    from app.services.pipeline_service import PipelineService

    service = PipelineService.__new__(PipelineService)
    chapter = SimpleNamespace(
        content=(
            "后来我娶了诺尔玛。诺尔玛是个和蔼可亲的老太太。"
            "她得了类风湿性关节炎，但很喜欢两个孩子。"
        ),
        meta={"title": "第一章"},
    )
    item = {
        "name": "诺尔玛·克兰道尔",
        "aliases": ["诺尔玛"],
        "description": "老年男性",
        "visual_anchor": "年老男性",
        "wardrobe_anchor": "保持连续一致的服装与材质细节。",
    }
    refined = service._refine_story_bible_character_from_source(
        item,
        [chapter],
        [{"title": "第一章", "summary": "", "context": chapter.content}],
    )
    canonical = service._canonicalize_story_bible_reference_item(refined, kind="character")
    assert "老年女性" in canonical["description"]
    assert canonical["reference_age_bucket"] == "elder"


def test_story_bible_character_alias_cleanup_removes_cross_character_pollution(tmp_path) -> None:
    fresh_app(database_url=f"sqlite:///{tmp_path / 'test_story_bible_alias_cleanup.db'}")

    from app.services.pipeline_service import PipelineService

    service = PipelineService.__new__(PipelineService)
    cleaned = service._clean_story_bible_character_aliases(
        [
            {"name": "诺尔玛·克兰道尔", "aliases": ["诺尔玛", "乍得", "克兰道尔", "老太太"]},
            {"name": "乍得", "aliases": []},
        ]
    )
    norma = next(item for item in cleaned if item["name"] == "诺尔玛·克兰道尔")
    assert norma["aliases"] == ["诺尔玛"]


def test_story_bible_scene_and_prop_identity_text_is_compacted(tmp_path) -> None:
    fresh_app(database_url=f"sqlite:///{tmp_path / 'test_story_bible_identity_compact.db'}")

    from app.services.pipeline_service import PipelineService

    service = PipelineService.__new__(PipelineService)

    scene = service._canonicalize_story_bible_reference_item(
        {
            "name": "宠物公墓",
            "description": "位于镇外小路尽头的墓地，埋葬被公路压死的宠物",
            "visual_anchor": "石碑与林间小路",
            "mood": "神秘略带诡异，真实空间、稳定结构、允许不同角度与不同光照但不改变核心布局。",
        },
        kind="scene",
    )
    assert scene["description"] == "林间墓地空间，石碑、小路与树线关系稳定，强调压抑但真实的自然光与地形纵深。"

    prop = service._canonicalize_story_bible_reference_item(
        {
            "name": "汽车",
            "description": "汽车 的独立物品参考，强调稳定轮廓、材质、尺寸与表面状态。",
            "visual_anchor": "物品参考图只保留本体形体、材质、磨损和关键结构，不加入手持人物或场景。",
            "material_anchor": "保持尺寸比例、表面材质、颜色与磨损状态一致。",
            "usage_context": "仅作为物品本体参考，不包含一次性剧情动作。",
            "reference_source_excerpt": "瑞琪儿驾驶的新车，行程不到5000英里 右侧保险杠、轮胎、方向盘 保持材质、尺寸、磨损状态和关键结构一致。 作为关键剧情道具反复出现",
        },
        kind="prop",
    )
    assert "瑞琪儿驾驶的新车" in prop["description"]
    assert "右侧保险杠、轮胎、方向盘" in prop["description"]
    assert prop["description"].count("保持尺寸比例") == 0


def test_story_bible_reference_generation_covers_all_items_without_hard_cap(tmp_path) -> None:
    fresh_app(database_url=f"sqlite:///{tmp_path / 'test_story_bible_limits.db'}")

    from app.services.pipeline_service import PipelineService

    image = Image.new("RGB", (16, 16), color=(240, 240, 240))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    data_url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")

    class FakeAdapter:
        def supports(self, capability: str, model: str) -> bool:
            return capability == "image"

    class FakeRegistry:
        def resolve(self, provider: str) -> FakeAdapter:
            return FakeAdapter()

        def list_catalog(self) -> list[object]:
            return []

    service = PipelineService.__new__(PipelineService)
    service.registry = FakeRegistry()
    service._resolve_binding = lambda project, step_name, capability: ("openrouter", "mock-image")

    async def fake_generate_storyboard_frame_with_fallback(**kwargs):
        return None, {
            "image_data_url": data_url,
            "mime_type": "image/png",
            "provider": "openrouter",
            "model": "mock-image",
        }, None, None

    service._generate_storyboard_frame_with_fallback = fake_generate_storyboard_frame_with_fallback
    service._materialize_story_bible_reference_asset = lambda *args, **kwargs: {
        "image_url": "file:///tmp/reference.png",
        "thumbnail_url": "file:///tmp/reference.png",
        "storage_key": "/tmp/reference.png",
        "export_url": "file:///tmp/reference.png",
    }

    project = SimpleNamespace(id="project-1", name="demo", style_profile={})
    step = SimpleNamespace(id="step-1")
    characters = [{"name": f"角色{index}", "description": "中年男性", "visual_anchor": "", "wardrobe_anchor": ""} for index in range(5)]
    scenes = [{"name": f"场景{index}", "description": "室内空间", "visual_anchor": "", "mood": ""} for index in range(5)]
    props = [{"name": f"道具{index}", "description": "关键物品", "visual_anchor": "", "material_anchor": "", "usage_context": ""} for index in range(7)]

    asyncio.run(service._generate_story_bible_reference_images(project, step, characters, scenes, props))

    assert all(len(item.get("identity_reference_views") or []) == 4 for item in characters)
    assert all(len(item.get("scene_reference_variants") or []) == 4 for item in scenes)
    assert all(len(item.get("prop_reference_views") or []) == 4 for item in props)


def test_story_bible_regenerate_single_item_endpoint(tmp_path) -> None:
    db_path = tmp_path / "test_story_bible_regenerate.db"
    app = fresh_app(database_url=f"sqlite:///{db_path}")

    with TestClient(app) as client:
        created = client.post("/api/v1/projects", json={"name": "story-bible-regenerate-demo", "target_duration_sec": 60})
        assert created.status_code == 201
        project_id = created.json()["id"]

        upload = client.post(
            f"/api/v1/projects/{project_id}/source-documents",
            files={
                "file": (
                    "novel.txt",
                    (
                        "第一章\n\n"
                        "路易斯和瑞琪儿搬到新家。艾丽和盖基在客厅里玩玩具火车。"
                    ).encode("utf-8"),
                    "text/plain",
                )
            },
        )
        assert upload.status_code == 201

        ingest = client.post(f"/api/v1/projects/{project_id}/steps/ingest_parse/run", json={"force": True})
        assert ingest.status_code == 200
        approve_ingest = client.post(
            f"/api/v1/projects/{project_id}/steps/{ingest.json()['id']}/approve",
            json={"scope_type": "step", "created_by": "tester"},
        )
        assert approve_ingest.status_code == 200
        chunk = client.post(f"/api/v1/projects/{project_id}/steps/chapter_chunking/run", json={"force": True})
        assert chunk.status_code == 200

        story_bible = chunk.json()["output_ref"]["artifact"]["story_bible"]
        target_name = story_bible["characters"][0]["name"]

        regenerate = client.post(
            f"/api/v1/projects/{project_id}/story-bible/regenerate-item",
            json={"kind": "characters", "name": target_name},
        )
        assert regenerate.status_code == 200
        updated_story_bible = regenerate.json()["style_profile"]["story_bible"]
        updated_item = next(item for item in updated_story_bible["characters"] if item["name"] == target_name)
        assert len(updated_item.get("identity_reference_views") or []) >= 1
        assert updated_item.get("reference_generation_status") in {"SUCCEEDED", "PARTIAL"}


def test_reference_image_data_url_downsamples_large_local_images(tmp_path) -> None:
    fresh_app(database_url=f"sqlite:///{tmp_path / 'test_story_bible_resize.db'}")

    from app.services.pipeline_service import PipelineService

    source = Image.effect_noise((1800, 1200), 120).convert("RGB")
    path = tmp_path / "large-reference.png"
    source.save(path, format="PNG")

    service = PipelineService.__new__(PipelineService)
    data_url = service._reference_image_data_url(str(path), None)

    assert isinstance(data_url, str)
    assert data_url.startswith("data:image/jpeg;base64,")

    encoded = data_url.split(",", 1)[1]
    decoded = base64.b64decode(encoded)
    with Image.open(BytesIO(decoded)) as image:
        assert max(image.size) <= 640
    assert len(decoded) < path.stat().st_size


def test_storyboard_reference_images_prefer_character_and_scene_over_props(tmp_path) -> None:
    fresh_app(database_url=f"sqlite:///{tmp_path / 'test_storyboard_ref_priority.db'}")

    from app.services.pipeline_service import PipelineService

    def make_png(name: str) -> str:
        path = tmp_path / name
        Image.new("RGB", (32, 32), color=(240, 240, 240)).save(path, format="PNG")
        return str(path)

    service = PipelineService.__new__(PipelineService)
    project = SimpleNamespace(
        style_profile={
            "story_bible": {
                "characters": [
                    {
                        "name": "辛巴达",
                        "identity_reference_storage_key": make_png("sinbad.png"),
                        "identity_reference_image_url": "",
                    }
                ],
                "scenes": [
                    {
                        "name": "海上",
                        "scene_reference_storage_key": make_png("sea.png"),
                        "scene_reference_image_url": "",
                    }
                ],
                "props": [
                    {
                        "name": "新大船",
                        "prop_reference_storage_key": make_png("ship.png"),
                        "prop_reference_image_url": "",
                    }
                ],
            }
        }
    )
    shot = {
        "characters": ["辛巴达"],
        "scene": "海上",
        "scene_hint": "海上",
        "props": ["新大船"],
        "visual": "辛巴达站在新大船甲板上，船正驶离港口，周围是广阔的大海。",
        "action": "新大船缓缓驶离港口。",
    }

    refs = service._story_bible_reference_images_for_shot(project, shot)

    labels = [item["label"] for item in refs]
    assert "characters:辛巴达" in labels
    assert "scenes:海上" in labels
    assert "props:新大船" not in labels


def test_storyboard_reference_images_fall_back_to_props_when_no_character_or_scene_exists(tmp_path) -> None:
    fresh_app(database_url=f"sqlite:///{tmp_path / 'test_storyboard_ref_prop_fallback.db'}")

    from app.services.pipeline_service import PipelineService

    path = tmp_path / "ship.png"
    Image.new("RGB", (32, 32), color=(240, 240, 240)).save(path, format="PNG")

    service = PipelineService.__new__(PipelineService)
    project = SimpleNamespace(
        style_profile={
            "story_bible": {
                "characters": [],
                "scenes": [],
                "props": [
                    {
                        "name": "新大船",
                        "prop_reference_storage_key": str(path),
                        "prop_reference_image_url": "",
                    }
                ],
            }
        }
    )
    shot = {
        "props": ["新大船"],
        "visual": "一艘新大船停在海面上。",
        "action": "船帆鼓起。",
    }

    refs = service._story_bible_reference_images_for_shot(project, shot)

    assert [item["label"] for item in refs] == ["props:新大船"]
