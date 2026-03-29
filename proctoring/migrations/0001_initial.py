import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def video_upload_to(instance, filename):
    return f"videos/{instance.sha256 or uuid.uuid4().hex}_{filename}"


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SystemSettings",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("alert_threshold", models.FloatField(default=0.65)),
                ("retention_days", models.PositiveIntegerField(default=30)),
            ],
            options={
                "verbose_name_plural": "System settings",
            },
        ),
        migrations.CreateModel(
            name="Video",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("file", models.FileField(upload_to=video_upload_to)),
                ("sha256", models.CharField(db_index=True, max_length=64)),
                ("original_filename", models.CharField(blank=True, max_length=255)),
                ("content_type", models.CharField(blank=True, max_length=128)),
                ("size_bytes", models.PositiveBigIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="videos",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="AnalysisJob",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("processing", "Processing"),
                            ("completed", "Completed"),
                            ("failed", "Failed"),
                        ],
                        db_index=True,
                        default="queued",
                        max_length=20,
                    ),
                ),
                ("error_message", models.TextField(blank=True)),
                ("rq_job_id", models.CharField(blank=True, max_length=128)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "video",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="jobs",
                        to="proctoring.video",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="AnalysisReport",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("cheat_probability", models.FloatField(default=0.0)),
                ("raw_json", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "job",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="report",
                        to="proctoring.analysisjob",
                    ),
                ),
                (
                    "video",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="reports",
                        to="proctoring.video",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="Alert",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("timestamp_seconds", models.FloatField(default=0.0)),
                ("alert_type", models.CharField(db_index=True, max_length=64)),
                ("confidence", models.FloatField(default=0.0)),
                ("evidence", models.TextField(blank=True)),
                (
                    "report",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="alerts",
                        to="proctoring.analysisreport",
                    ),
                ),
            ],
            options={
                "ordering": ["timestamp_seconds"],
            },
        ),
    ]
