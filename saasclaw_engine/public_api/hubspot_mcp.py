"""
HubSpot MCP Bridge — OAuth flow + MCP JSON-RPC client.

Lets any SaaSClaw-generated app talk to HubSpot's MCP server through
a unified bridge endpoint. Platform handles OAuth, token storage, and
MCP protocol translation.
"""
import base64
import hashlib
import json
import secrets
import time
import logging
import os
import functools

import requests
import urllib.request
import urllib.error
from django.http import JsonResponse, HttpResponseRedirect
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import HubSpotConnection

logger = logging.getLogger(__name__)


def require_jwt(view_func):
    """Decorator: require a valid JWT token in Authorization header."""
    @functools.wraps(view_func)
    def wrapper(request, *args, **kwargs):
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            return JsonResponse({'error': 'Authentication required'}, status=401)
        token = auth[7:]
        try:
            from rest_framework_simplejwt.tokens import AccessToken
            AccessToken(token)  # Validates signature + expiry
        except Exception:
            return JsonResponse({'error': 'Invalid or expired token'}, status=401)
        return view_func(request, *args, **kwargs)
    return wrapper

# HubSpot OAuth endpoints
HS_AUTHORIZE_URL = 'https://mcp.hubspot.com/oauth/authorize/user'
HS_TOKEN_URL = 'https://mcp.hubspot.com/oauth/v3/token'

# HubSpot MCP server
HS_MCP_URL = 'https://mcp.hubspot.com/'

# Whitelist of allowed MCP tools (read-only by default)
# Write tools like manage_crm_objects are blocked unless explicitly enabled
ALLOWED_MCP_TOOLS = {
    'search_crm_objects',
    'get_crm_objects',
    'get_user_details',
    'search_owners',
    'search_properties',
    'get_properties',
    'query_crm_data',
    'tool_guidance',
}
# Tools that can write — require allow_write=true in the request body
WRITE_TOOLS = {'manage_crm_objects'}

# SaaSClaw MCP Auth App credentials (from environment, not hardcoded)
import os
HS_CLIENT_ID = os.environ.get('HS_CLIENT_ID', 'f2364d15-6188-4cec-86f2-1c2f8232d47e')
HS_CLIENT_SECRET = os.environ.get('HS_CLIENT_SECRET', '')

# Callback URL (registered in HubSpot MCP auth app)
HS_REDIRECT_URI = 'https://app.saasclaw.ai/api/v1/hubspot/oauth/callback/'


def _generate_pkce():
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip('=')
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip('=')
    return verifier, challenge


# ─── OAuth Flow ────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['GET'])
def hubspot_oauth_start(request):
    """Initiate HubSpot OAuth flow with PKCE."""
    from saasclaw_engine.projects.models import Project

    project_slug = request.GET.get('project', '')
    if not project_slug:
        return JsonResponse({'error': 'project parameter required'}, status=400)

    try:
        project = Project.objects.get(slug=project_slug)
    except Project.DoesNotExist:
        return JsonResponse({'error': 'project not found'}, status=404)

    verifier, challenge = _generate_pkce()
    state = f'{project.slug}:{secrets.token_urlsafe(16)}'

    from django.core.cache import cache
    cache_key = f'hubspot_pkce:{state}'
    cache.set(cache_key, {'verifier': verifier, 'project_slug': project.slug}, timeout=600)

    params = {
        'client_id': HS_CLIENT_ID,
        'redirect_uri': HS_REDIRECT_URI,
        'response_type': 'code',
        'code_challenge': challenge,
        'code_challenge_method': 'S256',
        'state': state,
        'prompt': 'consent',
    }

    auth_url = f'{HS_AUTHORIZE_URL}?' + '&'.join(f'{k}={v}' for k, v in params.items())
    return HttpResponseRedirect(auth_url)


