from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView
from webpush.views import save_info

from tsp.decorators import login_required

urlpatterns = [
    # One sign-in page for everyone, with its attempt limit and two-step check.
    path('admin/login/', RedirectView.as_view(pattern_name='login', query_string=True)),
    path('admin/', admin.site.urls),
    # Only someone signed in may subscribe a browser to alerts.
    path('webpush/save_information', login_required(save_info), name='save_webpush_info'),
    path('webpush/', include('webpush.urls')),

    path('payments/', include('payments.urls')),
    path('profile/', include('profile.urls')),
    path('', include('tsp.urls')),
]
