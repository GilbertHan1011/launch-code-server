from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
import json

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ui.diagnostics import run_all_diagnostics
from ui.storage import (
    add_run,
    build_profile_from_form,
    default_profile,
    effective_profile,
    env_detection,
    get_profile,
    list_profiles,
    list_runs,
    now_iso,
    save_profile,
    delete_profile,
)
from ui.tasks import create_launch_task, launch_background, list_tasks, refresh_task, stop_task, load_task, submit_task_input
from ui.launcher import build_launch_command, render_command

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Launch Server UI")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

_latest_results: List[Dict[str, Any]] = []


def _display_name(profile: Dict[str, Any]) -> str:
    return profile.get("name") or profile.get("destination") or profile.get("hpc_real_host") or "Profile"


def _prepare_profiles() -> List[Dict[str, Any]]:
    profiles = []
    for profile in list_profiles():
        normalized = dict(profile)
        normalized["name"] = _display_name(normalized)
        profiles.append(normalized)
    return profiles


def _resolve_profile(profile_id: Optional[str]) -> Optional[Dict[str, Any]]:
    profiles = _prepare_profiles()
    if profile_id:
        for profile in profiles:
            if profile.get("id") == profile_id:
                return profile
    return profiles[0] if profiles else None


@app.get("/", response_class=HTMLResponse)
def root() -> RedirectResponse:
    return RedirectResponse(url="/profiles")


@app.get("/profiles", response_class=HTMLResponse)
def profiles(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "profiles.jinja2",
        {
            "request": request,
            "title": "Profiles",
            "active": "profiles",
            "profiles": _prepare_profiles(),
        },
    )


@app.get("/profiles/new", response_class=HTMLResponse)
def profile_new(request: Request) -> HTMLResponse:
    profile = default_profile()
    return templates.TemplateResponse(
        "profile_form.jinja2",
        {
            "request": request,
            "title": "New profile",
            "active": "profiles",
            "profile": profile,
            "effective": effective_profile(profile),
            "env_detected": env_detection(),
            "action": "/profiles",
        },
    )


@app.post("/profiles", response_class=HTMLResponse)
async def profile_create(request: Request) -> RedirectResponse:
    form = dict(await request.form())
    profile = build_profile_from_form(form)
    save_profile(profile)
    return RedirectResponse(url="/profiles", status_code=303)


@app.get("/profiles/{profile_id}/edit", response_class=HTMLResponse)
def profile_edit(request: Request, profile_id: str) -> HTMLResponse:
    profile = get_profile(profile_id) or default_profile()
    return templates.TemplateResponse(
        "profile_form.jinja2",
        {
            "request": request,
            "title": "Edit profile",
            "active": "profiles",
            "profile": profile,
            "effective": effective_profile(profile),
            "env_detected": env_detection(),
            "action": f"/profiles/{profile_id}",
        },
    )


@app.post("/profiles/{profile_id}", response_class=HTMLResponse)
async def profile_update(request: Request, profile_id: str) -> RedirectResponse:
    form = dict(await request.form())
    profile = build_profile_from_form(form, profile_id=profile_id)
    save_profile(profile)
    return RedirectResponse(url="/profiles", status_code=303)


@app.post("/profiles/{profile_id}/delete", response_class=HTMLResponse)
def profile_remove(profile_id: str) -> RedirectResponse:
    delete_profile(profile_id)
    return RedirectResponse(url="/profiles", status_code=303)


@app.get("/profiles/{profile_id}/export", response_class=JSONResponse)
def profile_export(profile_id: str) -> JSONResponse:
    profile = get_profile(profile_id)
    if not profile:
        return JSONResponse({"error": "profile not found"}, status_code=404)
    return JSONResponse(profile)


@app.post("/profiles/import", response_class=HTMLResponse)
async def profile_import(request: Request) -> RedirectResponse:
    form = dict(await request.form())
    raw = (form.get("profile_json") or "").strip()
    if not raw:
        return RedirectResponse(url="/profiles", status_code=303)
    data = json.loads(raw)
    if isinstance(data, dict):
        profile = build_profile_from_form(data, profile_id=data.get("id"))
        if data.get("name"):
            profile["name"] = data["name"]
        save_profile(profile)
    return RedirectResponse(url="/profiles", status_code=303)


@app.get("/diagnostics", response_class=HTMLResponse)
def diagnostics(request: Request) -> HTMLResponse:
    profiles = _prepare_profiles()
    active_profile = profiles[0] if profiles else None
    return templates.TemplateResponse(
        "diagnostics.jinja2",
        {
            "request": request,
            "title": "Diagnostics",
            "active": "diagnostics",
            "profiles": profiles,
            "active_profile": active_profile,
            "results": _latest_results,
            "runs": list_runs(),
        },
    )


