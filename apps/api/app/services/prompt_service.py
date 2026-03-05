from __future__ import annotations

from copy import deepcopy
from typing import Any

from workflow_engine import step_display_name

PROMPT_TEMPLATE_PRESETS: dict[str, list[dict[str, str]]] = {
    "ingest_parse": [
        {
            "template_id": "balanced_parse",
            "label": "标准解析",
            "description": "兼顾章节、人物、场景信息，适合作为默认入口。",
            "system": "你是小说解析器。保留原始文本结构，提取章节、人物、场景与叙事线索。",
            "task": "解析输入小说内容，输出结构化章节数组、人物列表、场景列表和关键叙事线索。",
        },
        {
            "template_id": "structure_first",
            "label": "结构优先",
            "description": "优先保证章节层级和段落边界稳定，适合长篇文本。",
            "system": "你是长篇小说结构分析器。优先稳定识别章节层级、段落边界和时间顺序。",
            "task": "按原文结构提取章节、子段落、时间线索与视角变化，不擅自改写原文内容。",
        },
        {
            "template_id": "scene_first",
            "label": "场景优先",
            "description": "优先提取空间变化和角色进出场，方便后续分镜。",
            "system": "你是场景解析器。优先识别角色出场、地点切换和场景状态。",
            "task": "输出按场景组织的文本片段，标记地点、时间、角色出场、情绪氛围与动作事件。",
        },
    ],
    "chapter_chunking": [
        {
            "template_id": "story_balance",
            "label": "剧情均衡",
            "description": "按剧情完整性切块，兼顾上下文重叠。",
            "system": "你是长文本切分器。控制上下文窗口并保持语义连续。",
            "task": "按剧情完整性切分章节，输出 chunk 列表、重叠文本和每块摘要。",
        },
        {
            "template_id": "conflict_focus",
            "label": "冲突聚焦",
            "description": "优先把冲突前后内容保留在同一块中。",
            "system": "你是剧情切分器。优先保留冲突、转折、揭示等关键事件的上下文完整性。",
            "task": "围绕冲突和转折切分文本，避免关键事件被拆散，并标注每块的剧情功能。",
        },
        {
            "template_id": "short_video",
            "label": "短视频节奏",
            "description": "切块更短，更适合快速做短片或预告片。",
            "system": "你是短片改编切分器。优先输出节奏紧凑、镜头转化效率高的文本块。",
            "task": "将文本切成适合短视频改编的小块，每块突出单一目标、冲突或情绪点。",
        },
    ],
    "story_scripting": [
        {
            "template_id": "balanced_script",
            "label": "标准剧本",
            "description": "兼顾冲突、人物和镜头感，适合作为默认模板。",
            "system": "你是编剧与导演助手。优先提取冲突、转折、情绪峰值。",
            "task": "生成剧情节点、剧本段落和镜头草案，明确每段的目标、阻力、转折和结果。",
        },
        {
            "template_id": "cinematic_script",
            "label": "电影叙事",
            "description": "强调戏剧起伏与镜头可拍性。",
            "system": "你是电影编剧。重视戏剧结构、悬念推进和视觉化叙事。",
            "task": "把小说内容改写成具有电影感的剧本段落，突出场面调度、冲突升级和情绪爆点。",
        },
        {
            "template_id": "character_driven",
            "label": "人物驱动",
            "description": "强调角色动机和关系变化。",
            "system": "你是人物戏编剧。优先刻画角色动机、关系拉扯和心理转折。",
            "task": "围绕角色目标、关系变化和情绪反应生成剧本结构，弱化无关背景铺陈。",
        },
    ],
    "shot_detailing": [
        {
            "template_id": "production_ready",
            "label": "可执行分镜",
            "description": "强调镜头落地性，适合进入出图阶段。",
            "system": "你是分镜细化器。确保人物、场景、动作、对白可执行。",
            "task": "输出每个镜头的人物形象、场景、动作、对白、构图、景别和时长建议。",
        },
        {
            "template_id": "continuity_first",
            "label": "连续性优先",
            "description": "优先控制角色和场景的一致性。",
            "system": "你是连续性分镜师。优先保证角色造型、场景布局、动作朝向和道具位置一致。",
            "task": "细化镜头时明确角色外观锚点、机位关系、空间方位和动作连续条件。",
        },
        {
            "template_id": "dialogue_focus",
            "label": "对白表演",
            "description": "适合对白戏、情绪戏较重的段落。",
            "system": "你是演员调度型分镜师。优先突出对白节奏、表情与反应镜头。",
            "task": "为每个镜头补足对白、停顿、视线、表演重点和反应镜头建议。",
        },
    ],
    "storyboard_image": [
        {
            "template_id": "balanced_image",
            "label": "标准出图",
            "description": "平衡叙事、造型和画面可读性。",
            "system": "你是视觉分镜提示词生成器。画面服务叙事，避免角色漂移。",
            "task": "基于镜头细节生成文生图提示词，明确主体、场景、动作、镜头语言和风格约束。",
        },
        {
            "template_id": "consistency_image",
            "label": "一致性优先",
            "description": "更强调角色和场景锚点。",
            "system": "你是连续性导向的出图提示词工程师。优先锁定角色脸部、服装、道具和空间结构。",
            "task": "输出适合多镜头连续出图的提示词，明确人物锚点、服饰锚点、场景锚点和禁改项。",
        },
        {
            "template_id": "stylized_image",
            "label": "风格强化",
            "description": "更强调风格圣经中的视觉方向。",
            "system": "你是风格化视觉总监。优先将风格圣经转成稳定的图像生成约束。",
            "task": "生成高风格统一性的图像提示词，明确配色、光照、材质、镜头气质和构图原则。",
        },
    ],
    "consistency_check": [
        {
            "template_id": "balanced_check",
            "label": "标准检查",
            "description": "综合检查人物、场景、动作和叙事连续性。",
            "system": "你是连续性监督员。评估角色、场景、动作、叙事一致性。",
            "task": "输出评分、问题列表、严重等级和修复建议，指出需回退的镜头。",
        },
        {
            "template_id": "character_check",
            "label": "人物一致",
            "description": "重点检查角色造型和身份稳定性。",
            "system": "你是角色连续性审片师。优先识别脸部、服装、发型、年龄感和道具是否漂移。",
            "task": "以人物一致性为核心输出评分和修复建议，并列出所有漂移项。",
        },
        {
            "template_id": "scene_motion_check",
            "label": "场景动作",
            "description": "重点检查空间关系和动作衔接。",
            "system": "你是场景与动作连续性审片师。优先检查机位关系、运动方向、空间布局和光照一致性。",
            "task": "输出场景连续性和动作衔接的评分、断点说明和修复建议。",
        },
    ],
    "segment_video": [
        {
            "template_id": "balanced_video",
            "label": "标准生视频",
            "description": "兼顾动作、镜头语言和一致性。",
            "system": "你是视频生成提示词工程师。优先动作衔接和镜头语言。",
            "task": "将分镜图和描述转换为图文生视频请求，明确主体运动、镜头运动、时长和风格约束。",
        },
        {
            "template_id": "motion_first_video",
            "label": "运动优先",
            "description": "强调动作流畅和镜头运动。",
            "system": "你是动作导向视频提示词工程师。优先运动连续、物理可信和镜头路径稳定。",
            "task": "输出更适合图生视频的动作指令，明确主体动势、速度、机位运动和禁用畸变。",
        },
        {
            "template_id": "consistency_video",
            "label": "连续性优先",
            "description": "适合人物连续镜头较多的段落。",
            "system": "你是连续性优先的视频生成提示词工程师。优先保持角色身份、场景布局和光照稳定。",
            "task": "为图文生视频生成请求补足连续性约束、参考图使用方式和镜头承接关系。",
        },
    ],
    "stitch_subtitle_tts": [
        {
            "template_id": "balanced_post",
            "label": "标准后期",
            "description": "兼顾字幕、配音和节奏统一。",
            "system": "你是后期剪辑师。保证节奏、字幕对齐、配音自然。",
            "task": "输出拼接计划、字幕和配音参数，保证每段时长、转场、字幕和旁白语气协调。",
        },
        {
            "template_id": "narration_first",
            "label": "旁白优先",
            "description": "适合旁白主导的改编方式。",
            "system": "你是旁白导向后期导演。优先保证旁白可听性、停顿节奏和信息密度。",
            "task": "输出适合旁白驱动短片的字幕和配音方案，明确语速、停顿、重音和字幕切分。",
        },
        {
            "template_id": "trailer_cut",
            "label": "预告剪辑",
            "description": "适合节奏更快的宣传片或短预告。",
            "system": "你是预告片剪辑师。优先保证节奏推进、情绪递增和信息击中率。",
            "task": "输出节奏更紧凑的拼接方案、字幕文案和配音建议，减少铺陈，强化钩子。",
        },
    ],
}

BASELINE_PROMPTS: dict[str, dict[str, str]] = {
    step_name: {
        "system": templates[0]["system"],
        "task": templates[0]["task"],
    }
    for step_name, templates in PROMPT_TEMPLATE_PRESETS.items()
}


def get_baseline_prompts(step_name: str) -> tuple[str, str]:
    prompt = BASELINE_PROMPTS.get(step_name)
    if not prompt:
        return "你是 AI 工作流助手。", "请输出结构化结果。"
    return prompt["system"], prompt["task"]


def list_prompt_templates(step_name: str | None = None) -> list[dict[str, Any]]:
    if step_name:
        templates = PROMPT_TEMPLATE_PRESETS.get(step_name, [])
        return [
            {
                "step_name": step_name,
                "step_display_name": step_display_name(step_name),
                **deepcopy(template),
                "system_prompt": template["system"],
                "task_prompt": template["task"],
            }
            for template in templates
        ]

    all_templates: list[dict[str, Any]] = []
    for name in PROMPT_TEMPLATE_PRESETS:
        all_templates.extend(list_prompt_templates(name))
    return all_templates
