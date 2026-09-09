from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('tsp', '0007_remove_userprofile'),
    ]

    operations = [
        migrations.RenameField(
            model_name='event',
            old_name='changed_at',
            new_name='updated_at',
        ),
        migrations.RenameField(
            model_name='travelrequest',
            old_name='changed_at',
            new_name='updated_at',
        ),
        migrations.RenameField(
            model_name='expense',
            old_name='changed_at',
            new_name='updated_at',
        ),
        migrations.RenameField(
            model_name='statehistory',
            old_name='changed_at',
            new_name='updated_at',
        ),
        migrations.RenameField(
            model_name='comment',
            old_name='changed_at',
            new_name='updated_at',
        ),
        migrations.RenameField(
            model_name='document',
            old_name='changed_at',
            new_name='updated_at',
        ),
        migrations.AlterModelOptions(
            name='travelrequest',
            options={'ordering': ['-updated_at']},
        ),
    ]
