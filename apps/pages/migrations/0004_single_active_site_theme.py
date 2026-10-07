from django.db import migrations


def keep_one_active(apps, schema_editor):
    """Only one site theme may be on from now on. Where several already are,
    keep the most recently edited one — the one an editor touched last — and
    switch the rest off."""
    SiteTheme = apps.get_model("pages", "SiteTheme")
    active = SiteTheme.objects.exclude(mode="off").order_by("-updated_at", "-id")
    keep = active.first()
    if keep:
        active.exclude(pk=keep.pk).update(mode="off")


class Migration(migrations.Migration):
    dependencies = [("pages", "0003_site_themes")]

    operations = [migrations.RunPython(keep_one_active, migrations.RunPython.noop)]
