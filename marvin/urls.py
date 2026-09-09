from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('webpush/', include('webpush.urls')),
    path('payments/', include('payments.urls')),
    path('profile/', include('profile.urls')),
    path('', include('tsp.urls')),
]
