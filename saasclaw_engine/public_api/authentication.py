"""JWT authentication for the public API.

Uses djangorestframework-simplejwt for access/refresh token pairs.
Falls back to session auth for browser-based access.
"""
from rest_framework.authentication import BaseAuthentication
from rest_framework_simplejwt.authentication import JWTAuthentication


class PublicAPIAuthentication(JWTAuthentication):
    """JWT auth for the public API."""
    pass


class OptionalAuthentication(BaseAuthentication):
    """Allow unauthenticated access — useful for single-user mode."""

    def authenticate(self, request):
        # Try JWT first
        try:
            jwt_auth = JWTAuthentication()
            return jwt_auth.authenticate(request)
        except Exception:
            return None  # Allow anonymous