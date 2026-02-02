import argparse
import contextlib
import getpass
import logging
import os
import shlex
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from io import StringIO
from os.path import expanduser
from typing import Optional, TextIO

from fabric import Connection
from sshconf import read_ssh_config

# --- Configuration & Data Structures ---

@dataclass
class RouterConfig:
    """Holds configuration for the Router-Jump connection."""
    address: str
    socket_path: str
    hpc_host: str
    hpc_port: str

    @classmethod
    def from_env(cls) -> Optional['RouterConfig']:
        """Factory to create config from environment variables."""
        if os.environ.get("LCS_ROUTER") and os.environ.get("LCS_HPC_HOST"):
            return cls(
                address=os.environ["LCS_ROUTER"],
                socket_path=os.environ.get("LCS_SOCKET", "/tmp/hpc_socket"),
                hpc_host=os.environ["LCS_HPC_HOST"],
                hpc_port=os.environ.get("LCS_HPC_PORT", "22")
            )
        return None

# --- Core Abstraction: SSH Executor ---

class SSHExecutor:
    """
    Abstracts the SSH command execution. 
    This allows us to swap between direct connections (Fabric) 
    and custom Router-Socket tunnel seamlessly.
    """
    def run(self, cmd: str, hide=False, warn=False, pty=False, out_stream: Optional[TextIO] = None, **kwargs):
        """Execute a command remotely. Returns a Result-like object."""
        raise NotImplementedError

    def is_connected(self) -> bool:
        """Check if the connection is active."""
        raise NotImplementedError
    
    @contextlib.contextmanager
    def forward_local(self, local_port: int, remote_port: int, remote_host: str):
        """Establish a local port forward tunnel."""
        raise NotImplementedError

class RouterSocketExecutor(SSHExecutor):
    """
    Executes commands via an existing SSH ControlMaster socket on a router.
    Flow: Local -> Router (SSH) -> HPC (Socket) -> Command
    """
    def __init__(self, config: RouterConfig):
        self.config = config
        self._connected = True
        logging.info(f"🚀 Using Router Jump: {config.address} -> {config.hpc_host} (Socket: {config.socket_path})")

    def run(self, cmd: str, hide=False, warn=False, pty=False, out_stream: Optional[TextIO] = None, **kwargs):
        # Wrap the command to be safe for the remote shell
        safe_cmd = shlex.quote(cmd)
        
        # Construct the nested SSH command
        # a. Connect to HPC using the socket (-S)
        remote_cmd = f"ssh -S {self.config.socket_path} -p {self.config.hpc_port} {self.config.hpc_host} {safe_cmd}"
        
        # b. Execute that command on the router
        full_cmd = ["ssh", self.config.address, remote_cmd]

        try:
            # Use Popen for pty, subprocess.run otherwise
            if pty:
                proc = subprocess.Popen(
                    full_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.PIPE,
                    text=True
                )
                stdout, stderr = proc.communicate()
                return_code = proc.returncode
            else:
                proc = subprocess.run(
                    full_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False  # We handle return codes manually
                )
                stdout = proc.stdout
                stderr = proc.stderr
                return_code = proc.returncode
            
            # Mimic Fabric's behavior: Write to out_stream if provided (used by exec_remote_python)
            if out_stream and stdout:
                out_stream.write(stdout)
                out_stream.flush()

            if return_code != 0 and not warn:
                raise RuntimeError(f"SSH Command Failed ({return_code}):\n{stderr}")

            # Return an object that mimics Fabric's Result
            return type('Result', (), {
                'ok': return_code == 0,
                'stdout': stdout,
                'stderr': stderr,
                'return_code': return_code,
                'command': cmd
            })()

        except Exception as e:
            if not warn:
                raise e
            return type('Result', (), {
                'ok': False, 
                'stdout': '', 
                'stderr': str(e),
                'return_code': 1,
                'command': cmd
            })()

    def is_connected(self) -> bool:
        """The socket is assumed to be persistent."""
        return self._connected

    @contextlib.contextmanager
    def forward_local(self, local_port: int, remote_port: int, remote_host: str):
        """
        Sets up a port forward tunnel: Local -> Router -> HPC
        Enhanced with better error handling and connection stability.
        """
        logging.info(f"🔗 Tunneling: localhost:{local_port} -> Router -> {remote_host}:{remote_port}")
        
        # 1. Extract HPC username (new logic)
        # Extract "hanlitian" from "hanlitian@172.16.78.132"
        try:
            hpc_user = self.config.hpc_host.split('@')[0]
        except IndexError:
            hpc_user = "root"  # Fallback, though unlikely to happen
        
        # 2. Construct ProxyCommand (unchanged)
        # This tells the router to use the Socket channel to forward traffic to HPC compute node
        # Note: Added -q (quiet mode) to prevent unnecessary output interference
        proxy_cmd = f"ssh -q -S {self.config.socket_path} -p {self.config.hpc_port} {self.config.hpc_host} -W {remote_host}:{remote_port}"
        
        # 3. Construct local tunnel command
        # Fix A: Add StrictHostKeyChecking=no to prevent fingerprint verification popup
        # Fix B: Use 127.0.0.1 to prevent DNS resolution failure
        # Fix C: Add ServerAliveInterval to prevent idle disconnection
        # Fix D: Explicitly specify HPC username to prevent using local Mac username
        cmd = [
            "ssh", "-N", 
            "-L", f"{local_port}:{remote_host}:{remote_port}",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "ServerAliveInterval=60",
            "-o", f"ProxyCommand=ssh -q {self.config.address} {shlex.quote(proxy_cmd)}",
            f"{hpc_user}@127.0.0.1"  # Force use of HPC username for login
        ]
        
        # 3. Start process and perform health check
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(2)  # Give it 2 seconds to establish connection
            if proc.poll() is not None:
                # If process has died, print error information
                _, stderr = proc.communicate()
                logging.error(f"❌ Tunnel died immediately!")
                logging.error(f"Reason: {stderr.decode() if stderr else 'Unknown'}")
                logging.info(f"Debug Command: {' '.join(cmd)}")  # Print command for manual debugging
                yield  # Even though it failed, let the program continue to prevent crash in Context Manager
            else:
                logging.info("✅ Tunnel established.")
                yield
        finally:
            logging.info("🛑 Closing tunnel...")
            proc.terminate()
            # Ensure child process is cleaned up
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

