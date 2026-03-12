from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.shortcuts import redirect

urlpatterns = [
    path('admin/', admin.site.urls),
    path('base/', include('base.urls')),
    path('', include('questionnaire.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