@app.post("/diagnostics/run", response_class=HTMLResponse)
async def diagnostics_run(request: Request) -> HTMLResponse:
    form = dict(await request.form())
    profile = _resolve_profile(form.get("profile_id"))
    results = run_all_diagnostics(profile or default_profile())
    _latest_results.clear()
    _latest_results.extend(results)
    status_counts = {
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "warn": sum(1 for r in results if r["status"] == "warn"),
        "fail": sum(1 for r in results if r["status"] == "fail"),
    }
    summary = f"ok:{status_counts['ok']} warn:{status_counts['warn']} fail:{status_counts['fail']}"
    add_run(
        {
            "timestamp": now_iso(),
            "kind": "diagnostics",
            "profile_id": profile.get("id") if profile else "",
            "profile_name": (profile or {}).get("name") or (profile or {}).get("destination") or "Profile",
            "summary": summary,
            "results": results,
        }
    )
    return templates.TemplateResponse(
        "diagnostics_results.jinja2",
        {
            "request": request,
            "results": results,
        },
    )


@app.get("/launch", response_class=HTMLResponse)
def launch(request: Request, task_id: Optional[str] = None) -> HTMLResponse:
    profiles = _prepare_profiles()
    profile = profiles[0] if profiles else default_profile()
    effective = effective_profile(profile)
    tasks = list_tasks(10)
    selected_task = None
    if task_id:
        selected_task = refresh_task(task_id)
    if not selected_task:
        selected_task = tasks[0] if tasks else None
    planned_command = render_command(build_launch_command(effective)) if effective else "launch_server"
    return templates.TemplateResponse(
        "launch.jinja2",
        {
            "request": request,
            "title": "Launch",
            "active": "launch",
            "profiles": profiles,
            "profile": profile,
            "effective": effective,
            "env_detected": env_detection(),
            "task": selected_task,
            "tasks": tasks,
            "planned_command": planned_command,
        },
    )


@app.post("/launch", response_class=HTMLResponse)
async def launch_run_view(request: Request) -> RedirectResponse:
    form = dict(await request.form())
    profile = _resolve_profile(form.get("profile_id")) or default_profile()
    task = create_launch_task(profile)
    launch_background(task["id"], cwd=str(BASE_DIR.parent))
    add_run(
        {
            "timestamp": now_iso(),
            "kind": "launch",
            "profile_id": profile.get("id", ""),
            "profile_name": _display_name(profile),
            "summary": f"queued {task['id']}",
            "results": [],
        }
    )
    return RedirectResponse(url=f"/launch?task_id={task['id']}", status_code=303)


@app.get("/launch/status/{task_id}", response_class=HTMLResponse)
def launch_status(request: Request, task_id: str) -> HTMLResponse:
    task = refresh_task(task_id)
    return templates.TemplateResponse(
        "launch_status.jinja2",
        {
            "request": request,
            "task": task,
        },
    )


@app.post("/launch/stop/{task_id}", response_class=HTMLResponse)
def launch_stop(request: Request, task_id: str) -> HTMLResponse:
    task = stop_task(task_id)
    task = refresh_task(task_id) if task else None
    return templates.TemplateResponse(
        "launch_status.jinja2",
        {
            "request": request,
            "task": task,
        },
    )


@app.post("/launch/input/{task_id}", response_class=HTMLResponse)
async def launch_input(request: Request, task_id: str) -> HTMLResponse:
    form = dict(await request.form())
    text = (form.get("task_input") or "")
    task = submit_task_input(task_id, text)
    task = refresh_task(task_id) if task else None
    return templates.TemplateResponse(
        "launch_status.jinja2",
        {
            "request": request,
            "task": task,
        },
    )


@app.get("/launch/log/{task_id}", response_class=PlainTextResponse)
def launch_log(task_id: str) -> PlainTextResponse:
    task = load_task(task_id)
    if not task:
        return PlainTextResponse("task not found", status_code=404)
    log_path = Path(task.get("log_path") or "")
    if not log_path.exists():
        return PlainTextResponse("log not found", status_code=404)
    return PlainTextResponse(log_path.read_text(encoding="utf-8"))


@app.get("/runs", response_class=HTMLResponse)
def runs_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "runs.jinja2",
        {
            "request": request,
            "title": "Runs",
            "active": "runs",
            "runs": list_runs(100),
        },
    )
