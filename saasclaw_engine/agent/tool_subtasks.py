"""Subtask management tools — extracted from tools.py.

Handles background commands, spawning subtasks, polling, and checking results.
"""
import json
import logging
import os
import subprocess
import threading
import time
import uuid

from django.conf import settings

logger = logging.getLogger(__name__)

MAX_OUTPUT = 65536

SANDBOX_IMAGE = "saasclaw-sandbox:latest"
SANDBOX_ENABLED = False

_bg_jobs: dict = {}  # job_id -> {"process": Popen, "output": str, "done": bool, "returncode": int}

def background_command(workspace_path: str, command: str) -> str:
    """Start a long-running command in the background, return a job ID.

    Runs inside Docker sandbox when available.
    """
    blocked = ["sudo", "rm -rf /", "curl ", "wget ", "nc ", "ssh ", "scp "]
    for b in blocked:
        if b in command:
            return f"Error: blocked command pattern '{b.strip()}'"

    job_id = uuid.uuid4().hex[:8]
    real_workspace = os.path.realpath(workspace_path)

    if SANDBOX_ENABLED and os.path.isdir(real_workspace):
        # Run in Docker sandbox
        docker_cmd = [
            "docker", "run", "--rm",
            "--network", "none",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=512m",
            "--tmpfs", "/home/sandbox:rw,size=64m",
            "--memory", "512m",
            "--cpus", "1",
            "--pids-limit", "100",
            "--user", "1001:1001",
            "--workdir", "/workspace",
            "-v", f"{real_workspace}:/workspace:rw",
            SANDBOX_IMAGE,
            "bash", "-c", command,
        ]
        proc = subprocess.Popen(
            docker_cmd, cwd=workspace_path,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
    else:
        logger.warning("HOST background exec (no sandbox): %s", command[:100])
        proc = subprocess.Popen(
            command, shell=True, cwd=workspace_path,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )

    _bg_jobs[job_id] = {"process": proc, "output": "", "done": False, "returncode": None}

    def _monitor():
        try:
            out, _ = proc.communicate(timeout=3600)
            _bg_jobs[job_id]["output"] = (out or "")[:65536]
            _bg_jobs[job_id]["done"] = True
            _bg_jobs[job_id]["returncode"] = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            _bg_jobs[job_id]["output"] = _bg_jobs[job_id].get("output", "") + "\n[Timed out after 1h]"
            _bg_jobs[job_id]["done"] = True
            _bg_jobs[job_id]["returncode"] = -1

    t = threading.Thread(target=_monitor, daemon=True)
    t.start()
    return f"Started background job '{job_id}'. Use poll_command to check status. Command: {command}"


def poll_command(workspace_path: str, job_id: str) -> str:
    """Check the status and output of a background job."""
    job = _bg_jobs.get(job_id)
    if not job:
        return f"Error: unknown job ID '{job_id}'"
    status = "DONE" if job["done"] else "RUNNING"
    rc = f" (exit code {job['returncode']})" if job["done"] else ""
    output = job["output"][-3000:] if job["output"] else "(no output yet)"
    return f"Job '{job_id}': {status}{rc}\n\nOutput:\n{output}"


# ---------------------------------------------------------------------------
# Subagent spawning
# ---------------------------------------------------------------------------

def spawn_subtask(workspace_path: str, task: str, model: str = "") -> str:
    """Spawn a background subtask using the agent runner."""
    try:
        import django
        import os as _os
        _os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
        django.setup()
        from saasclaw_engine.projects.models import Project
        from saasclaw_engine.agents.models import AgentTask
        from saasclaw_engine.studio_models.models import Workspace

        # Find workspace/project from path
        ws = Workspace.objects.filter(local_path=workspace_path).first()
        if not ws:
            # Derive project slug from workspace path via .git file
            slug = ""
            _git_file = os.path.join(workspace_path, ".git")
            if os.path.isfile(_git_file):
                with open(_git_file) as _gf:
                    _git_content = _gf.read().strip()
                if "gitdir" in _git_content:
                    _gitdir = _git_content.split("gitdir:")[-1].strip()
                    _parts = _gitdir.split("/")
                    if "projects" in _parts:
                        _idx = _parts.index("projects")
                        if _idx + 1 < len(_parts):
                            slug = _parts[_idx + 1]
            if not slug:
                return f"Error: could not determine project from workspace path '{workspace_path}'"
            try:
                project = Project.objects.get(slug=slug)
            except Project.DoesNotExist:
                return f"Error: project '{slug}' not found."
        else:
            project = ws.project

        # Create an AgentTask record
        user = project.owner if hasattr(project, 'owner') else None
        agent_task = AgentTask.objects.create(
            project=project,
            requested_by=user,
            task_type=AgentTask.TaskType.EDIT_CODE,
            prompt=task,
            status=AgentTask.Status.QUEUED,
        )

        # Launch via Celery
        from saasclaw_engine.agents.tasks import run_agent_subtask
        run_agent_subtask.delay(agent_task.id, model or None)
        return f"Spawned subtask '{agent_task.id}' ({agent_task.task_type}). Use check_subtask to check progress. Task: {task[:100]}"
    except Exception as exc:
        import traceback
        return f"Error spawning subtask: {exc}\n{traceback.format_exc()[-500:]}"


def check_subtask(workspace_path: str, task_id: str) -> str:
    """Check the status of a spawned subtask."""
    try:
        import django
        import os as _os
        _os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
        django.setup()
        from saasclaw_engine.agents.models import AgentTask
        from saasclaw_engine.studio_models.models import AgentSession, AgentMessage

        try:
            agent_task = AgentTask.objects.get(id=task_id)
        except (AgentTask.DoesNotExist, ValueError):
            return f"Error: subtask '{task_id}' not found"

        status_line = f"Subtask '{task_id}': {agent_task.status} ({agent_task.task_type})"
        if agent_task.result_summary:
            status_line += f"\n\nResult: {agent_task.result_summary[:500]}"
        if agent_task.error_message:
            status_line += f"\n\nError: {agent_task.error_message[:500]}"

        # Try to get recent messages if there's a linked session
        if agent_task.session_key:
            try:
                session = AgentSession.objects.get(id=agent_task.session_key)
                msgs = AgentMessage.objects.filter(session=session).order_by('-created_at')[:3]
                recent = "\n".join(f"[{m.role}] {m.content[:200]}" for m in reversed(msgs))
                status_line += f"\n\nRecent messages:\n{recent}"
            except (AgentSession.DoesNotExist, Exception):
                pass

        return status_line
    except Exception as exc:
        return f"Error checking subtask: {exc}"


# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file in the workspace. For large files (>200 lines), use start_line and end_line to read in sections of ~200 lines at a time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to the file."},
                    "start_line": {"type": "integer", "description": "Starting line number (1-indexed). Use for paginated reads of large files.", "default": 0},
                    "end_line": {"type": "integer", "description": "Ending line number (exclusive). 0 = read to end.", "default": 0},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file in the workspace. Use for new files or complete rewrites.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File name or relative path within the project. REQUIRED. Examples: 'index.html', 'css/style.css', 'app.js'. NEVER pass '.', '/', or empty string."},

                    "content": {"type": "string", "description": "Full file content to write."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_in_file",
            "description": "Apply targeted search/replace edits to an existing file. Much cheaper than rewriting the whole file. Each edit needs exact match of search text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to the file."},
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "search": {"type": "string", "description": "Exact text to find in the file (including whitespace)."},
                                "replace": {"type": "string", "description": "Text to replace it with."},
                            },
                            "required": ["search", "replace"],
                        },
                        "description": "List of search/replace edits to apply sequentially.",
                    },
                },
                "required": ["path", "edits"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory to list. Default: root.", "default": "."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Show git working tree status (modified, added, deleted files).",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Show uncommitted changes (diff).",
            "parameters": {
                "type": "object",
                "properties": {
                    "cached": {"type": "boolean", "description": "Show staged changes only.", "default": False},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit",
            "description": "Stage all changes and commit with a message.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Commit message."},
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a shell command in the workspace. Use for tests, linting, grep, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute."},
                    "timeout": {"type": "integer", "description": "Timeout in seconds.", "default": 120},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch a URL and extract readable text. Useful for reading docs, APIs, or reference material.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch."},
                    "max_chars": {"type": "integer", "description": "Max characters to return.", "default": 5000},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current information. Returns titles, URLs, and snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "count": {"type": "integer", "description": "Number of results.", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_env_var",
            "description": "Set an environment variable for the project's preview environment. Use for API keys, secrets, config.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Variable name (e.g. STRIPE_SECRET_KEY)."},
                    "value": {"type": "string", "description": "Variable value."},
                    "is_secret": {"type": "boolean", "description": "Mark as secret to hide value in UI.", "default": True},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_env_vars",
            "description": "List environment variables for the project. Returns key names and whether they're secrets.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_todos",
            "description": "Update the project todo list. Replaces all todos. Use this to plan tasks and mark progress.",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string", "description": "Task description"},
                                "done": {"type": "boolean", "description": "Whether the task is complete"},
                            },
                            "required": ["text"],
                        },
                        "description": "List of todo items",
                    },
                },
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_logs",
            "description": "Read server or deploy logs to debug issues. Use after deploying or when something is not working.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {"type": "string", "enum": ["django", "gunicorn", "nginx", "deploy"], "description": "Log source"},
                    "lines": {"type": "integer", "description": "Number of lines to read"},
                    "project_slug": {"type": "string", "description": "Project slug to filter"}
                },
                "required": ["source"]
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "test_api",
            "description": "Test an API endpoint to verify it works. Returns status code and response. Use after writing API code to confirm endpoints return 200.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL or path to test"},
                    "method": {"type": "string", "enum": ["GET", "POST", "DELETE"], "description": "HTTP method"},
                    "headers": {"type": "object", "description": "Headers e.g. {X-Form-Key: value}"},
                    "body": {"type": "string", "description": "Request body for POST"}
                },
                "required": ["url"]
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "db_command",
            "description": "Run a database command scoped to this project. Allowed: prisma migrate/db push/generate/studio/seed/validate, manage.py migrate/makemigrations/showmigrations/shell -c/loaddata/dumpdata. Runs outside sandbox to reach PostgreSQL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command to run"},
                    "timeout": {"type": "integer", "description": "Timeout seconds"}
                },
                "required": ["command"]
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "project_status",
            "description": "Read-only project infrastructure: nginx config, service status, env vars (secrets masked), deploy history. Use to debug routing or verify service status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "section": {"type": "string", "description": "Section: nginx/service/env/deploys/all"}
                },
                "required": []
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deploy_project",
            "description": "Deploy the project to preview. Builds, commits, merges to main, deploys to VPS, configures nginx, and returns the live URL. Use when the user says 'ship it', 'deploy', 'go live', or similar. Only preview is available from the wizard; production deployments are done separately.",
            "parameters": {
                "type": "object",
                "properties": {
                    "environment": {
                        "type": "string",
                        "enum": ["preview"],
                        "description": "Which environment to deploy to. Always preview.",
                        "default": "preview",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "Apply edits to multiple files in a single call. Use for refactoring across files. Each operation creates or edits one file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "operations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string", "description": "File path relative to workspace"},
                                "action": {"type": "string", "enum": ["create", "edit"]},
                                "content": {"type": "string", "description": "Full file content (for action=create)"},
                                "edits": {"type": "array", "description": "Search/replace pairs (for action=edit)", "items": {"type": "object", "properties": {"search": {"type": "string"}, "replace": {"type": "string"}}, "required": ["search", "replace"]}},
                            },
                            "required": ["path", "action"],
                        },
                        "description": "List of file operations",
                    },
                },
                "required": ["operations"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "background_command",
            "description": "Start a long-running command (build, test suite) in the background. Returns a job ID immediately. Use poll_command to check results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "poll_command",
            "description": "Check the status and output of a background command. Returns current output and whether it's still running.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "Job ID from background_command"},
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_subtask",
            "description": "Spawn a background subtask (a separate agent run) for complex multi-step work. Returns a task ID immediately. Use check_subtask to monitor progress.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "The task description for the subagent"},
                    "model": {"type": "string", "description": "Optional model override for the subagent"},
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_subtask",
            "description": "Check the status and output of a spawned subtask. Returns current status, recent messages, and results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID from spawn_subtask"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "supabase_sql",
            "description": "Execute SQL (CREATE TABLE, INSERT, ALTER, etc.) against the project's Supabase database. Requires SUPABASE_DB_PASSWORD env var to be set. Use this to create tables, set up RLS policies, or run migrations automatically.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "SQL to execute. Can include multiple statements separated by semicolons."},
                },
                "required": ["sql"],
            },
        },
    },
]


