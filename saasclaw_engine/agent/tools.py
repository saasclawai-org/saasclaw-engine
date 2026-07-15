"""Agent tools — file operations, git, and shell execution.

Each tool takes a workspace path and arguments, returns a string result.
Tools are designed to be safe: no root, no network, locked to workspace dir.
"""
import json
import logging
import os
import re
import subprocess
import threading
import uuid
import urllib.parse
import urllib.request
from html.parser import HTMLParser

import shlex
from django.conf import settings

# Max output size per tool call (truncated if exceeded)
MAX_OUTPUT = 20000

logger = logging.getLogger(__name__)

from .tool_subtasks import background_command, poll_command, spawn_subtask, check_subtask, TOOL_DEFINITIONS

# Docker sandbox configuration
SANDBOX_IMAGE = "saasclaw-sandbox:latest"
SANDBOX_ENABLED = False  # Set to False to disable Docker sandbox

# URL allowlist for web_fetch — only well-known public docs/APIs
WEB_FETCH_ALLOWED_HOSTS = {
    "developer.mozilla.org",
    "docs.python.org",
    "docs.djangoproject.com",
    "nextjs.org",
    "react.dev",
    "nodejs.org",
    "docs.npmjs.com",
    "tailwindcss.com",
    "stackoverflow.com",
    "github.com",
    "raw.githubusercontent.com",
    "api.github.com",
    "fonts.googleapis.com",
    "cdn.jsdelivr.net",
    "unpkg.com",
}

# Track files read in the current agent turn to prevent re-read loops
_read_cache: set = set()


def _truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (truncated, {len(text) - limit} more chars)"


def _safe_path(workspace_path: str, rel_path: str) -> str:
    """Resolve rel_path within workspace, preventing directory traversal."""
    full = os.path.realpath(os.path.join(workspace_path, rel_path))
    workspace_real = os.path.realpath(workspace_path)
    if not full.startswith(workspace_real + os.sep) and full != workspace_real:
        raise ValueError(f"Path '{rel_path}' is outside the workspace.")
    return full


# ---------------------------------------------------------------------------
# File tools
# ---------------------------------------------------------------------------

def read_file(workspace_path: str, path: str, start_line: int = 0, end_line: int = 0) -> str:
    """Read a file from the workspace.
    
    Optional start_line/end_line (1-indexed) for reading specific sections of large files.
    If omitted, reads the entire file (truncated at MAX_OUTPUT).
    """
    full = _safe_path(workspace_path, path)
    if not os.path.isfile(full):
        return f"Error: '{path}' is not a file or does not exist."
    
    basename = os.path.basename(path)
    if basename in SKIP_FILES:
        return f"Skipped '{basename}' — generated lock file, not useful to read."
    if _is_minified(basename):
        return f"Skipped '{basename}' — minified output. Read the source file instead."
    parts = path.split('/')
    if any(p in SKIP_DIRS for p in parts):
        return f"Skipped '{path}' — this is inside a build/dependency directory. Read from src/ instead."
    
    # Track reads to prevent re-reading the same file in a loop
    abs_path = os.path.normpath(full)
    mtime = os.path.getmtime(full)
    cache_key = (abs_path, mtime)
    if cache_key in _read_cache:
        return f"You already read '{path}' in this session. It hasn't changed. Use your previous reading instead of re-reading it."
    _read_cache.add(cache_key)
    
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            if start_line or end_line:
                # Paginated read: read specific line range
                all_lines = f.readlines()
                total = len(all_lines)
                start = max(1, start_line) - 1  # convert to 0-indexed
                end = end_line if end_line else total
                selected = all_lines[start:end]
                content = ''.join(selected)
                shown_start = start + 1
                shown_end = min(end, total)
                header = f"[Lines {shown_start}-{shown_end} of {total}]\n"
                if end < total:
                    return header + content + f"\n... ({total - end} more lines after {shown_end}). Read with start_line={end + 1}."
                return header + content
            else:
                content = f.read()
                total_lines = content.count('\n') + 1
                if len(content) > MAX_OUTPUT:
                    truncated = content[:MAX_OUTPUT]
                    lines_shown = truncated.count('\n') + 1
                    remaining = total_lines - lines_shown
                    return truncated + f"\n\n... (truncated at line {lines_shown} of {total_lines}, {remaining} more lines remaining). Use start_line={lines_shown + 1} to read the rest, or replace_in_file to edit specific sections."
                return content
    except Exception as exc:
        return f"Error reading file: {exc}"


def _load_saasclaw_config(workspace_path: str) -> dict:
    """Load .saasclaw project config if it exists."""
    import json as _json
    config_path = os.path.join(workspace_path, ".saasclaw")
    if os.path.isfile(config_path):
        try:
            with open(config_path) as f:
                return _json.load(f)
        except Exception:
            return {}
    # Also check for .saasclaw/config.json (directory-based projects)
    dir_config = os.path.join(workspace_path, ".saasclaw", "config.json")
    if os.path.isfile(dir_config):
        try:
            with open(dir_config) as f:
                return _json.load(f)
        except Exception:
            return {}
    return {}


def _match_glob(pattern: str, path: str) -> bool:
    """Simple glob matcher supporting ** and *."""
    import fnmatch
    # Normalize path separators
    path = path.replace("\\", "/")
    pattern = pattern.replace("\\", "/")
    # fnmatch doesn't handle ** well, so do a two-pass approach
    if "**" in pattern:
        # Convert ** to match any number of path segments
        # Split on ** and match segments
        parts = pattern.split("**")
        if len(parts) == 2:
            prefix, suffix = parts
            prefix = prefix.rstrip("/")
            suffix = suffix.lstrip("/")
            if prefix and not path.startswith(prefix + "/"):
                return False
            if suffix and not fnmatch.fnmatch(path.split("/")[-1] if "/" not in path else path, "*" + suffix):
                # Check if any suffix matches
                if not any(fnmatch.fnmatch(p, suffix) for p in path.split("/")):
                    return False
            return True
    return fnmatch.fnmatch(path, pattern)


