# Generated migration to set the production Site domain.
from django.db import migrations


def update_site_domain(apps, schema_editor):
    Site = apps.get_model("sites", "Site")
    Site.objects.filter(id=1).update(
        domain="nomorecheater.online",
        name="No More Cheaters",
    )


def reverse_site_domain(apps, schema_editor):
    Site = apps.get_model("sites", "Site")
    Site.objects.filter(id=1).update(
        domain="example.com",
        name="example.com",
    )


class Migration(migrations.Migration):
    dependencies = [
        ("sites", "0002_alter_domain_unique"),
        ("apis", "0009_workspacemembership_alter_workspaceinvite_exam_and_more"),
    ]

    operations = [
        migrations.RunPython(update_site_domain, reverse_site_domain),
    ]
