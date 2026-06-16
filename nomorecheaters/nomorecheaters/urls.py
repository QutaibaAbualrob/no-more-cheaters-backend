# """
# URL configuration for nomorecheaters project.

# The `urlpatterns` list routes URLs to views. For more information please see:
#     https://docs.djangoproject.com/en/5.2/topics/http/urls/
# Examples:
# Function views
#     1. Add an import:  from my_app import views
#     2. Add a URL to urlpatterns:  path('', views.home, name='home')
# Class-based views
#     1. Add an import:  from other_app.views import Home
#     2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
# Including another URLconf
#     1. Import the include() function: from django.urls import include, path
#     2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
# """
from django.contrib import admin
from django.conf import settings
from django.conf.urls.static import static
from django.urls import path, include
from django.views.generic import RedirectView


urlpatterns = [
    path('admin/', admin.site.urls),
    # ── Email link redirects ───────────────────────────────────────────────
    # allauth generates confirmation/reset URLs from these named patterns.
    # We redirect them to the React SPA so the user never sees a Django page.
    #
    # Email verification: the link in the signup email lands on the frontend's
    # /account/verify-email/<key> page, which POSTs the key to verify-email/.
    path(
        'api/auth/registration/account-confirm-email/<str:key>/',
        RedirectView.as_view(
            url=settings.FRONTEND_URL.rstrip('/') + '/account/verify-email/%(key)s',
            permanent=False,
        ),
        name='account_confirm_email',
    ),
    # Password reset: the link in the reset email lands on the frontend's
    # /account/password/reset/key/<uid>/<token>/ page.
    #
    # The frontend route is a splat — `/account/password/reset/key/*` (App.tsx)
    # — so it captures BOTH the uid and token segments; PasswordResetLink reads
    # the splat and api/auth.ts splits the trailing /<uid>/<token>/ back apart to
    # POST the confirm endpoint. The two-segment format below therefore matches
    # the SPA route (FE-BUG-01 / M14 resolved). PasswordResetRedirectTests pins
    # this Location format so the two halves cannot silently drift again.
    #
    # dj-rest-auth reverses 'password_reset_confirm' to build the reset link.
    # We redirect that to the React SPA so the user lands on the frontend form.
    path(
        'api/auth/password/reset/confirm/<str:uid>/<str:token>/',
        RedirectView.as_view(
            url=settings.FRONTEND_URL.rstrip('/')
            + '/account/password/reset/key/%(uid)s/%(token)s/',
            permanent=False,
        ),
        name='password_reset_confirm',
    ),
    # ── Auth API ───────────────────────────────────────────────────────────
    # dj-rest-auth is mounted under /api/auth/ to match the frontend
    # (src/api/auth.ts). These MUST come before the catch-all apis.urls
    # include so /api/auth/... never falls through to the <uuid:pk> routes.
    path('api/auth/', include('dj_rest_auth.urls')),
    path('api/auth/registration/', include('dj_rest_auth.registration.urls')),
    path('api/', include('apis.urls')),
    path('api-auth/', include('rest_framework.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
