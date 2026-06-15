"""
TRON-X System API
------------------
All system-control endpoints: OS, files, browser, email, code execution.
"""
from __future__ import annotations
from fastapi import APIRouter, HTTPException, Body, Query
from pydantic import BaseModel, Field
from typing import Optional

from src.system.powershell import run_powershell, nl_to_powershell
from src.intelligence.router import get_router
from src.system import control, files, browser, executor
from src.system.email_client import send_email, compose_draft

router = APIRouter(prefix="/api/system", tags=["system"])


class VolumeReq(BaseModel):
    level: int = Field(..., ge=0, le=100)

class BrightnessReq(BaseModel):
    level: int = Field(..., ge=0, le=100)

class AppReq(BaseModel):
    name: str

class ScreenshotReq(BaseModel):
    save_path: Optional[str] = None

class FileSearchReq(BaseModel):
    query: str
    root: str = "."
    extensions: Optional[list[str]] = None
    max_results: int = 50

class FileReadReq(BaseModel):
    path: str
    max_chars: int = 8000

class FileOpReq(BaseModel):
    src: str
    dst: str
    confirm: bool = False

class FileDeleteReq(BaseModel):
    path: str
    confirm: bool = False

class FileCreateReq(BaseModel):
    path: str
    content: str = ""

class BrowserOpenReq(BaseModel):
    url: str

class BrowserSearchReq(BaseModel):
    query: str
    engine: str = "google"

class EmailReq(BaseModel):
    to: str
    subject: str
    body: str
    html: bool = False
    cc: Optional[list[str]] = None
    confirm: bool = False

class EmailDraftReq(BaseModel):
    to: str
    subject: str
    context: str
    tone: str = "professional"
    persona: str = "jarvis"

class CodeExecReq(BaseModel):
    code: str
    timeout: int = Field(default=15, ge=1, le=60)
    explain: bool = False
    persona: str = "jarvis"

class PowerShellReq(BaseModel):
    command: str
    timeout: int = Field(default=15, ge=1, le=30)

class NLCommandReq(BaseModel):
    query: str
    session_id: Optional[str] = None

class KillProcessReq(BaseModel):
    identifier: str

class StartProcessReq(BaseModel):
    path_or_name: str


# OS control

@router.post("/volume")
async def set_volume(req: VolumeReq):
    return await control.set_volume(req.level)

@router.post("/mute")
async def mute():
    return await control.mute()

@router.post("/brightness")
async def set_brightness(req: BrightnessReq):
    return await control.set_brightness(req.level)

@router.post("/app/open")
async def open_app(req: AppReq):
    return await control.open_app(req.name)

@router.post("/app/close")
async def close_app(req: AppReq):
    return await control.close_app(req.name)

@router.post("/screenshot")
async def take_screenshot(req: ScreenshotReq):
    return await control.take_screenshot(req.save_path)

@router.get("/info")
async def system_info():
    return await control.get_system_info()


# File system

@router.post("/files/search")
async def search_files(req: FileSearchReq):
    return await files.search_files(req.query, req.root, req.extensions, req.max_results)

@router.post("/files/read")
async def read_file(req: FileReadReq):
    return await files.read_file(req.path, req.max_chars)

@router.post("/files/list")
async def list_dir(path: str = Body(default=".", embed=True)):
    return await files.list_directory(path)

@router.post("/files/rename")
async def rename_file(req: FileOpReq):
    return await files.rename_file(req.src, req.dst, req.confirm)

@router.post("/files/copy")
async def copy_file(req: FileOpReq):
    return await files.copy_file(req.src, req.dst, req.confirm)

@router.post("/files/delete")
async def delete_file(req: FileDeleteReq):
    return await files.delete_file(req.path, req.confirm)

@router.post("/files/create")
async def create_file(req: FileCreateReq):
    return await files.create_file(req.path, req.content)

@router.get("/files/disk")
async def disk_usage(path: str = "."):
    return await files.get_disk_usage(path)


