"""Codebase scanning and context patching for agent runner."""
import json
import logging
import os

from django.conf import settings

logger = logging.getLogger(__name__)


def _scan_project_files(workspace_path: str) -> str:
    """Quick scan of project files -- gives the agent a map of editable files only."""
    skip = {'.git', '__pycache__', '.venv', 'venv', 'node_modules', 'dist', 'build', 
            'out', '.next', '.cache', 'coverage', '.mypy_cache', '.pytest_cache', '.ruff_cache',
            '.nuxt', '.output', 'staticfiles', 'media', '__pypackages__', '.eggs', '*.egg-info'}
    skip_ext = {'.pyc', '.pyo', '.min.js', '.min.css', '.min.json', '.map', '.lock',
                '.log', '.svg', '.png', '.jpg', '.jpeg', '.gif', '.ico', '.webp', '.woff', '.woff2',
                '.ttf', '.eot', '.wasm', '.bin', '.exe', '.dll', '.so', '.dylib', '.zip', '.tar',
                '.gz', '.sqlite', '.sqlite3', '.db'}
    skip_files = {'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml', 'Pipfile.lock',
                  'poetry.lock', '.env', '.env.local', '.env.production', 'bun.lockb'}
    # Only show files the agent can meaningfully edit
    editable_ext = {'.py', '.js', '.jsx', '.ts', '.tsx', '.html', '.css', '.scss', '.less',
                    '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf', '.md', '.txt',
                    '.sh', '.bash', '.sql', '.graphql', '.gql', '.xml', '.csv', '.rb', '.php',
                    '.go', '.rs', '.java', '.kt', '.swift', '.vue', '.svelte', '.astro', '.mdx'}
    lines = []
    for root, dirs, files in os.walk(workspace_path):
        dirs[:] = [d for d in sorted(dirs) if d not in skip]
        rel = os.path.relpath(root, workspace_path)
        if rel == '.':
            rel = ''
        for f in sorted(files):
            if f in skip_files:
                continue
            _, ext = os.path.splitext(f)
            if ext in skip_ext:
                continue
            if ext and ext not in editable_ext:
                continue
            path = f"{rel}/{f}" if rel else f
            size = os.path.getsize(os.path.join(root, f))
            if size > 500:
                lines.append(f"  {path} ({size}b)")
            else:
                lines.append(f"  {path}")
        if len(lines) > 50:
            lines.append("  ... (more files)")
            break
    return "\n".join(lines) if lines else ""

def _patch_context_on_tool(project_id, tool_name, tool_args, tool_result):
    """Incrementally update cached project context when agent writes files."""
    if tool_result.startswith("Error:"):
        return
    try:
        from saasclaw_engine.projects.models import Project
        project = Project.objects.get(id=project_id)
        if not project.context_cache:
            return
        _do_patch_context(project, tool_name, tool_args, tool_result)
    except Exception:
        import logging
        logging.getLogger(__name__).debug("context patch failed", exc_info=True)