@csrf_exempt
@require_http_methods(['GET'])
def hubspot_oauth_callback(request):
    """Handle OAuth callback. Exchange code for tokens."""
    from django.core.cache import cache

    code = request.GET.get('code', '')
    state = request.GET.get('state', '')
    error = request.GET.get('error', '')

    if error:
        return HttpResponseRedirect(f'https://hubspot-health-checker.preview.saasclaw.ai/?hubspot=error')

    if not code or not state:
        return JsonResponse({'error': 'missing code or state'}, status=400)

    cache_key = f'hubspot_pkce:{state}'
    pkce_data = cache.get(cache_key)
    if not pkce_data:
        return JsonResponse({'error': 'invalid or expired state'}, status=400)

    project_slug = pkce_data['project_slug']
    verifier = pkce_data['verifier']
    cache.delete(cache_key)

    token_data = {
        'grant_type': 'authorization_code',
        'client_id': HS_CLIENT_ID,
        'client_secret': HS_CLIENT_SECRET,
        'redirect_uri': HS_REDIRECT_URI,
        'code': code,
        'code_verifier': verifier,
    }

    resp = requests.post(HS_TOKEN_URL, data=token_data, timeout=30)

    if resp.status_code != 200:
        logger.error('HubSpot token exchange failed: %s %s', resp.status_code, resp.text[:500])
        return JsonResponse({'error': f'token exchange failed: {resp.text[:200]}'}, status=502)

    tokens = resp.json()
    access_token = tokens.get('access_token', '')
    refresh_token = tokens.get('refresh_token', '')
    expires_in = tokens.get('expires_in', 3600)

    from saasclaw_engine.projects.models import Project
    project = Project.objects.get(slug=project_slug)

    conn, created = HubSpotConnection.objects.update_or_create(
        project=project,
        defaults={
            'access_token': access_token,
            'refresh_token': refresh_token,
            'expires_at': int(time.time()) + expires_in,
        }
    )

    logger.info('HubSpot OAuth success for project=%s (created=%s)', project_slug, created)

    return HttpResponseRedirect(
        f'https://hubspot-health-checker.preview.saasclaw.ai/?hubspot=connected'
    )


def _refresh_token_if_needed(conn):
    """Refresh access token if expired. Returns valid access token."""
    if time.time() < conn.expires_at - 300:
        return conn.access_token

    logger.info('Refreshing HubSpot token for project=%s', conn.project.slug)

    token_data = {
        'grant_type': 'refresh_token',
        'client_id': HS_CLIENT_ID,
        'client_secret': HS_CLIENT_SECRET,
        'refresh_token': conn.refresh_token,
    }

    resp = requests.post(HS_TOKEN_URL, data=token_data, timeout=30)

    if resp.status_code != 200:
        logger.error('HubSpot token refresh failed: %s %s', resp.status_code, resp.text[:500])
        raise RuntimeError(f'Token refresh failed: {resp.status_code}')

    tokens = resp.json()
    conn.access_token = tokens['access_token']
    conn.refresh_token = tokens.get('refresh_token', conn.refresh_token)
    conn.expires_at = int(time.time()) + tokens.get('expires_in', 3600)
    conn.save()

    return conn.access_token


# ─── MCP Client ────────────────────────────────────────────────────────────

_mcp_request_id = 0


def _mcp_call(access_token, method, params=None):
    """Make a JSON-RPC call to HubSpot MCP server."""
    global _mcp_request_id
    _mcp_request_id += 1

    payload = {
        'jsonrpc': '2.0',
        'id': _mcp_request_id,
        'method': method,
    }
    if params:
        payload['params'] = params

    headers = {
        'Authorization': f'Bearer {access_token}',
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/event-stream',
    }

    resp = requests.post(HS_MCP_URL, json=payload, headers=headers, timeout=60)

    if resp.status_code != 200:
        logger.error('MCP call failed: %s %s', resp.status_code, resp.text[:500])
        raise RuntimeError(f'MCP server returned {resp.status_code}: {resp.text[:200]}')

    content_type = resp.headers.get('content-type', '')

    if 'text/event-stream' in content_type:
        for line in resp.text.split('\n'):
            line = line.strip()
            if line.startswith('data: '):
                data = line[6:]
                if data == '[DONE]':
                    continue
                try:
                    return json.loads(data)
                except json.JSONDecodeError:
                    continue
        raise RuntimeError('MCP returned empty SSE stream')

    return resp.json()


def _mcp_call_tool(access_token, tool_name, arguments):
    """Call a specific MCP tool."""
    return _mcp_call(access_token, 'tools/call', {
        'name': tool_name,
        'arguments': arguments or {},
    })


def _mcp_list_tools(access_token):
    """List all available MCP tools."""
    return _mcp_call(access_token, 'tools/list', {})


