from django.contrib import admin
from .models import Project, ReferenceDocument, DocumentChunk, Question, Answer

@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ['name', 'user', 'status', 'total_questions', 'answered_questions', 'created_at']

@admin.register(ReferenceDocument)
class RefAdmin(admin.ModelAdmin):
    list_display = ['name', 'project', 'file_type', 'chunk_count', 'processed']

@admin.register(Question)
class QuestionAdmin(admin.ModelAdmin):
    list_display = ['order', 'project', 'text', 'status', 'category']

@admin.register(Answer)
class AnswerAdmin(admin.ModelAdmin):
    list_display = ['question', 'confidence_score', 'is_edited', 'generated_at']
