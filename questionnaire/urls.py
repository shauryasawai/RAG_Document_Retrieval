from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('project/new/', views.project_create, name='project_create'),
    path('project/<int:pk>/upload/', views.project_upload, name='project_upload'),
    path('project/<int:pk>/generate/', views.project_generate, name='project_generate'),
    path('project/<int:pk>/review/', views.project_review, name='project_review'),
    path('project/<int:pk>/export/', views.project_export, name='project_export'),
    path('project/<int:pk>/delete/', views.project_delete, name='project_delete'),
    path('project/<int:pk>/status/', views.project_status, name='project_status'),
    path('project/<int:pk>/ref-status/', views.ref_status, name='ref_status'),
    path('project/<int:pk>/reprocess/', views.reprocess_documents, name='reprocess_documents'),
    path('answer/<int:pk>/update/', views.answer_update, name='answer_update'),
]