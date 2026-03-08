# Installation

## Install server-side application (on the HPC)

```shell
pip install 'git+https://github.com/regulatory-genomics/launch-code-server.git#egg=launch-code-server'
```

## Install client-side application (on your computer)

```shell
pip install 'git+https://github.com/regulatory-genomics/launch-code-server.git#egg=launch-code-server'[client]
```

# Usage

1. Copy your ssh public key to the HPC server if you have not done so.
   You still need to do this even if you cannot use the public key to login to the HPC server.
2. Open a terminal on your computer and type `launch_server USERNAME@HOSTNAME`.
3. Open VS Code, and from the list of remote servers choose `vscode-server` to connect.

# Web UI (MVP)

This repository now includes a local Web UI for profile management, diagnostics, launch orchestration, and log inspection.

## Features

- **Profiles**: create/edit/delete profiles that map to `launch_server` options
- **Diagnostics**: run connectivity checks (local environment, router reachability, router socket, HPC SSH, Slurm access, port forwarding)
- **Launch**: queue background launch tasks, inspect live status, stop tasks, and view full logs
- **Connection summary**: extract job/node/forwarding/vscode alias details from task logs
- **Local storage**: profiles, diagnostics history, and launch tasks under `ui/data/`

## Recommended environment

Use your micromamba environment:

```shell
micromamba activate py311
python -m pip install fastapi uvicorn jinja2 python-multipart
```

## Run the UI

### Option A: helper script (recommended)

```shell
./scripts/run_ui.sh
```

Defaults:
- host: `127.0.0.1`
- port: `8765`
- env: `py311`

You can override them:

```shell
HOST=0.0.0.0 PORT=8765 ENV_NAME=py311 ./scripts/run_ui.sh
```

### Option B: direct uvicorn

```shell
python -m uvicorn ui.app:app --host 127.0.0.1 --port 8765
```

Open:

```text
http://127.0.0.1:8765
```

## UI Structure

```text
ui/
  app.py              FastAPI entrypoint
  storage.py          local JSON persistence for profiles and run history
  launcher.py         launch_server command builder / synchronous runner logic
  tasks.py            background task manager + task log parsing
  diagnostics/        diagnostic checks and orchestration
  templates/          Jinja templates
  static/             CSS assets
  data/               generated local JSON/task files
scripts/
  run_ui.sh           helper launcher for micromamba py311
```

## Current limitations

- Launch status updates use polling, not websocket/SSE streaming.
- Stop will attempt `scancel` only when a job_id has already been parsed from logs.
- Diagnostics still use lightweight subprocess checks rather than deep reuse of every internal path in `client.py`.