# ─── Bridge Endpoint ───────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(['POST'])
@require_jwt
def hubspot_mcp_call(request):
    """
    Bridge endpoint for apps to call HubSpot MCP tools.

    POST body:
        {"project": "slug", "tool": "search_crm_objects", "arguments": {...}}
        {"project": "slug", "action": "list_tools"}
        {"project": "slug", "action": "status"}
    """
    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'invalid JSON'}, status=400)

    project_slug = body.get('project', '')
    if not project_slug:
        return JsonResponse({'error': 'project required'}, status=400)

    from saasclaw_engine.projects.models import Project

    try:
        project = Project.objects.get(slug=project_slug)
    except Project.DoesNotExist:
        return JsonResponse({'error': 'project not found'}, status=404)

    action = body.get('action', '')

    if action == 'status':
        connected = HubSpotConnection.objects.filter(project=project).exists()
        return JsonResponse({'connected': connected})

    if action == 'list_tools':
        try:
            conn = HubSpotConnection.objects.get(project=project)
            access_token = _refresh_token_if_needed(conn)
            result = _mcp_list_tools(access_token)
            return JsonResponse(result)
        except HubSpotConnection.DoesNotExist:
            return JsonResponse({'error': 'HubSpot not connected'}, status=400)
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=502)

    # Resolve ticket-to-company associations using HubSpot v4 API
    # (MCP search doesn't return associations, so we need this)
    if action == 'ticket_associations':
        try:
            conn = HubSpotConnection.objects.get(project=project)
            access_token = _refresh_token_if_needed(conn)
            ticket_ids = body.get('ticket_ids', [])
            if not ticket_ids:
                return JsonResponse({'associations': {}})
            result = {}
            for tid in ticket_ids:
                url = f'https://api.hubapi.com/crm/v4/objects/tickets/{tid}/associations/companies'
                req = urllib.request.Request(url, headers={'Authorization': f'Bearer {access_token}'})
                try:
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = json.loads(resp.read())
                        company_ids = [r['toObjectId'] for r in data.get('results', [])]
                        result[str(tid)] = company_ids
                except Exception:
                    result[str(tid)] = []
            return JsonResponse({'associations': result})
        except HubSpotConnection.DoesNotExist:
            return JsonResponse({'error': 'HubSpot not connected for this project'}, status=400)
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=502)

    # Fetch notes associated with tickets — returns {ticket_id: [note_body, ...]}
    if action == 'ticket_notes':
        try:
            conn = HubSpotConnection.objects.get(project=project)
            access_token = _refresh_token_if_needed(conn)
            ticket_ids = body.get('ticket_ids', [])
            if not ticket_ids:
                return JsonResponse({'notes': {}})
            result = {}
            for tid in ticket_ids:
                url = f'https://api.hubapi.com/crm/v4/objects/tickets/{tid}/associations/notes'
                req = urllib.request.Request(url, headers={'Authorization': f'Bearer {access_token}'})
                try:
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = json.loads(resp.read())
                        note_ids = [r['toObjectId'] for r in data.get('results', [])]
                        bodies = []
                        for nid in note_ids:
                            note_url = f'https://api.hubapi.com/crm/v3/objects/notes/{nid}?properties=hs_note_body'
                            note_req = urllib.request.Request(note_url, headers={'Authorization': f'Bearer {access_token}'})
                            try:
                                with urllib.request.urlopen(note_req, timeout=10) as nresp:
                                    ndata = json.loads(nresp.read())
                                    body_text = ndata.get('properties', {}).get('hs_note_body', '')
                                    # Strip HTML tags (HubSpot notes are HTML)
                                    import re
                                    clean = re.sub('<[^<]+?>', '', body_text).strip()
                                    if clean:
                                        bodies.append(clean)
                            except Exception:
                                pass
                        result[str(tid)] = bodies
                except Exception:
                    result[str(tid)] = []
            return JsonResponse({'notes': result})
        except HubSpotConnection.DoesNotExist:
            return JsonResponse({'error': 'HubSpot not connected for this project'}, status=400)
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=502)

    # VADER sentiment analysis for ticket texts (fast, local, no LLM call)
    # POST body: { action: 'ticket_sentiment', texts: ['ticket 1 text', ...] }
    # Returns: { results: [{ sentiment, score, summary, flags }, ...] }
    if action == 'ticket_sentiment':
        try:
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            analyzer = SentimentIntensityAnalyzer()
            texts = body.get('texts', [])
            if not texts:
                return JsonResponse({'results': []})

            results = []
            for text in texts:
                scores = analyzer.polarity_scores(text)
                compound = scores['compound']  # -1 (very negative) to +1 (very positive)

                # Convert: our convention is higher = more negative
                # Tuned thresholds for CRM context (slightly more sensitive than defaults)
                if compound <= -0.5:
                    sentiment = 'very-negative'
                    score = 8
                elif compound < -0.15:
                    sentiment = 'negative'
                    score = 4
                elif compound >= 0.4:
                    sentiment = 'positive'
                    score = -2
                else:
                    sentiment = 'neutral'
                    score = 0

                # Extract flagged negative words
                import re
                flags = []
                negative_words = [
                    'upset', 'angry', 'furious', 'frustrated', 'unacceptable', 'broken',
                    'cannot', 'unable', 'fails', 'failed', 'cancel', 'leaving', 'leave',
                    'wrong', 'worst', 'terrible', 'horrible', 'awful', 'useless',
                    'unhappy', 'disappointed', 'complaint', 'urgent', 'escalate',
                ]
                lower = text.lower()
                for w in negative_words:
                    if w in lower:
                        flags.append(w)

                # Build short summary
                summary_parts = []
                if sentiment in ('very-negative', 'negative'):
                    summary_parts.append('Negative tone detected')
                    if flags:
                        summary_parts.append(f'keywords: {", ".join(flags[:3])}')
                elif sentiment == 'positive':
                    summary_parts.append('Positive tone')
                else:
                    summary_parts.append('Neutral tone')

                results.append({
                    'sentiment': sentiment,
                    'score': score,
                    'summary': ' — '.join(summary_parts),
                    'flags': flags[:5],
                    'compound': round(compound, 3),
                })

            return JsonResponse({'results': results})
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=502)

    tool_name = body.get('tool', '')
    arguments = body.get('arguments', {})

    if not tool_name:
        return JsonResponse({'error': 'tool name required'}, status=400)

    # Block non-whitelisted tools (prevents accidental writes from chatbot)
    if tool_name not in ALLOWED_MCP_TOOLS and tool_name not in WRITE_TOOLS:
        return JsonResponse({'error': f'Tool "{tool_name}" is not allowed. Only read-only CRM tools are permitted.'}, status=403)

    # Write tools require explicit allow_write flag — safety check
    if tool_name in WRITE_TOOLS and not body.get('allow_write', False):
        return JsonResponse({'error': 'Write operations require allow_write=true in the request body.'}, status=403)

    try:
        conn = HubSpotConnection.objects.get(project=project)
        access_token = _refresh_token_if_needed(conn)
        result = _mcp_call_tool(access_token, tool_name, arguments)
        return JsonResponse(result)
    except HubSpotConnection.DoesNotExist:
        return JsonResponse({'error': 'HubSpot not connected for this project'}, status=400)
    except Exception as e:
        logger.exception('MCP bridge error for tool=%s', tool_name)
        return JsonResponse({'error': str(e)}, status=502)


# ─── Chatbot Log Endpoint ─────────────────────────────────────────
# Receives debug logs from the browser chatbot JS, writes to a server-side file.
# The frontend predatorLog() function POSTs entries here (fire and forget).

import os
from datetime import datetime
from django.views.decorators.http import require_POST

CHATLOG_PATH = '/tmp/hubspot-chatbot.log'


@require_POST
@require_jwt
def hubspot_chatlog(request):
    """Append a chatbot debug entry to the server log file."""
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'ok': False, 'error': 'invalid json'}, status=400)

    ts = data.get('ts', datetime.utcnow().isoformat() + 'Z')
    phase = data.get('phase', '?')
    detail = data.get('detail', '')
    extra = data.get('data')

    line = f'[{ts}] [{phase}] {detail}'
    if extra is not None:
        try:
            line += ' ' + json.dumps(extra, default=str)[:500]
        except (TypeError, ValueError):
            line += f' {extra}'
    line += '\n'

    try:
        with open(CHATLOG_PATH, 'a') as f:
            f.write(line)
    except OSError:
        pass  # best effort

    return JsonResponse({'ok': True})