def _do_patch_context(project, tool_name, tool_args, tool_result):
    """Actual patching logic."""
    if tool_name == "write_file":
        path = tool_args.get("path", "")
        content_text = tool_args.get("content", "")
        if not path or not content_text:
            return
        lines = project.context_cache
        patched = lines
        Q = chr(34)
        
        # New file not in listing
        if path not in lines:
            patched += "\n  " + path
        
        # Extract types
        new_types = []
        for line in content_text.split("\n"):
            s = line.strip()
            if s.startswith("type ") and "=" in s:
                new_types.append(s.split("#")[0].strip().rstrip("{"))
            elif s.startswith("interface ") and "{" in s:
                new_types.append(s.split("{")[0].strip().rstrip(":"))
            elif s.startswith("export type ") and "=" in s:
                new_types.append(s.split("#")[0].strip())
            elif s.startswith("export interface "):
                new_types.append(s.split("{")[0].strip())
        
        for t in new_types:
            if t not in patched:
                if "Existing type definitions" not in patched:
                    patched += "\n\nExisting type definitions (MUST preserve these):"
                patched += "\n  " + path + ": " + t
        
        # Extract local imports
        new_imports = []
        for line in content_text.split("\n"):
            s = line.strip()
            if s.startswith("from " + Q + "@/"):
                mod = s.split(Q)[1] if Q in s else ""
                if mod.startswith("@/"):
                    new_imports.append(mod[2:])
            elif s.startswith("from " + Q + "./") or s.startswith("from " + Q + "../"):
                mod = s.split(Q)[1] if Q in s else ""
                new_imports.append(mod)
        
        filtered = [m for m in new_imports if m and "react" not in m]
        for m in filtered:
            if m not in patched:
                if "Local modules imported:" not in patched:
                    patched += "\n\nLocal modules imported: "
                if "Local modules imported:" in patched:
                    patched = patched.rstrip() + ", " + m
        
        # Python requirements
        if path == "requirements.txt":
            for line in content_text.split("\n"):
                s = line.strip()
                if s and not s.startswith("#") and not s.startswith("-"):
                    pkg = s.split("[")[0].split("=")[0].split(">")[0].split("<")[0].strip()
                    if pkg and pkg not in patched:
                        if "Installed packages:" in patched:
                            patched = patched.rstrip() + ", " + pkg
                        elif "NEVER rewrite requirements.txt" in patched:
                            patched += "\nInstalled packages: " + pkg
        
        if patched != lines:
            project.context_cache = patched
            from saasclaw_engine.projects.models import Project
            Project.objects.filter(id=project.id).update(context_cache=patched)
    
    elif tool_name == "replace_in_file":
        path = tool_args.get("path", "")
        edits = tool_args.get("edits", [])
        if not path or not edits:
            return
        lines = project.context_cache
        patched = lines
        Q = chr(34)
        
        new_types = []
        new_imports = []
        for edit in edits:
            replace_text = edit.get("replace", "")
            for line in replace_text.split("\n"):
                s = line.strip()
                if s.startswith("type ") and "=" in s:
                    new_types.append(s.split("#")[0].strip().rstrip("{"))
                elif s.startswith("interface ") and "{" in s:
                    new_types.append(s.split("{")[0].strip().rstrip(":"))
                elif s.startswith("from " + Q + "@/"):
                    mod = s.split(Q)[1] if Q in s else ""
                    if mod.startswith("@/"):
                        new_imports.append(mod[2:])
        
        for t in new_types:
            if t not in patched:
                if "Existing type definitions" not in patched:
                    patched += "\n\nExisting type definitions (MUST preserve these):"
                patched += "\n  " + path + ": " + t
        for m in new_imports:
            if m and m not in patched and "react" not in m:
                if "Local modules imported:" not in patched:
                    patched += "\n\nLocal modules imported: "
                if "Local modules imported:" in patched:
                    patched = patched.rstrip() + ", " + m
        
        if patched != lines:
            project.context_cache = patched
            from saasclaw_engine.projects.models import Project
            Project.objects.filter(id=project.id).update(context_cache=patched)

