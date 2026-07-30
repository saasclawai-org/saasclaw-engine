from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('projects', '0041_alter_project_framework'),
    ]

    operations = [
        migrations.CreateModel(
            name='SecurityScanResult',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('scan_type', models.CharField(choices=[('quick', 'Quick Scan'), ('full', 'Full Audit'), ('recent_changes', 'Recent Changes'), ('auto_deploy', 'Auto Deploy Scan')], default='full', max_length=20)),
                ('status', models.CharField(choices=[('running', 'Running'), ('completed', 'Completed'), ('failed', 'Failed')], default='running', max_length=20)),
                ('total_findings', models.IntegerField(default=0)),
                ('critical_count', models.IntegerField(default=0)),
                ('high_count', models.IntegerField(default=0)),
                ('medium_count', models.IntegerField(default=0)),
                ('low_count', models.IntegerField(default=0)),
                ('findings_json', models.JSONField(blank=True, default=list)),
                ('raw_output', models.TextField(blank=True, default='')),
                ('session_id', models.CharField(blank=True, default='', max_length=100)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('project', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='security_scans', to='projects.project')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='securityscanresult',
            index=models.Index(fields=['project', '-created_at'], name='idx_security_scan_proj'),
        ),
        migrations.AddIndex(
            model_name='securityscanresult',
            index=models.Index(fields=['status'], name='idx_security_scan_stat'),
        ),
    ]
