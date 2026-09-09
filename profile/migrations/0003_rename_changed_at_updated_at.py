from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('profile', '0002_move_contenttypes'),
    ]

    operations = [
        migrations.RenameField(
            model_name='userprofile',
            old_name='changed_at',
            new_name='updated_at',
        ),
    ]
