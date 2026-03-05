from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ProjectStatus(StrEnum):
    DRAFT = "DRAFT"
    RUNNING = "RUNNING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    APPROVED = "APPROVED"
    RENDERING = "RENDERING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class StepStatus(StrEnum):
    PENDING = "PENDING"
    GENERATING = "GENERATING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    APPROVED = "APPROVED"
    REWORK_REQUESTED = "REWORK_REQUESTED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class PipelineStepDefinition:
    order: int
    step_name: str
    display_name: str
    step_type: str
    requires_manual_gate: bool = True


PIPELINE_STEPS: list[PipelineStepDefinition] = [
    PipelineStepDefinition(order=1, step_name="ingest_parse", display_name="导入全文", step_type="chunk"),
    PipelineStepDefinition(order=2, step_name="chapter_chunking", display_name="切分章节", step_type="chunk"),
    PipelineStepDefinition(order=3, step_name="story_scripting", display_name="章节剧本", step_type="script"),
    PipelineStepDefinition(order=4, step_name="shot_detailing", display_name="分镜细化", step_type="shot_detail"),
    PipelineStepDefinition(order=5, step_name="storyboard_image", display_name="分镜出图", step_type="image"),
    PipelineStepDefinition(order=6, step_name="consistency_check", display_name="分镜校核", step_type="consistency"),
    PipelineStepDefinition(order=7, step_name="segment_video", display_name="视频片段", step_type="video"),
    PipelineStepDefinition(order=8, step_name="stitch_subtitle_tts", display_name="成片输出", step_type="tts"),
]

STEP_DISPLAY_NAME_MAP: dict[str, str] = {item.step_name: item.display_name for item in PIPELINE_STEPS}


def step_display_name(step_name: str) -> str:
    return STEP_DISPLAY_NAME_MAP.get(step_name, step_name)


def next_step_name(current_step_name: str) -> str | None:
    ordered = sorted(PIPELINE_STEPS, key=lambda s: s.order)
    for idx, step in enumerate(ordered):
        if step.step_name == current_step_name:
            if idx + 1 < len(ordered):
                return ordered[idx + 1].step_name
            return None
    return None
