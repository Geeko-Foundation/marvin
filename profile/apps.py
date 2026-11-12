from django.apps import AppConfig


class ProfileConfig(AppConfig):
    """Profile app configuration; ensures Marvin auth groups exist after migrate."""

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'profile'

    def ready(self):
        """Register a post-migrate hook to create groups and assign permissions."""
        from django.db.models.signals import post_migrate

        from profile.groups import ensure_groups

        def setup_groups(sender, **kwargs):
            """Run ``ensure_groups`` only for this app's migrations."""
            if sender.name != 'profile':
                return
            ensure_groups()

        # weak=False: setup_groups only lives inside this method, so with the
        # default weak reference it could be garbage collected before migrate
        # ran, leaving a fresh database with no groups and no error.
        post_migrate.connect(setup_groups, sender=self, weak=False)