def _file_size_warning(path: str, line_count: int, workspace_path: str = None) -> str:
    """Generate a warning or block based on .saasclaw config or defaults."""
    config = _load_saasclaw_config(workspace_path) if workspace_path else {}
    file_limits = config.get("file_limits", {})
    enforce = config.get("enforce", "warn")

    # Find matching limit
    limit = None
    matched_pattern = None
    for pattern, lim in file_limits.items():
        if pattern == "default":
            if limit is None:
                limit = lim
                matched_pattern = pattern
            continue
        if _match_glob(pattern, path):
            limit = lim
            matched_pattern = pattern
            break  # First match wins (config ordering)

    if limit is None:
        limit = file_limits.get("default", 500)

    if line_count <= limit:
        return ""

    # Build the message
    basename = os.path.basename(path)
    is_page = basename in ("page.tsx", "page.jsx")

    if is_page:
        msg = (
            f"\n\n🚫 BLOCKED: '{path}' is now {line_count} lines (limit: {limit}, matched: '{matched_pattern}'). "
            f"page.tsx MUST stay under {limit} lines. Extract your new code into a custom hook:\n"
            f"  1. Create src/hooks/use<Name>.ts with the state and handlers\n"
            f"  2. Create src/lib/<name>.ts with pure game logic (if not already done)\n"
            f"  3. In page.tsx: import the hook and add a thin dispatch case\n"
            f"Do NOT add more code to {path}. Create the hook file instead."
        )
    else:
        msg = (
            f"\n\n🚫 BLOCKED: '{path}' is now {line_count} lines (limit: {limit}, matched: '{matched_pattern}'). "
            f"Split this file into smaller modules before adding more code."
        )

    if enforce == "block":
        return msg + "\n\nERROR: File exceeds size limit. Write was REJECTED. Refactor and try again."
    else:
        return msg.replace("🚫 BLOCKED", "⚠️ WARNING")



def write_file(workspace_path: str, path: str, content: str) -> str:
    """Create or overwrite a file in the workspace."""
    import logging
    _logger = logging.getLogger(__name__)

    # Reject empty content (prevents 0-byte write loops)
    if not content or not content.strip():
        return f"Error: write_file called with empty content for '{path or '(no path)'}'. Provide the file content to write."

    # Auto-correct common mistakes
    if not path or path.strip('/') == '' or path.strip('/') == '.':
        # Model tried to write to workspace root - infer filename from content
        _logger.warning("write_file: empty/root path, auto-inferring from content (first 200 chars): %s", content[:200])
        content_lower = content.strip().lower()
        if content_lower.startswith("<!doctype html") or content_lower.startswith("<html"):
            path = "index.html"
            _logger.info("write_file: auto-corrected to index.html (detected HTML content)")
        elif "def " in content and "import " in content:
            # Python file - try to extract a class or function name
            import re
            m = re.search(r"class\s+(\w+)", content)
            if m:
                path = m.group(1).lower() + ".py"
            else:
                path = "main.py"
            _logger.info("write_file: auto-corrected to %s (detected Python content)", path)
        else:
            # Last resort: default to index.html for the project
            _logger.warning("write_file: could not detect file type, defaulting to index.html")
            path = "index.html"

    # Reject index.html writes in Next.js projects (should use page.tsx)
    if path == "index.html":
        has_next = os.path.exists(os.path.join(workspace_path, "next.config.js")) or                    os.path.exists(os.path.join(workspace_path, "next.config.ts")) or                    os.path.exists(os.path.join(workspace_path, "next.config.mjs"))
        if has_next:
            return "Error: Next.js projects use the App Router. Write to 'src/app/page.tsx' (or the relevant route) instead of 'index.html'."

    full = _safe_path(workspace_path, path)
    if os.path.isdir(full):
        return f"Error: '{path}' is a directory, not a file. Specify a file path like '{path}/index.html'."
    os.makedirs(os.path.dirname(full), exist_ok=True)
    # Check file size limit BEFORE writing (block mode)
    line_count = content.count('\n') + 1
    size_check = _file_size_warning(path, line_count, workspace_path)
    if "ERROR: File exceeds size limit" in size_check:
        return f"Error writing {path}: file would be {line_count} lines, exceeding the configured limit.{size_check}"

    try:
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Wrote {len(content)} bytes to {path}{size_check}"
    except Exception as exc:
        return f"Error writing file: {exc}"


def replace_in_file(workspace_path: str, path: str, edits: list) -> str:
    """Apply targeted search/replace edits to a file. Much cheaper than full rewrite.

    Each edit is {"search": "exact text to find", "replace": "replacement text"}.
    The search text must match exactly (including whitespace). If a search text
    appears multiple times, all occurrences are replaced.
    """
    full = _safe_path(workspace_path, path)
    if not os.path.isfile(full):
        return f"Error: '{path}' does not exist. Use write_file for new files."
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as exc:
        return f"Error reading file: {exc}"

    results = []
    for i, edit in enumerate(edits):
        search = edit.get("search", "")
        replace = edit.get("replace", "")
        # Fix double-escaped quotes from some LLMs (\\" -> ")
        if "\\" in search:
            search = search.replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t')
            replace = replace.replace('\\"', '"').replace('\\n', '\n').replace('\\t', '\t')
        if not search:
            results.append(f"  Edit {i+1}: skipped (empty search)")
            continue
        count = content.count(search)
        if count == 0:
            # Try with stripped whitespace for robustness
            search_stripped = search.strip()
            if search_stripped and search_stripped in content:
                content = content.replace(search_stripped, replace, 1)
                results.append(f"  Edit {i+1}: applied (1 match, fuzzy)")
            else:
                results.append(f"  Edit {i+1}: NOT FOUND")
                return f"Error applying edits to {path}:\n" + "\n".join(results) + f"\n\nFile unchanged. Search text was:\n---\n{search[:200]}\n---"
        else:
            content = content.replace(search, replace, count if count == 1 else 1)
            results.append(f"  Edit {i+1}: applied ({count} match{'es' if count > 1 else ''})")

    # Check file size limit BEFORE writing (block mode)
    line_count = content.count('\n') + 1
    warning = _file_size_warning(path, line_count, workspace_path)
    if "ERROR: File exceeds size limit" in warning:
        return f"Error editing {path}: file would be {line_count} lines, exceeding the configured limit.{warning}"

    try:
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Edited {path}:\n" + "\n".join(results) + warning
    except Exception as exc:
        return f"Error writing file: {exc}"


# Framework-agnostic dirs/files that agents should never waste tokens reading.
# Generated, installed, or build artifacts — not source code.
SKIP_DIRS = {
    # VCS / env
    ".git", "__pycache__", ".venv", "venv", "env", ".env",
    # Node
    "node_modules", "dist", "build", "out", ".next", ".nuxt", ".cache", "coverage",
    ".turbo", ".parcel-cache", "bower_components",
    # Python/Django
    ".mypy_cache", ".pytest_cache", ".tox", ".eggs", ".ruff_cache",
    # Misc
    ".vscode", ".idea", ".DS_Store", ".github", ".vercel", ".netlify",
}
SKIP_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "composer.lock",
    "Gemfile.lock", "poetry.lock", "Cargo.lock", "go.sum",
}


def _is_minified(filename: str) -> bool:
    return filename.endswith(".min.js") or filename.endswith(".min.css")


