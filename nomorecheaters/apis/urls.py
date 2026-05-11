
from django.urls import path


from .views import DeleteUserView, MyPreferencesView, UsersListView

urlpatterns = [
    path('', UsersListView.as_view(), name='users_list'),
    path('me/preferences/', MyPreferencesView.as_view(), name='my_preferences'),
    path('<uuid:pk>/', DeleteUserView.as_view(), name='delete_user'),
]
