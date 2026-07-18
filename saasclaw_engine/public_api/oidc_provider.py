"""
Minimal OIDC Provider for SaaSClaw.

Lets Penpot (and other services) use SaaSClaw as an identity provider.
Endpoints:
  - /.well-known/openid-configuration (discovery)
  - /oauth/authorize (authorization code flow)
  - /oauth/token (exchange code for access token)
  - /oauth/userinfo (return user details)
"""
import json
import time
import base64
import secrets
import hmac
import hashlib

from django.conf import settings
from django.contrib.auth.models import User
from django.http import JsonResponse, HttpResponseRedirect
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

# --- Configuration ---
OIDC_ISSUER = getattr(settings, 'OIDC_ISSUER', 'https://saasclaw.ai')
OIDC_CLIENT_ID = getattr(settings, 'OIDC_CLIENT_ID', 'penpot')
OIDC_CLIENT_SECRET = getattr(settings, 'OIDC_CLIENT_SECRET', '')

# In-memory code/token store (fine for single-instance)
_auth_codes = {}
_access_tokens = {}

CODE_TTL = 300       # 5 minutes
TOKEN_TTL = 3600     # 1 hour


def _get_client_secret():
    """Get client secret, deriving one from SECRET_KEY if not configured."""
    secret = OIDC_CLIENT_SECRET
    if not secret:
        secret = hashlib.sha256(settings.SECRET_KEY.encode() + b'oidc-penpot').hexdigest()
    return secret


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def _sign(payload: dict) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    h = _b64url(json.dumps(header, separators=(',', ':')).encode())
    p = _b64url(json.dumps(payload, separators=(',', ':')).encode())
    signing_input = f"{h}.{p}"
    sig = hmac.new(_get_client_secret().encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(sig)}"


def _verify(token: str) -> dict | None:
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return None
        signing_input = f"{parts[0]}.{parts[1]}"
        expected = hmac.new(_get_client_secret().encode(), signing_input.encode(), hashlib.sha256).digest()
        actual = base64.urlsafe_b64decode(parts[2] + '==')
        if not hmac.compare_digest(expected, actual):
            return None
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + '=='))
        if payload.get('exp', 0) < time.time():
            return None
        return payload
    except Exception:
        return None


# --- Endpoints ---

def oidc_discovery(request):
    base = OIDC_ISSUER.rstrip('/')
    return JsonResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "userinfo_endpoint": f"{base}/oauth/userinfo",
        "jwks_uri": f"{base}/oauth/jwks",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["HS256"],
        "scopes_supported": ["openid", "profile", "email"],
    })


def oidc_jwks(request):
    return JsonResponse({"keys": []})


@csrf_exempt
@require_http_methods(["GET", "POST"])
def oidc_authorize(request):
    client_id = request.GET.get('client_id') or request.POST.get('client_id', '')
    redirect_uri = request.GET.get('redirect_uri') or request.POST.get('redirect_uri', '')
    response_type = request.GET.get('response_type') or request.POST.get('response_type', '')
    state = request.GET.get('state') or request.POST.get('state', '')
    scope = request.GET.get('scope', 'openid profile email')

    if client_id != OIDC_CLIENT_ID:
        return JsonResponse({"error": "invalid_client"}, status=400)
    if response_type != 'code':
        return JsonResponse({"error": "unsupported_response_type"}, status=400)

    user = request.user
    if not user or not user.is_authenticated:
        login_url = reverse('login')
        return HttpResponseRedirect(f"{login_url}?next={request.get_full_path()}")

    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        'user_id': user.id,
        'client_id': client_id,
        'redirect_uri': redirect_uri,
        'scope': scope,
        'expires_at': time.time() + CODE_TTL,
    }

    # Cleanup expired
    now = time.time()
    for k in [k for k, v in _auth_codes.items() if v['expires_at'] < now]:
        del _auth_codes[k]

    sep = '&' if '?' in redirect_uri else '?'
    loc = f"{redirect_uri}{sep}code={code}"
    if state:
        loc += f"&state={state}"
    return HttpResponseRedirect(loc)


@csrf_exempt
@require_http_methods(["POST"])
def oidc_token(request):
    grant_type = request.POST.get('grant_type', '')
    client_id = request.POST.get('client_id', '')
    client_secret = request.POST.get('client_secret', '')
    code = request.POST.get('code', '')
    redirect_uri = request.POST.get('redirect_uri', '')

    if client_id != OIDC_CLIENT_ID:
        return JsonResponse({"error": "invalid_client"}, status=401)
    expected = _get_client_secret()
    if client_secret and client_secret != expected:
        return JsonResponse({"error": "invalid_client"}, status=401)

    if grant_type != 'authorization_code':
        return JsonResponse({"error": "unsupported_grant_type"}, status=400)

    data = _auth_codes.pop(code, None)
    if not data or data['expires_at'] < time.time():
        return JsonResponse({"error": "invalid_grant", "error_description": "Invalid or expired code"}, status=400)
    if data['redirect_uri'] != redirect_uri:
        return JsonResponse({"error": "invalid_grant", "error_description": "redirect_uri mismatch"}, status=400)

    try:
        user = User.objects.get(id=data['user_id'])
    except User.DoesNotExist:
        return JsonResponse({"error": "invalid_grant"}, status=400)

    now = int(time.time())
    access_token = _sign({
        'sub': str(user.id),
        'email': user.email,
        'name': user.get_full_name() or user.username,
        'preferred_username': user.username,
        'iat': now,
        'exp': now + TOKEN_TTL,
        'iss': OIDC_ISSUER,
        'aud': OIDC_CLIENT_ID,
    })
    _access_tokens[access_token] = now + TOKEN_TTL

    return JsonResponse({
        'access_token': access_token,
        'token_type': 'Bearer',
        'expires_in': TOKEN_TTL,
        'id_token': access_token,
        'scope': 'openid profile email',
    })


@csrf_exempt
@require_http_methods(["GET", "POST"])
def oidc_userinfo(request):
    auth = request.META.get('HTTP_AUTHORIZATION', '')
    token = auth[7:] if auth.startswith('Bearer ') else (
        request.POST.get('access_token') or request.GET.get('access_token')
    )
    if not token:
        return JsonResponse({"error": "invalid_token"}, status=401)

    payload = _verify(token)
    if not payload:
        return JsonResponse({"error": "invalid_token"}, status=401)

    return JsonResponse({
        'sub': payload.get('sub', ''),
        'email': payload.get('email', ''),
        'name': payload.get('name', ''),
        'preferred_username': payload.get('preferred_username', ''),
        'email_verified': True,
    })
