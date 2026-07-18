"""Figma OAuth + REST API integration.

Provides:
- OAuth2 token exchange
- File/node fetching with design token extraction
- Image export (PNG screenshots of frames)
- Wizard-facing tools (figma_get_frame, figma_get_design_tokens)
"""
import base64
import json
import logging
import re
from typing import Any
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

FIGMA_API_BASE = "https://api.figma.com/v1"
FIGMA_OAUTH_BASE = "https://www.figma.com/api/oauth/token"

# --- OAuth ---


def get_oauth_url(state: str) -> str:
    """Build the Figma OAuth authorization URL."""
    from django.conf import settings

    params = {
        "client_id": settings.FIGMA_CLIENT_ID,
        "redirect_uri": settings.FIGMA_REDIRECT_URI,
        "scope": "file_content:read file_metadata:read current_user:read file_versions:read",
        "state": state,
        "response_type": "code",
    }
    return f"https://www.figma.com/oauth?" + urlencode(params)


def exchange_code_for_token(code: str) -> dict:
    """Exchange OAuth authorization code for access + refresh tokens."""
    from django.conf import settings

    resp = requests.post(FIGMA_OAUTH_BASE, data={
        "client_id": settings.FIGMA_CLIENT_ID,
        "client_secret": settings.FIGMA_CLIENT_SECRET,
        "redirect_uri": settings.FIGMA_REDIRECT_URI,
        "code": code,
        "grant_type": "authorization_code",
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()


def refresh_token(refresh_token: str) -> dict:
    """Refresh an expired Figma access token."""
    from django.conf import settings

    resp = requests.post(FIGMA_OAUTH_BASE, data={
        "client_id": settings.FIGMA_CLIENT_ID,
        "client_secret": settings.FIGMA_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()


# --- URL parsing ---


def parse_figma_url(url: str) -> dict | None:
    """Extract file_key and node_id from a Figma URL.

    Examples:
        https://www.figma.com/design/abc123/My-File?node-id=1:2
        https://www.figma.com/file/abc123/My-File?node-id=1-2
    Returns: {"file_key": "abc123", "node_id": "1:2"} or None
    """
    # Match file key
    m = re.match(
        r'https?://(?:www\.)?figma\.com/(?:design|file|proto)/([a-zA-Z0-9]+)',
        url
    )
    if not m:
        return None
    file_key = m.group(1)

    # Match node-id (may use : or - as separator)
    node_match = re.search(r'node-id=([0-9]+[:-][0-9]+)', url)
    node_id = node_match.group(1).replace('-', ':') if node_match else None

    return {"file_key": file_key, "node_id": node_id}


# --- REST API ---


def _api_request(path: str, access_token: str, params: dict | None = None) -> dict:
    """Make an authenticated Figma API request."""
    headers = {"X-Figma-Token": access_token}
    resp = requests.get(
        f"{FIGMA_API_BASE}{path}",
        headers=headers,
        params=params,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def get_file(file_key: str, access_token: str, ids: str | None = None, depth: int | None = None) -> dict:
    """Fetch a Figma file or subset of nodes."""
    params: dict[str, Any] = {}
    if ids:
        params["ids"] = ids
    if depth:
        params["depth"] = depth
    return _api_request(f"/files/{file_key}", access_token, params)


def get_file_nodes(file_key: str, node_ids: list[str], access_token: str) -> dict:
    """Fetch specific nodes from a Figma file."""
    ids = ",".join(node_ids)
    return _api_request(f"/files/{file_key}/nodes", access_token, {"ids": ids})


def get_file_images(file_key: str, node_ids: list[str], access_token: str,
                    format: str = "png", scale: float = 2.0) -> dict:
    """Export Figma nodes as images. Returns {node_id: image_url}."""
    ids = ",".join(node_ids)
    params = {
        "ids": ids,
        "format": format,
        "scale": scale,
    }
    return _api_request(f"/images/{file_key}", access_token, params)


def download_image(url: str) -> bytes:
    """Download an image from a URL."""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content


# --- Design token extraction ---


def _rgb_to_hex(color: dict) -> str:
    """Convert Figma color (0-1 floats) to hex."""
    r = round(color.get("r", 0) * 255)
    g = round(color.get("g", 0) * 255)
    b = round(color.get("b", 0) * 255)
    a = color.get("a", 1)
    if a < 1:
        return f"#{r:02x}{g:02x}{b:02x}{round(a * 255):02x}"
    return f"#{r:02x}{g:02x}{b:02x}"


def _extract_paint(fills: list | None) -> str | None:
    """Extract the first solid color from a fill array."""
    if not fills:
        return None
    for fill in fills:
        if fill.get("type") == "SOLID" and fill.get("visible", True):
            return _rgb_to_hex(fill.get("color", {}))
    # Gradient — return first stop
    for fill in fills:
        if fill.get("type", "").startswith("GRADIENT") and fill.get("visible", True):
            stops = fill.get("gradientStops", [])
            if stops:
                return _rgb_to_hex(stops[0].get("color", {}))
    return None


def _extract_typography(node: dict) -> dict | None:
    """Extract typography info from a text node."""
    style = node.get("style", {})
    if not style:
        return None
    return {
        "font_family": style.get("fontFamily", ""),
        "font_size": style.get("fontSize", 16),
        "font_weight": style.get("fontWeight", 400),
        "line_height": style.get("lineHeightPx", style.get("lineHeightPercent", "")),
        "letter_spacing": style.get("letterSpacing", 0),
        "text_align": style.get("textAlignHorizontal", "LEFT").lower(),
    }


def _extract_effect(effects: list | None) -> list[dict]:
    """Extract shadow/blur effects."""
    if not effects:
        return []
    result = []
    for eff in effects:
        if not eff.get("visible", True):
            continue
        if eff.get("type") == "DROP_SHADOW":
            result.append({
                "type": "drop-shadow",
                "color": _rgb_to_hex(eff.get("color", {})),
                "offset_x": eff.get("offset", {}).get("x", 0),
                "offset_y": eff.get("offset", {}).get("y", 0),
                "radius": eff.get("radius", 0),
                "spread": eff.get("spread", 0),
            })
        elif eff.get("type") == "LAYER_BLUR":
            result.append({
                "type": "blur",
                "radius": eff.get("radius", 0),
            })
    return result


def extract_design_tokens(file_data: dict, node_id: str | None = None) -> dict:
    """Extract structured design tokens from a Figma file or specific node.

    Returns a dict with:
        colors: {name: hex} — all unique solid colors found
        typography: [{selector, font_family, font_size, weight, ...}]
        spacing: [values in px] — padding/gap values found
        radii: [values in px] — corner radius values
        shadows: [{type, color, offset_x, offset_y, radius, spread}]
        layout: {type, direction, padding, gap, children: [...]}
        component_count: int
    """
    tokens = {
        "colors": {},
        "typography": [],
        "spacing": [],
        "radii": [],
        "shadows": [],
        "layout": None,
        "component_count": 0,
    }

    # Get the target node
    if node_id:
        nodes = file_data.get("nodes", {})
        node_info = nodes.get(node_id, {})
        root = node_info.get("document", {})
    else:
        root = file_data.get("document", {})

    if not root:
        return tokens

    seen_colors = set()
    seen_radii = set()
    seen_spacing = set()

    def walk(node: dict, depth: int = 0):
        """Recursively walk the node tree extracting tokens."""
        node_type = node.get("type", "")

        # Count components
        if node_type == "COMPONENT" or node_type == "COMPONENT_SET":
            tokens["component_count"] += 1

        # Colors from fills
        fill_color = _extract_paint(node.get("fills"))
        if fill_color and fill_color not in seen_colors:
            name = node.get("name", fill_color).lower().replace(" ", "-")[:30]
            tokens["colors"][name] = fill_color
            seen_colors.add(fill_color)

        # Strokes (borders)
        stroke_color = _extract_paint(node.get("strokes"))
        if stroke_color and stroke_color not in seen_colors:
            name = f"border-{node.get('name', stroke_color).lower().replace(' ', '-')[:20]}"
            tokens["colors"][name] = stroke_color
            seen_colors.add(stroke_color)

        # Typography from text nodes
        if node_type == "TEXT":
            typo = _extract_typography(node)
            if typo:
                tokens["typography"].append({
                    "selector": node.get("name", "text"),
                    **typo,
                })

        # Corner radii
        radius = node.get("cornerRadius")
        if radius and radius not in seen_radii:
            tokens["radii"].append(radius)
            seen_radii.add(radius)
        # Individual corners
        for key in ("rectangleTopLeftRadius", "rectangleTopRightRadius",
                     "rectangleBottomLeftRadius", "rectangleBottomRightRadius"):
            r = node.get(key)
            if r and r not in seen_radii:
                tokens["radii"].append(r)
                seen_radii.add(r)

        # Layout: padding, gap, direction
        layout_mode = node.get("layoutMode", "NONE")
        if layout_mode and layout_mode != "NONE":
            if not tokens["layout"] and depth <= 2:
                tokens["layout"] = {
                    "type": layout_mode.lower(),
                    "padding_x": node.get("paddingLeft", 0),
                    "padding_y": node.get("paddingTop", 0),
                    "gap": node.get("itemSpacing", 0),
                }
            for pad_key in ("paddingLeft", "paddingRight", "paddingTop", "paddingBottom"):
                pv = node.get(pad_key, 0)
                if pv and pv not in seen_spacing:
                    tokens["spacing"].append(pv)
                    seen_spacing.add(pv)
            gap = node.get("itemSpacing", 0)
            if gap and gap not in seen_spacing:
                tokens["spacing"].append(gap)
                seen_spacing.add(gap)

        # Effects (shadows, blurs)
        effects = _extract_effect(node.get("effects"))
        if effects:
            tokens["shadows"].extend(effects)

        # Recurse children
        for child in node.get("children", []):
            walk(child, depth + 1)

    walk(root)

    # Deduplicate and sort
    tokens["radii"] = sorted(set(tokens["radii"]))
    tokens["spacing"] = sorted(set(tokens["spacing"]))
    tokens["typography"] = tokens["typography"][:20]  # Cap to avoid huge payloads

    return tokens


def format_tokens_for_prompt(tokens: dict) -> str:
    """Format design tokens as a concise string for the wizard system prompt."""
    lines = ["## Figma Design Tokens (match these exactly)"]

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
            if s["type"] == "drop-shadow":
                lines.append(
                    f"- box-shadow: {s['offset_x']}px {s['offset_y']}px "
                    f"{s['radius']}px {s['color']}"
                )

    if tokens["layout"]:
        l = tokens["layout"]
        direction = "row" if l["type"] == "horizontal" else "column"
        lines.append("### Layout")
        lines.append(
            f"- Primary layout: flexbox {direction}, "
            f"padding {l['padding_x']}x{l['padding_y']}px, "
            f"gap {l['gap']}px"
        )

    lines.append(f"\nComponents detected: {tokens['component_count']}")

    return "\n".join(lines)
