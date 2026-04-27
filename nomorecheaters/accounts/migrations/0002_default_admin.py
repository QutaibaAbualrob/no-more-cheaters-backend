from django.db import migrations
from django.contrib.auth.hashers import make_password


def create_default_admin(apps, schema_editor):
    User = apps.get_model("accounts", "User")

    email = "workgost037@gmail.com"
    password = "123work123"

    if User.objects.filter(email__iexact=email).exists():
        return

    admin = User(
        email=email,
        name="Default Admin",
        institution="",
        role="admin",
        is_staff=True,
        is_superuser=True,
        is_active=True,
    )
    admin.password = make_password(password)
    admin.save()


def remove_default_admin(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    User.objects.filter(email__iexact="workgost037@gmail.com").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_default_admin, reverse_code=remove_default_admin),
    ]
