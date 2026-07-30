"""Security scan API endpoints."""
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status


def _get_project(slug, user):
    """Get project for user, return (project, error_response)."""
    from saasclaw_engine.projects.models import Project
    try:
        project = Project.objects.get(slug=slug)
    except Project.DoesNotExist:
        return None, Response({'error': 'Project not found'}, status=status.HTTP_404_NOT_FOUND)
    return project, None


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def list_security_scans(request, slug):
    """List all security scans for a project."""
    from saasclaw_engine.agent.models import SecurityScanResult
    project, err = _get_project(slug, request.user)
    if err:
        return err
    
    scans = SecurityScanResult.objects.filter(project=project).order_by('-created_at')[:20]
    data = []
    for scan in scans:
        data.append({
            'id': scan.id,
            'scan_type': scan.scan_type,
            'status': scan.status,
            'total_findings': scan.total_findings,
            'critical_count': scan.critical_count,
            'high_count': scan.high_count,
            'medium_count': scan.medium_count,
            'low_count': scan.low_count,
            'summary': scan.summary,
            'created_at': scan.created_at.isoformat() if scan.created_at else None,
            'completed_at': scan.completed_at.isoformat() if scan.completed_at else None,
        })
    return Response({'results': data})


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def security_scan_detail(request, slug, scan_id):
    """Get detailed results for a specific scan."""
    from saasclaw_engine.agent.models import SecurityScanResult
    project, err = _get_project(slug, request.user)
    if err:
        return err
    
    try:
        scan = SecurityScanResult.objects.get(id=scan_id, project=project)
    except SecurityScanResult.DoesNotExist:
        return Response({'error': 'Scan not found'}, status=status.HTTP_404_NOT_FOUND)
    
    return Response({
        'id': scan.id,
        'scan_type': scan.scan_type,
        'status': scan.status,
        'total_findings': scan.total_findings,
        'critical_count': scan.critical_count,
        'high_count': scan.high_count,
        'medium_count': scan.medium_count,
        'low_count': scan.low_count,
        'summary': scan.summary,
        'findings': scan.findings_json,
        'raw_output': scan.raw_output,
        'session_id': scan.session_id,
        'created_at': scan.created_at.isoformat() if scan.created_at else None,
        'completed_at': scan.completed_at.isoformat() if scan.completed_at else None,
    })


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def trigger_security_scan(request, slug):
    """Create a security scan record. The wizard tool does the actual scanning."""
    from saasclaw_engine.agent.models import SecurityScanResult
    project, err = _get_project(slug, request.user)
    if err:
        return err
    
    scan_type = request.data.get('scan_type', 'quick')
    
    scan = SecurityScanResult.objects.create(
        project=project,
        scan_type=scan_type,
        status='running',
    )
    
    return Response({
        'id': scan.id,
        'scan_type': scan.scan_type,
        'status': scan.status,
        'message': f'Security scan started. Ask the wizard to run a {scan_type} security scan.'
    }, status=status.HTTP_201_CREATED)
