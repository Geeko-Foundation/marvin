from django.apps import AppConfig


class PaymentsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'payments'
    verbose_name = 'Payments'

    def ready(self):
        """Eager-load gateway and adapter plugins from Django settings."""
        from payments.registry import load_plugins
        load_plugins()
