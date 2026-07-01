from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0031_project_form_api_key'),
    ]

    operations = [
        migrations.AddField(
            model_name='project',
            name='deleted_at',
            field=models.DateTimeField(blank=True, db_index=True, help_text='Soft delete timestamp — null means active', null=True, verbose_name='deleted at'),
        ),
    ]
