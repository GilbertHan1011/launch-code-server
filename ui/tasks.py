from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from ui.launcher import build_launch_command, render_command
from ui.storage import DATA_DIR, now_iso

TASKS_DIR = DATA_DIR / "tasks"
JOB_TAB_RE = re.compile(r"(?m)^(\d+)\t([^\t\n]+)\t(\d+)\s*$")
JOB_LOG_RE = re.compile(r"A job \(id=(\d+)\) has been reserved on node ([^\s]+)")
FORWARD_RE = re.compile(r"Setup port forwarding: localhost:(\d+) => ([^:]+):(\d+)")
VSCODE_RE = re.compile(r"Connect to host '([^']+)' in VS Code")


def _ensure_tasks_dir() -> None:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)


def _task_file(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


def _task_log(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.log"


def _extract_summary(content: str) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}

    m = JOB_TAB_RE.search(content)
    if m:
        summary["job_id"] = m.group(1)
        summary["node"] = m.group(2)
        summary["server_port"] = int(m.group(3))

    m = JOB_LOG_RE.search(content)
    if m:
        summary.setdefault("job_id", m.group(1))
        summary.setdefault("node", m.group(2))

    m = FORWARD_RE.search(content)
    if m:
        summary["local_forward_port"] = int(m.group(1))
        summary["forward_host"] = m.group(2)
        summary["forward_port"] = int(m.group(3))

    m = VSCODE_RE.search(content)
    if m:
        summary["vscode_alias"] = m.group(1)

    summary["ready"] = bool(summary.get("job_id") or summary.get("node") or summary.get("local_forward_port"))
    return summary


def create_launch_task(profile: Dict[str, Any]) -> Dict[str, Any]:
    _ensure_tasks_dir()
    task_id = f"launch-{uuid.uuid4().hex[:12]}"
    command = build_launch_command(profile)
    task = {
        "id": task_id,
        "kind": "launch",
        "status": "pending",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "profile_id": profile.get("id", ""),
        "profile_name": profile.get("name") or profile.get("destination") or "Profile",
        "command": render_command(command),
        "argv": command,
        "log_path": str(_task_log(task_id)),
        "pid": None,
        "return_code": None,
        "duration_ms": None,
        "stdout_tail": "",
        "stderr_tail": "",
        "error": "",
        "summary_data": {},
        "scancel": None,
    }
    _task_file(task_id).write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8")
    return task


def load_task(task_id: str) -> Optional[Dict[str, Any]]:
    path = _task_file(task_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_task(task: Dict[str, Any]) -> None:
    _ensure_tasks_dir()
    task["updated_at"] = now_iso()
    _task_file(task["id"]).write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8")


def list_tasks(limit: int = 20) -> List[Dict[str, Any]]:
    _ensure_tasks_dir()
    tasks = []
    for path in sorted(TASKS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            tasks.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return tasks[:limit]


def launch_background(task_id: str, cwd: Optional[str] = None) -> Dict[str, Any]:
    task = load_task(task_id)
    if not task:
        raise FileNotFoundError(task_id)

    log_path = Path(task["log_path"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    script = TASKS_DIR / f"{task_id}.runner.py"
    script.write_text(
        (
            "import json, subprocess, time, pathlib, os\n"
            f"task_path = pathlib.Path({str(_task_file(task_id))!r})\n"
            f"log_path = pathlib.Path({str(log_path)!r})\n"
            f"argv = {task['argv']!r}\n"
            f"cwd = {str(cwd or os.getcwd())!r}\n"
            "task = json.loads(task_path.read_text(encoding='utf-8'))\n"
            "task['status'] = 'running'\n"
            "task['started_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')\n"
            "task['runner_pid'] = os.getpid()\n"
            "task_path.write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding='utf-8')\n"
            "started = time.perf_counter()\n"
            "with log_path.open('w', encoding='utf-8') as logf:\n"
            "    logf.write('$ ' + ' '.join(argv) + '\\n\\n')\n"
            "    logf.flush()\n"
            "    proc = subprocess.Popen(argv, cwd=cwd, stdout=logf, stderr=subprocess.STDOUT, text=True)\n"
            "    task = json.loads(task_path.read_text(encoding='utf-8'))\n"
            "    task['child_pid'] = proc.pid\n"
            "    task_path.write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding='utf-8')\n"
            "    rc = proc.wait()\n"
            "duration_ms = int((time.perf_counter() - started) * 1000)\n"
            "task = json.loads(task_path.read_text(encoding='utf-8'))\n"
            "if task.get('status') != 'stopped':\n"
            "    task['status'] = 'success' if rc == 0 else 'failed'\n"
            "task['return_code'] = rc\n"
            "task['duration_ms'] = duration_ms\n"
            "try:\n"
            "    content = log_path.read_text(encoding='utf-8')\n"
            "except Exception:\n"
            "    content = ''\n"
            "task['stdout_tail'] = content[-8000:]\n"
            "task_path.write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding='utf-8')\n"
        ),
        encoding="utf-8",
    )

    proc = subprocess.Popen(
        [sys.executable, str(script)],
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    task["pid"] = proc.pid
    task["status"] = "queued"
    save_task(task)
    return task


def stop_task(task_id: str) -> Optional[Dict[str, Any]]:
    task = load_task(task_id)
    if not task:
        return None

    if not task.get("summary_data"):
        log_path = Path(task.get("log_path") or "")
        if log_path.exists():
            try:
                content = log_path.read_text(encoding="utf-8")
                task["summary_data"] = _extract_summary(content)
            except Exception:
                pass

    killed = []
    for key in ("child_pid", "runner_pid", "pid"):
        pid = task.get(key)
        if not pid:
            continue
        try:
            os.kill(int(pid), signal.SIGTERM)
            killed.append(f"{key}:{pid}")
        except ProcessLookupError:
            pass
        except Exception as exc:
            task["error"] = str(exc)

    job_id = (task.get("summary_data") or {}).get("job_id")
    if job_id:
        try:
            res = subprocess.run(
                ["scancel", str(job_id)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=10,
            )
            task["scancel"] = {
                "job_id": str(job_id),
                "return_code": res.returncode,
                "stdout": res.stdout.strip(),
                "stderr": res.stderr.strip(),
                "ok": res.returncode == 0,
            }
        except Exception as exc:
            task["scancel"] = {
                "job_id": str(job_id),
                "return_code": None,
                "stdout": "",
                "stderr": str(exc),
                "ok": False,
            }

    task["status"] = "stopped"
    task["stopped_at"] = now_iso()
    if killed:
        task["stop_info"] = ", ".join(killed)
    save_task(task)
    return task


def refresh_task(task_id: str) -> Optional[Dict[str, Any]]:
    task = load_task(task_id)
    if not task:
        return None
    log_path = Path(task.get("log_path") or "")
    if log_path.exists():
        try:
            content = log_path.read_text(encoding="utf-8")
            task["stdout_tail"] = content[-8000:]
            task["summary_data"] = _extract_summary(content)
        except Exception:
            pass
    save_task(task)
    return task