# 🔵 Strategy B: Direct Fabric Connection - Original mode with password/OTP
class DirectFabricExecutor(SSHExecutor):
    """
    Executes commands via direct Fabric connection (original mode).
    This is the fallback when Router configuration is not available.
    """
    def __init__(self, host: str, user: str, port: Optional[int] = None, gateway: Optional[Connection] = None, connect_kwargs: Optional[dict] = None):
        logging.info(f"🔌 Mode: Direct Connection ({user}@{host})")
        # Merge timeout into connect_kwargs (Fabric uses 'timeout' not 'connect_timeout')
        final_kwargs = connect_kwargs or {}
        if 'timeout' not in final_kwargs:
            final_kwargs['timeout'] = 90
        
        self.conn = Connection(
            host, 
            user=user, 
            port=port, 
            gateway=gateway,
            connect_kwargs=final_kwargs,
            inline_ssh_env=True
        )
        self._try_connect()

    def _try_connect(self):
        """Try to connect, falling back to password prompt if key auth fails."""
        try:
            self.conn.open()
        except Exception:
            # Fallback to password prompt if key fails
            password = getpass.getpass(f"🔑 ({self.conn.user}@{self.conn.host}) Password/OTP: ")
            self.conn.connect_kwargs['password'] = password
            self.conn.connect_kwargs['allow_agent'] = False
            self.conn.connect_kwargs['look_for_keys'] = False
            self.conn.open()
        
        # Set keepalive on the transport after connection is established
        if self.conn.is_connected and self.conn.transport:
            self.conn.transport.set_keepalive(30)
        
        # Store password for potential reconnection attempts
        if 'password' in self.conn.connect_kwargs:
            self.conn._stored_password = self.conn.connect_kwargs['password']

    def run(self, cmd: str, hide=False, warn=False, pty=False, out_stream: Optional[TextIO] = None, **kwargs):
        """Execute command via Fabric. Supports out_stream for exec_remote_python."""
        if out_stream:
            # Fabric's run can accept out_stream parameter
            return self.conn.run(cmd, hide=hide, warn=warn, pty=pty, out_stream=out_stream, **kwargs)
        return self.conn.run(cmd, hide=hide, warn=warn, pty=pty, **kwargs)

    def is_connected(self) -> bool:
        """Check if Fabric connection is active."""
        return self.conn.is_connected

    @contextlib.contextmanager
    def forward_local(self, local_port: int, remote_port: int, remote_host: str):
        """Use Fabric's built-in port forwarding."""
        with self.conn.forward_local(local_port, remote_port, remote_host):
            yield