def list_files(workspace_path: str, path: str = ".") -> str:
    """List files in a directory, with sizes. Skips build artifacts, deps, and generated files."""
    full = _safe_path(workspace_path, path)
    if not os.path.isdir(full):
        return f"Error: '{path}' is not a directory."

    # Cache list_files too — prevent re-listing same dir in a loop
    list_cache_key = os.path.normpath(full)
    if list_cache_key in _read_cache:
        return f"You already listed '{path}' in this session. The files haven't changed. Use your previous listing."
    _read_cache.add(list_cache_key)

    entries = []
    skipped = []
    try:
        for name in sorted(os.listdir(full)):
            if name in SKIP_DIRS:
                skipped.append(name)
                continue
            if name in SKIP_FILES or _is_minified(name):
                skipped.append(name)
                continue
            entry_path = os.path.join(full, name)
            if os.path.isdir(entry_path):
                entries.append(f"  {name}/")
            else:
                size = os.path.getsize(entry_path)
                entries.append(f"  {name} ({size}b)")
    except Exception as exc:
        return f"Error listing directory: {exc}"

    if not entries and not skipped:
        return f"'{path}' is empty."

    result = f"{path}:\n" + "\n".join(entries)
    if skipped:
        result += f"\n\n(hidden: {', '.join(sorted(skipped))})"
    if len(entries) > 10:
        result += "\n\n💡 Focus on source files in src/. Skip config unless needed."
    return result


# ---------------------------------------------------------------------------
# Git tools
# ---------------------------------------------------------------------------

def _git(workspace_path: str, *args) -> str:
    """Run a git command in the workspace."""
    try:
        import os as _os, pwd as _pwd
        _env = dict(_os.environ)
        _env["HOME"] = "/srv/saasclaw"
        _env["GIT_SSH_COMMAND"] = "ssh -i /srv/saasclaw/.ssh/id_ed25519_deploy -o StrictHostKeyChecking=no"
        _env["GIT_AUTHOR_NAME"] = _env.get("GIT_AUTHOR_NAME", "SaaSClaw Agent")
        _env["GIT_AUTHOR_EMAIL"] = _env.get("GIT_AUTHOR_EMAIL", "saasclaw@saasclaw.ai")
        _env["GIT_COMMITTER_NAME"] = _env.get("GIT_COMMITTER_NAME", "SaaSClaw Agent")
        _env["GIT_COMMITTER_EMAIL"] = _env.get("GIT_COMMITTER_EMAIL", "saasclaw@saasclaw.ai")
        _pw = _pwd.getpwnam("saasclaw")
        result = subprocess.run(
            ["git"] + list(args),
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=30,
            user=_pw.pw_uid,
            group=_pw.pw_gid,
            env=_env,
        )
        output = result.stdout + result.stderr
        return _truncate(output.strip()) if output else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: git command timed out."
    except Exception as exc:
        return f"Error: {exc}"


def git_status(workspace_path: str) -> str:
    return _git(workspace_path, "status", "--short")


def git_diff(workspace_path: str, cached: bool = False) -> str:
    args = ["diff"]
    if cached:
        args.append("--cached")
    return _git(workspace_path, *args)


def git_commit(workspace_path: str, message: str) -> str:
    # Stage all changes then commit
    _git(workspace_path, "add", "-A")
    return _git(workspace_path, "commit", "-m", message)


def git_log(workspace_path: str, limit: int = 10) -> str:
    return _git(workspace_path, "log", f"-{limit}", "--oneline")


# ---------------------------------------------------------------------------
# Docker sandbox
# ---------------------------------------------------------------------------

