"""Authentication classes for the public API.

Supports two auth methods:
1. JWT (Bearer eyJ...) — via djangorestframework-simplejwt
2. API Key (Bearer sk_... or X-API-Key: sk_...) — via ApiKey model

API keys are long-lived, user-scoped credentials ideal for
server-to-server integration with the Java SDK.
"""
from rest_framework.authentication import BaseAuthentication
from rest_framework_simplejwt.authentication import JWTAuthentication

from .models import ApiKey


class PublicAPIAuthentication(JWTAuthentication):
    """JWT auth for the public API (existing behavior)."""
    pass


class ApiKeyAuthentication(BaseAuthentication):
    """Authenticate via API key in Authorization header or X-API-Key.

    Accepts:
      Authorization: Bearer sk_xxx
      X-API-Key: sk_xxx

    Returns (user, None) on success, None if no key present,
    None if key is invalid (falls through to other auth methods).
    """

    def authenticate(self, request):
        key = self._extract_key(request)
        if not key:
            return None  # No API key — let JWT auth try

        apikey, user = ApiKey.verify_key(key)
        if user is None:
            return None  # Invalid key — fall through to JWT

        # Update usage stats (fire-and-forget, don't block the request)
        try:
            from django.utils import timezone
            apikey.usage_count += 1
            apikey.last_used_at = timezone.now()
            apikey.save(update_fields=['usage_count', 'last_used_at'])
        except Exception:
            pass

        return (user, None)

    def authenticate_header(self, request):
        return 'Bearer'

    def _extract_key(self, request):
        """Extract API key from request, trying multiple locations."""
        # X-API-Key header (most common for API keys)
        x_key = request.META.get('HTTP_X_API_KEY')
        if x_key and x_key.startswith('sk_'):
            return x_key.strip()

        # Authorization: Bearer sk_...
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if auth_header.startswith('Bearer '):
            token = auth_header[7:].strip()
            # API keys start with sk_, JWTs start with eyJ
            if token.startswith('sk_'):
                return token

        return None


class CombinedAuthentication(BaseAuthentication):
    """Try API key first, then JWT, then optionally allow anonymous.

    This is the default auth class for all public API endpoints.
    Order matters: API key is cheapest (single DB lookup), JWT is heavier.
    """

    def authenticate(self, request):
        # Try API key first
        api_key_auth = ApiKeyAuthentication()
        result = api_key_auth.authenticate(request)
        if result is not None:
            return result

        # Fall back to JWT
        jwt_auth = JWTAuthentication()
        try:
            result = jwt_auth.authenticate(request)
            if result is not None:
                return result
        except Exception:
            pass

        return None

    def authenticate_header(self, request):
        return 'Bearer'


class OptionalAuthentication(BaseAuthentication):
    """Allow unauthenticated access — useful for single-user mode."""

    def authenticate(self, request):
        # Try combined first
        combined = CombinedAuthentication()
        result = combined.authenticate(request)
        if result is not None:
            return result
        return None  # Allow anonymous