# --- Business Logic ---

def run_with_retry(conn: SSHExecutor, command: str, retries: int = 3, **kwargs):
    """Run a command with retry logic for connection issues."""
    for attempt in range(retries):
        try:
            return conn.run(command, **kwargs)
        except Exception as e:
            if attempt < retries - 1:
                logging.warning(f"Command failed (attempt {attempt + 1}/{retries}), retrying: {e}")
                time.sleep(2)
                # Try to reconnect if needed (only for DirectFabricExecutor)
                try:
                    if not conn.is_connected():
                        # For DirectFabricExecutor, try to reconnect
                        if hasattr(conn, '_try_connect'):
                            conn._try_connect()
                        # For RouterSocketExecutor, connection is assumed persistent
                except Exception as reconnect_error:
                    logging.warning(f"Reconnection failed: {reconnect_error}")
            else:
                logging.error(f"Command failed after {retries} attempts")
                raise

def exec_remote_python(conn: SSHExecutor, code: str, env_name: Optional[str] = None) -> str:
    """Executes code/command on the remote server, handling Conda/Mamba activation."""
    if env_name:
        escaped_code = code.replace("'", "'\"'\"'")
        
        # Build a robust shell command that runs on the remote server:
        # 1. Start a Login Shell (bash -l) to load ~/.bashrc and PATH.
        # 2. Search for the package manager in priority order.
        # 3. Generate the activation hook using the correct syntax.
        shell_cmd = (
            f"bash -l -c '"
            # --- Discovery Logic ---
            f"MGR=\"\"; "
            f"for cmd in micromamba mamba conda; do "
            f"    if command -v $cmd &> /dev/null; then MGR=$cmd; break; fi; "
            f"done; "
            
            # --- Fallback to Environment Variables if 'command -v' failed ---
            f"if [ -z \"$MGR\" ]; then "
            f"    if [ -n \"$MAMBA_EXE\" ]; then MGR=\"$MAMBA_EXE\"; "
            f"    elif [ -n \"$CONDA_EXE\" ]; then MGR=\"$CONDA_EXE\"; "
            f"    else echo \"Error: No conda/mamba/micromamba found in PATH or env vars.\"; exit 127; fi; "
            f"fi; "
            # --- Activation Logic ---
            # Micromamba uses 'shell hook', Conda/Mamba use 'shell.bash hook'
            f"if [[ \"$MGR\" == *\"micromamba\"* ]]; then "
            f"    eval \"$($MGR shell hook --shell bash)\"; "
            f"else "
            f"    eval \"$($MGR shell.bash hook)\"; "
            f"fi && "
            
            f"$MGR activate {env_name} && "
            f"{escaped_code}'"
        )
        
        # Override the code to be executed
        code = shell_cmd
    
    # Capture output via StringIO
    output_buffer = StringIO()
    sys_stdout_backup = sys.stdout
    sys.stdout = output_buffer
    
    try:
        # The executor knows to write to out_stream
        run_with_retry(conn, code, retries=3, out_stream=sys.stdout, hide='err')
    finally:
        sys.stdout = sys_stdout_backup
        
    return output_buffer.getvalue()

def launch_compute(conn: SSHExecutor, partition: Optional[str], n_cpus: int, memory_per_cpu: str, 
                   node: Optional[str], timeout: int = 300, env_name: Optional[str] = None):
    """Launch a compute job and return job_id, node, and port."""
    partition_arg = f'--partition {partition}' if partition else ''
    node_arg = f'--compute_node {node}' if node else ''
    
    cmd = f"vscode_server launch {partition_arg} --number_of_cpus {n_cpus} --timeout {timeout} --memory_per_cpu {memory_per_cpu} {node_arg}"
    output = exec_remote_python(conn, cmd, env_name=env_name)
    
    try:
        job_id, node, port = output.strip().split('\t')
        return int(job_id), node, int(port)
    except ValueError as e:
        raise ValueError(f"Failed to parse server output. Got: '{output}'") from e

def check_compute(conn: SSHExecutor, host: str, port: int, env_name: Optional[str] = None) -> str:
    """Check compute node status."""
    return exec_remote_python(conn, f"vscode_server check --host {host} --port {port}", env_name=env_name).strip()

