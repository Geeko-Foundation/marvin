from django.db import migrations


def move_userprofile_content_type(apps, schema_editor):
    ContentType = apps.get_model('contenttypes', 'ContentType')
    Permission = apps.get_model('auth', 'Permission')

    old_ct = ContentType.objects.filter(app_label='tsp', model='userprofile').first()
    if not old_ct:
        return

    new_ct = ContentType.objects.filter(app_label='profile', model='userprofile').first()
    if new_ct and new_ct.pk != old_ct.pk:
        for perm in Permission.objects.filter(content_type=old_ct):
            existing = Permission.objects.filter(
                content_type=new_ct,
                codename=perm.codename,
            ).first()
            if existing:
                Group = apps.get_model('auth', 'Group')
                for group in Group.objects.filter(permissions=perm):
                    group.permissions.remove(perm)
                    group.permissions.add(existing)
                perm.delete()
            else:
                perm.content_type = new_ct
                perm.save(update_fields=['content_type'])
        old_ct.delete()
    else:
        old_ct.app_label = 'profile'
        old_ct.save(update_fields=['app_label'])


def revert_userprofile_content_type(apps, schema_editor):
    ContentType = apps.get_model('contenttypes', 'ContentType')
    ContentType.objects.filter(app_label='profile', model='userprofile').update(app_label='tsp')


class Migration(migrations.Migration):

    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
        ('profile', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(move_userprofile_content_type, revert_userprofile_content_type),
    ]
