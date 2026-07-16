"""API key management views — list, create, revoke, delete."""
from django.shortcuts import get_object_or_404

from rest_framework.decorators import api_view, permission_classes, authentication_classes, parser_classes
from rest_framework.permissions import IsAdminUser
from rest_framework.parsers import JSONParser
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework.response import Response

from .models import ApiKey


@api_view(['GET', 'POST'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAdminUser])
@parser_classes([JSONParser])
def api_keys_list_create(request):
    """GET: list keys. POST: create a new key."""
    user = request.user

    if request.method == 'GET':
        keys = ApiKey.objects.filter(user=user)
        data = [{
            'id': str(k.id),
            'prefix': k.prefix,
            'name': k.name,
            'usageLimit': k.usage_limit,
            'usageCount': k.usage_count,
            'createdAt': k.created_at.isoformat(),
            'lastUsedAt': k.last_used_at.isoformat() if k.last_used_at else None,
            'active': k.active,
        } for k in keys]
        return Response({'keys': data})

    # POST — create
    name = request.data.get('name', '').strip()
    if not name:
        return Response({'error': 'name is required'}, status=400)

    usage_limit = request.data.get('usageLimit')
    if usage_limit is not None:
        try:
            usage_limit = int(usage_limit)
            if usage_limit < 1:
                return Response({'error': 'usageLimit must be positive'}, status=400)
        except (ValueError, TypeError):
            return Response({'error': 'usageLimit must be a positive integer'}, status=400)

    apikey, raw_key = ApiKey.create_key(user=user, name=name, usage_limit=usage_limit)
    return Response({
        'id': str(apikey.id),
        'prefix': apikey.prefix,
        'name': apikey.name,
        'key': raw_key,  # Only shown once at creation
        'usageLimit': apikey.usage_limit,
        'usageCount': apikey.usage_count,
        'createdAt': apikey.created_at.isoformat(),
        'active': apikey.active,
    }, status=201)


@api_view(['POST'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAdminUser])
@parser_classes([JSONParser])
def api_key_revoke(request, key_id):
    """Revoke (deactivate) an API key."""
    apikey = get_object_or_404(ApiKey, id=key_id, user=request.user)
    apikey.active = False
    apikey.save(update_fields=['active'])
    return Response({'id': str(apikey.id), 'active': False})


@api_view(['DELETE'])
@authentication_classes([JWTAuthentication])
@permission_classes([IsAdminUser])
@parser_classes([JSONParser])
def api_key_delete(request, key_id):
    """Permanently delete an API key."""
    apikey = get_object_or_404(ApiKey, id=key_id, user=request.user)
    apikey.delete()
    return Response({'deleted': True})
