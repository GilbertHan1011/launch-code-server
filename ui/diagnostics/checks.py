from __future__ import annotations

import shlex
import socket
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple


def _run_command(cmd: List[str], timeout: int = 8) -> Tuple[int, str, str, int]:
    started = time.perf_counter()
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )
    duration_ms = int((time.perf_counter() - started) * 1000)
    return result.returncode, result.stdout.strip(), result.stderr.strip(), duration_ms


def _ssh_base_opts() -> List[str]:
    return [
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
    ]


def _ssh_command(host: str, remote_cmd: str, port: Optional[int] = None) -> List[str]:
    cmd = ["ssh"] + _ssh_base_opts()
    if port:
        cmd += ["-p", str(port)]
    cmd += [host, remote_cmd]
    return cmd


def _router_nested_command(router: str, socket_path: str, hpc_host: str, hpc_port: int, remote_cmd: str) -> str:
    inner = f"ssh -S {shlex.quote(socket_path)} -p {hpc_port} {shlex.quote(hpc_host)} {shlex.quote(remote_cmd)}"
    return inner


def _format_result(
    name: str,
    status: str,
    summary: str,
    details: str,
    command: str,
    duration_ms: int,
) -> Dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "summary": summary,
        "details": details,
        "command": command,
        "duration_ms": duration_ms,
    }


def check_local_environment(profile: Dict[str, Any]) -> Dict[str, Any]:
    start = time.perf_counter()
    py_cmd = ["python3", "--version"]
    ssh_cmd = ["ssh", "-V"]
    py_code, py_out, py_err, _ = _run_command(py_cmd)
    ssh_code, ssh_out, ssh_err, _ = _run_command(ssh_cmd)
    duration_ms = int((time.perf_counter() - start) * 1000)
    ok = py_code == 0 and ssh_code == 0
    status = "ok" if ok else "warn"
    summary = "python3 and ssh available" if ok else "missing local tools"
    details = "\n".join(
        [
            f"python3: {py_out or py_err}",
            f"ssh: {ssh_out or ssh_err}",
        ]
    )
    return _format_result(
        "Local environment",
        status,
        summary,
        details,
        "python3 --version; ssh -V",
        duration_ms,
    )


def check_router_reachable(profile: Dict[str, Any]) -> Dict[str, Any]:
    if profile.get("mode") != "router_socket":
        return _format_result(
            "Router reachable",
            "warn",
            "router mode not enabled",
            "mode is not router_socket",
            "ssh <router> exit",
            0,
        )
    router = profile.get("router_address") or ""
    if not router:
        return _format_result(
            "Router reachable",
            "fail",
            "router address missing",
            "router_address is empty",
            "ssh <router> exit",
            0,
        )
    cmd = _ssh_command(router, "exit 0")
    code, out, err, duration_ms = _run_command(cmd)
    status = "ok" if code == 0 else "fail"
    summary = "router reachable" if code == 0 else "router unreachable"
    details = out or err or "no output"
    return _format_result(
        "Router reachable",
        status,
        summary,
        details,
        " ".join(cmd),
        duration_ms,
    )


def check_router_socket(profile: Dict[str, Any]) -> Dict[str, Any]:
    if profile.get("mode") != "router_socket":
        return _format_result(
            "Router socket",
            "warn",
            "router mode not enabled",
            "mode is not router_socket",
            "ssh <router> 'test -S <socket>'",
            0,
        )
    router = profile.get("router_address") or ""
    socket_path = profile.get("router_socket_path") or ""
    if not router or not socket_path:
        return _format_result(
            "Router socket",
            "fail",
            "router socket config missing",
            "router_address or router_socket_path missing",
            "ssh <router> 'test -S <socket>'",
            0,
        )
    remote_cmd = f"test -S {shlex.quote(socket_path)}"
    cmd = _ssh_command(router, remote_cmd)
    code, out, err, duration_ms = _run_command(cmd)
    status = "ok" if code == 0 else "fail"
    summary = "router socket present" if code == 0 else "router socket missing"
    details = out or err or "no output"
    return _format_result(
        "Router socket",
        status,
        summary,
        details,
        " ".join(cmd),
        duration_ms,
    )


