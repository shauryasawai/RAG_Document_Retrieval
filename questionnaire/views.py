import os
import json
import threading
import tempfile
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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _save_upload_to_tmp(uploaded_file) -> str:
    """
    Write a Django UploadedFile to /tmp (the only writable dir on Vercel).
    Returns the absolute path of the temp file.
    Caller is responsible for deleting it with os.unlink() when done.
    """
    ext = '.' + uploaded_file.name.rsplit('.', 1)[-1].lower()
    fd, tmp_path = tempfile.mkstemp(suffix=ext, dir='/tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            for chunk in uploaded_file.chunks():
                f.write(chunk)
    except Exception:
        os.close(fd)
        raise
    return tmp_path


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
    except Exception:
        return settings.OPENAI_API_KEY


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

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

        # ── Upload & parse questionnaire ────────────────────────────────────
        if action == 'upload_questionnaire':
            qfile = request.FILES.get('questionnaire')
            if qfile:
                ext = qfile.name.rsplit('.', 1)[-1].lower()
                tmp_path = _save_upload_to_tmp(qfile)
                try:
                    questions_text = rag_engine.extract_questions_from_file(tmp_path, ext)
                finally:
                    # Always delete — Vercel /tmp is ephemeral anyway
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

                if questions_text:
                    api_key = get_api_key(request.user)
                    categories = rag_engine.categorize_questions(questions_text, api_key)

                    project.questions.all().delete()
                    for i, (qtext, cat) in enumerate(zip(questions_text, categories)):
                        Question.objects.create(
                            project=project,
                            order=i + 1,
                            text=qtext,
                            category=cat
                        )
                    project.total_questions = len(questions_text)
                    project.save()
                    messages.success(request, f'Extracted {len(questions_text)} questions successfully!')
                else:
                    messages.warning(request, 'Could not extract questions. Try a different format.')

        # ── Upload reference documents ──────────────────────────────────────
        elif action == 'upload_reference':
            ref_files = request.FILES.getlist('references')
            api_key = get_api_key(request.user)
            user_pk = request.user.pk

            for rfile in ref_files:
                ext = rfile.name.rsplit('.', 1)[-1].lower()

                # Write to /tmp first so the background thread can read it
                tmp_path = _save_upload_to_tmp(rfile)

                # Create a lightweight DB record (no FileField write)
                ref = ReferenceDocument.objects.create(
                    project=project,
                    name=rfile.name,
                    file_type=ext
                    # NOTE: 'file' FileField is intentionally left blank.
                    # We process from /tmp and never persist the binary.
                )

                threading.Thread(
                    target=process_reference_doc,
                    args=(ref.pk, api_key, user_pk, tmp_path),
                    daemon=True
                ).start()

            messages.success(request, f'Uploading {len(ref_files)} reference document(s)...')

        # ── Manual question entry ───────────────────────────────────────────
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


# ---------------------------------------------------------------------------
# Token-usage helpers
# ---------------------------------------------------------------------------

def _get_or_create_usage(user) -> "TokenUsage":
    usage, _ = TokenUsage.objects.get_or_create(user=user)
    return usage


def _check_token_limit(user):
    """Return (within_limit: bool, usage: TokenUsage)."""
    usage = _get_or_create_usage(user)
    return usage.is_within_limit(), usage


# ---------------------------------------------------------------------------
# Background processing — reads from /tmp, deletes when done
# ---------------------------------------------------------------------------

def process_reference_doc(ref_pk, api_key, user_pk=None, tmp_path=None):
    """
    Background processing of reference documents.

    tmp_path  — path of the file in /tmp written by the upload view.
                If provided it is deleted after chunking completes.
    """
    try:
        ref = ReferenceDocument.objects.get(pk=ref_pk)
        ext = ref.file_type.lower()

        # Resolve the file path: prefer the explicit tmp_path arg,
        # fall back to the FileField path (local-dev compatibility).
        if tmp_path and os.path.exists(tmp_path):
            file_path = tmp_path
        elif ref.file and ref.file.name:
            try:
                file_path = ref.file.path
            except Exception:
                file_path = None
        else:
            file_path = None

        if not file_path or not os.path.exists(file_path):
            print(f"[process_reference_doc] No readable file for ref {ref_pk}. Skipping.")
            ref.processed = True
            ref.save()
            return

        # ── Extract text ───────────────────────────────────────────────────
        if ext == 'pdf':
            pages = rag_engine.extract_text_from_pdf(file_path)
            raw_items = [(p['text'], None, p['page']) for p in pages]
        elif ext in ('docx', 'doc'):
            sections = rag_engine.extract_text_from_docx(file_path)
            raw_items = [(s['text'], s.get('section', ''), None) for s in sections]
        else:
            sections = rag_engine.extract_text_from_txt(file_path)
            raw_items = [(s['text'], s.get('section', ''), None) for s in sections]

        # ── Delete the temp file immediately after reading ──────────────────
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        # ── Chunk ──────────────────────────────────────────────────────────
        all_chunks = []
        for text, section, page in raw_items:
            for chunk in rag_engine.chunk_text(text):
                all_chunks.append((chunk, section or '', page))

        if not all_chunks:
            ref.processed = True
            ref.save()
            return

        # ── Embed ──────────────────────────────────────────────────────────
        texts = [c[0] for c in all_chunks]
        embeddings = [[0.0] * 1536] * len(texts)
        embed_tokens = 0

        if api_key:
            embeddings, embed_tokens = rag_engine.get_embeddings(texts, api_key)

        if user_pk and embed_tokens:
            try:
                usage = _get_or_create_usage(User.objects.get(pk=user_pk))
                usage.add_usage(embed_tokens, 0)
            except Exception as e:
                print(f"Token tracking error: {e}")

        # ── Persist chunks ─────────────────────────────────────────────────
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
        # Clean up tmp file even on error
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

@login_required
def project_generate(request, pk):
    project = get_object_or_404(Project, pk=pk, user=request.user)

    if request.method == 'POST':
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
            args=(project.pk, api_key, question_ids, request.user.pk),
            daemon=True
        ).start()

        messages.success(request, 'AI is generating answers. This may take a few minutes...')
        return redirect('project_review', pk=pk)

    questions = project.questions.all()
    refs = project.references.filter(processed=True)
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

            if user_pk:
                try:
                    usage = _get_or_create_usage(User.objects.get(pk=user_pk))
                    u = result.get('usage', {})
                    usage.add_usage(u.get('prompt_tokens', 0), u.get('completion_tokens', 0))
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
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Review / export / misc
# ---------------------------------------------------------------------------

