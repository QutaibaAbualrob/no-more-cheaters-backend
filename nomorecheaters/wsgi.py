import os
from django.core.wsgi import get_wsgi_application
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "nomorecheaters.settings")
application = get_wsgi_application()