def _run_in_sandbox(workspace_path: str, command: str, timeout: int = 120):
    """Execute a command inside an isolated Docker container.

    Returns output string on success, or None to signal fallback to host.
    """
    command = re.sub(r'\bpython\b(?!3)', 'python3', command)
    real_workspace = os.path.realpath(workspace_path)
    if not os.path.isdir(real_workspace):
        return f"Error: workspace path '{workspace_path}' does not exist."

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

    try:
        result = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=timeout + 15,
        )
        output = result.stdout + result.stderr
        return _truncate(output.strip()) if output.strip() else "(no output)"
    except FileNotFoundError:
        logger.warning("Docker not found, falling back to host")
        return None
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s (sandbox)."
    except Exception as exc:
        logger.warning("Sandbox error, falling back: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Shell tool
# ---------------------------------------------------------------------------

def run_command(workspace_path: str, command: str, timeout: int = 120) -> str:
    """Execute a shell command in the workspace.

    Blocked commands: rm -rf /, sudo, curl, wget, nc, ssh, scp.
    Runs inside Docker sandbox when available (network disabled, filesystem isolated).
    Falls back to host execution if Docker is unavailable.
    """
    blocked = ["sudo", "rm -rf /", "curl ", "wget ", "nc ", "ssh ", "scp "]
    for b in blocked:
        if b in command:
            return f"Error: blocked command pattern '{b.strip()}'"

    # Try Docker sandbox first
    if SANDBOX_ENABLED:
        result = _run_in_sandbox(workspace_path, command, timeout)
        if result is not None:
            return result

    # Fallback: host execution
    logger.warning("HOST exec (no sandbox): %s", command[:100])
    command = re.sub(r'\bpython\b(?!3)', 'python3', command)
    try:
        result = subprocess.run(
            command, cwd=workspace_path,
            capture_output=True, text=True,
            timeout=timeout, shell=True,
        )
        output = result.stdout + result.stderr
        return _truncate(output.strip()) if output else "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s."
    except Exception as exc:
        return f"Error: {exc}"


def _deploy_project_tool(workspace_path: str, environment: str = "preview", session_id: str | None = None) -> str:
    """Deploy the project to preview or production.
    
    This triggers the SaaSClaw deploy pipeline:
    1. Builds the project (npm run build / python manage.py collectstatic)
    2. Merges the work branch into main
    3. Pushes to origin
    4. Deploys to the VPS
    5. Configures nginx
    6. Returns the live URL
    """
    import django
    import json as _json
    
    try:
        from saasclaw_engine.projects.models import Project
        from saasclaw_engine.deployments.models import Environment
        from saasclaw_engine.studio_models.models import Workspace
        
        # Find the project from the workspace path
        ws = Workspace.objects.filter(local_path=workspace_path, is_active=True).first()
        if not ws:
            # Try matching by path prefix
            ws = Workspace.objects.filter(local_path=workspace_path).first()
        if not ws:
            return "Error: Could not determine project from workspace path."
        
        project = ws.project
        env = project.environments.filter(name=environment).first()
        if not env:
            return f"Error: No '{environment}' environment configured for project '{project.slug}'."
        
        results = []
        
        # Step 1: Detect project type and build
        files_present = set()
        try:
            files_present = set(os.listdir(workspace_path))
        except Exception:
            pass
        
        is_node = "package.json" in files_present
        is_django = "manage.py" in files_present
        
        if is_node:
            results.append("📦 Building Node.js project...")
            # Install dependencies if node_modules is missing or incomplete
            if not os.path.isdir(os.path.join(workspace_path, "node_modules")):
                install_result = run_command(workspace_path, "npm install", timeout=120)
                results.append(install_result)
            build_result = run_command(workspace_path, "npm run build", timeout=120)
            results.append(build_result)
            
            # Check for dist/ or build/
            dist_dir = None
            for candidate in ["dist", "build", "out"]:
                if os.path.isdir(os.path.join(workspace_path, candidate)):
                    dist_dir = candidate
                    break
            if not dist_dir:
                results.append("⚠️ Could not find build output directory (dist/, build/, out/).")
                return "\n".join(results)
            
        elif is_django:
            results.append("🐍 Building Django project...")
            collect_result = run_command(workspace_path, "python manage.py collectstatic --noinput", timeout=120)
            results.append(collect_result)
            dist_dir = None  # Django uses different deploy path
            
        else:
            # Static site — just use workspace root
            dist_dir = None
            results.append("📄 Static project detected.")
        
        # Step 2: Commit any uncommitted changes
        status = git_status(workspace_path)
        if "nothing to commit" not in status and "clean" not in status.lower():
            results.append("📝 Committing changes...")
            commit_result = git_commit(workspace_path, f"Deploy: {environment} deployment")
            results.append(commit_result)
        
        # Step 3: Merge work branch into main and push
        deploy_repo = f"/srv/saasclaw/projects/{project.slug}/repo"
        if os.path.isdir(deploy_repo):
            results.append("🔄 Merging to main...")
            # Fetch from work branch
            _git_merge(deploy_repo, project, ws, results)
        
        # Step 4: Deploy via celery task
        results.append(f"🚀 Deploying to {environment}...")
        try:
            from saasclaw_engine.agents.tasks import run_preview_deploy_job
            task_result = run_preview_deploy_job.delay(project.id, ws.user.id, session_id=session_id)
            deployment_id = task_result.get(timeout=180)
            results.append(f"✅ Deploy task completed (id: {deployment_id})")
        except Exception as exc:
            error_msg = str(exc)
            results.append(f"❌ Deploy failed:\n{error_msg}")
            results.append("\n💡 Fix the errors above, then run deploy again.")
            return "\n".join(results)
        
        # Step 5: Copy build output to web root + configure nginx (for static sites)
        if env.runtime_kind == 'static' and dist_dir:
            web_root = f"/srv/saasclaw/projects/{project.slug}/runtime/{environment}/web"
            results.append(f"🌐 Deploying files to {web_root}...")
            try:
                import os as _os
                import shutil as _shutil
                import os as _os
                _os.makedirs(web_root, exist_ok=True)
                src_dist = _os.path.join(workspace_path, dist_dir)
                # Clear old contents
                for old in _os.listdir(web_root):
                    old_path = _os.path.join(web_root, old)
                    if _os.path.isdir(old_path):
                        _shutil.rmtree(old_path)
                    else:
                        _os.remove(old_path)
                # Copy new build output
                for item in _os.listdir(src_dist):
                    src_item = _os.path.join(src_dist, item)
                    dst_item = _os.path.join(web_root, item)
                    if _os.path.isdir(src_item):
                        _shutil.copytree(src_item, dst_item)
                    else:
                        _shutil.copy2(src_item, dst_item)
                results.append(f"✅ Copied {dist_dir}/ contents to {web_root}")
            except Exception as exc:
                results.append(f"⚠️ File copy error: {exc}")
            
            results.append(f"🌐 Configuring nginx for {env.domain}...")
            try:
                _ensure_nginx_config(project.slug, env.domain, web_root, environment)
                results.append(f"✅ Nginx configured")
            except Exception as exc:
                results.append(f"⚠️ Nginx config error: {exc}")
        
        url = f"https://{env.domain}"
        results.append(f"\n🎉 **Deployed to {environment}!** Live at: {url}")
        
        return "\n".join(results)
        
    except Exception as exc:
        import traceback
        return f"Deploy error: {exc}\n{traceback.format_exc()[-500:]}"


def _git_merge(deploy_repo, project, workspace, results):
    """Merge work branch into main in the deploy repo.

    The workspace branch may only exist locally — push it to origin first
    so the deploy repo (which resets to origin/main) can see it.
    """
    import subprocess as sp
    work_branch = workspace.work_branch
    try:
        # Push the workspace branch to origin so the deploy repo can fetch it
        workspace_path = workspace.local_path
        push = sp.run(
            ["git", "push", "origin", work_branch],
            cwd=workspace_path, capture_output=True, text=True, timeout=30,
        )
        if push.returncode == 0:
            results.append(f"✅ Pushed {work_branch} to origin")
        else:
            # Branch may already exist on remote with same content — check if it's a non-fast-forward
            if "Everything up-to-date" in push.stdout or "Everything up-to-date" in push.stderr:
                results.append(f"ℹ️ {work_branch} already on origin")
            else:
                results.append(f"⚠️ Could not push {work_branch}: {push.stderr[:200]}")

        sp.run(["git", "fetch", "origin"], cwd=deploy_repo, capture_output=True, timeout=30)
        merge = sp.run(
            ["git", "merge", f"origin/{work_branch}", "--no-edit"],
            cwd=deploy_repo, capture_output=True, text=True, timeout=30
        )
        if "CONFLICT" in merge.stdout or "conflict" in merge.stderr:
            sp.run(["git", "merge", "--abort"], cwd=deploy_repo, capture_output=True)
            results.append("⚠️ Merge conflict — aborting deploy.")
            return
        # Check if merge actually did anything
        if "Already up to date" in merge.stdout or "Already up to date" in merge.stderr:
            results.append(f"ℹ️ {work_branch} already merged into main")
        else:
            results.append(f"✅ Merged {work_branch} into main")

        push = sp.run(
            ["git", "push", "origin", "main"],
            cwd=deploy_repo, capture_output=True, text=True, timeout=30
        )
        if push.returncode == 0:
            results.append("✅ Pushed to origin/main")
        else:
            results.append(f"⚠️ Push error: {push.stderr[:200]}")
    except Exception as exc:
        results.append(f"⚠️ Git error: {exc}")


def _ensure_nginx_config(slug, domain, web_root, environment='preview'):
    """Create or update nginx config for a project."""
    import subprocess as sp
    suffix = 'preview' if environment == 'preview' else 'production'
    site_name = f"saasclaw-{slug}-{suffix}"
    site_file = f"/etc/nginx/sites-available/{site_name}"
    site_enabled = f"/etc/nginx/sites-enabled/{site_name}"
    
    # Determine which SSL cert to use
    if environment == 'preview':
        ssl_cert = f"/etc/letsencrypt/live/{settings.PREVIEW_BASE_DOMAIN}/fullchain.pem"
        ssl_key = f"/etc/letsencrypt/live/{settings.PREVIEW_BASE_DOMAIN}/privkey.pem"
    else:
        ssl_cert = "/etc/letsencrypt/live/saasclaw.ai/fullchain.pem"
        ssl_key = "/etc/letsencrypt/live/saasclaw.ai/privkey.pem"
    
    # Include security headers if available
    security_include = ""
    security_path = "/etc/nginx/snippets/saasclaw-security.conf"
    if os.path.exists(security_path):
        security_include = f"    include {security_path};\n"

    # Include preview branding if available
    branding_include = ""
    if environment == 'preview':
        branding_path = "/etc/nginx/snippets/saasclaw-preview-branding.conf"
        if os.path.exists(branding_path):
            branding_include = f"    include {branding_path};\n"

    config = f"""server {{
    listen 80;
    listen [::]:80;
    server_name {domain};
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name {domain};

    ssl_certificate {ssl_cert};
    ssl_certificate_key {ssl_key};
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
{security_include}{branding_include}
    client_max_body_size 25m;

    root {web_root};
    index index.html;

    # Proxy Tax API to Django
    location /api/v1/tax/ {{
        proxy_pass http://127.0.0.1:8010/api/v1/tax/;
        proxy_set_header Host saasclaw.ai;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_redirect off;
    }}

    # SSE streaming for agent sessions
    location ~ /api/v1/projects/[^/]+/sessions/[^/]+/send/ {{
        proxy_pass http://127.0.0.1:8010;
        proxy_set_header Host saasclaw.ai;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_redirect off;
        proxy_http_version 1.1;
        proxy_set_header Connection '';
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }}

    # Proxy Form API to Django
    location /api/forms/ {{
        proxy_pass http://127.0.0.1:8010;
        proxy_set_header Host saasclaw.ai;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_redirect off;
    }}

    location / {{
        try_files $uri $uri/ /index.html;
    }}
}}
"""
    with open(site_file, 'w') as f:
        f.write(config)
    if not os.path.exists(site_enabled):
        os.symlink(site_file, site_enabled)
    sp.run(['nginx', '-t'], capture_output=True, timeout=10)
    sp.run(['systemctl', 'reload', 'nginx'], capture_output=True, timeout=10)


# ---------------------------------------------------------------------------
# Tool registry — maps tool names to functions
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Web tools
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    """Extract readable text from HTML, skipping scripts/styles."""
    def __init__(self):
        super().__init__()
        self.text = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "footer", "header"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "header"):
            self._skip = False
        elif tag in ("p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr"):
            self.text.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.text.append(data.strip())


