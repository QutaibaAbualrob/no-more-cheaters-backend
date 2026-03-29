from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from proctoring.models import SystemSettings, Video


class Command(BaseCommand):
    help = "Delete raw video files older than retention_days (SystemSettings)."

    def handle(self, *args, **options):
        settings_obj = SystemSettings.get()
        days = settings_obj.retention_days
        cutoff = timezone.now() - timedelta(days=days)
        qs = Video.objects.filter(created_at__lt=cutoff)
        count = 0
        for video in qs.iterator():
            if video.file:
                video.file.delete(save=False)
            video.delete()
            count += 1
        self.stdout.write(self.style.SUCCESS(f"Removed {count} video(s) older than {days} days."))
