






Using third party packages in authentication in django:
    
    book pages: 103 - 113

    1- dj-rest-auth (log in, log out, password reset, password reset confirm):

        a- First we will add log in, log out, and password reset API endpoints:

            python -m pip install dj-rest-auth

            add it in settings


    2- django-allauth (sign up new user, sign up using social media):

        a- added to installed apps:
            "django.contrib.sites"

            "allauth"
            "allauth.account"
            "allauth.socialaccount"
            "dj_rest_auth"
            "dj_rest_auth.registration"


        b- added new values to TEMPLATES in settings:

            "django.template.context_processors.request"

        c- added new vars:

            Needed for user account confirmation:
                EMAIL_BACKEND = "django.core.mail.backends.console. 

            Needed as allauth uses a features in django to host multiple sites from one django project so we have to specifiy be default:

                SITE_ID = 1

    #Endpoints summery:

        dj-rest-auth/login/
        dj-rest-auth/logout/

        dj-rest-auth/password/reset
        dj-rest-auth/password/reset/confirm
        

        dj-rest-auth/registration/
        
#Changing the default django User model:

    Problems found

        In admin.py, I imported SystemSetting, but the actual model name was SystemSettings.

        Django raised reverse accessor clashes for groups and user_permissions because I created a custom User model but had not told Django to use it as the main authentication model.

        After adding AUTH_USER_MODEL = 'apis.User' in settings.py, Django correctly swapped out auth.User.

        Then another error appeared because some files such as views.py were still importing User from django.contrib.auth.models, which no longer works after swapping the user model.

        Fixes applied
            Corrected the typo in admin.py:

        
            from .models import User, ExamSession, Video, Alert, AuditLog, SystemSettings
            Added the custom user model setting in settings.py:

        
        AUTH_USER_MODEL = 'apis.User'
            Updated imports in files like views.py and serializers.py to use the custom user model instead of django.contrib.auth.models.User.

        Recommended approach:
            from django.contrib.auth import get_user_model

            User = get_user_model()
        Or directly:

            from .models import User

    

# User preferences endpoint:

    Added a separate UserPreferences model instead of putting UI and notification
    settings directly on the custom User model.

    Why:

        User is for account identity and authentication fields.

        UserPreferences is for per-user app settings that may grow over time,
        such as notifications, language, timezone, theme, and frontend metadata.

    Model:

        UserPreferences has a OneToOneField to AUTH_USER_MODEL with
        related_name='preferences'.

        Default values:

            email_notifications = True
            dashboard_alerts = True
            preferred_language = 'en'
            timezone = 'UTC'
            theme = 'SYSTEM'
            metadata = {}

    Serializers:

        UserPreferencesReadSerializer:

            Used for GET responses.
            Shows user_email, but does not expose a writable user id.

        UserPreferencesUpdateSerializer:

            Used for PATCH requests.
            Allows only preference fields to be updated.
            Does not expose user, so clients cannot move preferences to another account.

    View:

        MyPreferencesView:

            GET /me/preferences/
                Creates default preferences lazily for the authenticated user
                if no row exists yet, then returns them.

            PATCH /me/preferences/
                Updates only the authenticated user's own preferences.

        Authentication is enforced by the global DRF setting:

            DEFAULT_PERMISSION_CLASSES = [
                'rest_framework.permissions.IsAuthenticated',
            ]

    URL:

        me/preferences/

    Admin:

        UserPreferences is registered in admin with filters, search fields,
        autocomplete for user, and updated_at as read-only.

    Migration:

        0003_userpreferences.py

        Run:

            python manage.py migrate

    Tests:

        Added model tests for default values.
        Added serializer tests to check safe fields and owner protection.
        Added API tests for authentication, lazy creation, updates, and owner safety.

        Current test result:

            python manage.py test apis
            38 tests passing