def ensure_proxy_env_config(conn: SSHExecutor, local_port: int):
    """Ensure the user's .bashrc exports proxy variables on compute nodes."""
    marker = "# >>> launch_code_server proxy >>>"
    
    # Test basic command execution first with non-interactive shell
    try:
        logging.info("Testing connection with simple command...")
        test_result = run_with_retry(conn, "/bin/bash -c 'echo test'", retries=2, hide=True)
        if not test_result.ok:
            logging.warning("Basic command test failed, connection may be unstable")
            logging.warning("Skipping proxy configuration - you may need to configure it manually")
            return
    except Exception as e:
        logging.error(f"Connection test failed: {e}")
        logging.warning("Skipping proxy configuration due to connection instability")
        logging.warning(f"You can manually add this to your ~/.bashrc on compute nodes:")
        logging.warning(f"  if [[ \"$(hostname)\" != *\"login\"* ]]; then")
        logging.warning(f"    export http_proxy=http://127.0.0.1:{local_port}")
        logging.warning(f"    export https_proxy=http://127.0.0.1:{local_port}")
        logging.warning(f"  fi")
        return  # Don't raise, just skip proxy setup
    
    # Check if configuration already exists (using non-interactive shell)
    check_cmd = f"/bin/bash -c 'grep -q \"{marker}\" ~/.bashrc 2>/dev/null'"
    result = run_with_retry(conn, check_cmd, retries=3, warn=True, hide=True)
    
    if result.ok:
        logging.info("Proxy config already present in .bashrc.")
        return
    
    logging.info("Appending proxy config to .bashrc...")
    
    # Create the configuration block
    config_block = f"""{marker}
if [[ "$(hostname)" != *"login"* ]]; then
    export http_proxy=http://127.0.0.1:{local_port}
    export https_proxy=http://127.0.0.1:{local_port}
    export HTTP_PROXY=http://127.0.0.1:{local_port}
    export HTTPS_PROXY=http://127.0.0.1:{local_port}
    export ALL_PROXY=http://127.0.0.1:{local_port}
fi
# <<< launch_code_server proxy <<<"""
    
    # Use printf to append (more reliable than cat with heredoc)
    # Escape the config block for printf
    escaped_block = config_block.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$')
    append_cmd = f'/bin/bash -c "printf \"\\n{escaped_block}\\n\" >> ~/.bashrc"'
    
    try:
        run_with_retry(conn, append_cmd, retries=3, hide=False)
        logging.info("Proxy config successfully added to .bashrc.")
    except Exception as e:
        logging.error(f"Failed to update .bashrc: {e}")
        raise

