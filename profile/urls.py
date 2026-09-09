from django.urls import path

from profile import views

urlpatterns = [
    path('', views.profile_view, name='profile'),
]
