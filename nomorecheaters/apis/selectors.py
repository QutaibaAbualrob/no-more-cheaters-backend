from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import ExamSession, Report, Video


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
