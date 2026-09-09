from django.apps import AppConfig


class TspConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'tsp'

    def ready(self):
        from django.db.models.signals import post_migrate

        from tsp.groups import ensure_groups

        def setup_groups(sender, **kwargs):
            if sender.name != 'tsp':
                return
            ensure_groups()

        post_migrate.connect(setup_groups, sender=self)
