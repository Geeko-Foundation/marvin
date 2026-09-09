from django.urls import path

from payments import views

app_name = 'payments'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('process/', views.process_payment, name='process'),
    path('batch-export/', views.batch_export, name='batch_export'),
    path('<int:pk>/status/', views.payment_status, name='status'),
    path('gateways/<str:gateway_id>/currencies/', views.gateway_currencies, name='gateway_currencies'),
]
