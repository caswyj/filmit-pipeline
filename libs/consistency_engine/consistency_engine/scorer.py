from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True, frozen=True)
class ConsistencyResult:
    score: int
    dimensions: dict[str, int]
    should_rework: bool
    details: dict[str, Any] = field(default_factory=dict)


def score_consistency(payload: dict[str, Any] | str | None, threshold: int = 75) -> ConsistencyResult:
    context = payload if isinstance(payload, dict) else {"text": str(payload or "")}
    frames = [item for item in context.get("frames", []) if isinstance(item, dict)]
    if not frames:
        return ConsistencyResult(
            score=48,
            dimensions={
                "chapter_internal_character": 45,
                "chapter_internal_scene": 45,
                "reference_adherence": 50,
                "cross_chapter_style": 52,
            },
            should_rework=True,
            details={"reason": "no_frames"},
        )

    story_bible = context.get("story_bible") or {}
    characters = [item for item in story_bible.get("characters", []) if isinstance(item, dict)]
    scenes = [item for item in story_bible.get("scenes", []) if isinstance(item, dict)]
    neighbor_frames = [item for item in context.get("neighbor_frames", []) if isinstance(item, dict)]

    frame_profiles = [_frame_profile(frame, characters, scenes) for frame in frames]
    neighbor_profiles = [_frame_profile(frame, characters, scenes) for frame in neighbor_frames]

    character_score = _adjacent_anchor_consistency(frame_profiles, "character_anchors", empty_baseline=72)
    scene_score = _scene_consistency(frame_profiles)
    reference_score = _reference_adherence(frame_profiles)
    cross_chapter_style = _cross_chapter_style(frame_profiles, neighbor_profiles)

    total = round(
        character_score * 0.31
        + scene_score * 0.27
        + reference_score * 0.22
        + cross_chapter_style * 0.20
    )

    low_frames = [
        {
            "shot_index": profile["shot_index"],
            "score": profile["frame_consistency_score"],
            "character_anchors": sorted(profile["character_anchors"]),
            "scene_anchors": sorted(profile["scene_anchors"]),
        }
        for profile in frame_profiles
        if profile["frame_consistency_score"] < threshold
    ]

    return ConsistencyResult(
        score=int(max(1, min(100, total))),
        dimensions={
            "chapter_internal_character": int(round(character_score)),
            "chapter_internal_scene": int(round(scene_score)),
            "reference_adherence": int(round(reference_score)),
            "cross_chapter_style": int(round(cross_chapter_style)),
        },
        should_rework=total < threshold,
        details={
            "frame_count": len(frames),
            "neighbor_frame_count": len(neighbor_frames),
            "low_frames": low_frames[:8],
            "character_anchor_counts": _aggregate_anchor_counts(frame_profiles, "character_anchors"),
            "scene_anchor_counts": _aggregate_anchor_counts(frame_profiles, "scene_anchors"),
        },
    )


def _frame_profile(frame: dict[str, Any], characters: list[dict[str, Any]], scenes: list[dict[str, Any]]) -> dict[str, Any]:
    frame_text = " ".join(
        [
            str(frame.get("title") or ""),
            str(frame.get("summary") or ""),
            str(frame.get("visual") or ""),
            str(frame.get("action") or ""),
            str(frame.get("dialogue") or ""),
            str(frame.get("prompt") or ""),
        ]
    ).strip()
    character_anchors = _match_anchors(frame_text, characters)
    scene_anchors = _match_anchors(frame_text, scenes)
    image_signature = _image_signature(frame.get("storage_key"))
    reference_signal = _reference_signal(character_anchors, scene_anchors, image_signature)
    frame_consistency_score = round(
        max(
            40.0,
            min(
                98.0,
                55.0
                + min(len(character_anchors), 3) * 9.0
                + min(len(scene_anchors), 3) * 7.0
                + reference_signal * 0.14,
            ),
        )
    )
    return {
        "shot_index": int(frame.get("shot_index") or 0),
        "frame_text": frame_text,
        "character_anchors": character_anchors,
        "scene_anchors": scene_anchors,
        "image_signature": image_signature,
        "frame_consistency_score": int(frame_consistency_score),
    }


def _match_anchors(frame_text: str, anchors: list[dict[str, Any]]) -> set[str]:
    lowered = frame_text.lower()
    tokens = set(_tokenize(frame_text))
    matched: set[str] = set()
    for item in anchors:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        anchor_tokens = set(_tokenize(" ".join([name, str(item.get("description") or ""), str(item.get("visual_anchor") or "")])))
        if name.lower() in lowered or (anchor_tokens and tokens.intersection(anchor_tokens)):
            matched.add(name)
    return matched


def _adjacent_anchor_consistency(
    profiles: list[dict[str, Any]],
    field_name: str,
    *,
    empty_baseline: float,
) -> float:
    if len(profiles) <= 1:
        return 86.0
    scores: list[float] = []
    for current, nxt in zip(profiles, profiles[1:]):
        left = current[field_name]
        right = nxt[field_name]
        if not left and not right:
            scores.append(empty_baseline)
            continue
        if not left or not right:
            scores.append(empty_baseline - 14.0)
            continue
        union = len(left | right)
        jaccard = len(left & right) / union if union else 0.0
        scores.append(52.0 + jaccard * 46.0)
    return _mean(scores)


