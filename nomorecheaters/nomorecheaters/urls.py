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
    # allauth builds the confirmation link from the URL named
    # `account_confirm_email`. dj-rest-auth does not register it by default, so
    # we declare it here BEFORE the registration include and redirect the user
    # to the SPA's verify-email page (which POSTs the key to verify-email/).
    # Without this the emailed link 404s on the backend.
    path(
        'api/auth/registration/account-confirm-email/<str:key>/',
        RedirectView.as_view(
            url=settings.FRONTEND_URL.rstrip('/') + '/account/verify-email/%(key)s',
            permanent=False,
        ),
        name='account_confirm_email',
    ),
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
