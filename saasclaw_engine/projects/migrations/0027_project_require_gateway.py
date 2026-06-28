from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('projects', '0026_waitinglist'),
    ]

    operations = [
        migrations.AddField(
            model_name='project',
            name='require_gateway',
            field=models.BooleanField(default=False, help_text='When true, agent must use local/gateway LLM endpoint — cloud providers blocked'),
        ),
    ]