def _scan_codebase_context(workspace_path: str) -> str:
    """Scan the existing codebase to build a framework-agnostic project context."""
    hints = []
    Q = chr(34)  # double quote

    def _read_file(rel_path, max_lines=80):
        fp = os.path.join(workspace_path, rel_path)
        if not os.path.isfile(fp):
            return None
        try:
            with open(fp, errors="replace") as f:
                result = []
                for i, line in enumerate(f):
                    if i >= max_lines:
                        break
                    result.append(line.rstrip())
                return result
        except Exception:
            return None

    def _read_package_json():
        fp = os.path.join(workspace_path, "package.json")
        if not os.path.isfile(fp):
            return None
        try:
            with open(fp) as f:
                return json.load(f)
        except Exception:
            return None

    # --- Phase 1: Identify project type ---
    top_files = set()
    try:
        top_files = set(os.listdir(workspace_path))
    except Exception:
        pass

    project_type = "unknown"
    pkg = _read_package_json()
    deps = {}
    dev_deps = {}
    scripts = {}
    if pkg:
        deps = pkg.get("dependencies", {})
        dev_deps = pkg.get("devDependencies", {})
        scripts = pkg.get("scripts", {})
        if "next" in deps or "next" in dev_deps:
            project_type = "Next.js"
        elif "react" in deps or "react" in dev_deps:
            project_type = "React"
        elif "vite" in deps or "vite" in dev_deps:
            project_type = "Vite"
        elif "express" in deps:
            project_type = "Express"
        elif os.path.exists(os.path.join(workspace_path, "hugo.toml")):
            project_type = "Hugo"
        elif "package.json" in top_files:
            project_type = "Node.js"
    if any(fname.endswith('.csproj') for fname in top_files):
        project_type = ".NET"
    elif "manage.py" in top_files:
        project_type = "Django"
    elif "app.py" in top_files:
        project_type = "Flask"
    elif "Cargo.toml" in top_files:
        project_type = "Rust"
    elif "go.mod" in top_files:
        project_type = "Go"
    elif "requirements.txt" in top_files or "pyproject.toml" in top_files:
        project_type = "Python"

    hints.append(f"Project type: {project_type}")

    # --- Phase 2: Extract build/test commands ---
    build_cmd = None
    test_cmd = None
    if scripts:
        if "build" in scripts:
            build_cmd = "npm run build"
        if "test" in scripts:
            test_cmd = "npm test"
        if "dev" in scripts:
            hints.append("Dev server: npm run dev")
    if project_type == "Django":
        test_cmd = "python manage.py test --settings=config.test_settings"
        hints.append("Dev server: python manage.py runserver")
    elif project_type == "Flask":
        test_cmd = "pytest"
    elif project_type == "Rust":
        build_cmd = "cargo build"
        test_cmd = "cargo test"
    elif project_type == "Go":
        test_cmd = "go test ./..."
    elif project_type == "Hugo":
        build_cmd = "hugo"

    if build_cmd:
        hints.append(f"Build command: {build_cmd}")
    if test_cmd:
        hints.append(f"Test command: {test_cmd}")

    # --- Phase 3: Source structure ---
    source_dirs = []
    for d in ["src", "lib", "app", "components", "pages", "routes", "services",
              "models", "handlers", "api", "utils", "helpers", "templates",
              "static", "public", "config", "types", "interfaces"]:
        full = os.path.join(workspace_path, d)
        if os.path.isdir(full):
            source_dirs.append(d)
    if source_dirs:
        hints.append(f"Source directories: {', '.join(source_dirs)}")

    # --- Phase 4: Scan source files for types and imports ---
    import glob as _glob
    source_files = []
    for ext in ["*.py", "*.ts", "*.tsx", "*.js", "*.jsx", "*.cs", "*.go", "*.rs"]:
        source_files.extend(_glob.glob(os.path.join(workspace_path, ext)))
        for d in source_dirs:
            source_files.extend(_glob.glob(os.path.join(workspace_path, d, "**", ext), recursive=True))
    source_files = source_files[:40]

    type_defs = []
    local_imports = []
    for fp in source_files:
        rel = os.path.relpath(fp, workspace_path)
        try:
            with open(fp, errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("type ") and "=" in line:
                        type_defs.append(rel + ": " + line.split("#")[0].strip().rstrip("{"))
                    elif line.startswith("interface ") and "{" in line:
                        type_defs.append(rel + ": " + line.split("{")[0].strip().rstrip(":"))
                    elif line.startswith("export type ") and "=" in line:
                        type_defs.append(rel + ": " + line.split("#")[0].strip())
                    elif line.startswith("export interface "):
                        type_defs.append(rel + ": " + line.split("{")[0].strip())
                    # C# type extraction
                    elif line.startswith("public class ") or line.startswith("public record "):
                        cs_type = line.split("{")[0].strip().rstrip(":").rstrip()
                        if cs_type:
                            type_defs.append(rel + ": " + cs_type)
                    elif line.startswith("using ") and ";" in line:
                        mod = line.split("using ")[1].split(";")[0].strip()
                        if mod and not mod.startswith("System") and not mod.startswith("Microsoft"):
                            local_imports.append(mod)
                    # Local imports
                    elif line.startswith("from " + Q + "@/"):
                        mod = line.split(Q)[1] if Q in line else ""
                        if mod.startswith("@/"):
                            local_imports.append(mod[2:])
                    elif line.startswith("from " + Q + "./") or line.startswith("from " + Q + "../"):
                        mod = line.split(Q)[1] if Q in line else ""
                        local_imports.append(mod)
        except Exception:
            continue

    if type_defs:
        hints.append("")
        hints.append("Existing type definitions (MUST preserve these):")
        for td in sorted(set(type_defs))[:15]:
            hints.append(f"  {td}")

    if local_imports:
        filtered = sorted(set(m for m in local_imports if m and not m.startswith("react")))
        if filtered:
            hints.append("")
            hints.append(f"Local modules imported: {', '.join(filtered[:20])}")

    # --- Phase 5: Django-specific scan ---
    if project_type == "Django":
        settings_file = None
        for cand in ["config/settings.py", "project/settings.py"]:
            if os.path.exists(os.path.join(workspace_path, cand)):
                settings_file = cand
                break
        if settings_file:
            settings_lines = _read_file(settings_file)
            if settings_lines:
                installed_apps = []
                in_apps = False
                for sl in settings_lines:
                    if "INSTALLED_APPS" in sl:
                        in_apps = True
                        continue
                    if in_apps:
                        if "]" in sl:
                            break
                        app = sl.strip().strip(",'\" ")
                        if app and not app.startswith("#"):
                            installed_apps.append(app)
                if installed_apps:
                    hints.append("")
                    hints.append(f"INSTALLED_APPS ({len(installed_apps)}): {', '.join(installed_apps)}")
                    hints.append("NEVER rewrite INSTALLED_APPS - use replace_in_file to add entries.")

        req_file = _read_file("requirements.txt")
        if req_file:
            pkgs = [l.split("[")[0].split("=")[0].split(">")[0].strip() for l in req_file
                     if l.strip() and not l.startswith("#") and not l.startswith("-")]
            hints.append("")
            hints.append(f"Installed packages: {', '.join(pkgs[:15])}")
            hints.append("NEVER rewrite requirements.txt - use replace_in_file to add deps.")

        hints.append("")
        hints.append("Testing: use SQLite in-memory, NOT from /srv/saasclaw/projects/")
        hints.append("")
        hints.append("Architecture: keep views.py thin (request/response only).")
        hints.append("Business logic → services.py or services/ package (one service per domain).")
        hints.append("Complex queries → model methods or managers, not inline ORM in views.")
        hints.append("Forms/validation → forms.py.")
        hints.append("Permissions/authorization → policies.py or policies/ package.")
        hints.append("Never put imports, helpers, or business logic at the top of views.py.")
        hints.append("")
        hints.append("File size limits (CRITICAL):")
        hints.append("- NEVER create files over 500 lines. Split if growing past 500.")
        hints.append("- models.py > 300 lines → split into models/ package.")
        hints.append("- views.py > 200 lines → split into views/ package.")
        hints.append("- services.py > 200 lines → convert to services/ package.")
        hints.append("")
        hints.append("Django conventions:")
        hints.append("- Views: parse request → call service → return response. No ORM in views.")
        hints.append("- Services: all business logic and side effects. Raise exceptions for errors.")
        hints.append("- Policies: permission checks (can_user_edit, can_deploy, etc.). Import in views.")
        hints.append("- Models: data + relationships only. Custom managers for table-level queries.")
        hints.append("- Tests: tests/test_models.py, tests/test_views.py, tests/test_services.py.")
        hints.append("- Never import models across apps directly — use service layer.")

    # --- Phase 5b: Next.js/React architecture conventions ---
    if project_type in ("Next.js", "React", "Vite", "Express"):
        hints.append("")
        hints.append("Architecture: keep route handlers and pages thin.")
        hints.append("")
        hints.append("File size limits (CRITICAL):")
        hints.append("- NEVER create files over 500 lines. Split if growing past 500.")
        hints.append("- page.tsx/page.jsx: keep under 150 lines. Extract logic to lib/, components/, hooks/.")
        hints.append("- A monolithic page.tsx with embedded game/app logic is NEVER acceptable.")
        hints.append("- React components: one per file. If over 200 lines, extract sub-components.")
        hints.append("")
        hints.append("Next.js/React conventions:")
        hints.append("- src/app/page.tsx → thin shell, imports and composes components. No business logic.")
        hints.append("- src/components/ → one component per file (GameBoard.tsx, not index.tsx)")
        hints.append("- src/lib/ or src/services/ → game rules, API calls, business logic, data transforms")
        hints.append("- src/hooks/ → custom hooks (useGameState, usePlayer, etc.)")
        hints.append("- src/types/ → shared TypeScript interfaces and types")
        hints.append("- src/app/api/<name>/route.ts → one concern per route, under 100 lines")
        hints.append("- State: useReducer for complex state. Lift to parent, pass via props.")
        hints.append("- NEVER inline all game/app logic in a single useState in page.tsx.")
        hints.append("- Extract reusable logic into custom hooks so components stay declarative.")
        hints.append("")
        hints.append("File splitting rules:")
        hints.append("- New game/feature: src/components/GameName.tsx + src/lib/gameName.ts + import in page.tsx")
        hints.append("- If page.tsx is already 150+ lines: REFACTOR by extracting to lib/ and components/ BEFORE adding more.")
        hints.append("- Multiple sub-views: create src/components/GameName/ directory with separate files.")

    # --- Phase 5b2: .NET / EF Core conventions ---
    if project_type == ".NET":
        hints.append("")
        hints.append(".NET / EF Core conventions (CRITICAL):")
        hints.append("- Each entity/model class in its own .cs file, NOT all in one file.")
        hints.append("- ALWAYS add [JsonIgnore] on navigation properties (e.g. public Provider? Provider) to prevent JSON serialization cycles.")
        hints.append("- ALWAYS use .Include() when querying entities with navigation properties to eager-load related data.")
        hints.append("- Use db.Database.Migrate() for schema changes, NOT EnsureCreated(). EnsureCreated skips if __EFMigrationsHistory exists.")
        hints.append("- Seed data in AppDbContext.OnModelCreating or a separate DbSeeder, NOT inline in Program.cs.")
        hints.append("- Minimal API: group related endpoints with MapGroup, keep Program.cs under 200 lines, extract handlers.")
        hints.append("- Use [Required], [MaxLength] attributes for validation on entity properties.")
        hints.append("- Foreign keys: always define both the FK property (int ProviderId) and the navigation property (Provider? Provider).")
        hints.append("- File size: never exceed 500 lines. Split Program.cs into separate files when it grows.")
        hints.append("")

    # --- Phase 5c: Go conventions ---
    if project_type == "Go":
        hints.append("")
        hints.append("Architecture: keep handlers thin.")
        hints.append("Business logic → internal/ or service/ package.")
        hints.append("Data access → store/ or repository package.")
        hints.append("File size: never exceed 500 lines. Split handlers, services, models into separate files.")

    # --- Phase 5d: Rust conventions ---
    if project_type == "Rust":
        hints.append("")
        hints.append("Architecture: keep main.rs / handlers thin.")
        hints.append("Business logic → separate modules.")
        hints.append("Data access → model or repository modules.")
        hints.append("File size: never exceed 500 lines. Split into modules by concern.")

    # --- Phase 6: Key files listing ---
    key_files = []
    skip = {"node_modules", "__pycache__", ".next", ".git", "dist", "build", ".venv"}
    for name in sorted(top_files):
        if name.startswith("."):
            continue
        if name in skip:
            continue
        if os.path.isdir(os.path.join(workspace_path, name)):
            key_files.append(name + "/")
        else:
            key_files.append(name)
    if key_files:
        hints.append("")
        hints.append(f"Files in root: {', '.join(key_files)}")

    return "\n".join(hints)
