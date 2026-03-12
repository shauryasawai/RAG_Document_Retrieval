import os
import json
import threading
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse
from django.contrib import messages
from django.utils import timezone
from django.conf import settings
from django.db.models import Avg

from .models import Project, ReferenceDocument, DocumentChunk, Question, Answer, TokenUsage
from base.models import UserProfile
from . import rag_engine
from . import exporter
from django.contrib.auth.models import User

MAX_USERS = 3

def _check_single_user_limit(request):
    """Return an error JsonResponse if user limit is reached, else None."""
    if User.objects.count() > MAX_USERS:
        return JsonResponse(
            {'error': f'User limit of {MAX_USERS} reached. Contact the administrator.'},
            status=403
        )
    return None

def get_api_key(user):
    try:
        profile = user.userprofile
        return profile.openai_api_key or settings.OPENAI_API_KEY
    except:
        return settings.OPENAI_API_KEY


@login_required
def dashboard(request):
    projects = Project.objects.filter(user=request.user)
    stats = {
        'total_projects': projects.count(),
        'completed': projects.filter(status='completed').count(),
        'in_progress': projects.filter(status__in=['processing', 'review']).count(),
        'total_questions': sum(p.total_questions for p in projects),
    }
    return render(request, 'questionnaire/dashboard.html', {
        'projects': projects[:10],
        'stats': stats
    })


@login_required
def project_create(request):
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        description = request.POST.get('description', '').strip()
        if not name:
            messages.error(request, 'Project name is required.')
            return redirect('dashboard')
        project = Project.objects.create(
            user=request.user,
            name=name,
            description=description
        )
        return redirect('project_upload', pk=project.pk)
    return redirect('dashboard')


@login_required
def project_upload(request, pk):
    project = get_object_or_404(Project, pk=pk, user=request.user)
    if request.method == 'POST':
        action = request.POST.get('action')
        
        if action == 'upload_questionnaire':
            qfile = request.FILES.get('questionnaire')
            if qfile:
                project.questionnaire_file = qfile
                project.save()
                # Parse questions
                ext = qfile.name.split('.')[-1].lower()
                file_path = project.questionnaire_file.path
                questions_text = rag_engine.extract_questions_from_file(file_path, ext)
                
                if questions_text:
                    api_key = get_api_key(request.user)
                    categories = rag_engine.categorize_questions(questions_text, api_key)
                    
                    # Clear existing questions
                    project.questions.all().delete()
                    for i, (qtext, cat) in enumerate(zip(questions_text, categories)):
                        Question.objects.create(
                            project=project,
                            order=i+1,
                            text=qtext,
                            category=cat
                        )
                    project.total_questions = len(questions_text)
                    project.save()
                    messages.success(request, f'Extracted {len(questions_text)} questions successfully!')
                else:
                    messages.warning(request, 'Could not extract questions. Try a different format.')

        elif action == 'upload_reference':
            ref_files = request.FILES.getlist('references')
            for rfile in ref_files:
                ext = rfile.name.split('.')[-1].lower()
                ref = ReferenceDocument.objects.create(
                    project=project,
                    name=rfile.name,
                    file=rfile,
                    file_type=ext
                )
                # Process in background thread
                threading.Thread(
                    target=process_reference_doc,
                    args=(ref.pk, get_api_key(request.user))
                ).start()
            messages.success(request, f'Uploading {len(ref_files)} reference document(s)...')

        elif action == 'add_question_manual':
            qtext = request.POST.get('question_text', '').strip()
            if qtext:
                order = project.questions.count() + 1
                Question.objects.create(project=project, order=order, text=qtext, category='Manual')
                project.total_questions = project.questions.count()
                project.save()
                messages.success(request, 'Question added.')

        return redirect('project_upload', pk=pk)

    refs = project.references.all()
    questions = project.questions.all()
    return render(request, 'questionnaire/upload.html', {
        'project': project,
        'references': refs,
        'questions': questions,
    })

def _get_or_create_usage(user) -> "TokenUsage":
    usage, _ = TokenUsage.objects.get_or_create(user=user)
    return usage


def _check_token_limit(user):
    """Return (within_limit: bool, usage: TokenUsage)."""
    usage = _get_or_create_usage(user)
    return usage.is_within_limit(), usage