def ensure_proxy_tunnel(conn: SSHExecutor, node: str, login_host: str, target_host: str, 
                        target_port: int, local_port: int):
    """Start (if needed) the SSH tunnel that exposes the cluster HTTP proxy."""
    # First check if tunnel is already running - look for actual LISTEN socket
    check_cmd = f"ss -tln | grep ':{local_port} '"
    result = conn.run(f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null {node} '{check_cmd}'", hide=True, warn=True)
    
    # Check if we got actual output showing a listening socket
    if result.stdout.strip() and "LISTEN" in result.stdout:
        logging.info(f"Proxy tunnel already running on port {local_port}")
        return

    # Prompt for OTP locally
    otp = getpass.getpass(f"Enter OATH OTP for tunnel on {node}: ")
    
    logging.info("Starting proxy tunnel with provided OTP...")

    # Use SSH_ASKPASS with a helper script to handle OTP prompt non-interactively
    askpass_script = textwrap.dedent(f"""
        #!/bin/bash
        echo "{otp}"
    """).strip()

    setup_cmds = f"""
cat <<'EOF' > ~/.ssh_askpass_tunnel
{askpass_script}
EOF
chmod +x ~/.ssh_askpass_tunnel
export SSH_ASKPASS=~/.ssh_askpass_tunnel
export DISPLAY=:0  # SSH_ASKPASS needs DISPLAY set (even dummy) or setsid
setsid ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PubkeyAuthentication=no -o PreferredAuthentications=keyboard-interactive,password -N -f -L {local_port}:{target_host}:{target_port} {login_host} < /dev/null
rm ~/.ssh_askpass_tunnel
    """

    full_cmd = f"ssh -tt -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null {node} {shlex.quote(setup_cmds)}"
    
    conn.run(full_cmd, pty=True, warn=True)
    
    # Wait for tunnel to bind
    logging.info("Waiting for tunnel to establish...")
    time.sleep(3)
    
    # Verify tunnel is running
    verify_cmd = f"ss -tln | grep ':{local_port} '"
    result = conn.run(f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null {node} '{verify_cmd}'", hide=True, warn=True)
    
    if result.stdout.strip() and "LISTEN" in result.stdout:
        logging.info(f"Proxy tunnel successfully established on {node}:{local_port}")
    else:
        logging.warning(f"Could not verify proxy tunnel on {node}:{local_port}")
        logging.warning(f"Manual check: ssh {node} 'ss -tln | grep {local_port}'")

def get_ssh_user_from_config(hostname: str) -> str:
    """Get login information from ssh configuration file."""
    c = read_ssh_config(expanduser("~/.ssh/config"))
    if hostname in c.hosts():
        return c.host(hostname).get('user')
    raise ValueError(f"Host {hostname} is not in your ssh configuration file")

def update_vscode_ssh_config(user: str, port: int):
    """Updates the local SSH config to allow VS Code to connect to localhost port."""
    host_alias = 'vscode-server'
    config_path = expanduser("~/.ssh/config")
    c = read_ssh_config(config_path)

    # Only update if changed
    if host_alias in c.hosts():
        h = c.host(host_alias)
        if h.get('hostname') == 'localhost' and h.get('user') == user and str(h.get('port')) == str(port):
            return 
        c.remove(host_alias)

    c.add(host_alias, Hostname="localhost", User=user, Port=port,
          StrictHostKeyChecking="no", UserKnownHostsFile="/dev/null")
    c.save()

# --- Factory Function ---

def create_connection(args, user: str, host: str) -> SSHExecutor:
    """
    Smart factory that automatically chooses the connection strategy:
    1. If Router config is available (Env or CLI) -> RouterSocketExecutor (passwordless)
    2. Otherwise -> DirectFabricExecutor (original mode with password/OTP)
    """
    # 1. Try to build Router Config
    env_config = RouterConfig.from_env()
    router_addr = args.router or (env_config.address if env_config else None)
    hpc_real = args.hpc_real_host or (env_config.hpc_host if env_config else None)

    if router_addr and hpc_real:
        # Use Router Jump Mode
        config = RouterConfig(
            address=router_addr,
            socket_path=args.router_socket or (env_config.socket_path if env_config else "/tmp/hpc_socket"),
            hpc_host=hpc_real,
            hpc_port=args.hpc_real_port or (env_config.hpc_port if env_config else "22")
        )
        return RouterSocketExecutor(config)
    
    # 2. Fallback to Direct Connection (Original Way)
    # Handle legacy jump host logic
    gateway = None
    if args.jump_host:
        logging.info(f"Using standard jump host: {args.jump_host}")
        gateway_conn = Connection(
            args.jump_host, 
            user=args.jump_user or user, 
            port=args.jump_port or 22
        )
        try:
            gateway_conn.open()
        except Exception:
            # Prompt for jump server password
            j_pass = getpass.getpass(f"({args.jump_user or user}@{args.jump_host}) Jump Server Password: ")
            gateway_conn.connect_kwargs = {'password': j_pass}
            gateway_conn.open()
        
        # Set keepalive on gateway connection
        if gateway_conn.is_connected and gateway_conn.transport:
            gateway_conn.transport.set_keepalive(60)
        gateway = gateway_conn
    
    return DirectFabricExecutor(
        host, 
        user, 
        args.port, 
        gateway=gateway, 
        connect_kwargs={'timeout': 90}
    )

# --- Main Entry Point ---

def main():
    logging.basicConfig(
        stream=sys.stderr,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO, 
    )

    parser = argparse.ArgumentParser(description="Reserve vscode remote server via Router Jump")
    parser.add_argument('destination', type=str, help="HPC Headnode (e.g. user@hpc-alias)")
    parser.add_argument('-p', "--port", type=int, help="Port to connect to") 
    parser.add_argument('-f', "--forward-port", type=int, default=2222, help="Local port for VS Code")
    parser.add_argument("--partition", type=str, help="Slurm partition")
    parser.add_argument("--compute-node", type=str, help="Specific compute node hostname")
    parser.add_argument("-n", "--num-cpus", type=int, default=1, help="Number of CPUs requested for the job.")
    parser.add_argument("-m", "--memory_per_cpu", type=str, default="8G", help="Memory per cpu requested for the job.")
    parser.add_argument("--env", "--micromamba-env", type=str, dest="env_name", help="Micromamba environment name to activate before running commands.")
    parser.add_argument("--timeout", type=int, default=3000, help="Server idle timeout in seconds (default: 300).")
    parser.add_argument("--setup-proxy", action="store_true", help="Automatically configure HTTP proxy tunnel on compute nodes.")
    parser.add_argument("--proxy-login-host", type=str, default="login01", help="Login host used to reach the HTTP proxy.")
    parser.add_argument("--proxy-target-host", type=str, default="172.16.75.119", help="Internal HTTP proxy host.")
    parser.add_argument("--proxy-target-port", type=int, default=3128, help="Internal HTTP proxy port.")
    parser.add_argument("--proxy-local-port", type=int, default=9999, help="Local port on compute nodes that exposes the HTTP proxy.")
    
    # Router Config Args (Optional - enables Router Jump mode)
    parser.add_argument("--router", type=str, default=os.environ.get("LCS_ROUTER"), 
                        help="Router SSH address (enables Router Jump mode, or set LCS_ROUTER env var)")
    parser.add_argument("--router-socket", type=str, default=os.environ.get("LCS_SOCKET", "/tmp/hpc_socket"), 
                        help="Path to the SSH socket on the router (or set LCS_SOCKET env var)")
    parser.add_argument("--hpc-real-host", type=str, default=os.environ.get("LCS_HPC_HOST"), 
                        help="Real HPC user@ip (enables Router Jump mode, or set LCS_HPC_HOST env var)")
    parser.add_argument("--hpc-real-port", type=str, default=os.environ.get("LCS_HPC_PORT", "22"), 
                        help="Real HPC SSH port (or set LCS_HPC_PORT env var)")
    
    # Legacy jump host support (for Direct mode)
    parser.add_argument("--jump-host", type=str, help="Jump server address (for Direct mode)")
    parser.add_argument("--jump-port", type=int, default=22, help="Jump server port")
    parser.add_argument("--jump-user", type=str, help="User for jump server (defaults to target user)")

    args = parser.parse_args()

    # 1. Resolve User/Host
    if '@' in args.destination:
        user, host = args.destination.split('@')
    else:
        host = args.destination
        user = get_ssh_user_from_config(host)

    # 2. Create Connection (Auto-Switching between Router and Direct modes)
    conn = create_connection(args, user, host)
    
    # Wait for connection to stabilize before running commands
    logging.info("Waiting for connection to stabilize...")
    time.sleep(2)

    if args.setup_proxy:
        logging.info("Ensuring proxy environment variables are configured...")
        ensure_proxy_env_config(conn, args.proxy_local_port)

    # 4. Main Workflow
    logging.info("Trying to reserve a remote compute node...")
    job_id, node, port = launch_compute(
        conn, args.partition, args.num_cpus, args.memory_per_cpu, 
        args.compute_node, args.timeout, args.env_name
    )
    logging.info(f"A job (id={job_id}) has been reserved on node {node}")

    if args.setup_proxy:
        logging.info("Setting up proxy tunnel on compute node...")
        try:
            ensure_proxy_tunnel(
                conn,
                node,
                args.proxy_login_host,
                args.proxy_target_host,
                args.proxy_target_port,
                args.proxy_local_port,
            )
        except Exception as err:
            logging.warning(f"Failed to configure proxy tunnel: {err}")
            logging.info(f"You can manually run on {node}: ssh -N -L {args.proxy_local_port}:{args.proxy_target_host}:{args.proxy_target_port} {args.proxy_login_host}")

    # 5. Port Forwarding & Keep-Alive
    logging.info(f"Setup port forwarding: localhost:{args.forward_port} => {node}:22")
    
    with conn.forward_local(args.forward_port, 22, remote_host=node):
        update_vscode_ssh_config(user, args.forward_port)
        logging.info("🚀 VS Code Server is ready!")
        logging.info(f"👉 Connect to host 'vscode-server' in VS Code.")
        logging.info("Press Ctrl+C to stop.")

        patience = 3
        time.sleep(15)
        try:
            while True:
                # Simple heartbeat check
                resp = check_compute(conn, node, port, env_name=args.env_name)
                if not resp.startswith('SUCCESS'):
                    logging.error(f"An error occurred during the communication with the remote server: {resp}")
                    if patience <= 0:
                        sys.exit(1)
                    patience -= 1
                else:
                    patience = 3
                time.sleep(15)
        except KeyboardInterrupt:
            logging.info("\nShutting down...")

if __name__ == "__main__":
    main()
