from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('studio_models', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='SiteSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('project_approval_required', models.BooleanField(default=False, help_text='When enabled, users must submit a project request that staff approves before creating projects.')),
                ('secret_scan_enabled', models.BooleanField(default=True, help_text='Scan committed code for secrets (AWS keys, tokens, private keys) during deploy.')),
                ('dependency_scan_enabled', models.BooleanField(default=True, help_text='Run npm audit / pip check during deploy for known vulnerabilities.')),
                ('block_deploy_on_findings', models.BooleanField(default=False, help_text='Block deploy when high/critical security findings are detected (advisory by default).')),
                ('default_require_gateway', models.BooleanField(default=False, help_text='New projects default to LLM gateway mode (data stays on-server).')),
                ('ai_disclosure_required', models.BooleanField(default=True, help_text='Require AI disclosure checkbox on project intake form.')),
                ('pii_guard_enabled', models.BooleanField(default=True, help_text='Redact PII (SSNs, credit cards, etc.) before sending to LLM providers.')),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('updated_by', models.ForeignKey(blank=True, help_text='Last user to update settings.', null=True, on_delete=django.db.models.deletion.SET_NULL, to='auth.user')),
            ],
            options={
                'verbose_name': 'Site Settings',
                'verbose_name_plural': 'Site Settings',
            },
        ),
        migrations.RunSQL(
            sql="INSERT INTO studio_models_sitesettings (id, project_approval_required, secret_scan_enabled, dependency_scan_enabled, block_deploy_on_findings, default_require_gateway, ai_disclosure_required, pii_guard_enabled, updated_at) VALUES (1, false, true, true, false, false, true, true, NOW()) ON CONFLICT DO NOTHING;",
            reverse_sql="",
        ),
    ]