# Browser

@router.post("/browser/open")
async def browser_open(req: BrowserOpenReq):
    return await browser.open_url(req.url)

@router.post("/browser/screenshot")
async def browser_screenshot(req: BrowserOpenReq):
    return await browser.take_browser_screenshot(req.url)

@router.post("/browser/search")
async def browser_search(req: BrowserSearchReq):
    return await browser.search_web(req.query, req.engine)


# Email

@router.post("/email/send")
async def email_send(req: EmailReq):
    return await send_email(
        to=req.to, subject=req.subject, body=req.body,
        html=req.html, cc=req.cc, confirm=req.confirm,
    )

@router.post("/email/draft")
async def email_draft(req: EmailDraftReq):
    return await compose_draft(
        to=req.to, subject=req.subject, context=req.context,
        tone=req.tone, persona=req.persona,
    )


# Code execution

@router.post("/exec")
async def run_code(req: CodeExecReq):
    if req.explain:
        return await executor.execute_and_explain(req.code, req.persona)
    return await executor.execute_python(req.code, req.timeout)


# PowerShell / process / service (Phase 4)

@router.post("/powershell")
async def execute_powershell(req: PowerShellReq):
    return await run_powershell(req.command, req.timeout)

@router.post("/nl-command")
async def nl_command(req: NLCommandReq):
    return await nl_to_powershell(req.query, get_router())

@router.get("/processes")
async def list_processes(sort_by: str = Query(default="cpu")):
    return await control.list_processes(sort_by)

@router.post("/process/kill")
async def kill_process(req: KillProcessReq):
    return await control.kill_process(req.identifier)

@router.post("/process/start")
async def start_process(req: StartProcessReq):
    return await control.start_process(req.path_or_name)

@router.get("/services")
async def list_services(state: str = Query(default="all")):
    return await control.list_services(state)

@router.post("/service/{name}/{action}")
async def service_action(name: str, action: str):
    valid = {"start", "stop", "restart"}
    if action.lower() not in valid:
        raise HTTPException(status_code=400, detail=f"action must be one of {valid}")
    return await control.service_action(name, action)


# Phase 5 -- folder analysis, duplicates, batch ops, archives

class FolderSummaryReq(BaseModel):
    path: str

class FindDuplicatesReq(BaseModel):
    root: str
    extensions: Optional[list[str]] = None

class RenameBatchReq(BaseModel):
    root: str
    pattern: str
    template: str
    confirm: bool = False

class OrganizeFolderReq(BaseModel):
    root: str
    confirm: bool = False

class ArchiveReq(BaseModel):
    sources: list[str]
    dest: str
    confirm: bool = False

class ExtractReq(BaseModel):
    src: str
    dest: str
    confirm: bool = False


@router.post("/files/summary")
async def folder_summary(req: FolderSummaryReq):
    return await files.folder_summary(req.path)

@router.post("/files/duplicates")
async def find_duplicates(req: FindDuplicatesReq):
    return await files.find_duplicates(req.root, req.extensions)

@router.post("/files/rename-batch")
async def rename_batch(req: RenameBatchReq):
    return await files.rename_batch(req.root, req.pattern, req.template, req.confirm)

@router.post("/files/organize")
async def organize_folder(req: OrganizeFolderReq):
    return await files.organize_folder(req.root, req.confirm)

@router.post("/files/archive")
async def create_archive(req: ArchiveReq):
    return await files.create_archive(req.sources, req.dest, req.confirm)

@router.post("/files/extract")
async def extract_archive(req: ExtractReq):
    return await files.extract_archive(req.src, req.dest, req.confirm)


# Phase 6 -- persistent browser agent (BrowserAgent singleton)

from src.agents.browser_agent import BrowserAgent

class BrowserNavigateReq(BaseModel):
    url: str
    wait_until: str = "domcontentloaded"

