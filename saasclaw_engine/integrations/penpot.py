"""Penpot design platform integration.

Provides:
- Cookie-based auth (login-with-password RPC command)
- Project / file listing
- Full file data extraction (pages, frames, shapes, colors, typography)
- Design token extraction formatted for the AI wizard

All API calls go to Penpot's RPC endpoint:
    POST /api/rpc/command/<command>

Auth uses a cookie set by login-with-password (auth-token cookie).
The Penpot frontend strips Authorization headers, so cookie auth is required.
"""
import logging
import re
from typing import Any

import requests

logger = logging.getLogger(__name__)

# Default base URL for internal Penpot instance
PENPOT_BASE_URL = "http://127.0.0.1:9002"


def _get_base_url() -> str:
    """Get the Penpot base URL from Django settings or default."""
    try:
        from django.conf import settings
        return getattr(settings, 'PENPOT_BASE_URL', PENPOT_BASE_URL)
    except Exception:
        return PENPOT_BASE_URL


class PenpotClient:
    """Client for the Penpot RPC API using cookie-based auth."""

    def __init__(self, base_url: str | None = None, email: str = "", password: str = ""):
        self.base_url = base_url or _get_base_url()
        self.email = email
        self.password = password
        self.session = requests.Session()
        self.session.headers.update({
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        })
        self._profile: dict | None = None

    # --- Auth ---

    def login(self) -> dict:
        """Log in to Penpot and store the auth cookie.

        Returns the profile dict from the login response.
        """
        resp = self.session.post(
            f"{self.base_url}/api/rpc/command/login-with-password",
            json={"email": self.email, "password": self.password},
            timeout=15,
        )
        resp.raise_for_status()
        self._profile = resp.json()
        logger.info("Penpot login successful for %s", self.email)
        return self._profile

    @property
    def profile(self) -> dict | None:
        """Cached profile data from login or get_profile."""
        if self._profile is None:
            return self.get_profile()
        return self._profile

    def get_profile(self) -> dict:
        """Get the current user's profile."""
        resp = self.session.post(
            f"{self.base_url}/api/rpc/command/get-profile",
            json={},
            timeout=15,
        )
        resp.raise_for_status()
        self._profile = resp.json()
        return self._profile

    # --- Projects ---

    def get_all_projects(self) -> list[dict]:
        """Get all projects for the logged-in user."""
        resp = self.session.post(
            f"{self.base_url}/api/rpc/command/get-all-projects",
            json={},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def get_project_files(self, project_id: str) -> list[dict]:
        """Get all files in a Penpot project."""
        resp = self.session.post(
            f"{self.base_url}/api/rpc/command/get-project-files",
            json={"project-id": project_id},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    # --- Files ---

    def get_file(self, file_id: str) -> dict:
        """Get file metadata + full data (pages, frames, shapes)."""
        resp = self.session.post(
            f"{self.base_url}/api/rpc/command/get-file",
            json={"id": file_id},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def get_file_data(self, file_id: str) -> dict:
        """Get the full design data from a file.

        Returns the file's data object which includes:
        - pages: list of page IDs
        - pagesIndex: {page_id: {objects, name, id}}
        - options: {componentsV2, baseFontSize}
        """
        file_data = self.get_file(file_id)
        return file_data.get('data', {})

    # --- Teams ---

    def get_all_teams(self) -> list[dict]:
        """Get all teams for the logged-in user."""
        resp = self.session.post(
            f"{self.base_url}/api/rpc/command/get-all-teams",
            json={},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def create_team(self, name: str) -> dict:
        """Create a new team."""
        resp = self.session.post(
            f"{self.base_url}/api/rpc/command/create-team",
            json={"name": name},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def create_project(self, team_id: str, name: str) -> dict:
        """Create a new project in a team."""
        resp = self.session.post(
            f"{self.base_url}/api/rpc/command/create-project",
            json={"team-id": team_id, "name": name},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def create_file(self, project_id: str, name: str) -> dict:
        """Create a new file in a project."""
        resp = self.session.post(
            f"{self.base_url}/api/rpc/command/create-file",
            json={"project-id": project_id, "name": name},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()


# --- Design token extraction ---

def _hex_to_name(hex_val: str) -> str:
    """Generate a short name from a hex color."""
    return hex_val.lstrip('#').lower()[:6]


def extract_penpot_design_tokens(file_data: dict) -> dict:
    """Extract structured design tokens from a Penpot file.

    Returns a dict with:
        colors: {name: hex} — all unique solid colors found
        typography: [{selector, font_family, font_size, weight, ...}]
        spacing: [values in px]
        radii: [values in px]
        shadows: [{type, color, offset_x, offset_y, radius}]
        layout: {type, direction, padding, gap}
        component_count: int
        page_count: int
        frame_count: int
    """
    tokens = {
        "colors": {},
        "typography": [],
        "spacing": [],
        "radii": [],
        "shadows": [],
        "layout": None,
        "component_count": 0,
        "page_count": 0,
        "frame_count": 0,
    }

    data = file_data.get('data', file_data)
    pages_index = data.get('pagesIndex', {})

    seen_colors = set()
    seen_radii = set()
    seen_spacing = set()

    tokens["page_count"] = len(pages_index)

    for page_id, page in pages_index.items():
        objects = page.get('objects', {})
        for obj_id, obj in objects.items():
            _walk_penpot_node(obj, tokens, seen_colors, seen_radii, seen_spacing, depth=0)

    # Deduplicate and sort
    tokens["radii"] = sorted(set(tokens["radii"]))
    tokens["spacing"] = sorted(set(tokens["spacing"]))
    tokens["typography"] = tokens["typography"][:20]

    return tokens


def _walk_penpot_node(node: dict, tokens: dict, seen_colors: set,
                       seen_radii: set, seen_spacing: set, depth: int = 0):
    """Recursively walk a Penpot node tree extracting tokens."""
    node_type = node.get('type', '')

    # Count frames and components
    if node_type == 'frame':
        tokens["frame_count"] += 1
    if node_type in ('component', 'component-group'):
        tokens["component_count"] += 1

    # Colors from fills
    fills = node.get('fills', [])
    for fill in fills:
        fill_color = fill.get('fillColor')
        fill_opacity = fill.get('fillOpacity', 1)
        if fill_color and fill_color not in seen_colors:
            # Ensure hex format
            color = fill_color
            if not color.startswith('#'):
                color = '#' + color
            if fill_opacity < 1:
                alpha = round(fill_opacity * 255)
                color = f"{color}{alpha:02x}"
            name = _hex_to_name(color)
            tokens["colors"][name] = color
            seen_colors.add(fill_color)

    # Colors from strokes
    strokes = node.get('strokes', [])
    for stroke in strokes:
        stroke_color = stroke.get('strokeColor')
        if stroke_color and stroke_color not in seen_colors:
            color = stroke_color
            if not color.startswith('#'):
                color = '#' + color
            name = f"border-{_hex_to_name(color)}"
            tokens["colors"][name] = color
            seen_colors.add(stroke_color)

    # Typography from text nodes
    if node_type == 'text':
        typo = _extract_penpot_typography(node)
        if typo:
            tokens["typography"].append({
                "selector": node.get('name', 'text'),
                **typo,
            })

    # Corner radii
    for rkey in ('r1', 'r2', 'r3', 'r4'):
        r = node.get(rkey)
        if r and r not in seen_radii:
            tokens["radii"].append(r)
            seen_radii.add(r)

    # Layout: padding, gap
    layout = node.get('layout')
    if layout and layout != 'none':
        if not tokens["layout"] and depth <= 2:
            tokens["layout"] = {
                "type": layout,
                "padding_x": node.get('paddingLeft', 0) or 0,
                "padding_y": node.get('paddingTop', 0) or 0,
                "gap": node.get('gap', 0) or 0,
            }
        for pad_key in ('paddingLeft', 'paddingRight', 'paddingTop', 'paddingBottom'):
            pv = node.get(pad_key, 0) or 0
            if pv and pv not in seen_spacing:
                tokens["spacing"].append(pv)
                seen_spacing.add(pv)
        gap = node.get('gap', 0) or 0
        if gap and gap not in seen_spacing:
            tokens["spacing"].append(gap)
            seen_spacing.add(gap)

    # Shadows
    shadows = node.get('shadows', [])
    for shadow in shadows:
        if not shadow.get('enabled', True):
            continue
        tokens["shadows"].append({
            "type": "drop-shadow" if shadow.get('style') == 'drop-shadow' else 'inner-shadow',
            "color": shadow.get('color', '#000000'),
            "offset_x": shadow.get('offsetX', 0) or 0,
            "offset_y": shadow.get('offsetY', 0) or 0,
            "radius": shadow.get('blur', 0) or 0,
            "spread": shadow.get('spread', 0) or 0,
        })

    # Recurse children (shapes within frames)
    shapes = node.get('shapes', [])
    for child in shapes:
        _walk_penpot_node(child, tokens, seen_colors, seen_radii, seen_spacing, depth + 1)


def _extract_penpot_typography(node: dict) -> dict | None:
    """Extract typography info from a Penpot text node."""
    font = node.get('font', {})
    if not font:
        return None
    return {
        "font_family": font.get('family', ''),
        "font_size": font.get('size', 16),
        "font_weight": font.get('style', 'regular'),
        "line_height": font.get('line-height', ''),
        "letter_spacing": font.get('letter-spacing', 0),
        "text_align": font.get('text-align', 'left').lower() if font.get('text-align') else 'left',
        "text_transform": font.get('text-transform', 'none').lower() if font.get('text-transform') else 'none',
    }


def format_penpot_tokens_for_prompt(tokens: dict, file_name: str = "") -> str:
    """Format design tokens as a concise string for the wizard system prompt."""
    lines = ["## Penpot Design Tokens" + (f" ({file_name})" if file_name else "")]

    if tokens["colors"]:
        lines.append("### Colors")
        for name, hex_val in list(tokens["colors"].items())[:15]:
            lines.append(f"- {name}: {hex_val}")

    if tokens["typography"]:
        lines.append("### Typography")
        for t in tokens["typography"][:8]:
            lines.append(
                f"- {t['selector']}: {t['font_family']} {t['font_size']}px "
                f"weight {t['font_weight']}"
            )

    if tokens["radii"]:
        lines.append("### Border Radius")
        lines.append(f"- Values: {', '.join(str(r) for r in tokens['radii'][:8])}px")

    if tokens["spacing"]:
        lines.append("### Spacing")
        lines.append(f"- Values: {', '.join(str(s) for s in tokens['spacing'][:10])}px")

    if tokens["shadows"]:
        lines.append("### Shadows")
        for s in tokens["shadows"][:5]:
            lines.append(
                f"- box-shadow: {s['offset_x']}px {s['offset_y']}px "
                f"{s['radius']}px {s['color']}"
            )

    if tokens["layout"]:
        l = tokens["layout"]
        direction = l.get("type", "none")
        lines.append("### Layout")
        lines.append(
            f"- Primary layout: {direction}, "
            f"padding {l['padding_x']}x{l['padding_y']}px, "
            f"gap {l['gap']}px"
        )

    lines.append(f"\nPages: {tokens['page_count']}, Frames: {tokens['frame_count']}, Components: {tokens['component_count']}")

    return "\n".join(lines)


def extract_file_summary(file_data: dict) -> dict:
    """Extract a concise summary of a Penpot file for the wizard.

    Returns:
        {
            "file_name": str,
            "file_id": str,
            "pages": [{"id": str, "name": str, "frame_count": int}],
            "tokens": {...},       # design tokens
            "prompt_text": str,    # formatted for wizard system prompt
        }
    """
    data = file_data.get('data', file_data)
    pages_index = data.get('pagesIndex', {})
    file_name = file_data.get('name', '')

    pages_summary = []
    for page_id, page in pages_index.items():
        objects = page.get('objects', {})
        frame_count = sum(1 for o in objects.values() if o.get('type') == 'frame')
        pages_summary.append({
            "id": page_id,
            "name": page.get('name', 'Untitled'),
            "frame_count": frame_count,
        })

    tokens = extract_penpot_design_tokens(file_data)
    prompt_text = format_penpot_tokens_for_prompt(tokens, file_name)

    return {
        "file_name": file_name,
        "file_id": file_data.get('id', ''),
        "pages": pages_summary,
        "tokens": tokens,
        "prompt_text": prompt_text,
    }