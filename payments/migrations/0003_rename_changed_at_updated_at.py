from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('payments', '0002_audit_fields'),
    ]

    operations = [
        migrations.RenameField(
            model_name='paymentrecord',
            old_name='changed_at',
            new_name='updated_at',
        ),
    ]
