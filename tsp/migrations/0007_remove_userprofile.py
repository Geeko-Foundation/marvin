from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('profile', '0002_move_contenttypes'),
        ('tsp', '0006_groups'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.DeleteModel(
                    name='UserProfile',
                ),
            ],
            database_operations=[],
        ),
    ]
