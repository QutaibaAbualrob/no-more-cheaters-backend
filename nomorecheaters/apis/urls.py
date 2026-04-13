
from django.urls import path


from .views import UsersListView

urlpatterns = [
    path('', UsersListView.as_view(), name='users_list'),
]
