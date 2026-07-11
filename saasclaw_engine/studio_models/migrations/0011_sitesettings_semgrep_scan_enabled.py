from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('studio_models', '0010_sitesettings_allow_personal_keys'),
    ]

    operations = [
        migrations.AddField(
            model_name='sitesettings',
            name='semgrep_scan_enabled',
            field=models.BooleanField(
                default=True,
                help_text='Run Semgrep static analysis during deploy to detect malware and dangerous code patterns.',
            ),
        ),
    ]
