






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

    