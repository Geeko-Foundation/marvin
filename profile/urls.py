from django.urls import path

from profile import views

urlpatterns = [
    path('', views.profile_view, name='profile'),
    path('password/', views.password_change_email, name='password_change_email'),
    path('two-step/setup/', views.two_factor_setup, name='two_factor_setup'),
    path('two-step/verify/', views.two_factor_verify, name='two_factor_verify'),
    path('two-step/backup-codes/', views.two_factor_backup_codes, name='two_factor_backup_codes'),
    path('two-step/off/', views.two_factor_disable, name='two_factor_disable'),
]
