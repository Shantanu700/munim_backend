from django.apps import AppConfig


class TwinConfig(AppConfig):
    name = "modules.twin"

    def ready(self):
        from modules.twin import signals
