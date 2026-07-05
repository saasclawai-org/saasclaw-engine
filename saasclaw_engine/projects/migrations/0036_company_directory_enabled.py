from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0035_add_openclaw_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='project',
            name='company_directory_enabled',
            field=models.BooleanField(default=False, help_text='When true, wizard has access to Company Directory API data for this project'),
        ),
    ]
