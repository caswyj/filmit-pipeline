from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ChapterChunk, ModelRun, Project, PromptVersion, ReviewAction
from app.services.style_service import normalize_style_profile

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]{2,}")


class AgentRetrievalService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def search_project_knowledge(self, project: Project, query: str, limit: int = 6) -> list[dict[str, Any]]:
        query = str(query or "").strip()
        if not query:
            return []
        query_lower = query.lower()
        tokens = self._tokenize(query)
        candidates: list[dict[str, Any]] = []

        for item in self._story_bible_candidates(project):
            score = self._score_text(query_lower, tokens, item["text"])
            if score > 0:
                candidates.append({**item, "score": score})

        for chapter in self.db.scalars(
            select(ChapterChunk).where(ChapterChunk.project_id == project.id).order_by(ChapterChunk.chapter_index.asc())
        ).all():
            title = str((chapter.meta or {}).get("title") or f"章节 {chapter.chapter_index + 1}")
            excerpt = self._clip(chapter.content)
            score = self._score_text(query_lower, tokens, f"{title}\n{chapter.content}")
            if score > 0:
                candidates.append(
                    {
                        "kind": "chapter",
                        "title": title,
                        "snippet": excerpt,
                        "source_ref": chapter.id,
                        "score": score,
                    }
                )

        for prompt in self.db.scalars(
            select(PromptVersion)
            .where(PromptVersion.project_id == project.id)
            .order_by(PromptVersion.created_at.desc())
            .limit(12)
        ).all():
            text = "\n".join(filter(None, [prompt.system_prompt, prompt.task_prompt]))
            score = self._score_text(query_lower, tokens, text)
            if score > 0:
                candidates.append(
                    {
                        "kind": "prompt_version",
                        "title": f"{prompt.step_name} 提示词版本",
                        "snippet": self._clip(text),
                        "source_ref": prompt.id,
                        "score": score,
                    }
                )

        for review in self.db.scalars(
            select(ReviewAction)
            .where(ReviewAction.project_id == project.id)
            .order_by(ReviewAction.created_at.desc())
            .limit(18)
        ).all():
            text = json.dumps(review.editor_payload or {}, ensure_ascii=False)
            if review.action_type:
                text = f"{review.action_type}\n{text}"
            score = self._score_text(query_lower, tokens, text)
            if score > 0:
                candidates.append(
                    {
                        "kind": "review_action",
                        "title": f"{review.action_type} 审核记录",
                        "snippet": self._clip(text),
                        "source_ref": review.id,
                        "score": score,
                    }
                )

        for run in self.db.scalars(
            select(ModelRun).where(ModelRun.project_id == project.id).order_by(ModelRun.created_at.desc()).limit(12)
        ).all():
            text = json.dumps(
                {
                    "step_name": run.step_name,
                    "request_summary": run.request_summary,
                    "response_summary": run.response_summary,
                },
                ensure_ascii=False,
            )
            score = self._score_text(query_lower, tokens, text)
            if score > 0:
                candidates.append(
                    {
                        "kind": "model_run",
                        "title": f"{run.step_name} 模型运行",
                        "snippet": self._clip(text),
                        "source_ref": run.id,
                        "score": score,
                    }
                )

        deduped: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for item in sorted(candidates, key=lambda raw: (-int(raw["score"]), raw["kind"], raw["title"])):
            marker = (str(item["kind"]), str(item["source_ref"]))
            if marker in seen:
                continue
            seen.add(marker)
            deduped.append({key: value for key, value in item.items() if key != "text"})
            if len(deduped) >= limit:
                break
        return deduped

    def _story_bible_candidates(self, project: Project) -> list[dict[str, Any]]:
        profile = normalize_style_profile(project.style_profile)
        story_bible = profile.get("story_bible", {}) if isinstance(profile, dict) else {}
        candidates: list[dict[str, Any]] = []
        for group_name, label in (("characters", "人物"), ("scenes", "场景")):
            for item in story_bible.get(group_name, []) or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                description = str(item.get("description") or item.get("visual_anchor") or "").strip()
                text = f"{name}\n{description}"
                candidates.append(
                    {
                        "kind": "story_bible",
                        "title": f"{label}: {name}",
                        "snippet": self._clip(description or name),
                        "source_ref": name,
                        "text": text,
                    }
                )
        visual_style = story_bible.get("visual_style", {}) if isinstance(story_bible, dict) else {}
        if isinstance(visual_style, dict):
            text = json.dumps(visual_style, ensure_ascii=False)
            candidates.append(
                {
                    "kind": "story_bible",
                    "title": "Story Bible 视觉风格",
                    "snippet": self._clip(text),
                    "source_ref": "visual_style",
                    "text": text,
                }
            )
        return candidates

    def _tokenize(self, text: str) -> list[str]:
        tokens = [item.group(0).lower() for item in TOKEN_PATTERN.finditer(text)]
        if not tokens and text.strip():
            return [text.strip().lower()]
        return tokens

    def _score_text(self, query_lower: str, tokens: list[str], content: str) -> int:
        content_lower = str(content or "").lower()
        if not content_lower:
            return 0
        score = 0
        if query_lower in content_lower:
            score += 12
        for token in tokens:
            if token and token in content_lower:
                score += 3 + min(len(token), 6)
        return score

    def _clip(self, value: str, limit: int = 240) -> str:
        value = str(value or "").strip()
        return value if len(value) <= limit else f"{value[:limit]}..."
