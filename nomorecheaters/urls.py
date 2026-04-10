from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
   # path("django-rq/", include("django_rq.urls")),
    path("api/auth/", include("accounts.urls")),
    path("api/", include("proctoring.urls")),
    path("api/audit/", include("audit.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
