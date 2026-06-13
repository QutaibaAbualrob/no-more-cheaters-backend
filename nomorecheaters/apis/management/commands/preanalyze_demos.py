"""Pre-analyze demo videos so they load instantly during a live demo.

Task 1.5: run the real AI pipeline (Task 1.4's ``build_ai_report`` via
:func:`apis.tasks.run_analysis`) over a handful of short clips ahead of time and
persist the resulting Alerts/Report to the database. During the demo these
sessions open with zero wait, while a fresh upload can still be analysed live to
show the pipeline actually working.

Usage::

    # Analyse specific clips
    python manage.py preanalyze_demos clip1.mp4 clip2.mov clip3.mp4

    # Or drop clips into a folder and point the command at it
    python manage.py preanalyze_demos --dir demo_videos

    # Attribute the seeded data to a particular instructor account
    python manage.py preanalyze_demos --dir demo_videos --instructor prof@uni.edu

    # Wipe previously-seeded demo data and start clean
    python manage.py preanalyze_demos --dir demo_videos --reset

The command mirrors the real upload path: it deduplicates by SHA-256 content
hash, copies each file into ``MEDIA_ROOT`` through the ``Video.file`` field, and
walks each session through the normal job lifecycle and audit trail. It is
idempotent — re-running skips clips already analysed unless ``--force`` is given.
"""

from __future__ import annotations

import hashlib
import mimetypes
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.files import File
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.crypto import get_random_string

from apis.models import (
    Alert, AnalysisJob, AuditLog, Exam, ExamSession, Report, Video,
)
from apis.services import record_audit_log
from apis.tasks import run_analysis


User = get_user_model()

ALLOWED_EXTENSIONS = {'.mp4', '.mov', '.m4v', '.webm', '.avi', '.mkv'}
DEFAULT_INSTRUCTOR_EMAIL = 'demo-instructor@nomorecheater.online'
DEFAULT_EXAM_NAME = 'Demo Exam'