def _scene_consistency(profiles: list[dict[str, Any]]) -> float:
    if len(profiles) <= 1:
        return 84.0
    scores: list[float] = []
    for current, nxt in zip(profiles, profiles[1:]):
        scene_tokens = _pair_anchor_similarity(current["scene_anchors"], nxt["scene_anchors"], baseline=68.0)
        image_similarity = _image_similarity(current["image_signature"], nxt["image_signature"])
        scores.append(scene_tokens * 0.55 + image_similarity * 0.45)
    return _mean(scores)


def _reference_adherence(profiles: list[dict[str, Any]]) -> float:
    scores: list[float] = []
    for profile in profiles:
        anchor_count = len(profile["character_anchors"]) + len(profile["scene_anchors"])
        image_signal = profile["image_signature"].get("contrast_score", 60.0)
        if anchor_count == 0:
            scores.append(58.0 + image_signal * 0.15)
            continue
        scores.append(min(98.0, 60.0 + anchor_count * 8.0 + image_signal * 0.12))
    return _mean(scores)


def _cross_chapter_style(profiles: list[dict[str, Any]], neighbor_profiles: list[dict[str, Any]]) -> float:
    if not neighbor_profiles:
        intra_images = [profile["image_signature"] for profile in profiles if profile["image_signature"]]
        if len(intra_images) <= 1:
            return 80.0
        scores = [
            _image_similarity(left, right)
            for left, right in zip(intra_images, intra_images[1:])
        ]
        return _mean(scores)

    anchor_scores: list[float] = []
    image_scores: list[float] = []
    for profile in profiles:
        for neighbor in neighbor_profiles:
            anchor_scores.append(
                _pair_anchor_similarity(profile["character_anchors"] | profile["scene_anchors"], neighbor["character_anchors"] | neighbor["scene_anchors"], baseline=62.0)
            )
            image_scores.append(_image_similarity(profile["image_signature"], neighbor["image_signature"]))
    return _mean(anchor_scores) * 0.45 + _mean(image_scores) * 0.55


def _pair_anchor_similarity(left: set[str], right: set[str], *, baseline: float) -> float:
    if not left and not right:
        return baseline
    if not left or not right:
        return baseline - 18.0
    union = len(left | right)
    jaccard = len(left & right) / union if union else 0.0
    return 48.0 + jaccard * 50.0


def _aggregate_anchor_counts(profiles: list[dict[str, Any]], field_name: str) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for profile in profiles:
        for item in profile[field_name]:
            counts[item] = counts.get(item, 0) + 1
    return [
        {"name": name, "count": count}
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:8]
    ]


def _tokenize(text: str) -> list[str]:
    raw_tokens = re.findall(r"[A-Za-z]{2,}|[\u4e00-\u9fff]{2,8}", text.lower())
    stopwords = {
        "他们",
        "我们",
        "自己",
        "一个",
        "一些",
        "已经",
        "没有",
        "于是",
        "那里",
        "这里",
        "然后",
        "because",
        "there",
        "their",
        "with",
        "that",
    }
    return [token for token in raw_tokens if token not in stopwords]


def _reference_signal(character_anchors: set[str], scene_anchors: set[str], image_signature: dict[str, float]) -> float:
    anchor_bonus = min(30.0, len(character_anchors) * 8.0 + len(scene_anchors) * 6.0)
    return min(100.0, anchor_bonus + image_signature.get("contrast_score", 55.0))


def _image_signature(storage_key: Any) -> dict[str, float]:
    path = Path(str(storage_key or "")).expanduser()
    if not path.exists() or not path.is_file():
        return {"brightness": 50.0, "contrast_score": 55.0, "hash": ""}

    try:
        from PIL import Image, ImageStat
    except Exception:  # noqa: BLE001
        return {"brightness": 50.0, "contrast_score": 55.0, "hash": ""}

    try:
        image = Image.open(path).convert("L").resize((8, 8))
        pixels = list(image.getdata())
        mean_pixel = sum(pixels) / max(len(pixels), 1)
        bits = "".join("1" if pixel >= mean_pixel else "0" for pixel in pixels)
        contrast = ImageStat.Stat(image).stddev[0]
        return {
            "brightness": float(mean_pixel),
            "contrast_score": float(min(100.0, max(10.0, contrast * 4.2))),
            "hash": bits,
        }
    except Exception:  # noqa: BLE001
        return {"brightness": 50.0, "contrast_score": 55.0, "hash": ""}


def _image_similarity(left: dict[str, float], right: dict[str, float]) -> float:
    left_hash = str(left.get("hash") or "")
    right_hash = str(right.get("hash") or "")
    if not left_hash or not right_hash:
        brightness_delta = abs(float(left.get("brightness", 50.0)) - float(right.get("brightness", 50.0)))
        return max(52.0, 88.0 - brightness_delta * 0.8)
    distance = sum(1 for l, r in zip(left_hash, right_hash) if l != r)
    similarity = 1.0 - distance / max(len(left_hash), 1)
    brightness_delta = abs(float(left.get("brightness", 50.0)) - float(right.get("brightness", 50.0)))
    contrast_delta = abs(float(left.get("contrast_score", 55.0)) - float(right.get("contrast_score", 55.0)))
    penalty = min(24.0, brightness_delta * 0.18 + contrast_delta * 0.12)
    return max(40.0, min(98.0, 54.0 + similarity * 44.0 - penalty))


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)
