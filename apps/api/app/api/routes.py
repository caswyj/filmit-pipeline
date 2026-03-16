from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi import File, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import AgentSessionService
from app.db.models import Project, SourceDocument
from app.schemas.agent import AgentMessageRead, AgentSendMessagePayload, AgentSessionRead, AgentTurnRead, AgentRunRead
from app.schemas.chapter import ChapterRead
from app.schemas.demo import DemoCaseRead, DemoImportPayload
from app.db.session import get_db
from app.schemas.document import SourceDocumentRead
from app.schemas.project import ModelBindingPayload, ProjectCreate, ProjectRead, ProjectTimelineRead, ProjectUpdate
from app.schemas.prompt import PromptTemplateRead
from app.schemas.provider import ProviderModelRead
from app.schemas.review import (
    ApprovePayload,
    EditContinuePayload,
    EditPromptRegeneratePayload,
    SwitchModelRerunPayload,
)
from app.schemas.style import StylePresetRead
from app.schemas.step import AssetRead, ExportRead, ProjectRunResponse, StepRead, StepRunPayload
from app.schemas.step import BatchStepRunPayload, BatchStepRunResponse
from app.schemas.storyboard import SelectStoryboardVersionPayload, StoryboardVersionRead
from app.services.demo_service import get_demo_case, list_demo_cases
from app.services.pipeline_service import PipelineService
from app.services.prompt_service import list_prompt_templates
from app.services.storage_service import project_category_dir, storage_root
from app.services.style_service import list_style_presets, normalize_style_profile

router = APIRouter(prefix="/api/v1", tags=["v1"])
GENERATED_DIR = storage_root()


def get_service(db: Session = Depends(get_db)) -> PipelineService:
    return PipelineService(db)


def get_agent_service(db: Session = Depends(get_db)) -> AgentSessionService:
    return AgentSessionService(db)


def _get_project_or_404(db: Session, project_id: str) -> Project:
    project = db.scalar(select(Project).where(Project.id == project_id))
    if not project:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    return project


def _register_source_document(
    db: Session,
    project: Project,
    *,
    file_name: str,
    content: bytes,
    parse_status: str = "UPLOADED",
) -> SourceDocument:
    suffix = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    if suffix not in {"pdf", "txt"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="only pdf/txt files are supported")

    upload_dir = project_category_dir(project.id, project.name, "sources")
    storage_key = str(upload_dir / file_name)
    with open(storage_key, "wb") as fp:
        fp.write(content)

    doc = SourceDocument(
        project_id=project.id,
        file_name=file_name,
        file_type=suffix,
        storage_key=storage_key,
        parse_status=parse_status,
        page_map={},
    )
    db.add(doc)
    project.input_path = storage_key
    db.add(project)
    db.commit()
    db.refresh(doc)
    db.refresh(project)
    return doc