@login_required
def project_review(request, pk):
    project = get_object_or_404(Project, pk=pk, user=request.user)
    questions = project.questions.select_related('answer').all()

    for q in questions:
        try:
            _ = q.answer
            q.has_answer_flag = True
        except Exception:
            q.has_answer_flag = False

    categories = {}
    for q in questions:
        cat = q.category or 'General'
        categories.setdefault(cat, []).append(q)

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

    project = answer.question.project
    project.answered_questions = project.questions.filter(
        status__in=['answered', 'reviewed']
    ).count()
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
        'refs': [
            {'id': r.pk, 'name': r.name, 'processed': r.processed, 'chunks': r.chunk_count}
            for r in refs
        ]
    })


@login_required
def project_export(request, pk):
    project = get_object_or_404(Project, pk=pk, user=request.user)
    fmt = request.GET.get('format', 'docx')

    if fmt == 'docx':
        file_path = exporter.export_to_docx(project)
        with open(file_path, 'rb') as f:
            response = HttpResponse(
                f.read(),
                content_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document'
            )
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
    api_key = get_api_key(request.user)

    def _reprocess():
        try:
            for ref in project.references.all():
                ref.chunks.all().delete()
                ref.chunk_count = 0
                ref.processed = False
                ref.save()
                # No tmp_path here — file is already gone; re-upload is needed
                # This path is only useful in local dev where the FileField path exists
                process_reference_doc(ref.pk, api_key)
        except Exception as e:
            print(f"Reprocess error: {e}")

    threading.Thread(target=_reprocess, daemon=True).start()
    return JsonResponse({'status': 'reprocessing'})


@login_required
def token_usage_status(request):
    """AJAX: returns current token usage for the logged-in user."""
    usage = _get_or_create_usage(request.user)
    return JsonResponse({
        'tokens_used': usage.total_tokens_used,
        'token_limit': usage.max_token_limit,
        'cost_usd': float(usage.total_cost_usd),
        'within_limit': usage.is_within_limit(),
        'percent_used': round(usage.total_tokens_used / usage.max_token_limit * 100, 1)
    })