from django.core.management.base import BaseCommand

from apps.story.home_layout import seed_recommended_layout


class Command(BaseCommand):
    help = (
        "Create the recommended homepage sections. Does nothing if any "
        "homepage section already exists, so it never changes an existing layout."
    )

    def handle(self, *args, **options):
        created = seed_recommended_layout()
        if created:
            self.stdout.write(self.style.SUCCESS(f"Created {created} homepage sections."))
        else:
            self.stdout.write("Homepage sections already exist — nothing changed.")