@router.post("/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db), svc: PipelineService = Depends(get_service)) -> Project:
    project = Project(
        name=payload.name,
        description=payload.description,
        target_duration_sec=payload.target_duration_sec,
        input_path=payload.input_path,
        output_path=payload.output_path,
        style_profile=normalize_style_profile(payload.style_profile),
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    svc.ensure_pipeline_steps(project)
    db.refresh(project)
    return project


@router.get("/projects/{project_id}", response_model=ProjectRead)
def get_project(project_id: str, db: Session = Depends(get_db)) -> Project:
    return _get_project_or_404(db, project_id)


@router.patch("/projects/{project_id}", response_model=ProjectRead)
def update_project(project_id: str, payload: ProjectUpdate, db: Session = Depends(get_db)) -> Project:
    project = _get_project_or_404(db, project_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        if key == "style_profile":
            value = normalize_style_profile(value)
        setattr(project, key, value)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.get("/projects", response_model=list[ProjectRead])
def list_projects(db: Session = Depends(get_db)) -> list[Project]:
    return list(db.scalars(select(Project).order_by(Project.created_at.desc())).all())


@router.get("/demo-cases", response_model=list[DemoCaseRead])
def get_demo_cases() -> list[DemoCaseRead]:
    return [DemoCaseRead(**item) for item in list_demo_cases()]


@router.get("/style-presets", response_model=list[StylePresetRead])
def get_style_presets() -> list[StylePresetRead]:
    return [StylePresetRead(**item) for item in list_style_presets()]


@router.post("/demo-cases/{demo_id}/import", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def import_demo_case(
    demo_id: str,
    payload: DemoImportPayload,
    db: Session = Depends(get_db),
    svc: PipelineService = Depends(get_service),
) -> Project:
    try:
        demo = get_demo_case(demo_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    if not demo.source_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"demo source missing: {demo.source_path}")

    project = Project(
        name=payload.name or demo.recommended_project_name,
        description=f"Imported demo case: {demo.title}",
        target_duration_sec=payload.target_duration_sec or demo.target_duration_sec,
        style_profile=normalize_style_profile({"demo_case": demo.id, "preset_id": "gloom_noir"}),
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    svc.ensure_pipeline_steps(project)
    _register_source_document(
        db,
        project,
        file_name=demo.file_name,
        content=demo.source_path.read_bytes(),
        parse_status="IMPORTED_DEMO",
    )
    db.refresh(project)
    return project


@router.post("/projects/{project_id}/source-documents", response_model=SourceDocumentRead, status_code=status.HTTP_201_CREATED)
async def upload_source_document(
    project_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> SourceDocumentRead:
    project = _get_project_or_404(db, project_id)
    content = await file.read()
    doc = _register_source_document(
        db,
        project,
        file_name=file.filename or "unknown.txt",
        content=content,
    )
    return SourceDocumentRead.model_validate(doc)


@router.get("/projects/{project_id}/source-documents", response_model=list[SourceDocumentRead])
def list_source_documents(project_id: str, db: Session = Depends(get_db)) -> list[SourceDocumentRead]:
    _get_project_or_404(db, project_id)
    docs = list(
        db.scalars(
            select(SourceDocument)
            .where(SourceDocument.project_id == project_id)
            .order_by(SourceDocument.created_at.desc())
        ).all()
    )
    return [SourceDocumentRead.model_validate(item) for item in docs]


@router.get("/projects/{project_id}/chapters", response_model=list[ChapterRead])
def list_project_chapters(project_id: str, db: Session = Depends(get_db), svc: PipelineService = Depends(get_service)) -> list[ChapterRead]:
    _get_project_or_404(db, project_id)
    return [ChapterRead(**item) for item in svc.list_chapters(project_id)]


@router.post("/projects/{project_id}/model-bindings", response_model=ProjectRead)
def bind_models(
    project_id: str,
    payload: ModelBindingPayload,
    db: Session = Depends(get_db),
    svc: PipelineService = Depends(get_service),
) -> Project:
    project = _get_project_or_404(db, project_id)
    return svc.apply_model_bindings(project, payload.bindings)


@router.post("/projects/{project_id}/story-bible/rebuild", response_model=ProjectRead)
async def rebuild_story_bible(
    project_id: str,
    db: Session = Depends(get_db),
    svc: PipelineService = Depends(get_service),
) -> ProjectRead:
    project = _get_project_or_404(db, project_id)
    try:
        updated = await svc.rebuild_story_bible_references(project)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return ProjectRead.model_validate(updated)


@router.get("/projects/{project_id}/agent/sessions/default", response_model=AgentSessionRead)
def get_default_agent_session(
    project_id: str,
    db: Session = Depends(get_db),
    agent_svc: AgentSessionService = Depends(get_agent_service),
) -> AgentSessionRead:
    project = _get_project_or_404(db, project_id)
    session = agent_svc.get_or_create_default_session(project)
    return AgentSessionRead.model_validate(session)


@router.get("/projects/{project_id}/agent/sessions/default/messages", response_model=list[AgentMessageRead])
def list_default_agent_messages(
    project_id: str,
    db: Session = Depends(get_db),
    agent_svc: AgentSessionService = Depends(get_agent_service),
) -> list[AgentMessageRead]:
    project = _get_project_or_404(db, project_id)
    messages = agent_svc.list_messages(project)
    return [AgentMessageRead.model_validate(item) for item in messages]


@router.post("/projects/{project_id}/agent/sessions/default/messages", response_model=AgentTurnRead)
async def send_message_to_default_agent_session(
    project_id: str,
    payload: AgentSendMessagePayload,
    db: Session = Depends(get_db),
    agent_svc: AgentSessionService = Depends(get_agent_service),
) -> AgentTurnRead:
    project = _get_project_or_404(db, project_id)
    result = await agent_svc.send_message(project, payload.message, page_context=payload.page_context)
    run = result["run"]
    return AgentTurnRead(
        session=AgentSessionRead.model_validate(result["session"]),
        user_message=AgentMessageRead.model_validate(result["user_message"]),
        assistant_message=AgentMessageRead.model_validate(result["assistant_message"]),
        run=AgentRunRead.model_validate(run),
    )


@router.get("/providers/models", response_model=list[ProviderModelRead])
def list_provider_models(svc: PipelineService = Depends(get_service)) -> list[ProviderModelRead]:
    return [ProviderModelRead(**item) for item in svc.list_provider_catalog()]


@router.get("/prompt-templates", response_model=list[PromptTemplateRead])
def get_prompt_templates(step_name: str | None = None) -> list[PromptTemplateRead]:
    return [PromptTemplateRead(**item) for item in list_prompt_templates(step_name)]


@router.post("/projects/{project_id}/run", response_model=ProjectRunResponse)
async def run_project(
    project_id: str,
    db: Session = Depends(get_db),
    svc: PipelineService = Depends(get_service),
) -> ProjectRunResponse:
    project = _get_project_or_404(db, project_id)
    try:
        current = await svc.run_project(project)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    db.refresh(project)
    return ProjectRunResponse(
        project_id=project.id,
        status=project.status,
        current_step=StepRead.model_validate(current) if current else None,
    )


@router.post("/projects/{project_id}/steps/{step_name}/run", response_model=StepRead)
async def run_specific_step(
    project_id: str,
    step_name: str,
    payload: StepRunPayload,
    db: Session = Depends(get_db),
    svc: PipelineService = Depends(get_service),
) -> StepRead:
    project = _get_project_or_404(db, project_id)
    try:
        params = dict(payload.params)
        if payload.chapter_id:
            params["chapter_id"] = payload.chapter_id
        step = await svc.run_specific_step(project, step_name, force=payload.force, params=params)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return StepRead.model_validate(step)


@router.post("/projects/{project_id}/steps/{step_name}/run-all-chapters", response_model=BatchStepRunResponse)
async def run_step_for_all_chapters(
    project_id: str,
    step_name: str,
    payload: BatchStepRunPayload,
    db: Session = Depends(get_db),
    svc: PipelineService = Depends(get_service),
) -> BatchStepRunResponse:
    project = _get_project_or_404(db, project_id)
    try:
        result = await svc.run_step_for_all_chapters(project, step_name, force=payload.force, params=payload.params)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    current_step = result.get("current_step")
    result["current_step"] = StepRead.model_validate(current_step) if current_step else None
    return BatchStepRunResponse.model_validate(result)


@router.post("/projects/{project_id}/steps/{step_name}/run-failed-chapters", response_model=BatchStepRunResponse)
async def run_step_for_failed_chapters(
    project_id: str,
    step_name: str,
    payload: BatchStepRunPayload,
    db: Session = Depends(get_db),
    svc: PipelineService = Depends(get_service),
) -> BatchStepRunResponse:
    project = _get_project_or_404(db, project_id)
    try:
        result = await svc.run_step_for_failed_chapters(project, step_name, force=payload.force, params=payload.params)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    current_step = result.get("current_step")
    result["current_step"] = StepRead.model_validate(current_step) if current_step else None
    return BatchStepRunResponse.model_validate(result)


@router.get("/projects/{project_id}/steps", response_model=list[StepRead])
def get_steps(project_id: str, db: Session = Depends(get_db), svc: PipelineService = Depends(get_service)) -> list[StepRead]:
    _get_project_or_404(db, project_id)
    return [StepRead.model_validate(item) for item in svc.list_steps(project_id)]


@router.get("/projects/{project_id}/timeline", response_model=ProjectTimelineRead)
def get_timeline(
    project_id: str,
    db: Session = Depends(get_db),
    svc: PipelineService = Depends(get_service),
) -> ProjectTimelineRead:
    project = _get_project_or_404(db, project_id)
    return ProjectTimelineRead.model_validate(svc.project_timeline(project))


@router.post("/projects/{project_id}/steps/{step_id}/approve", response_model=ProjectRunResponse)
async def approve_step(
    project_id: str,
    step_id: str,
    payload: ApprovePayload,
    db: Session = Depends(get_db),
    svc: PipelineService = Depends(get_service),
) -> ProjectRunResponse:
    project = _get_project_or_404(db, project_id)
    try:
        current = await svc.approve_step(project, step_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    db.refresh(project)
    return ProjectRunResponse(
        project_id=project.id,
        status=project.status,
        current_step=StepRead.model_validate(current) if current else None,
    )


@router.post("/projects/{project_id}/steps/{step_id}/approve-all-chapters", response_model=BatchStepRunResponse)
async def approve_all_chapters(
    project_id: str,
    step_id: str,
    payload: ApprovePayload,
    db: Session = Depends(get_db),
    svc: PipelineService = Depends(get_service),
) -> BatchStepRunResponse:
    project = _get_project_or_404(db, project_id)
    try:
        result = await svc.approve_step_for_all_chapters(project, step_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    current_step = result.get("current_step")
    result["current_step"] = StepRead.model_validate(current_step) if current_step else None
    return BatchStepRunResponse.model_validate(result)


@router.post("/projects/{project_id}/steps/{step_id}/approve-review-required-chapters", response_model=BatchStepRunResponse)
async def approve_review_required_consistency_chapters(
    project_id: str,
    step_id: str,
    payload: ApprovePayload,
    db: Session = Depends(get_db),
    svc: PipelineService = Depends(get_service),
) -> BatchStepRunResponse:
    project = _get_project_or_404(db, project_id)
    try:
        result = await svc.approve_review_required_consistency_chapters(project, step_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    current_step = result.get("current_step")
    result["current_step"] = StepRead.model_validate(current_step) if current_step else None
    return BatchStepRunResponse.model_validate(result)


@router.post("/projects/{project_id}/steps/{step_id}/approve-failed-chapters", response_model=BatchStepRunResponse)
async def approve_failed_chapters(
    project_id: str,
    step_id: str,
    payload: ApprovePayload,
    db: Session = Depends(get_db),
    svc: PipelineService = Depends(get_service),
) -> BatchStepRunResponse:
    project = _get_project_or_404(db, project_id)
    try:
        result = await svc.approve_failed_step_for_all_chapters(project, step_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    current_step = result.get("current_step")
    result["current_step"] = StepRead.model_validate(current_step) if current_step else None
    return BatchStepRunResponse.model_validate(result)


@router.post("/projects/{project_id}/steps/{step_id}/rerun-pending-chapters", response_model=BatchStepRunResponse)
async def rerun_pending_chapters(
    project_id: str,
    step_id: str,
    payload: ApprovePayload,
    db: Session = Depends(get_db),
    svc: PipelineService = Depends(get_service),
) -> BatchStepRunResponse:
    project = _get_project_or_404(db, project_id)
    try:
        result = await svc.rerun_pending_step_for_all_chapters(project, step_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    current_step = result.get("current_step")
    result["current_step"] = StepRead.model_validate(current_step) if current_step else None
    return BatchStepRunResponse.model_validate(result)


@router.post("/projects/{project_id}/steps/{step_id}/rework-regenerate-rescore-chapters", response_model=BatchStepRunResponse)
async def rework_regenerate_rescore_chapters(
    project_id: str,
    step_id: str,
    payload: ApprovePayload,
    db: Session = Depends(get_db),
    svc: PipelineService = Depends(get_service),
) -> BatchStepRunResponse:
    project = _get_project_or_404(db, project_id)
    try:
        result = await svc.regenerate_rework_requested_consistency_chapters(project, step_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    current_step = result.get("current_step")
    result["current_step"] = StepRead.model_validate(current_step) if current_step else None
    return BatchStepRunResponse.model_validate(result)


@router.post("/projects/{project_id}/steps/{step_id}/edit-continue", response_model=ProjectRunResponse)
async def edit_continue(
    project_id: str,
    step_id: str,
    payload: EditContinuePayload,
    db: Session = Depends(get_db),
    svc: PipelineService = Depends(get_service),
) -> ProjectRunResponse:
    project = _get_project_or_404(db, project_id)
    try:
        current = await svc.edit_continue(project, step_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    db.refresh(project)
    return ProjectRunResponse(
        project_id=project.id,
        status=project.status,
        current_step=StepRead.model_validate(current) if current else None,
    )


@router.post("/projects/{project_id}/steps/{step_id}/edit-continue-all-chapters", response_model=BatchStepRunResponse)
async def edit_continue_all_chapters(
    project_id: str,
    step_id: str,
    payload: EditContinuePayload,
    db: Session = Depends(get_db),
    svc: PipelineService = Depends(get_service),
) -> BatchStepRunResponse:
    project = _get_project_or_404(db, project_id)
    try:
        result = await svc.edit_continue_for_all_chapters(project, step_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    current_step = result.get("current_step")
    result["current_step"] = StepRead.model_validate(current_step) if current_step else None
    return BatchStepRunResponse.model_validate(result)


@router.post("/projects/{project_id}/steps/{step_id}/edit-prompt-regenerate", response_model=StepRead)
async def edit_prompt_regenerate(
    project_id: str,
    step_id: str,
    payload: EditPromptRegeneratePayload,
    db: Session = Depends(get_db),
    svc: PipelineService = Depends(get_service),
) -> StepRead:
    project = _get_project_or_404(db, project_id)
    try:
        step = await svc.edit_prompt_regenerate(project, step_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return StepRead.model_validate(step)


@router.post("/projects/{project_id}/steps/{step_id}/edit-prompt-regenerate-all-chapters", response_model=BatchStepRunResponse)
async def edit_prompt_regenerate_all_chapters(
    project_id: str,
    step_id: str,
    payload: EditPromptRegeneratePayload,
    db: Session = Depends(get_db),
    svc: PipelineService = Depends(get_service),
) -> BatchStepRunResponse:
    project = _get_project_or_404(db, project_id)
    try:
        result = await svc.edit_prompt_regenerate_for_all_chapters(project, step_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    current_step = result.get("current_step")
    result["current_step"] = StepRead.model_validate(current_step) if current_step else None
    return BatchStepRunResponse.model_validate(result)


@router.post("/projects/{project_id}/steps/{step_id}/switch-model-rerun", response_model=StepRead)
async def switch_model_rerun(
    project_id: str,
    step_id: str,
    payload: SwitchModelRerunPayload,
    db: Session = Depends(get_db),
    svc: PipelineService = Depends(get_service),
) -> StepRead:
    project = _get_project_or_404(db, project_id)
    try:
        step = await svc.switch_model_rerun(project, step_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return StepRead.model_validate(step)


@router.post("/projects/{project_id}/steps/{step_id}/switch-model-rerun-all-chapters", response_model=BatchStepRunResponse)
async def switch_model_rerun_all_chapters(
    project_id: str,
    step_id: str,
    payload: SwitchModelRerunPayload,
    db: Session = Depends(get_db),
    svc: PipelineService = Depends(get_service),
) -> BatchStepRunResponse:
    project = _get_project_or_404(db, project_id)
    try:
        result = await svc.switch_model_rerun_for_all_chapters(project, step_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    current_step = result.get("current_step")
    result["current_step"] = StepRead.model_validate(current_step) if current_step else None
    return BatchStepRunResponse.model_validate(result)


@router.get("/projects/{project_id}/assets", response_model=list[AssetRead])
def list_assets(project_id: str, db: Session = Depends(get_db), svc: PipelineService = Depends(get_service)) -> list[AssetRead]:
    _get_project_or_404(db, project_id)
    return [AssetRead.model_validate(item) for item in svc.list_assets(project_id)]


@router.get("/projects/{project_id}/steps/{step_id}/storyboard-versions", response_model=list[StoryboardVersionRead])
def list_storyboard_versions(
    project_id: str,
    step_id: str,
    chapter_id: str | None = None,
    db: Session = Depends(get_db),
    svc: PipelineService = Depends(get_service),
) -> list[StoryboardVersionRead]:
    _get_project_or_404(db, project_id)
    try:
        versions = svc.list_storyboard_versions(project_id, step_id, chapter_id=chapter_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return [StoryboardVersionRead.model_validate(item) for item in versions]


@router.post(
    "/projects/{project_id}/steps/{step_id}/storyboard-versions/{version_id}/select",
    response_model=StepRead,
)
def select_storyboard_version(
    project_id: str,
    step_id: str,
    version_id: str,
    payload: SelectStoryboardVersionPayload,
    db: Session = Depends(get_db),
    svc: PipelineService = Depends(get_service),
) -> StepRead:
    project = _get_project_or_404(db, project_id)
    try:
        step = svc.select_storyboard_version(project, step_id, version_id, payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return StepRead.model_validate(step)


@router.post("/projects/{project_id}/render/final", response_model=ExportRead)
async def render_final(project_id: str, db: Session = Depends(get_db), svc: PipelineService = Depends(get_service)) -> ExportRead:
    project = _get_project_or_404(db, project_id)
    try:
        export_job = await svc.render_final(project)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return ExportRead.model_validate(export_job)


@router.post("/projects/{project_id}/final-cut", response_model=ExportRead)
async def generate_final_cut(project_id: str, db: Session = Depends(get_db), svc: PipelineService = Depends(get_service)) -> ExportRead:
    project = _get_project_or_404(db, project_id)
    try:
        export_job = await svc.generate_final_cut(project, force=True)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return ExportRead.model_validate(export_job)


@router.get("/projects/{project_id}/exports/{export_id}", response_model=ExportRead)
def get_export(
    project_id: str,
    export_id: str,
    db: Session = Depends(get_db),
    svc: PipelineService = Depends(get_service),
) -> ExportRead:
    _get_project_or_404(db, project_id)
    try:
        export_job = svc.get_export(project_id, export_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return ExportRead.model_validate(export_job)


@router.get("/local-files/{file_path:path}")
def get_local_generated_file(file_path: str, download: bool = False) -> FileResponse:
    target = (GENERATED_DIR / file_path).resolve()
    if not str(target).startswith(str(GENERATED_DIR)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid file path")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="file not found")
    if download:
        return FileResponse(target, filename=target.name)
    return FileResponse(target)