def check_hpc_ssh(profile: Dict[str, Any]) -> Dict[str, Any]:
    mode = profile.get("mode")
    if mode == "direct":
        destination = profile.get("destination") or ""
        if not destination:
            return _format_result(
                "HPC SSH",
                "fail",
                "destination missing",
                "destination is empty",
                "ssh -p <ssh_port> <destination> exit",
                0,
            )
        port = profile.get("ssh_port")
        cmd = _ssh_command(destination, "exit 0", port=port)
        code, out, err, duration_ms = _run_command(cmd)
        status = "ok" if code == 0 else "fail"
        summary = "direct SSH reachable" if code == 0 else "direct SSH failed"
        details = out or err or "no output"
        return _format_result(
            "HPC SSH",
            status,
            summary,
            details,
            " ".join(cmd),
            duration_ms,
        )
    if mode == "router_socket":
        router = profile.get("router_address") or ""
        socket_path = profile.get("router_socket_path") or ""
        hpc_host = profile.get("hpc_real_host") or ""
        hpc_port = profile.get("hpc_real_port") or 22
        if not router or not socket_path or not hpc_host:
            return _format_result(
                "HPC SSH",
                "warn",
                "router config incomplete",
                "router_address, router_socket_path, or hpc_real_host missing",
                "ssh <router> "
                "\"ssh -S <socket> -p <port> <hpc_host> exit\"",
                0,
            )
        nested = _router_nested_command(router, socket_path, hpc_host, int(hpc_port), "exit 0")
        cmd = _ssh_command(router, nested)
        code, out, err, duration_ms = _run_command(cmd)
        status = "ok" if code == 0 else "fail"
        summary = "router SSH reachable" if code == 0 else "router SSH failed"
        details = out or err or "no output"
        return _format_result(
            "HPC SSH",
            status,
            summary,
            details,
            " ".join(cmd),
            duration_ms,
        )
    return _format_result(
        "HPC SSH",
        "warn",
        "unknown mode",
        f"mode={mode}",
        "ssh ...",
        0,
    )


def check_slurm_access(profile: Dict[str, Any]) -> Dict[str, Any]:
    mode = profile.get("mode")
    remote_cmd = "command -v sbatch && sbatch --version"
    if mode == "direct":
        destination = profile.get("destination") or ""
        if not destination:
            return _format_result(
                "Slurm access",
                "warn",
                "destination missing",
                "destination is empty",
                "ssh -p <ssh_port> <destination> 'command -v sbatch && sbatch --version'",
                0,
            )
        port = profile.get("ssh_port")
        cmd = _ssh_command(destination, remote_cmd, port=port)
        code, out, err, duration_ms = _run_command(cmd)
        status = "ok" if code == 0 else "fail"
        summary = "sbatch available" if code == 0 else "sbatch not available"
        details = out or err or "no output"
        return _format_result(
            "Slurm access",
            status,
            summary,
            details,
            " ".join(cmd),
            duration_ms,
        )
    if mode == "router_socket":
        router = profile.get("router_address") or ""
        socket_path = profile.get("router_socket_path") or ""
        hpc_host = profile.get("hpc_real_host") or ""
        hpc_port = profile.get("hpc_real_port") or 22
        if not router or not socket_path or not hpc_host:
            return _format_result(
                "Slurm access",
                "warn",
                "router config incomplete",
                "router_address, router_socket_path, or hpc_real_host missing",
                "ssh <router> "
                "\"ssh -S <socket> -p <port> <hpc_host> 'command -v sbatch && sbatch --version'\"",
                0,
            )
        nested = _router_nested_command(router, socket_path, hpc_host, int(hpc_port), remote_cmd)
        cmd = _ssh_command(router, nested)
        code, out, err, duration_ms = _run_command(cmd)
        status = "ok" if code == 0 else "fail"
        summary = "sbatch available" if code == 0 else "sbatch not available"
        details = out or err or "no output"
        return _format_result(
            "Slurm access",
            status,
            summary,
            details,
            " ".join(cmd),
            duration_ms,
        )
    return _format_result(
        "Slurm access",
        "warn",
        "unknown mode",
        f"mode={mode}",
        "ssh ...",
        0,
    )


def check_port_forwarding(profile: Dict[str, Any]) -> Dict[str, Any]:
    port = profile.get("local_forward_port") or 0
    start = time.perf_counter()
    status = "ok"
    summary = "local forward port available"
    details = ""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", int(port)))
        sock.close()
        details = f"bind to 127.0.0.1:{port} succeeded"
    except Exception as exc:
        status = "warn"
        summary = "local forward port in use or unavailable"
        details = str(exc)
    duration_ms = int((time.perf_counter() - start) * 1000)
    return _format_result(
        "Port forwarding config",
        status,
        summary,
        details,
        f"bind 127.0.0.1:{port}",
        duration_ms,
    )


def run_all_diagnostics(profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        check_local_environment(profile),
        check_router_reachable(profile),
        check_router_socket(profile),
        check_hpc_ssh(profile),
        check_slurm_access(profile),
        check_port_forwarding(profile),
    ]
