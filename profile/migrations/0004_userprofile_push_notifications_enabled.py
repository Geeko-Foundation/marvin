from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('profile', '0003_rename_changed_at_updated_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='userprofile',
            name='push_notifications_enabled',
            field=models.BooleanField(
                default=False,
                help_text='Receive browser push notifications for TSP activity.',
            ),
        ),
    ]
