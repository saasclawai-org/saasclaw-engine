"""Change LocalTaxBracket FK from StateTaxProfile to LocalTaxInfo.

Since we deleted all existing brackets, we can drop and recreate.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('tax_data', '0007_localtaxbracket_localtaxinfo'),
    ]

    operations = [
        # Delete the old LocalTaxBracket model entirely
        migrations.DeleteModel(
            name='LocalTaxBracket',
        ),
        # Recreate with new FK to LocalTaxInfo
        migrations.CreateModel(
            name='LocalTaxBracket',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('filing_status', models.CharField(choices=[('single', 'Single/MFS'), ('married', 'MFJ'), ('head_of_household', 'HOH')], max_length=20)),
                ('min_amount', models.PositiveIntegerField(help_text='Lower bound of bracket (annual)')),
                ('max_amount', models.PositiveIntegerField(blank=True, null=True, help_text='Upper bound (null = no limit)')),
                ('rate', models.DecimalField(decimal_places=6, help_text='Tax rate (e.g. 0.03876 = 3.876%)', max_digits=6)),
                ('local_tax', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='brackets', to='tax_data.localtaxinfo')),
            ],
            options={
                'verbose_name': 'Local Tax Bracket',
                'verbose_name_plural': 'Local Tax Brackets',
                'ordering': ['local_tax', 'filing_status', 'min_amount'],
            },
        ),
    ]
