"""Delete videos past their 30-day retention window, plus their evidence.

A :class:`~apis.models.Video` stores ``expires_at`` (set to ``uploaded_at + 30
days`` at upload). This command removes every video whose window has passed:

* deletes the stored video file from disk,
* deletes that session's generated clips (``media/clips/<session_id>/``) and
  face snapshots (``media/snapshots/<session_id>/``),
* deletes the :class:`Video` row,
* writes a :class:`~apis.models.AuditLog` entry per deletion.

Run it daily (see the cron example in the project docs):

    python manage.py delete_expired_videos
"""

import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from apis.models import AuditLog, Video


class Command(BaseCommand):
    help = 'Delete videos (and their clips/snapshots) past the 30-day retention window.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='List what would be deleted without deleting anything.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        now = timezone.now()
        media_root = Path(settings.MEDIA_ROOT)

        expired = (
            Video.objects
            .select_related('session')
            .filter(expires_at__isnull=False, expires_at__lt=now)
        )

        count = 0
        for video in expired:
            session_id = str(video.session_id)
            filename = video.original_filename
            self.stdout.write(f'Expiring video {video.id} (session {session_id}) — {filename}')

            if dry_run:
                count += 1
                continue

            # 1. Remove the generated evidence directories for this session.
            for subdir in ('clips', 'snapshots'):
                target = media_root / subdir / session_id
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)

            # 2. Remove the stored video file from disk.
            if video.file:
                video.file.delete(save=False)

            # 3. Audit, then drop the row.
            AuditLog.objects.create(
                user=None,
                action=AuditLog.ActionType.VIDEO_DELETED,
                target_resource=str(video.id),
                metadata={
                    'reason': 'retention-expired',
                    'session_id': session_id,
                    'original_filename': filename,
                    'expired_at': video.expires_at.isoformat() if video.expires_at else None,
                },
            )
            video.delete()
            count += 1

        verb = 'Would delete' if dry_run else 'Deleted'
        self.stdout.write(self.style.SUCCESS(f'{verb} {count} expired video(s).'))