def process_reference_doc(ref_pk, api_key, user_pk=None):
    """Background processing of reference documents — now tracks token usage."""
    try:
        ref = ReferenceDocument.objects.get(pk=ref_pk)
        file_path = ref.file.path
        ext = ref.file_type.lower()

        if ext == 'pdf':
            pages = rag_engine.extract_text_from_pdf(file_path)
            raw_items = [(p['text'], None, p['page']) for p in pages]
        elif ext in ['docx', 'doc']:
            sections = rag_engine.extract_text_from_docx(file_path)
            raw_items = [(s['text'], s.get('section', ''), None) for s in sections]
        else:
            sections = rag_engine.extract_text_from_txt(file_path)
            raw_items = [(s['text'], s.get('section', ''), None) for s in sections]

        all_chunks = []
        for text, section, page in raw_items:
            chunks = rag_engine.chunk_text(text)
            for chunk in chunks:
                all_chunks.append((chunk, section or '', page))

        if not all_chunks:
            ref.processed = True
            ref.save()
            return

        texts = [c[0] for c in all_chunks]
        embeddings = [[0.0] * 1536] * len(texts)
        embed_tokens = 0

        if api_key:
            embeddings, embed_tokens = rag_engine.get_embeddings(texts, api_key)

        # Track embedding token usage
        if user_pk and embed_tokens:
            try:
                usage = _get_or_create_usage(User.objects.get(pk=user_pk))
                usage.add_usage(embed_tokens, 0)
            except Exception as e:
                print(f"Token tracking error: {e}")

        for i, (chunk_text, section, page) in enumerate(all_chunks):
            dc = DocumentChunk(
                document=ref,
                content=chunk_text,
                chunk_index=i,
                section_title=section or '',
                page_number=page
            )
            if i < len(embeddings):
                dc.set_embedding(embeddings[i])
            dc.save()

        ref.chunk_count = len(all_chunks)
        ref.processed = True
        ref.save()
        print(f"Processed {ref.name}: {len(all_chunks)} chunks, {embed_tokens} embed tokens")
    except Exception as e:
        print(f"Error processing doc {ref_pk}: {e}")


@login_required
def project_generate(request, pk):
    project = get_object_or_404(Project, pk=pk, user=request.user)

    if request.method == 'POST':
        # Enforce token limit
        within_limit, usage = _check_token_limit(request.user)
        if not within_limit:
            messages.error(
                request,
                f'Token limit reached ({usage.total_tokens_used:,} / {usage.max_token_limit:,} tokens). '
                f'Contact your administrator to increase the limit.'
            )
            return redirect('project_review', pk=pk)

        project.status = 'processing'
        project.save()

        api_key = get_api_key(request.user)
        question_ids = request.POST.getlist('question_ids')

        threading.Thread(
            target=generate_answers_task,
            args=(project.pk, api_key, question_ids, request.user.pk)
        ).start()

        messages.success(request, 'AI is generating answers. This may take a few minutes...')
        return redirect('project_review', pk=pk)

    questions = project.questions.all()
    refs = project.references.filter(processed=True)

    # Pass usage info to template
    _, usage = _check_token_limit(request.user)
    return render(request, 'questionnaire/generate.html', {
        'project': project,
        'questions': questions,
        'references': refs,
        'token_usage': usage,
    })


def generate_answers_task(project_pk, api_key, question_ids=None, user_pk=None):
    """Background task — generates answers and tracks token usage."""
    try:
        project = Project.objects.get(pk=project_pk)

        # Token limit check before starting
        if user_pk:
            try:
                user = User.objects.get(pk=user_pk)
                within_limit, usage = _check_token_limit(user)
                if not within_limit:
                    print(f"Token limit reached for user {user_pk}. Aborting generation.")
                    project.status = 'review'
                    project.save()
                    return
            except Exception as e:
                print(f"Token limit check error: {e}")

        chunks_data = []
        for ref in project.references.filter(processed=True):
            for chunk in ref.chunks.all():
                emb = chunk.get_embedding()
                chunks_data.append((chunk.pk, chunk.content, emb, ref.name, chunk.page_number))

        if not chunks_data:
            project.status = 'review'
            project.save()
            return

        questions = project.questions.all()
        if question_ids:
            questions = questions.filter(pk__in=question_ids)

        answered = 0
        total_confidence = 0.0

        for question in questions:
            # Re-check limit before each question
            if user_pk:
                try:
                    user = User.objects.get(pk=user_pk)
                    within_limit, usage = _check_token_limit(user)
                    if not within_limit:
                        print(f"Token limit hit mid-generation at question {question.pk}. Stopping.")
                        break
                except Exception:
                    pass

            question.status = 'generating'
            question.save()

            relevant = rag_engine.retrieve_relevant_chunks(
                question.text, chunks_data, api_key, top_k=5
            )

            result = rag_engine.generate_answer(
                question.text, relevant, api_key, project.name
            )

            # Track tokens from this answer
            if user_pk:
                try:
                    usage = _get_or_create_usage(User.objects.get(pk=user_pk))
                    u = result.get('usage', {})
                    usage.add_usage(
                        u.get('prompt_tokens', 0),
                        u.get('completion_tokens', 0)
                    )
                except Exception as e:
                    print(f"Token tracking error: {e}")

            Answer.objects.update_or_create(
                question=question,
                defaults={
                    'generated_answer': result['answer'],
                    'citations': result['citations'],
                    'confidence_score': result['confidence'],
                }
            )

            ans = question.answer
            chunk_ids = [r[1] for r in relevant if r[0] > 0.3]
            ans.relevant_chunks.set(DocumentChunk.objects.filter(pk__in=chunk_ids))

            question.status = 'answered'
            question.save()
            answered += 1
            total_confidence += result['confidence']

        project.answered_questions = project.questions.filter(
            status__in=['answered', 'reviewed']
        ).count()
        project.confidence_score = (total_confidence / answered) if answered > 0 else 0
        project.status = 'review'
        project.save()

    except Exception as e:
        print(f"Generation task error: {e}")
        try:
            Project.objects.filter(pk=project_pk).update(status='review')
        except:
            pass


