from django.db import models
from django.contrib.auth.models import User
import json

class Project(models.Model):
    STATUS_CHOICES = [
        ('setup', 'Setting Up'),
        ('processing', 'Processing'),
        ('review', 'Under Review'),
        ('completed', 'Completed'),
    ]
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='projects')
    name = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='setup')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    questionnaire_file = models.FileField(upload_to='questionnaires/', null=True, blank=True)
    total_questions = models.IntegerField(default=0)
    answered_questions = models.IntegerField(default=0)
    confidence_score = models.FloatField(default=0.0)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return self.name

    @property
    def progress_percent(self):
        if self.total_questions == 0:
            return 0
        return int((self.answered_questions / self.total_questions) * 100)

class ReferenceDocument(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='references')
    name = models.CharField(max_length=300)
    file = models.FileField(upload_to='references/')
    file_type = models.CharField(max_length=20)
    chunk_count = models.IntegerField(default=0)
    processed = models.BooleanField(default=False)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

class DocumentChunk(models.Model):
    document = models.ForeignKey(ReferenceDocument, on_delete=models.CASCADE, related_name='chunks')
    content = models.TextField()
    chunk_index = models.IntegerField()
    embedding = models.TextField(blank=True)  # JSON-serialized vector
    page_number = models.IntegerField(null=True, blank=True)
    section_title = models.CharField(max_length=500, blank=True)

    def get_embedding(self):
        if self.embedding:
            return json.loads(self.embedding)
        return None

    def set_embedding(self, vector):
        self.embedding = json.dumps(vector)

class Question(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('generating', 'Generating'),
        ('answered', 'Answered'),
        ('reviewed', 'Reviewed'),
    ]
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='questions')
    order = models.IntegerField()
    text = models.TextField()
    category = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f"Q{self.order}: {self.text[:80]}"

class Answer(models.Model):
    question = models.OneToOneField(Question, on_delete=models.CASCADE, related_name='answer')
    generated_answer = models.TextField()
    edited_answer = models.TextField(blank=True)
    citations = models.JSONField(default=list)
    confidence_score = models.FloatField(default=0.0)
    relevant_chunks = models.ManyToManyField(DocumentChunk, blank=True)
    generated_at = models.DateTimeField(auto_now_add=True)
    edited_at = models.DateTimeField(null=True, blank=True)
    is_edited = models.BooleanField(default=False)

    @property
    def final_answer(self):
        return self.edited_answer if self.is_edited else self.generated_answer

    def __str__(self):
        return f"Answer to Q{self.question.order}"
