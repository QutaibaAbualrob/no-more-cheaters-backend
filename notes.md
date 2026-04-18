






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

    Endpoints summery:

        dj-rest-auth/login/
        dj-rest-auth/logout/

        dj-rest-auth/password/reset
        dj-rest-auth/password/reset/confirm
        

        dj-rest-auth/registration/
        

