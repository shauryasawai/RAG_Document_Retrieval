from django.contrib import admin
from .models import Project, ReferenceDocument, DocumentChunk, Question, Answer, TokenUsage


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


@admin.register(TokenUsage)
class TokenUsageAdmin(admin.ModelAdmin):
    list_display = ['user', 'total_tokens_used', 'max_token_limit', 'total_cost_usd', 'last_updated']
    list_editable = ['max_token_limit']
    search_fields = ['user__username']