@login_required
def project_review(request, pk):
    project = get_object_or_404(Project, pk=pk, user=request.user)
    questions = project.questions.select_related('answer').all()

    # Annotate each question with a plain boolean so templates never need
    # a custom filter or a try/except to check for a related Answer row.
    for q in questions:
        try:
            _ = q.answer  # touch the cached relation
            q.has_answer_flag = True
        except Exception:
            q.has_answer_flag = False

    # Group by category
    categories = {}
    for q in questions:
        cat = q.category or 'General'
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(q)

    return render(request, 'questionnaire/review.html', {
        'project': project,
        'questions': questions,
        'categories': categories,
    })


@login_required
def answer_update(request, pk):
    """AJAX endpoint to save edited answer"""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    
    answer = get_object_or_404(Answer, pk=pk, question__project__user=request.user)
    data = json.loads(request.body)
    
    answer.edited_answer = data.get('text', '')
    answer.is_edited = True
    answer.edited_at = timezone.now()
    answer.save()
    
    answer.question.status = 'reviewed'
    answer.question.save()
    
    # Update project count
    project = answer.question.project
    project.answered_questions = project.questions.filter(status__in=['answered', 'reviewed']).count()
    project.save()
    
    return JsonResponse({'success': True, 'is_edited': True})


@login_required
def project_status(request, pk):
    """AJAX: get generation progress"""
    project = get_object_or_404(Project, pk=pk, user=request.user)
    answered = project.questions.filter(status__in=['answered', 'reviewed']).count()
    total = project.questions.count()
    
    return JsonResponse({
        'status': project.status,
        'answered': answered,
        'total': total,
        'progress': int((answered / total * 100)) if total > 0 else 0,
        'confidence': round(project.confidence_score * 100, 1)
    })


@login_required
def ref_status(request, pk):
    """AJAX: check if reference docs are processed"""
    project = get_object_or_404(Project, pk=pk, user=request.user)
    refs = project.references.all()
    return JsonResponse({
        'refs': [{'id': r.pk, 'name': r.name, 'processed': r.processed, 'chunks': r.chunk_count} for r in refs]
    })


@login_required
def project_export(request, pk):
    project = get_object_or_404(Project, pk=pk, user=request.user)
    fmt = request.GET.get('format', 'docx')
    
    if fmt == 'docx':
        file_path = exporter.export_to_docx(project)
        with open(file_path, 'rb') as f:
            response = HttpResponse(f.read(), content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document')
            response['Content-Disposition'] = f'attachment; filename="{project.name}_answers.docx"'
        os.unlink(file_path)
        return response
    elif fmt == 'json':
        data = exporter.export_to_json(project)
        response = HttpResponse(json.dumps(data, indent=2), content_type='application/json')
        response['Content-Disposition'] = f'attachment; filename="{project.name}_answers.json"'
        return response
    
    return redirect('project_review', pk=pk)


@login_required
def project_delete(request, pk):
    project = get_object_or_404(Project, pk=pk, user=request.user)
    if request.method == 'POST':
        project.delete()
        messages.success(request, 'Project deleted.')
    return redirect('dashboard')

@login_required
def reprocess_documents(request, pk):
    """Re-embed all reference documents for a project (e.g. after adding API key)."""
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    project = get_object_or_404(Project, pk=pk, user=request.user)
    api_key = request.user.userprofile.openai_api_key if hasattr(request.user, 'userprofile') else ''

    def _reprocess():
        try:
            for ref in project.references.all():
                # Delete existing chunks and reprocess
                ref.chunks.all().delete()
                ref.chunk_count = 0
                ref.processed = False
                ref.save()
                process_reference_doc(ref.pk, api_key)
        except Exception as e:
            print(f"Reprocess error: {e}")

    import threading
    threading.Thread(target=_reprocess, daemon=True).start()
    return JsonResponse({'status': 'reprocessing'})


@login_required
def token_usage_status(request):
    """AJAX or page: returns current token usage for the logged-in user."""
    usage = _get_or_create_usage(request.user)
    return JsonResponse({
        'tokens_used': usage.total_tokens_used,
        'token_limit': usage.max_token_limit,
        'cost_usd': float(usage.total_cost_usd),
        'within_limit': usage.is_within_limit(),
        'percent_used': round(usage.total_tokens_used / usage.max_token_limit * 100, 1)
    })