class Command(BaseCommand):
    help = 'Run the AI pipeline on demo clips and seed Alerts/Reports for instant demo loading.'

    def add_arguments(self, parser):
        parser.add_argument(
            'videos',
            nargs='*',
            help='Paths to video clips to pre-analyze.',
        )
        parser.add_argument(
            '--dir',
            dest='directory',
            help='Directory to scan for video clips (in addition to any positional paths).',
        )
        parser.add_argument(
            '--instructor',
            dest='instructor_email',
            default=DEFAULT_INSTRUCTOR_EMAIL,
            help=f'Email of the instructor to own the demo data (default: {DEFAULT_INSTRUCTOR_EMAIL}).',
        )
        parser.add_argument(
            '--exam',
            dest='exam_name',
            default=DEFAULT_EXAM_NAME,
            help=f'Exam name to group the demo sessions under (default: "{DEFAULT_EXAM_NAME}").',
        )
        parser.add_argument(
            '--reset',
            action='store_true',
            help='Delete the existing demo exam (and its sessions/videos/reports) before seeding.',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Re-analyze clips whose content was already imported, instead of skipping them.',
        )
        parser.add_argument(
            '--no-analyze',
            action='store_true',
            help='Import the videos and create QUEUED jobs but do not run the pipeline.',
        )

    def handle(self, *args, **options):
        paths = self._gather_paths(options['videos'], options.get('directory'))
        if not paths:
            raise CommandError(
                'No video clips found. Pass file paths and/or --dir <folder> containing clips.'
            )

        instructor = self._get_or_create_instructor(options['instructor_email'])
        exam = self._prepare_exam(instructor, options['exam_name'], reset=options['reset'])

        analyze = not options['no_analyze']
        summary = []
        for path in paths:
            result = self._process_clip(path, exam, instructor, analyze=analyze, force=options['force'])
            if result:
                summary.append(result)

        self._print_summary(summary, analyzed=analyze)

    # -- discovery ---------------------------------------------------------

    def _gather_paths(self, positional, directory) -> list[Path]:
        """Collect unique, existing clip paths from positional args and --dir."""
        candidates: list[Path] = [Path(p) for p in positional]
        if directory:
            folder = Path(directory)
            if not folder.is_dir():
                raise CommandError(f'--dir is not a directory: {folder}')
            candidates.extend(
                sorted(p for p in folder.iterdir()
                       if p.suffix.lower() in ALLOWED_EXTENSIONS)
            )

        seen: set[Path] = set()
        paths: list[Path] = []
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if not candidate.is_file():
                self.stderr.write(self.style.WARNING(f'  skip (not a file): {candidate}'))
                continue
            if candidate.suffix.lower() not in ALLOWED_EXTENSIONS:
                self.stderr.write(self.style.WARNING(
                    f'  skip (unsupported format {candidate.suffix}): {candidate}'
                ))
                continue
            paths.append(candidate)
        return paths

    # -- setup -------------------------------------------------------------

    def _get_or_create_instructor(self, email: str):
        """Find or create the instructor account that owns the demo data."""
        user = User.objects.filter(email__iexact=email).first()
        if user:
            return user
        username = email.split('@')[0] or 'demo-instructor'
        user = User.objects.create_user(
            username=username,
            email=email,
            password=get_random_string(20),
            role=User.Role.INSTRUCTOR,
        )
        self.stdout.write(self.style.SUCCESS(f'Created demo instructor: {email}'))
        return user

    def _prepare_exam(self, instructor, exam_name: str, *, reset: bool):
        """Get/create the demo exam, optionally wiping prior demo data first."""
        if reset:
            deleted, _ = Exam.objects.filter(instructor=instructor, name=exam_name).delete()
            if deleted:
                self.stdout.write(self.style.WARNING(
                    f'Reset: removed previous demo exam "{exam_name}" and related data.'
                ))
        exam, created = Exam.objects.get_or_create(
            instructor=instructor,
            name=exam_name,
            defaults={'description': 'Pre-analyzed demo sessions for live presentation.'},
        )
        if created:
            self.stdout.write(self.style.SUCCESS(f'Created demo exam: "{exam_name}"'))
        return exam

    # -- per-clip processing ----------------------------------------------

    def _process_clip(self, path: Path, exam, instructor, *, analyze: bool, force: bool):
        """Import one clip, then run the pipeline (unless suppressed)."""
        self.stdout.write(f'\n> {path.name}')

        file_hash = self._hash_file(path)
        existing = Video.objects.filter(file_hash=file_hash).select_related('session').first()
        if existing and not force:
            self.stdout.write(self.style.WARNING(
                '  already imported — skipping (use --force to re-analyze).'
            ))
            return None

        if existing:
            session = existing.session
            self.stdout.write('  re-using existing video (--force): re-analyzing.')
        else:
            session = self._create_session(exam, path)
            self._create_video(session, path, file_hash)
            self.stdout.write('  imported video + session.')

        if not analyze:
            AnalysisJob.objects.update_or_create(
                session=session,
                defaults={'status': AnalysisJob.Status.QUEUED, 'ai_model_version': 'yolo11x'},
            )
            self.stdout.write('  queued (analysis skipped: --no-analyze).')
            return {'session': session, 'report': None}

        return self._run_pipeline(session, instructor)

    def _create_session(self, exam, path: Path):
        """Create a session with a unique student identifier from the filename."""
        base = (path.stem or 'student')[:220]
        candidate = base
        suffix = 1
        while ExamSession.objects.filter(exam=exam, student_identifier=candidate).exists():
            suffix += 1
            candidate = f'{base}-{suffix}'
        return ExamSession.objects.create(exam=exam, student_identifier=candidate)

    def _create_video(self, session, path: Path, file_hash: str):
        """Copy the clip into MEDIA_ROOT through the Video.file field."""
        content_type = mimetypes.guess_type(path.name)[0] or 'video/mp4'
        video = Video(
            session=session,
            original_filename=path.name,
            content_type=content_type,
            size_bytes=path.stat().st_size,
            file_hash=file_hash,
            expires_at=timezone.now() + timedelta(days=30),
        )
        with path.open('rb') as fh:
            video.file.save(path.name, File(fh), save=True)
        return video

    def _run_pipeline(self, session, instructor):
        """Drive the session through the real analysis lifecycle + audit trail."""
        job, _ = AnalysisJob.objects.update_or_create(
            session=session,
            defaults={
                'status': AnalysisJob.Status.QUEUED,
                'ai_model_version': 'yolo11x',
                'started_at': None,
                'completed_at': None,
                'error_message': '',
                'metadata': {'source': 'preanalyze-demos'},
            },
        )
        # Mirror the API's ANALYSIS_STARTED entry; run_analysis writes the
        # ANALYSIS_COMPLETED / REPORT_GENERATED entries itself on success.
        video_id = str(session.video.id) if hasattr(session, 'video') else str(session.id)
        record_audit_log(
            AuditLog.ActionType.ANALYSIS_STARTED, user=instructor, target_resource=video_id,
        )

        self.stdout.write('  analyzing (this downloads model weights on first run)...')
        try:
            run_analysis(str(job.id), actor_id=str(instructor.id))
        except Exception as exc:  # noqa: BLE001 — report and keep going to next clip
            self.stderr.write(self.style.ERROR(f'  analysis FAILED: {exc}'))
            return {'session': session, 'report': None, 'error': str(exc)}

        report = Report.objects.filter(session=session).first()
        alert_count = Alert.objects.filter(session=session).count()
        if report:
            self.stdout.write(self.style.SUCCESS(
                f'  done: {alert_count} alert(s), '
                f'cheating probability {report.overall_cheating_probability:.0%}.'
            ))
        return {'session': session, 'report': report}

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open('rb') as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    def _print_summary(self, summary, *, analyzed: bool):
        self.stdout.write('\n' + '=' * 60)
        if not summary:
            self.stdout.write(self.style.WARNING('No new clips processed.'))
            return
        self.stdout.write(self.style.SUCCESS(f'Processed {len(summary)} clip(s):'))
        for item in summary:
            session = item['session']
            report = item.get('report')
            if item.get('error'):
                line = f'  [FAIL] {session.student_identifier}: FAILED ({item["error"]})'
                self.stdout.write(self.style.ERROR(line))
            elif report is not None:
                self.stdout.write(
                    f'  [ok] {session.student_identifier}: '
                    f'{report.total_alerts} alert(s), '
                    f'{report.overall_cheating_probability:.0%} probability'
                )
            else:
                state = 'queued (not analyzed)' if not analyzed else 'imported'
                self.stdout.write(f'  - {session.student_identifier}: {state}')
        if analyzed:
            self.stdout.write(
                '\nThese sessions are now stored and will load instantly during the demo.\n'
                'Tip: upload a fresh clip live to show the pipeline running in real time.'
            )
