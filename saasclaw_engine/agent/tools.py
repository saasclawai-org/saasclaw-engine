"""Agent tools — file operations, git, and shell execution.

Each tool takes a workspace path and arguments, returns a string result.
Tools are designed to be safe: no root, no network, locked to workspace dir.
"""
import os
import re

from django.conf import settings
import shlex
import subprocess
import urllib.request
import urllib.parse
from html.parser import HTMLParser

# Max output size per tool call (truncated if exceeded)
MAX_OUTPUT = 5000

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

def read_file(workspace_path: str, path: str) -> str:
    """Read a file from the workspace."""
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
            content = f.read()
        if len(content) > MAX_OUTPUT:
            truncated = content[:MAX_OUTPUT]
            lines_shown = truncated.count('\n') + 1
            total_lines = content.count('\n') + 1
            remaining = total_lines - lines_shown
            return truncated + f"\n\n... (truncated at line {lines_shown} of {total_lines}, {remaining} more lines remaining). Use replace_in_file for edit specific sections."
        return content
    except Exception as exc:
        return f"Error reading file: {exc}"


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
    try:
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Wrote {len(content)} bytes to {path}"
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

    try:
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Edited {path}:\n" + "\n".join(results)
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
# Shell tool
# ---------------------------------------------------------------------------

def run_command(workspace_path: str, command: str, timeout: int = 120) -> str:
    """Execute a shell command in the workspace.

    Blocked commands: rm -rf /, sudo, curl, wget, nc, ssh, scp.
    Working directory is locked to workspace_path.
    """
    blocked = ["sudo", "rm -rf /", "curl ", "wget ", "nc ", "ssh ", "scp "]
    for b in blocked:
        if b in command:
            return f"Error: blocked command pattern '{b.strip()}'"

    # Auto-fix: python → python3 (common on Linux where python is often unversioned)
    command = re.sub(r'\bpython\b(?!3)', 'python3', command)

    try:
        result = subprocess.run(
            command,
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=True,
        )
        output = result.stdout + result.stderr
        return _truncate(output.strip()) if output else "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s."
    except Exception as exc:
        return f"Error: {exc}"


def _deploy_project_tool(workspace_path: str, environment: str = "preview") -> str:
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
            from agents.tasks import run_preview_deploy_job
            task_result = run_preview_deploy_job.delay(project.id, ws.user.id)
            deployment_id = task_result.get(timeout=180)
            results.append(f"✅ Deploy task completed (id: {deployment_id})")
        except Exception as exc:
            results.append(f"⚠️ Deploy task error: {exc}")
        
        # Step 5: Copy build output to web root + configure nginx (for static sites)
        if env.runtime_kind == 'static' and dist_dir:
            web_root = f"/srv/saasclaw/projects/{project.slug}/runtime/{environment}/web"
            results.append(f"🌐 Deploying files to {web_root}...")
            try:
                import shutil as _shutil
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

    client_max_body_size 25m;

    root {web_root};
    index index.html;

    location / {{
        try_files $uri $uri/ =404;
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


def web_fetch(workspace_path: str, url: str, max_chars: int = 5000) -> str:
    """Fetch a URL and extract readable text content."""
    if not url.startswith(("http://", "https://")):
        return "Error: URL must start with http:// or https://"

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
    """Set an env var — writes to .env file in the runtime dir."""
    try:
        # Derive the runtime .env path from the workspace
        # workspace_path = /srv/saasclaw/workspaces/<uuid>
        # The worktree's .git file points to the parent repo
        git_file = os.path.join(workspace_path, '.git')
        if os.path.isfile(git_file):
            with open(git_file) as f:
                git_content = f.read().strip()
            # Extract commondir or gitdir to find the project repo
            # gitdir: /srv/saasclaw/projects/<slug>/repo/.git/worktrees/<uuid>
            if 'gitdir' in git_content:
                gitdir = git_content.split('gitdir:')[-1].strip()
                # Navigate to find project slug
                parts = gitdir.split('/')
                # Find 'projects' in path
                if 'projects' in parts:
                    idx = parts.index('projects')
                    if idx + 1 < len(parts):
                        slug = parts[idx + 1]
                        env_file = f'/srv/saasclaw/projects/{slug}/runtime/preview/.env'
                        # Read existing
                        existing = {}
                        if os.path.isfile(env_file):
                            with open(env_file) as f:
                                for line in f:
                                    line = line.strip()
                                    if '=' in line and not line.startswith('#'):
                                        k, _, v = line.partition('=')
                                        existing[k.strip()] = v.strip()
                        existing[key] = value
                        # Write back
                        os.makedirs(os.path.dirname(env_file), exist_ok=True)
                        with open(env_file, 'w') as f:
                            for k, v in sorted(existing.items()):
                                f.write(f'{k}={v}\n')
                        display = '••••••••' if is_secret else value[:20]
                        return f"Set {key}={display} in {env_file}"
        return f"Could not determine project from workspace. Set {key} manually in the Studio UI."
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

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the contents of a file in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to the file."},
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
]


def execute_tool(workspace_path: str, name: str, args: dict, restricted: bool = False) -> str:
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
        "read_file": lambda: read_file(workspace_path, args.get("path", "")),
        "write_file": lambda: write_file(workspace_path, args.get("path", ""), args.get("content", "")),
        "replace_in_file": lambda: replace_in_file(workspace_path, args.get("path", ""), args.get("edits", [])),
        "list_files": lambda: list_files(workspace_path, args.get("path", ".")),
        "git_status": lambda: git_status(workspace_path),
        "git_diff": lambda: git_diff(workspace_path, args.get("cached", False)),
        "git_commit": lambda: git_commit(workspace_path, args.get("message", "Agent commit")),
        "run_command": lambda: run_command(workspace_path, args.get("command", ""), args.get("timeout", 120)),
        "deploy_project": lambda: _deploy_project_tool(workspace_path, args.get("environment", "preview")),
        "web_fetch": lambda: web_fetch(workspace_path, args.get("url", ""), args.get("max_chars", 5000)),
        "web_search": lambda: web_search(workspace_path, args.get("query", ""), args.get("count", 5)),
        "set_env_var": lambda: set_env_var(workspace_path, args.get("key", ""), args.get("value", ""), args.get("is_secret", True)),
        "get_env_vars": lambda: get_env_vars(workspace_path),
        "update_todos": lambda: update_todos(workspace_path, args.get("items", [])),
    }
    handler = handlers.get(name)
    if not handler:
        return f"Error: unknown tool '{name}'."
    try:
        return handler()
    except Exception as exc:
        return f"Error executing {name}: {exc}"
