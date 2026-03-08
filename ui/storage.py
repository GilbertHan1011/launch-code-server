from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PROFILES_FILE = DATA_DIR / "profiles.json"
RUNS_FILE = DATA_DIR / "runs.json"

PROFILE_FIELDS: List[Dict[str, Any]] = [
    {"name": "mode", "default": "direct"},
    {"name": "destination", "default": ""},
    {"name": "ssh_port", "default": 22},
    {"name": "router_address", "default": ""},
    {"name": "router_socket_path", "default": "/tmp/hpc_socket"},
    {"name": "hpc_real_host", "default": ""},
    {"name": "hpc_real_port", "default": 22},
    {"name": "partition", "default": ""},
    {"name": "compute_node", "default": ""},
    {"name": "num_cpus", "default": 1},
    {"name": "memory_per_cpu", "default": "8G"},
    {"name": "timeout", "default": 3000},
    {"name": "env_name", "default": ""},
    {"name": "setup_proxy", "default": False},
    {"name": "proxy_login_host", "default": "login01"},
    {"name": "proxy_target_host", "default": "172.16.75.119"},
    {"name": "proxy_target_port", "default": 3128},
    {"name": "proxy_local_port", "default": 9999},
    {"name": "local_forward_port", "default": 2222},
    {"name": "vscode_alias", "default": "vscode-server"},
]

INT_FIELDS = {
    "ssh_port",
    "hpc_real_port",
    "num_cpus",
    "timeout",
    "proxy_target_port",
    "proxy_local_port",
    "local_forward_port",
}


def _ensure_storage() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not PROFILES_FILE.exists():
        PROFILES_FILE.write_text("[]", encoding="utf-8")
    if not RUNS_FILE.exists():
        RUNS_FILE.write_text("[]", encoding="utf-8")


def _read_json(path: Path) -> List[Dict[str, Any]]:
    _ensure_storage()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def _write_json(path: Path, data: List[Dict[str, Any]]) -> None:
    _ensure_storage()
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def list_profiles() -> List[Dict[str, Any]]:
    return _read_json(PROFILES_FILE)


def get_profile(profile_id: str) -> Optional[Dict[str, Any]]:
    for profile in list_profiles():
        if profile.get("id") == profile_id:
            return profile
    return None


def delete_profile(profile_id: str) -> None:
    profiles = [p for p in list_profiles() if p.get("id") != profile_id]
    _write_json(PROFILES_FILE, profiles)


def save_profile(profile: Dict[str, Any]) -> Dict[str, Any]:
    profiles = list_profiles()
    for idx, existing in enumerate(profiles):
        if existing.get("id") == profile.get("id"):
            profiles[idx] = profile
            _write_json(PROFILES_FILE, profiles)
            return profile
    profiles.append(profile)
    _write_json(PROFILES_FILE, profiles)
    return profile


def build_profile_from_form(form: Dict[str, Any], profile_id: Optional[str] = None) -> Dict[str, Any]:
    profile: Dict[str, Any] = {"id": profile_id or str(uuid.uuid4())}
    for field in PROFILE_FIELDS:
        name = field["name"]
        value = form.get(name, field["default"])
        if name in INT_FIELDS:
            if value in (None, ""):
                value = field["default"]
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = field["default"]
        if name == "setup_proxy":
            value = bool(value) and str(value).lower() not in {"0", "false", "off"}
        profile[name] = value
    profile["name"] = form.get("name", profile.get("destination") or "Profile")
    return profile


def default_profile() -> Dict[str, Any]:
    profile: Dict[str, Any] = {"id": ""}
    for field in PROFILE_FIELDS:
        profile[field["name"]] = field["default"]
    profile["name"] = ""
    return profile


def list_runs(limit: int = 20) -> List[Dict[str, Any]]:
    runs = _read_json(RUNS_FILE)
    return runs[:limit]


def add_run(run: Dict[str, Any]) -> None:
    runs = _read_json(RUNS_FILE)
    runs.insert(0, run)
    _write_json(RUNS_FILE, runs[:200])


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
