"""
Wipe ALL application data and start from a clean slate.

Removes every row from the database (users, exams, sessions, videos, alerts,
reports, audit logs, workspaces, notifications, …) AND deletes the uploaded
media files on disk (exam videos, snapshots, evidence clips, annotated videos).
The database schema and migrations are left intact — only the *data* is cleared.

This is destructive and irreversible. It refuses to run without --yes.

Examples
--------
    # Wipe everything, then recreate an admin login in one shot:
    python manage.py wipe_data --yes \
        --superuser admin@example.com --password "ChangeMe123!"

    # Wipe DB rows but KEEP the uploaded media files on disk:
    python manage.py wipe_data --yes --keep-media
"""

import shutil
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Wipe all application data (DB rows + uploaded media) and start fresh."

    def add_arguments(self, parser):
        parser.add_argument(
            '--yes',
            action='store_true',
            help='Required. Confirms you really want to erase all data.',
        )
        parser.add_argument(
            '--keep-media',
            action='store_true',
            help='Do not delete uploaded files under MEDIA_ROOT (only wipe the DB).',
        )
        parser.add_argument(
            '--superuser',
            metavar='EMAIL',
            help='After wiping, create a superuser with this email/username.',
        )
        parser.add_argument(
            '--password',
            help='Password for the --superuser account (required with --superuser).',
        )
        parser.add_argument(
            '--role',
            default='ADMIN',
            help="Role for the new superuser (default: ADMIN).",
        )

    def handle(self, *args, **opts):
        if not opts['yes']:
            raise CommandError(
                'Refusing to wipe data without --yes. This deletes ALL rows and '
                'uploaded files irreversibly. Re-run with --yes to proceed.'
            )

        db_name = settings.DATABASES['default'].get('NAME')
        self.stdout.write(self.style.WARNING(
            f'Wiping ALL data from database "{db_name}"...'
        ))

        # 1. Clear every table's rows. flush truncates all tables and re-emits the
        #    post-migrate signal, which recreates baseline rows (content types,
        #    permissions, the default Site). Schema + migration history untouched.
        call_command('flush', '--no-input', verbosity=0)
        self.stdout.write(self.style.SUCCESS('  - database rows cleared'))

        # 2. Delete uploaded media files on disk (the DB rows pointing at them are
        #    already gone). We empty MEDIA_ROOT's contents but keep the dir itself.
        if opts['keep_media']:
            self.stdout.write('  - media files kept (--keep-media)')
        else:
            self._wipe_media()

        # 3. Optionally recreate a superuser so you can log in immediately.
        if opts['superuser']:
            self._create_superuser(opts)

        self.stdout.write(self.style.SUCCESS('Done. Data has been reset to scratch.'))

    # ------------------------------------------------------------------ helpers
    def _wipe_media(self):
        media_root = Path(settings.MEDIA_ROOT)
        if not media_root.exists():
            self.stdout.write('  - no media directory to clear')
            return

        removed = 0
        for child in media_root.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
                removed += 1
            except OSError as exc:  # keep going; report what failed
                self.stderr.write(f'    ! could not remove {child}: {exc}')
        self.stdout.write(self.style.SUCCESS(
            f'  - media files cleared ({removed} item(s) under {media_root})'
        ))

    def _create_superuser(self, opts):
        password = opts.get('password')
        if not password:
            raise CommandError('--superuser requires --password.')

        from django.contrib.auth import get_user_model

        User = get_user_model()
        email = opts['superuser']
        user = User.objects.create_superuser(
            username=email,
            email=email,
            password=password,
            role=opts['role'],
        )
        self.stdout.write(self.style.SUCCESS(
            f'  - superuser created: {user.email} (role={opts["role"]})'
        ))