def _is_url_allowed(url: str) -> bool:
    """Check if a URL's host is in the allowlist."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        # Allow if host matches allowlist or any parent domain
        for allowed in WEB_FETCH_ALLOWED_HOSTS:
            if host == allowed or host.endswith("." + allowed):
                return True
        return False
    except Exception:
        return False


def web_fetch(workspace_path: str, url: str, max_chars: int = 5000) -> str:
    """Fetch a URL and extract readable text content.

    Restricted to well-known documentation/API hosts to prevent data exfiltration.
    """
    if not url.startswith(("http://", "https://")):
        return "Error: URL must start with http:// or https://"

    if not _is_url_allowed(url):
        allowed_list = ", ".join(sorted(WEB_FETCH_ALLOWED_HOSTS))
        return (f"Error: URL host '{url}' is not in the allowed list. "
                f"Allowed hosts: {allowed_list}")

    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; SaaSClaw-Studio/1.0)",
                "Accept": "text/html,application/json,text/plain",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read(500_000).decode("utf-8", errors="replace")  # 500KB max

        if "json" in content_type:
            return raw[:max_chars]

        if "html" in content_type:
            extractor = _TextExtractor()
            extractor.feed(raw)
            text = " \n".join(t for t in extractor.text if t)
            # Collapse excessive whitespace
            text = re.sub(r"\n{3,}", "\n\n", text)
            return text[:max_chars]

        return raw[:max_chars]

    except Exception as exc:
        return f"Error fetching URL: {exc}"


def web_search(workspace_path: str, query: str, count: int = 5) -> str:
    """Search the web using DuckDuckGo's lite endpoint (free, no API key needed)."""
    try:
        search_url = "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote(query)
        req = urllib.request.Request(
            search_url,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read(200_000).decode("utf-8", errors="replace")

        # Extract results from lite DDG HTML
        results = []
        for m in re.finditer(r'href="(https?://[^"]+)"[^>]*>([^<]{10,})</a>', raw, re.S):
            url = m.group(1)
            title = m.group(2).strip()
            if "duckduckgo" in url:
                continue
            results.append((url, title))
            if len(results) >= count:
                break

        if not results:
            return "No results found."

        lines = []
        for i, (url, title) in enumerate(results):
            lines.append(f"{i+1}. {title}\n   {url}")
        return "\n\n".join(lines)

    except Exception as exc:
        return f"Error searching: {exc}"


def get_env_vars(workspace_path: str) -> str:
    """List env vars for this project from the database."""
    try:
        import django
        import os
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
        django.setup()
        from saasclaw_engine.deployments.models import EnvironmentVariable, Environment
        # Find environment by matching workspace path to project
        # workspace_path is like /srv/saasclaw/workspaces/<uuid>
        # The project slug appears in /srv/saasclaw/projects/<slug>/
        # We can't easily map back, so list all preview env vars
        envs = Environment.objects.filter(slug='preview')
        lines = []
        for env in envs:
            vars_qs = EnvironmentVariable.objects.filter(environment=env).order_by('key')
            for v in vars_qs:
                val_display = '••••••••' if v.is_secret else v.value[:40]
                lines.append(f"{v.key}={val_display} ({env.project.slug})")
        return '\n'.join(lines) if lines else 'No custom env vars set.'
    except Exception as exc:
        return f"Note: Could not read env vars from DB. Use the Studio UI to manage them. Error: {exc}"


def set_env_var(workspace_path: str, key: str, value: str, is_secret: bool = True) -> str:
    """Set an env var — writes to DB EnvironmentVariable, repo .env, and runtime .env."""
    try:
        import django
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
        django.setup()
        from saasclaw_engine.deployments.models import EnvironmentVariable, Environment
        from saasclaw_engine.projects.models import Project

        git_file = os.path.join(workspace_path, '.git')
        slug = None
        if os.path.isfile(git_file):
            with open(git_file) as f:
                git_content = f.read().strip()
            if 'gitdir' in git_content:
                gitdir = git_content.split('gitdir:')[-1].strip()
                parts = gitdir.split('/')
                if 'projects' in parts:
                    idx = parts.index('projects')
                    if idx + 1 < len(parts):
                        slug = parts[idx + 1]

        if not slug:
            return f"Could not determine project from workspace. Set {key} manually in the Studio UI."

        project = Project.objects.filter(slug=slug).first()
        if not project:
            return f"Project '{slug}' not found."

        actions = []

        # 1. Write to DB EnvironmentVariable (used by deploy pipeline)
        for env in Environment.objects.filter(project=project):
            EnvironmentVariable.objects.update_or_create(
                environment=env, key=key,
                defaults={'value': value, 'is_secret': is_secret, 'project': project},
            )
        actions.append('DB')

        # 2. Write to repo .env (Vite reads this at build time)
        repo_env_file = os.path.join(project.workspace_root, 'repo', '.env')
        existing = {}
        if os.path.isfile(repo_env_file):
            with open(repo_env_file) as f:
                for line in f:
                    line = line.strip()
                    if '=' in line and not line.startswith('#'):
                        k, _, v = line.partition('=')
                        existing[k.strip()] = v.strip()
        existing[key] = value
        os.makedirs(os.path.dirname(repo_env_file), exist_ok=True)
        with open(repo_env_file, 'w') as f:
            for k, v in sorted(existing.items()):
                f.write(f'{k}={v}\n')
        actions.append('repo .env')

        # 3. Write to runtime .env (for already-running instances)
        runtime_env_file = os.path.join(project.workspace_root, 'runtime', 'preview', '.env')
        existing_rt = {}
        if os.path.isfile(runtime_env_file):
            with open(runtime_env_file) as f:
                for line in f:
                    line = line.strip()
                    if '=' in line and not line.startswith('#'):
                        k, _, v = line.partition('=')
                        existing_rt[k.strip()] = v.strip()
        existing_rt[key] = value
        os.makedirs(os.path.dirname(runtime_env_file), exist_ok=True)
        with open(runtime_env_file, 'w') as f:
            for k, v in sorted(existing_rt.items()):
                f.write(f'{k}={v}\n')
        actions.append('runtime .env')

        display = '••••••••' if is_secret else value[:20]
        return f"Set {key}={display} in: {', '.join(actions)}"
    except Exception as exc:
        return f"Error setting env var: {exc}"


def _project_slug_from_workspace(workspace_path: str) -> str:
    """Derive project slug from workspace path via .git file."""
    git_file = os.path.join(workspace_path, '.git')
    if os.path.isfile(git_file):
        with open(git_file) as f:
            git_content = f.read().strip()
        if 'gitdir' in git_content:
            gitdir = git_content.split('gitdir:')[-1].strip()
            parts = gitdir.split('/')
            if 'projects' in parts:
                idx = parts.index('projects')
                if idx + 1 < len(parts):
                    return parts[idx + 1]
    return ''


def supabase_sql(workspace_path: str, sql: str) -> str:
    """Execute SQL against the project's Supabase database via direct Postgres connection.
    
    Requires SUPABASE_DB_PASSWORD env var to be set. The project ref is extracted
    from VITE_SUPABASE_URL. Connects to db.<ref>.supabase.co:5432 as postgres.
    """
    try:
        import django
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
        django.setup()
        from saasclaw_engine.deployments.models import EnvironmentVariable, Environment
        from saasclaw_engine.projects.models import Project

        slug = _project_slug_from_workspace(workspace_path)
        if not slug:
            return "Error: Could not determine project slug from workspace."

        project = Project.objects.filter(slug=slug).first()
        if not project:
            return f"Error: Project '{slug}' not found."

        # Gather env vars from all environments
        env_vars = {}
        for env in Environment.objects.filter(project=project):
            for ev in EnvironmentVariable.objects.filter(environment=env):
                if ev.key not in env_vars:
                    env_vars[ev.key] = ev.value

        # Also check repo .env and runtime .env
        for env_file_path in [
            os.path.join(project.workspace_root, 'repo', '.env'),
            os.path.join(project.workspace_root, 'runtime', 'preview', '.env'),
        ]:
            if os.path.isfile(env_file_path):
                with open(env_file_path) as f:
                    for line in f:
                        line = line.strip()
                        if '=' in line and not line.startswith('#'):
                            k, _, v = line.partition('=')
                            if k.strip() not in env_vars:
                                env_vars[k.strip()] = v.strip()

        db_password = env_vars.get('SUPABASE_DB_PASSWORD')
        if not db_password:
            return ("Error: SUPABASE_DB_PASSWORD not set. Ask the user for their Supabase "
                    "database password (found in Supabase dashboard → Project Settings → Database). "
                    "Then set it with set_env_var.")

        # Extract project ref from Supabase URL
        supabase_url = env_vars.get('VITE_SUPABASE_URL', '')
        # URL format: https://<project-ref>.supabase.co
        import re
        ref_match = re.match(r'https?://([a-z0-9]+)\.supabase\.(co|in|red)', supabase_url)
        if not ref_match:
            return f"Error: Could not extract project ref from VITE_SUPABASE_URL='{supabase_url}'"
        project_ref = ref_match.group(1)

        # Connect and execute using psycopg (v3)
        import psycopg
        db_host = f'db.{project_ref}.supabase.co'
        conn = psycopg.connect(
            host=db_host,
            port=5432,
            dbname='postgres',
            user='postgres',
            password=db_password,
            connect_timeout=10,
        )
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    # Try to fetch results for SELECT/RETURNING queries
                    try:
                        rows = cur.fetchall()
                        col_names = [desc[0] for desc in cur.description] if cur.description else []
                        result_lines = []
                        if col_names:
                            result_lines.append(' | '.join(col_names))
                            result_lines.append('-' * (len(' | '.join(col_names))))
                            for row in rows[:50]:
                                result_lines.append(' | '.join(str(v) for v in row))
                            if len(rows) > 50:
                                result_lines.append(f'... ({len(rows)} total rows)')
                        if cur.rowcount > 0 and not rows:
                            result_lines.append(f'{cur.rowcount} row(s) affected.')
                        return '\n'.join(result_lines) if result_lines else 'OK (no rows returned)'
                    except psycopg.ProgrammingError:
                        # No results to fetch (DDL, INSERT without RETURNING, etc.)
                        return f'OK ({cur.rowcount} row(s) affected)' if cur.rowcount >= 0 else 'OK'
        finally:
            conn.close()
    except psycopg.OperationalError as exc:
        return f"Error connecting to Supabase DB: {exc}"
    except Exception as exc:
        return f"Error executing SQL: {exc}"


def update_todos(workspace_path: str, items: list) -> str:
    """Sync the project todo list. items = [{text, done}, ...]"""
    try:
        import django
        import os
        os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
        django.setup()
        from saasclaw_engine.studio_models.models import Todo
        from saasclaw_engine.projects.models import Project
        slug = _project_slug_from_workspace(workspace_path)
        if not slug:
            return "Could not determine project from workspace."
        try:
            project = Project.objects.get(slug=slug)
        except Project.DoesNotExist:
            return f"Project '{slug}' not found."
        Todo.objects.filter(project=project).delete()
        for i, item in enumerate(items):
            Todo.objects.create(
                project=project,
                text=item.get('text', ''),
                done=item.get('done', False),
                order=i,
            )
        return f"Updated {len(items)} todos."
    except Exception as exc:
        return f"Error updating todos: {exc}"


# ---------------------------------------------------------------------------
# Multi-file apply_patch tool
# ---------------------------------------------------------------------------

def apply_patch_tool(workspace_path: str, operations: list) -> str:
    """Apply edits to multiple files in a single tool call.

    Each operation specifies a file path and either 'create' (new file)
    or 'edit' (search/replace pairs). Best-effort: continues on failures.
    """
    results = []
    for op in operations:
        path = op.get("path", "")
        action = op.get("action", "")
        if not path:
            results.append("Error: operation missing 'path'")
            continue
        if action == "create":
            content = op.get("content", "")
            r = write_file(workspace_path, path, content)
            results.append(r)
        elif action == "edit":
            edits = op.get("edits", [])
            r = replace_in_file(workspace_path, path, edits)
            results.append(r)
        else:
            results.append(f"Error: unknown action '{action}' for {path}. Use 'create' or 'edit'.")
    return "\n".join(results)


def _read_logs_tool(workspace_path, source='django', lines=50, project_slug=''):
    import subprocess as _sub, os as _os
    log_map = {
        'django': '/srv/saasclaw/logs/django.log',
        'gunicorn': '/srv/saasclaw/logs/gunicorn-error.log',
        'nginx': '/srv/saasclaw/logs/nginx-error.log',
        'deploy': None,
    }
    if source == 'deploy' and project_slug:
        import django as _dj
        import sys as _sys
        _os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
        _sys.path.insert(0, '/srv/saasclaw/app')
        _dj.setup()
        from saasclaw_engine.deployments.models import Deployment
        from saasclaw_engine.projects.models import Project as _P
        p = _P.objects.filter(slug=project_slug).first()
        if not p:
            return f"No project '{project_slug}'"
        deps = Deployment.objects.filter(project=p).order_by('-created_at')[:3]
        if not deps:
            return 'No deployments found.'
        lines_out = []
        for d in deps:
            lines_out.append(f"Deploy #{d.id} ({d.status}) at {d.created_at}")
            if d.output:
                lines_out.append(d.output[-2000:])
            lines_out.append('')
        return '\n'.join(lines_out)
    log_path = log_map.get(source, '')
    if not log_path or not _os.path.isfile(log_path):
        return f"Log not found for '{source}'. Available: {list(log_map.keys())}"
    try:
        result = _sub.run(['tail', '-n', str(lines), log_path], capture_output=True, text=True, timeout=10)
        output = result.stdout
        if project_slug:
            filtered = [l for l in output.split('\n') if project_slug.lower() in l.lower()]
            output = '\n'.join(filtered) if filtered else '(no matching lines)'
        return output[-4000:]
    except Exception as e:
        return f'Error: {e}'


def _test_api_tool(workspace_path, url='', method='GET', headers=None, body=''):
    import subprocess as _sub
    cmd = ['curl', '-s', '-w', '\n%{http_code}', '--max-time', '10']
    if method:
        cmd.extend(['-X', method])
    for k, v in (headers or {}).items():
        cmd.extend(['-H', f'{k}: {v}'])
    if body:
        cmd.extend(['-d', body])
    cmd.append(url)
    try:
        result = _sub.run(cmd, capture_output=True, text=True, timeout=15)
        parts = result.stdout.rsplit('\n', 1)
        body_out = parts[0] if len(parts) == 2 else result.stdout
        status = parts[1] if len(parts) == 2 else 'unknown'
        return f'Status: {status}\n{body_out[:2000]}'
    except Exception as e:
        return f'Error: {e}'



def _db_command_tool(workspace_path, command="", timeout=60):
    """Run a whitelisted DB command outside Docker sandbox."""
    import subprocess as _sub, os as _os, sys as _sys, django as _dj
    _os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    _sys.path.insert(0, "/srv/saasclaw/app")
    _dj.setup()
    from saasclaw_engine.studio_models.models import Workspace

    safe = [
        "npx prisma migrate", "npx prisma db push", "npx prisma generate",
        "npx prisma studio", "npx prisma seed", "npx prisma validate",
        "prisma migrate", "prisma db push", "prisma generate",
        "prisma studio", "prisma seed", "prisma validate",
        "python manage.py migrate", "python manage.py makemigrations",
        "python manage.py showmigrations", "python manage.py shell -c",
        "python manage.py loaddata", "python manage.py dumpdata",
    ]
    cmd = command.strip()
    if not any(cmd.startswith(p) for p in safe):
        return "Error: command not whitelisted. Allowed: prisma migrate/db push/generate/studio/seed/validate, manage.py migrate/makemigrations/showmigrations/shell -c/loaddata/dumpdata"
    if "--sql" in cmd or "DROP " in cmd.upper() or "DELETE FROM" in cmd.upper():
        return "Error: raw SQL not allowed."

    ws = Workspace.objects.filter(local_path=workspace_path, is_active=True).first()
    if not ws:
        ws = Workspace.objects.filter(local_path=workspace_path).first()
    if not ws:
        return "Error: cannot resolve project."
    project = ws.project
    slug = project.slug
    repo_path = f"/srv/saasclaw/projects/{slug}/repo"
    if not _os.path.isdir(repo_path):
        return f"Error: repo not found at {repo_path}"

    env = dict(_os.environ)
    env["PYTHONPATH"] = "/srv/saasclaw/app"
    env["DJANGO_SETTINGS_MODULE"] = "config.settings"
    try:
        for ec in project.environments.all():
            for var in ec.variables.all():
                k, v = var.key, var.value or ""
                if k in ("DATABASE_URL","POSTGRES_PASSWORD","POSTGRES_HOST","POSTGRES_PORT","POSTGRES_USER","POSTGRES_DB","DJANGO_SECRET_KEY","SECRET_KEY"):
                    env[k] = v
    except Exception:
        pass

    try:
        result = _sub.run(cmd, shell=True, cwd=repo_path, env=env,
                          capture_output=True, text=True, timeout=timeout)
        output = (result.stdout or "") + (result.stderr or "")
        return output[-4000:] if len(output) > 4000 else output
    except _sub.TimeoutExpired:
        return f"Error: timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"


def _project_status_tool(workspace_path, section=""):
    """Read-only project infrastructure info."""
    import subprocess as _sub, os as _os, sys as _sys, django as _dj
    _os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    _sys.path.insert(0, "/srv/saasclaw/app")
    _dj.setup()
    from saasclaw_engine.studio_models.models import Workspace
    from saasclaw_engine.deployments.models import Deployment

    ws = Workspace.objects.filter(local_path=workspace_path, is_active=True).first()
    if not ws:
        ws = Workspace.objects.filter(local_path=workspace_path).first()
    if not ws:
        return "Error: cannot resolve project."
    project = ws.project
    slug = project.slug
    parts = []
    sections = [s.strip() for s in section.split(",")] if section else ["all"]

    if "nginx" in sections or "all" in sections:
        nginx_path = f"/etc/nginx/sites-enabled/saasclaw-{slug}-preview"
        if _os.path.isfile(nginx_path):
            with open(nginx_path) as f:
                parts.append(f"=== Nginx Config ({nginx_path}) ===\n{f.read()[:3000]}")
        else:
            parts.append(f"=== Nginx: no config at {nginx_path} ===")

    if "service" in sections or "all" in sections:
        svc = f"saasclaw-{slug}-preview"
        try:
            r = _sub.run(["systemctl", "is-active", svc], capture_output=True, text=True, timeout=5)
            parts.append(f"=== Service: {svc} ===\nStatus: {r.stdout.strip()}")
        except Exception:
            parts.append(f"=== Service: {svc} not found (static or not deployed) ===")

    if "env" in sections or "all" in sections:
        parts.append("=== Environment Variables (secrets masked) ===")
        try:
            found = False
            for ec in project.environments.all():
                for var in ec.variables.all():
                    found = True
                    k, v = var.key, var.value or ""
                    if any(w in k.upper() for w in ["SECRET","PASSWORD","KEY","TOKEN","CREDENTIAL"]):
                        parts.append(f"  {k} = {v[:8]}***" if len(v) > 8 else f"  {k} = ***")
                    else:
                        parts.append(f"  {k} = {v}")
            if not found:
                parts.append("  (no env vars set)")
        except Exception as e:
            parts.append(f"  Error: {e}")

    if "deploys" in sections or "all" in sections:
        parts.append("=== Recent Deploys ===")
        try:
            deps = Deployment.objects.filter(project=project).order_by("-created_at")[:5]
            if not deps:
                parts.append("  No deployments yet.")
            for d in deps:
                parts.append(f"  #{d.id} | {d.status} | {d.environment.slug} | {d.created_at}")
        except Exception as e:
            parts.append(f"  Error: {e}")

    if "all" in sections:
        parts.append("=== Project Info ===")
        parts.append(f"  Slug: {slug}")
        parts.append(f"  Runtime: {project.runtime or 'unknown'}")
        parts.append(f"  Preview: {project.preview_domain or 'not set'}")
        if project.form_api_key:
            parts.append(f"  Form API key: {project.form_api_key[:12]}***")
        else:
            parts.append("  Form API key: not set")

    return "\n".join(parts)

def execute_tool(workspace_path: str, name: str, args: dict, restricted: bool = False, session_id: str | None = None) -> str:
    """Dispatch a tool call by name.
    
    The 'restricted' flag is kept for backward compatibility but no longer
    blanket-blocks write tools. Tool access is controlled entirely by the
    profile's allowed_tools list, which is filtered when building the tool
    definitions sent to the LLM (see runner.py). If a tool call makes it
    here, it was in the allowed list.
    """
    # Safety net: block known-dangerous shell commands regardless of profile
    if name == 'run_command':
        cmd = args.get('command', '')
        blocked_patterns = ['sudo', 'rm -rf /', 'curl ', 'wget ', 'nc ', 'ssh ', 'scp ']
        for b in blocked_patterns:
            if b in cmd:
                return f"Error: blocked command pattern '{b.strip()}'."
    handlers = {
        "read_file": lambda: read_file(workspace_path, args.get("path", ""), int(args.get("start_line", 0)), int(args.get("end_line", 0))),
        "write_file": lambda: write_file(workspace_path, args.get("path", ""), args.get("content", "")),
        "replace_in_file": lambda: replace_in_file(workspace_path, args.get("path", ""), args.get("edits", [])),
        "list_files": lambda: list_files(workspace_path, args.get("path", ".")),
        "git_status": lambda: git_status(workspace_path),
        "git_diff": lambda: git_diff(workspace_path, args.get("cached", False)),
        "git_commit": lambda: git_commit(workspace_path, args.get("message", "Agent commit")),
        "run_command": lambda: run_command(workspace_path, args.get("command", ""), args.get("timeout", 120)),
        "read_logs": lambda: _read_logs_tool(workspace_path, args.get("source", "django"), int(args.get("lines", 50)), args.get("project_slug", "")),
        "test_api": lambda: _test_api_tool(workspace_path, args.get("url", ""), args.get("method", "GET"), args.get("headers"), args.get("body", "")),
        "db_command": lambda: _db_command_tool(workspace_path, args.get("command", ""), int(args.get("timeout", 60))),
        "project_status": lambda: _project_status_tool(workspace_path, args.get("section", "all")),
        "deploy_project": lambda: _deploy_project_tool(workspace_path, args.get("environment", "preview"), session_id=session_id),
        "web_fetch": lambda: web_fetch(workspace_path, args.get("url", ""), args.get("max_chars", 5000)),
        "web_search": lambda: web_search(workspace_path, args.get("query", ""), args.get("count", 5)),
        "set_env_var": lambda: set_env_var(workspace_path, args.get("key", ""), args.get("value", ""), args.get("is_secret", True)),
        "get_env_vars": lambda: get_env_vars(workspace_path),
        "update_todos": lambda: update_todos(workspace_path, args.get("items", [])),
        "apply_patch": lambda: apply_patch_tool(workspace_path, args.get("operations", [])),
        "background_command": lambda: background_command(workspace_path, args.get("command", "")),
        "poll_command": lambda: poll_command(workspace_path, args.get("job_id", "")),
        "spawn_subtask": lambda: spawn_subtask(workspace_path, args.get("task", ""), args.get("model", "")),
        "check_subtask": lambda: check_subtask(workspace_path, args.get("task_id", "")),
        "supabase_sql": lambda: supabase_sql(workspace_path, args.get("sql", "")),
    }
    handler = handlers.get(name)
    if not handler:
        return f"Error: unknown tool '{name}'."
    try:
        return handler()
    except Exception as exc:
        return f"Error executing {name}: {exc}"