class BrowserActionReq(BaseModel):
    action: str
    url: Optional[str] = None
    selector: Optional[str] = None
    fields: Optional[dict[str, str]] = None
    submit_selector: Optional[str] = None
    direction: str = "down"
    amount: int = 500
    query: Optional[str] = None
    save_path: Optional[str] = None
    return_base64: bool = False

class BrowserScreenshotReq(BaseModel):
    url: str
    save_path: Optional[str] = None
    return_base64: bool = False


@router.post("/browser/navigate")
async def browser_navigate_persistent(req: BrowserNavigateReq):
    agent = await BrowserAgent.get()
    return await agent.navigate(req.url, req.wait_until)

@router.post("/browser/action")
async def browser_action(req: BrowserActionReq):
    agent = await BrowserAgent.get()
    kwargs = {k: v for k, v in req.model_dump().items() if v is not None and k != "action"}
    return await agent.action(req.action, **kwargs)

@router.post("/browser/screenshot/v2")
async def browser_screenshot_v2(req: BrowserScreenshotReq):
    agent = await BrowserAgent.get()
    return await agent.screenshot(req.url, req.save_path, req.return_base64)

@router.delete("/browser/session")
async def browser_session_shutdown():
    inst = BrowserAgent._instance
    if inst is not None:
        await inst.stop()
    return {"stopped": True}


# Phase 7 -- language-specific execution endpoints

class PyExecReq(BaseModel):
    code: str
    timeout: int = Field(default=15, ge=1, le=60)
    auto_install: bool = True

class JsExecReq(BaseModel):
    code: str
    timeout: int = Field(default=15, ge=1, le=30)

class BashExecReq(BaseModel):
    code: str
    timeout: int = Field(default=15, ge=1, le=30)


@router.post("/execute/python")
async def execute_python_safe(req: PyExecReq):
    return await executor.execute_python_safe(req.code, req.timeout, req.auto_install)

@router.post("/execute/js")
async def execute_js(req: JsExecReq):
    return await executor.execute_js(req.code, req.timeout)

@router.post("/execute/bash")
async def execute_bash(req: BashExecReq):
    return await executor.execute_bash(req.code, req.timeout)


# Phase 8 -- screen capture, OCR, vision description

from src.vision import screen as vision_screen

class ScreenCaptureReq(BaseModel):
    save_path: Optional[str] = None
    region: Optional[dict] = None
    monitor: int = 1
    return_base64: bool = False

class WindowCaptureReq(BaseModel):
    title: str
    save_path: Optional[str] = None
    return_base64: bool = False

class OCRReq(BaseModel):
    path: str
    engine: str = "auto"

class OCRScreenReq(BaseModel):
    region: Optional[dict] = None
    engine: str = "auto"

class DescribeScreenReq(BaseModel):
    region: Optional[dict] = None
    prompt: str = "Describe what you see on this screen in detail."
    return_base64: bool = False

class DescribeImageReq(BaseModel):
    path: str
    prompt: str = "Describe this image in detail."


@router.post("/vision/screenshot")
async def vision_screenshot(req: ScreenCaptureReq):
    return await vision_screen.capture_screen(
        req.save_path, req.region, req.monitor, req.return_base64
    )

@router.post("/vision/screenshot/window")
async def vision_screenshot_window(req: WindowCaptureReq):
    return await vision_screen.capture_window(req.title, req.save_path, req.return_base64)

@router.post("/vision/ocr")
async def vision_ocr(req: OCRReq):
    return await vision_screen.ocr_image(req.path, req.engine)

@router.post("/vision/ocr/screen")
async def vision_ocr_screen(req: OCRScreenReq):
    return await vision_screen.ocr_screen(req.region, req.engine)

@router.post("/vision/describe")
async def vision_describe(req: DescribeScreenReq):
    return await vision_screen.describe_screen(req.region, req.prompt, req.return_base64)

@router.post("/vision/describe/image")
async def vision_describe_image(req: DescribeImageReq):
    return await vision_screen.describe_image(req.path, req.prompt)
