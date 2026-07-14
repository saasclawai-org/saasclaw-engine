"""Permissions for the public API.

IsOwner: ensures users can only access their own projects.
AllowAny: for single-user mode.
"""
from rest_framework.permissions import BasePermission


class IsOwner(BasePermission):
    """Allow access only to objects owned by the request user."""

    def has_object_permission(self, request, view, obj):
        return obj.owner == request.user


class AllowAny(BasePermission):
    """Allow any access — for single-user mode or public endpoints."""

    def has_permission(self, request, view):
        return True

    def has_object_permission(self, request, view, obj):
        return True