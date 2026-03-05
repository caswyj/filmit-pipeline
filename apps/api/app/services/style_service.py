from __future__ import annotations

from copy import deepcopy
from typing import Any


STYLE_PRESETS: list[dict[str, Any]] = [
    {
        "id": "cinematic",
        "label": "电影质感",
        "description": "高动态范围、真实镜头语言、强调情绪光影与景深。",
        "keywords": ["cinematic", "dramatic lighting", "35mm", "shallow depth of field"],
        "palette": ["amber", "teal", "charcoal"],
        "lighting": "戏剧化体积光与高反差补光",
        "rendering": "写实电影画面",
        "camera_language": "中近景、推拉摇移、景别递进",
        "motion_feel": "克制、平稳、强调叙事节奏",
    },
    {
        "id": "cyberpunk",
        "label": "赛博朋克",
        "description": "霓虹、高密度城市、潮湿反光表面与未来感机械细节。",
        "keywords": ["cyberpunk", "neon", "megacity", "rainy streets"],
        "palette": ["neon magenta", "electric cyan", "black chrome"],
        "lighting": "霓虹边缘光、广告屏溢光、夜景潮湿反射",
        "rendering": "高细节未来都市写实",
        "camera_language": "低机位、广角透视、快速平移",
        "motion_feel": "紧张、电子脉冲感、都市压迫",
    },
    {
        "id": "gothic",
        "label": "哥特式",
        "description": "尖拱、古堡、宗教式空间与冷峻神秘氛围。",
        "keywords": ["gothic", "cathedral", "stone", "ornate shadows"],
        "palette": ["burgundy", "slate", "bone white"],
        "lighting": "烛光、月光、窗格阴影",
        "rendering": "高细节暗色写实",
        "camera_language": "长焦压缩、仰拍、静态构图",
        "motion_feel": "缓慢、压抑、神秘",
    },
    {
        "id": "gloom_noir",
        "label": "阴郁黑色",
        "description": "偏黑色电影与心理惊悚气质，强调阴影和不安全感。",
        "keywords": ["noir", "gloomy", "moody", "psychological tension"],
        "palette": ["graphite", "cold blue", "desaturated sepia"],
        "lighting": "低照度、局部高光、大片阴影",
        "rendering": "阴郁写实",
        "camera_language": "静止镜头、缓慢推进、局部特写",
        "motion_feel": "压抑、悬疑、情绪缓慢累积",
    },
    {
        "id": "chibi",
        "label": "Q版",
        "description": "夸张头身比、可爱表情与简洁友好的动作节奏。",
        "keywords": ["chibi", "cute", "super deformed", "playful"],
        "palette": ["peach", "sky blue", "mint"],
        "lighting": "柔和漫反射与高亮轮廓",
        "rendering": "二维卡通渲染",
        "camera_language": "中心构图、轻快摇镜、表情特写",
        "motion_feel": "轻快、夸张、富有弹性",
    },
    {
        "id": "realistic",
        "label": "写实",
        "description": "追求真实人物比例、真实材质和可信环境细节。",
        "keywords": ["realistic", "natural texture", "authentic", "lifelike"],
        "palette": ["earth", "skin tone", "natural green"],
        "lighting": "自然光源逻辑与物理可信照明",
        "rendering": "高写实",
        "camera_language": "观察式镜头、纪录片式构图",
        "motion_feel": "自然、克制、可信",
    },
    {
        "id": "flat_illustration",
        "label": "平面插画",
        "description": "强图形感、简化透视、块面颜色与清晰轮廓。",
        "keywords": ["flat illustration", "graphic", "clean shapes", "poster-like"],
        "palette": ["mustard", "ink blue", "coral"],
        "lighting": "弱真实光照，强调色块关系",
        "rendering": "二维平面插画",
        "camera_language": "海报式构图、清晰前后层次",
        "motion_feel": "节奏鲜明、图形转场友好",
    },
    {
        "id": "stylized_3d",
        "label": "三维风格化",
        "description": "具备三维体积与材质，同时保留动画美术夸张感。",
        "keywords": ["stylized 3d", "animated feature", "volumetric", "polished"],
        "palette": ["warm key light", "deep navy", "accent orange"],
        "lighting": "柔和体积光与层次明确的轮廓光",
        "rendering": "高质量三维风格化渲染",
        "camera_language": "电影动画式调度",
        "motion_feel": "流畅、清晰、可读性强",
    },
    {
        "id": "anime",
        "label": "动画番剧",
        "description": "线条清晰、色彩干净，适合角色戏和节奏明确的分镜。",
        "keywords": ["anime", "clean lineart", "cel shading", "expressive"],
        "palette": ["sakura pink", "ultramarine", "soft gold"],
        "lighting": "赛璐璐分区光影与边缘高光",
        "rendering": "二次元番剧渲染",
        "camera_language": "夸张透视、情绪特写、速度线式动势",
        "motion_feel": "情绪化、节奏强、表演突出",
    },
    {
        "id": "ink_wash",
        "label": "水墨",
        "description": "留白、晕染和笔触层次，强调诗性与气韵。",
        "keywords": ["ink wash", "brushstroke", "negative space", "poetic"],
        "palette": ["ink black", "rice paper", "mineral gray"],
        "lighting": "弱写实光影，以墨色层次代替真实打光",
        "rendering": "东方水墨写意",
        "camera_language": "留白构图、缓慢横移、山水式景别",
        "motion_feel": "安静、悠长、富有呼吸感",
    },
]


