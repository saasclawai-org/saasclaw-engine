from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0032_project_soft_delete'),
    ]

    operations = [
        migrations.AddField(
            model_name='formsubmission',
            name='environment',
            field=models.CharField(
                choices=[('preview', 'Preview'), ('production', 'Production')],
                db_index=True,
                default='preview',
                max_length=20,
            ),
        ),
    ]
