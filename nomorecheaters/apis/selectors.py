from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone

from .models import (
    Exam, ExamSession, Report, Student, Video, Workspace, WorkspaceInvite,
    WorkspaceMembership,
)


User = get_user_model()


def is_admin(user):
    """Return True when the user can manage cross-account platform data."""
    return bool(
        user
        and user.is_authenticated
        and (user.is_superuser or getattr(user, 'role', '') == User.Role.ADMIN)
    )


def is_dean(user):
    """Return True when the user has dean-level oversight access.

    Deans get read-only visibility across all instructors and exam halls.
    Admins/superusers implicitly satisfy this check as well.
    """
    return bool(
        user
        and user.is_authenticated
        and (
            user.is_superuser
            or getattr(user, 'role', '') in {User.Role.ADMIN, User.Role.DEAN}
        )
    )


def users_visible_to(user):
    """Scope user records to admins, while ordinary users only see themselves."""
    queryset = User.objects.all().order_by('-created_at')
    if is_admin(user):
        return queryset
    return queryset.filter(pk=user.pk)


def owned_students(user):
    """Scope roster students to their owner; admins see every roster."""
    queryset = Student.objects.select_related('owner')
    if is_admin(user):
        return queryset
    return queryset.filter(owner=user)


def owned_sessions(user):
    """Scope exam sessions to the authenticated instructor unless admin."""
    queryset = ExamSession.objects.select_related('exam', 'exam__instructor')
    if is_admin(user):
        return queryset
    return queryset.filter(exam__instructor=user)


def owned_videos(user):
    """Scope uploaded videos to the authenticated instructor unless admin."""
    queryset = Video.objects.select_related(
        'session',
        'session__exam',
        'session__exam__instructor',
        # OneToOne sources for VideoReadSerializer's annotated_video_url + analysis.
        'session__analysis_job',
        'session__report',
    )
    if is_admin(user):
        return queryset
    return queryset.filter(session__exam__instructor=user)


def owned_reports(user):
    """Scope analysis reports to the authenticated instructor unless admin."""
    queryset = Report.objects.select_related('session', 'session__exam')
    if is_admin(user):
        return queryset
    return queryset.filter(session__exam__instructor=user)


def recent_day_window(days_count=7):
    """Return date objects and display labels for recent activity charts."""
    today = timezone.localdate()
    days = [today - timedelta(days=offset) for offset in range(days_count - 1, -1, -1)]
    labels = [day.strftime('%a') for day in days]
    return days, labels


def assigned_supervisor_ids(exam):
    """Return the set of user ids allowed to supervise an exam.

    That is the exam's owner plus every instructor who has ACCEPTED a workspace
    invite for it.
    """
    ids = {exam.instructor_id}
    ids.update(
        WorkspaceInvite.objects
        .filter(exam=exam, status=WorkspaceInvite.Status.ACCEPTED)
        .values_list('instructor_id', flat=True)
    )
    return ids


def can_supervise_exam(user, exam):
    """True when *user* may upload/record for *exam* (assigned supervisor or dean/admin)."""
    if is_dean(user):
        return True
    return user.id in assigned_supervisor_ids(exam)


def calendar_exams(user):
    """Exams that should appear on *user*'s calendar.

    * **ADMIN** — every exam.
    * **DEAN** — exams the dean owns plus exams owned by anyone who shares one of
      the dean's workspaces (so a dean sees the schedule across their members).
    * **INSTRUCTOR** — exams they own plus every exam they are an accepted
      supervisor of (an accepted per-exam :class:`WorkspaceInvite`). This is what
      makes a dean's assignment show up on the instructor's calendar.
    """
    queryset = Exam.objects.select_related('instructor')
    if is_admin(user):
        return queryset
    if is_dean(user):
        return queryset.filter(instructor_id__in=workspace_related_user_ids(user))
    invited_exam_ids = (
        WorkspaceInvite.objects
        .filter(instructor=user, status=WorkspaceInvite.Status.ACCEPTED, exam__isnull=False)
        .values_list('exam_id', flat=True)
    )
    return queryset.filter(Q(instructor=user) | Q(id__in=invited_exam_ids)).distinct()


def actionable_exams(user):
    """Exams a user can act on: owned + accepted-invite (every exam for dean/admin)."""
    queryset = Exam.objects.select_related('instructor')
    if is_dean(user):
        return queryset
    invited_exam_ids = (
        WorkspaceInvite.objects
        .filter(instructor=user, status=WorkspaceInvite.Status.ACCEPTED)
        .values_list('exam_id', flat=True)
    )
    return queryset.filter(Q(instructor=user) | Q(id__in=invited_exam_ids)).distinct()


# ─── Workspace-scoped report visibility ──────────────────────────────────────
#
# There is no Exam→Workspace foreign key. A workspace links a dean (owner) to
# instructors (accepted memberships). So an exam "belongs" to a workspace when
# its owner participates in that workspace. Report visibility therefore follows
# the people who share a workspace, giving the required isolation: instructors
# in different workspaces never see each other's reports.


def _participating_workspace_ids(user):
    """Workspace ids the user takes part in — as a member, or as the owning dean."""
    member_ws = WorkspaceMembership.objects.filter(instructor=user).values_list('workspace_id', flat=True)
    owned_ws = Workspace.objects.filter(owner=user).values_list('id', flat=True)
    return set(member_ws) | set(owned_ws)


def workspace_related_user_ids(user):
    """User ids that share at least one workspace with *user* (co-members + dean)."""
    ws_ids = _participating_workspace_ids(user)
    related = {user.id}
    related.update(
        WorkspaceMembership.objects
        .filter(workspace_id__in=ws_ids)
        .values_list('instructor_id', flat=True)
    )
    related.update(
        Workspace.objects.filter(id__in=ws_ids).values_list('owner_id', flat=True)
    )
    related.discard(None)
    return related


def visible_exams(user):
    """Exams whose analysis reports *user* may view.

    * **ADMIN** — every exam.
    * **DEAN** — exams owned by the dean or by any member of the dean's workspaces.
    * **INSTRUCTOR** — own exams, exams of co-members in shared workspaces (and the
      workspace's dean), plus any exam the user is an accepted per-exam supervisor of.
    """
    queryset = Exam.objects.select_related('instructor')
    if is_admin(user):
        return queryset
    related = workspace_related_user_ids(user)
    invited_exam_ids = (
        WorkspaceInvite.objects
        .filter(instructor=user, status=WorkspaceInvite.Status.ACCEPTED, exam__isnull=False)
        .values_list('exam_id', flat=True)
    )
    return queryset.filter(
        Q(instructor_id__in=related) | Q(id__in=invited_exam_ids)
    ).distinct()


def can_view_report(user, exam):
    """True when *user* may view the analysis report for *exam* (workspace-scoped)."""
    if is_admin(user):
        return True
    return visible_exams(user).filter(pk=exam.pk).exists()


def visible_sessions(user):
    """Exam sessions whose reports *user* may view (Analysis page exam list)."""
    return (
        ExamSession.objects
        .select_related('exam', 'exam__instructor', 'report', 'analysis_job')
        .filter(exam__in=visible_exams(user))
    )


def visible_videos(user):
    """Uploaded videos whose session reports *user* may view (History scope)."""
    return (
        Video.objects
        .select_related(
            'session', 'session__exam', 'session__exam__instructor',
            # OneToOne sources for VideoReadSerializer's annotated_video_url + analysis.
            'session__analysis_job', 'session__report',
        )
        .filter(session__exam__in=visible_exams(user))
    )
