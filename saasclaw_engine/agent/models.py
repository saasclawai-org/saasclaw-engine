from django.db import models
from saasclaw_engine.projects.models import Project


class SecurityScanResult(models.Model):
    """Stores results from a VulnHunter-style security scan."""
    
    SEVERITY_CHOICES = [
        ('critical', 'Critical'),
        ('high', 'High'),
        ('medium', 'Medium'),
        ('low', 'Low'),
        ('info', 'Info'),
    ]
    
    SCAN_TYPE_CHOICES = [
        ('quick', 'Quick Scan'),
        ('full', 'Full Audit'),
        ('recent_changes', 'Recent Changes'),
        ('auto_deploy', 'Auto Deploy Scan'),
    ]
    
    STATUS_CHOICES = [
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='security_scans')
    scan_type = models.CharField(max_length=20, choices=SCAN_TYPE_CHOICES, default='full')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='running')
    
    # Summary counts
    total_findings = models.IntegerField(default=0)
    critical_count = models.IntegerField(default=0)
    high_count = models.IntegerField(default=0)
    medium_count = models.IntegerField(default=0)
    low_count = models.IntegerField(default=0)
    
    # Full findings as JSON array
    findings_json = models.JSONField(default=list, blank=True)
    
    # The raw scan output (wizard's analysis)
    raw_output = models.TextField(blank=True, default='')
    
    # Session that triggered the scan
    session_id = models.CharField(max_length=100, blank=True, default='')
    
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['project', '-created_at']),
            models.Index(fields=['status']),
        ]
    
    def __str__(self):
        return f"{self.project.slug} — {self.scan_type} ({self.status})"
    
    @property
    def summary(self):
        """Short summary string."""
        parts = []
        if self.critical_count:
            parts.append(f"🔴 {self.critical_count}")
        if self.high_count:
            parts.append(f"🟠 {self.high_count}")
        if self.medium_count:
            parts.append(f"🟡 {self.medium_count}")
        if self.low_count:
            parts.append(f"🔵 {self.low_count}")
        if not parts:
            return "✅ No issues"
        return " ".join(parts)
