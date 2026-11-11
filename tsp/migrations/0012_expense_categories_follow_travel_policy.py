from django.db import migrations, models

# What each retired category becomes, and the label kept in the description.
RETIRED = {
    'transportation': 'Transportation',
    'meals': 'Meals',
    'registration': 'Registration',
    'visa': 'Visa Fees',
}


def move_retired_categories(apps, schema_editor):
    """
    Put retired lines on "Other", keeping what they were in the description.

    The old "Transportation" covered flights, trains and local transport
    alike, so which of the new categories a line belongs to cannot be worked
    out from the row. Guessing would put wrong figures under the wrong
    heading, so the line moves to "Other" and says what it used to be.
    """
    Expense = apps.get_model('tsp', 'Expense')
    for value, label in RETIRED.items():
        for expense in Expense.objects.filter(category=value):
            expense.category = 'other'
            expense.description = f'{label}: {expense.description}'.strip(': ')
            expense.save(update_fields=['category', 'description'])


def restore_retired_categories(apps, schema_editor):
    """Undo the move for a reverse migration, reading the prefix back."""
    Expense = apps.get_model('tsp', 'Expense')
    for value, label in RETIRED.items():
        prefix = f'{label}: '
        for expense in Expense.objects.filter(category='other', description__startswith=prefix):
            expense.category = value
            expense.description = expense.description[len(prefix):]
            expense.save(update_fields=['category', 'description'])


class Migration(migrations.Migration):

    dependencies = [
        ('tsp', '0011_exchange_rates'),
    ]

    operations = [
        migrations.RunPython(move_retired_categories, restore_retired_categories),
        migrations.AlterField(
            model_name='expense',
            name='category',
            field=models.CharField(
                choices=[
                    ('flight', 'Flight'),
                    ('train', 'Train or Bus'),
                    ('lodging', 'Lodging'),
                    ('other', 'Other'),
                ],
                max_length=20,
            ),
        ),
    ]
