from django.apps import AppConfig


class ProfileConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'profile'

    def ready(self):
        from django.db.models.signals import post_migrate

        from profile.groups import ensure_groups

        def setup_groups(sender, **kwargs):
            if sender.name != 'profile':
                return
            ensure_groups()

        post_migrate.connect(setup_groups, sender=self)
