"""Add category field to Project for dashboard grouping."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0039_project_linked_project_project_linked_project_role"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="category",
            field=models.CharField(
                max_length=20,
                choices=[
                    ("game", "🎮 Games"),
                    ("business", "💼 Business"),
                    ("finance", "🧮 Finance"),
                    ("dev_tools", "🛠️ Dev Tools"),
                    ("tasks", "📋 Task Apps"),
                    ("content", "📄 Content"),
                    ("utility", "🛠️ Utilities"),
                    ("other", "📦 Other"),
                ],
                default="other",
                help_text="Project category for dashboard grouping",
            ),
        ),
    ]
