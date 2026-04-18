
from django.urls import path


from .views import UsersListView, DeleteUserView

urlpatterns = [
    path('', UsersListView.as_view(), name='users_list'),
    path('<int:pk>/', DeleteUserView.as_view(), name='delete_user'),
]