def list_style_presets() -> list[dict[str, Any]]:
    return deepcopy(STYLE_PRESETS)


def get_style_preset(preset_id: str) -> dict[str, Any] | None:
    for preset in STYLE_PRESETS:
        if preset["id"] == preset_id:
            return deepcopy(preset)
    return None


def normalize_style_profile(profile: dict[str, Any] | None) -> dict[str, Any]:
    profile = deepcopy(profile or {})
    existing_story_bible = deepcopy(profile.get("story_bible") or {})
    passthrough = {
        key: value
        for key, value in profile.items()
        if key not in {"preset_id", "preset_label", "custom_style", "custom_directives", "story_bible"}
    }
    preset_id = str(profile.get("preset_id") or "cinematic")
    preset = get_style_preset(preset_id) or get_style_preset("cinematic") or deepcopy(STYLE_PRESETS[0])
    custom_style = str(profile.get("custom_style") or "").strip()
    custom_directives = str(profile.get("custom_directives") or "").strip()

    story_bible = {
        "visual_style": {
            "preset_id": preset["id"],
            "preset_label": preset["label"],
            "preset_description": preset["description"],
            "keywords": preset["keywords"],
            "palette": preset["palette"],
            "lighting": preset["lighting"],
            "rendering": preset["rendering"],
            "camera_language": preset["camera_language"],
            "motion_feel": preset["motion_feel"],
            "custom_style": custom_style,
            "custom_directives": custom_directives,
        },
        "consistency_guardrails": [
            "保持同一角色在不同镜头中的服装、发型、年龄感一致",
            "保持同一场景的空间布局、主色调、时间段一致",
            "镜头运动与动作连续，不凭空切换朝向和道具位置",
        ],
    }
    if isinstance(existing_story_bible, dict):
        for key, value in existing_story_bible.items():
            if key in {"visual_style", "consistency_guardrails"}:
                continue
            story_bible[key] = value
    return {
        **passthrough,
        "preset_id": preset["id"],
        "preset_label": preset["label"],
        "custom_style": custom_style,
        "custom_directives": custom_directives,
        "story_bible": story_bible,
    }


def build_style_prompt(style_profile: dict[str, Any] | None) -> str:
    normalized = normalize_style_profile(style_profile)
    visual_style = normalized["story_bible"]["visual_style"]
    story_bible = normalized["story_bible"]
    lines = [
        "风格圣经约束：",
        f"- 基础风格：{visual_style['preset_label']}（{visual_style['preset_description']}）",
        f"- 关键词：{', '.join(visual_style['keywords'])}",
        f"- 配色：{', '.join(visual_style['palette'])}",
        f"- 光照：{visual_style['lighting']}",
        f"- 渲染：{visual_style['rendering']}",
        f"- 镜头语言：{visual_style['camera_language']}",
        f"- 动势：{visual_style['motion_feel']}",
    ]
    if visual_style["custom_style"]:
        lines.append(f"- 用户自定义风格名：{visual_style['custom_style']}")
    if visual_style["custom_directives"]:
        lines.append(f"- 用户附加约束：{visual_style['custom_directives']}")
    characters = story_bible.get("characters")
    if isinstance(characters, list) and characters:
        lines.append("- 核心人物锚点：")
        for item in characters[:5]:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            desc = item.get("description") or item.get("visual_anchor")
            if name:
                lines.append(f"  - {name}: {desc}")
                ref = item.get("reference_storage_key") or item.get("reference_image_url")
                if ref:
                    lines.append(f"  - {name} 参考图: {ref}")
    scenes = story_bible.get("scenes")
    if isinstance(scenes, list) and scenes:
        lines.append("- 核心场景锚点：")
        for item in scenes[:5]:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            desc = item.get("description") or item.get("visual_anchor")
            if name:
                lines.append(f"  - {name}: {desc}")
                ref = item.get("reference_storage_key") or item.get("reference_image_url")
                if ref:
                    lines.append(f"  - {name} 参考图: {ref}")
    return "\n".join(lines)
