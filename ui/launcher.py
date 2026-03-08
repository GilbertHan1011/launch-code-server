from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT_SCRIPT = REPO_ROOT / "src" / "launch_code_server" / "client.py"


def build_launch_command(profile: Dict[str, Any]) -> List[str]:
    cmd: List[str] = [sys.executable, str(CLIENT_SCRIPT)]
    destination = (profile.get("destination") or "").strip()
    if destination:
        cmd.append(destination)

    ssh_port = profile.get("ssh_port")
    if ssh_port not in (None, "", 22, "22"):
        cmd += ["-p", str(ssh_port)]

    partition = (profile.get("partition") or "").strip()
    if partition:
        cmd += ["--partition", partition]

    env_name = (profile.get("env_name") or "").strip()
    if env_name:
        cmd += ["--env", env_name]

    num_cpus = profile.get("num_cpus")
    if num_cpus not in (None, "", 1, "1"):
        cmd += ["-n", str(num_cpus)]

    memory_per_cpu = (profile.get("memory_per_cpu") or "").strip()
    if memory_per_cpu and memory_per_cpu != "8G":
        cmd += ["-m", memory_per_cpu]

    timeout = profile.get("timeout")
    if timeout not in (None, "", 3000, "3000"):
        cmd += ["--timeout", str(timeout)]

    compute_node = (profile.get("compute_node") or "").strip()
    if compute_node:
        cmd += ["--compute-node", compute_node]

    if profile.get("setup_proxy"):
        cmd.append("--setup-proxy")
        proxy_login_host = (profile.get("proxy_login_host") or "").strip()
        proxy_target_host = (profile.get("proxy_target_host") or "").strip()
        proxy_target_port = profile.get("proxy_target_port")
        proxy_local_port = profile.get("proxy_local_port")
        if proxy_login_host:
            cmd += ["--proxy-login-host", proxy_login_host]
        if proxy_target_host:
            cmd += ["--proxy-target-host", proxy_target_host]
        if proxy_target_port not in (None, ""):
            cmd += ["--proxy-target-port", str(proxy_target_port)]
        if proxy_local_port not in (None, ""):
            cmd += ["--proxy-local-port", str(proxy_local_port)]

    mode = (profile.get("mode") or "direct").strip()
    if mode == "router_socket":
        router_address = (profile.get("router_address") or "").strip()
        router_socket_path = (profile.get("router_socket_path") or "").strip()
        hpc_real_host = (profile.get("hpc_real_host") or "").strip()
        hpc_real_port = profile.get("hpc_real_port")
        if router_address:
            cmd += ["--router", router_address]
        if router_socket_path:
            cmd += ["--router-socket", router_socket_path]
        if hpc_real_host:
            cmd += ["--hpc-real-host", hpc_real_host]
        if hpc_real_port not in (None, ""):
            cmd += ["--hpc-real-port", str(hpc_real_port)]

    return cmd


def render_command(cmd: List[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)


def run_launch(profile: Dict[str, Any], timeout_seconds: int = 20) -> Dict[str, Any]:
    cmd = build_launch_command(profile)
    started = time.perf_counter()
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_seconds,
        check=False,
        env=env,
    )
    duration_ms = int((time.perf_counter() - started) * 1000)
    return {
        "ok": result.returncode == 0,
        "return_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "duration_ms": duration_ms,
        "command": render_command(cmd),
        "timed_out": False,
    }
