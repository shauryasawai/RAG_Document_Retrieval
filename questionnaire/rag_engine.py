"""
RAG Engine - Core AI component for document processing and answer generation
"""
import json
import math
import re
from typing import List, Tuple
import PyPDF2
import docx

# ── Tuning constants ───────────────────────────────────────────────────────
CHUNK_SIZE        = 350
CHUNK_OVERLAP     = 50
RETRIEVAL_TOP_K   = 3
CONTEXT_CHARS     = 400
MAX_ANSWER_TOKENS = 350
EMBED_BATCH       = 100
# ──────────────────────────────────────────────────────────────────────────


# ── Text cleaning ──────────────────────────────────────────────────────────

def _clean_pdf_text(text: str) -> str:
    text = re.sub(r' *-\n +', '-', text)
    text = re.sub(r'(\w) -\n(\w)', r'\1-\2', text)
    text = re.sub(r'(\w) - (\w)', r'\1-\2', text)
    text = re.sub(r'(\w) -(\w)',  r'\1-\2', text)
    text = re.sub(r'(\w)- (\w)',  r'\1-\2', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'_{3,}', '___', text)
    return text


# ── Extractors ─────────────────────────────────────────────────────────────

def extract_text_from_pdf(file_path: str) -> List[dict]:
    pages = []
    try:
        with open(file_path, 'rb') as f:
            reader = PyPDF2.PdfReader(f)
            for i, page in enumerate(reader.pages):
                text = _clean_pdf_text(page.extract_text() or '')
                if text.strip():
                    pages.append({'text': text, 'page': i + 1})
    except Exception as e:
        print(f"PDF extraction error: {e}")
    return pages


def extract_text_from_docx(file_path: str) -> List[dict]:
    _q_re   = re.compile(r'^Q\s*\d+\.?$', re.IGNORECASE)
    _ph_re  = re.compile(r'^(answer|source|citation|_+|\s*)$', re.IGNORECASE)
    sections = []
    try:
        doc = docx.Document(file_path)
        current_section = ''
        buffer = []

        def flush():
            nonlocal buffer
            if buffer:
                sections.append({'text': '\n'.join(buffer), 'section': current_section})
                buffer = []

        def cell_txt(cell):
            return ' '.join(p.text.strip() for p in cell.paragraphs if p.text.strip()).strip()

        for child in doc.element.body:
            tag = child.tag.split('}')[-1]
            if tag == 'p':
                para = docx.text.paragraph.Paragraph(child, doc)
                style = ''
                try:
                    style = para.style.name or ''
                except Exception:
                    pass
                text = para.text.strip()
                if style.startswith('Heading') and text:
                    flush()
                    current_section = text
                elif text:
                    buffer.append(text)
            elif tag == 'tbl':
                table = docx.table.Table(child, doc)
                for row in table.rows:
                    if not row.cells:
                        continue
                    first = cell_txt(row.cells[0])
                    if _q_re.match(first) or _ph_re.match(first):
                        continue
                    parts = [cell_txt(c) for c in row.cells
                             if cell_txt(c) and not _ph_re.match(cell_txt(c))]
                    if parts:
                        buffer.append(' '.join(parts))
        flush()
    except Exception as e:
        print(f"DOCX extraction error: {e}")
    return sections


def extract_text_from_txt(file_path: str) -> List[dict]:
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            return [{'text': f.read(), 'section': 'Document'}]
    except Exception as e:
        print(f"TXT extraction error: {e}")
        return []


# ── Question extraction ────────────────────────────────────────────────────

def _extract_questions_from_pdf(file_path: str) -> List[str]:
    pages = extract_text_from_pdf(file_path)
    if not pages:
        return []
    full_text = "\n".join(p["text"] for p in pages)
    full_text = re.sub(r"[^\n]*(?:CONFIDENTIAL|Page\s+\d+)\s+(?=Q\s*\d)", "\n",
                       full_text, flags=re.IGNORECASE)
    full_text = re.sub(r"^[^\n]*CONFIDENTIAL[^\n]*$", "",
                       full_text, flags=re.IGNORECASE | re.MULTILINE)
    full_text = re.sub(r"\n{3,}", "\n\n", full_text)

    known_categories: set = set()
    for m in re.finditer(r"^Q\s*\d{1,3}\s+(.+?)  ", full_text, re.MULTILINE):
        cat = m.group(1).strip()
        if 1 <= len(cat.split()) <= 4:
            known_categories.add(cat)

    q_dbl_re   = re.compile(r"^Q\s*\d{1,3}\s+.+?  (.+)$",  re.MULTILINE)
    q_plain_re = re.compile(r"^Q\s*\d{1,3}\s+(.+)$",        re.MULTILINE)
    num_re     = re.compile(r"^\d{1,3}[.)]\s+(.+)$",          re.MULTILINE)
    stop_re    = re.compile(
        r"^(?:Answer\b|Source\b|___|Section\s|DECLARATION|INSTRUCTIONS)",
        re.IGNORECASE)

    questions: list = []
    current_parts: list = []
    in_question = False

    def flush():
        nonlocal current_parts, in_question
        if current_parts:
            joined = " ".join(" ".join(p.split()) for p in current_parts).strip()
            if len(joined) >= 15:
                questions.append(joined)
        current_parts.clear()
        in_question = False

    for raw_line in full_text.split("\n"):
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stop_re.match(stripped) or re.match(r"^Section\s+[A-Z]", stripped):
            flush()
            continue
        m = q_dbl_re.match(stripped)
        if m:
            flush()
            if m.group(1).strip():
                current_parts = [m.group(1).strip()]
            in_question = True
            continue
        m = q_plain_re.match(stripped)
        if m:
            remainder = m.group(1).strip()
            flush()
            if remainder and remainder not in known_categories:
                current_parts = [remainder]
            in_question = True
            continue
        m = num_re.match(stripped)
        if m:
            flush()
            current_parts = [m.group(1).strip()]
            in_question = True
            continue
        if in_question:
            current_parts.append(stripped)
            if " ".join(current_parts).rstrip().endswith("?"):
                flush()
    flush()
    return questions


def _extract_questions_from_docx_tables(file_path: str) -> List[str]:
    questions: List[str] = []
    try:
        doc = docx.Document(file_path)
        q_label_re     = re.compile(r'^Q[\s\-]?\d+\.?$', re.IGNORECASE)
        num_label_re   = re.compile(r'^\d{1,3}[\.\)\:]$')
        placeholder_re = re.compile(
            r'^(answer|source|citation|response|comments?|_+|[-\u2013\u2014]+|\s*)$',
            re.IGNORECASE)
        inline_q_re    = re.compile(
            r'^(?:Q[\s\-]?\d+|Question\s+\d+|No\.?\s*\d+)[\.\:\)]\s+(.{10,})',
            re.IGNORECASE)

        def cell_full_text(cell) -> str:
            return ' '.join(p.text.strip() for p in cell.paragraphs if p.text.strip()).strip()

        for table in doc.tables:
            if not table.rows:
                continue
            cell_texts = []
            for ct in [cell_full_text(c) for c in table.rows[0].cells]:
                if not cell_texts or ct != cell_texts[-1]:
                    cell_texts.append(ct)
            first = cell_texts[0] if cell_texts else ''
            if len(cell_texts) == 1:
                m = inline_q_re.match(first)
                if m:
                    questions.append(m.group(1).strip())
                continue
            if q_label_re.match(first) or num_label_re.match(first):
                for ct in reversed(cell_texts[1:]):
                    if ct and not placeholder_re.match(ct) and len(ct) > 10:
                        questions.append(ct)
                        break
                continue
            m = inline_q_re.match(first)
            if m:
                questions.append(m.group(1).strip())
    except Exception as e:
        print(f"Table question extraction error: {e}")
    return questions


def extract_questions_from_file(file_path: str, file_type: str) -> List[str]:
    questions: List[str] = []

    if file_type == 'pdf':
        questions = _extract_questions_from_pdf(file_path)
        if questions:
            return _deduplicate(questions)

    if file_type in ('docx', 'doc'):
        questions = _extract_questions_from_docx_tables(file_path)
        if questions:
            return _deduplicate(questions)

    # Fallback: full-text line parser
    if file_type == 'pdf':
        full_text = '\n'.join(p['text'] for p in extract_text_from_pdf(file_path))
    elif file_type in ('docx', 'doc'):
        full_text = '\n'.join(s['text'] for s in extract_text_from_docx(file_path))
    else:
        full_text = '\n'.join(s['text'] for s in extract_text_from_txt(file_path))

    _tab_q_re = re.compile(
        r'^(?:Q[\s\-]?\d+\.?|Question\s+\d+[\.\:\)]|\d{1,3}[\.\)\:])\t',
        re.IGNORECASE)
    cleaned_lines = []
    for raw in full_text.split('\n'):
        if _tab_q_re.match(raw):
            parts = [p.strip() for p in raw.split('\t') if p.strip()]
            candidates = [p for p in parts[1:] if len(p) > 15]
            if candidates:
                cleaned_lines.append(max(candidates, key=len))
        else:
            cleaned_lines.append(raw)
    full_text = '\n'.join(cleaned_lines)

    start_patterns = [
        re.compile(r'^\d{1,3}[\.\)]\s+\S'),
        re.compile(r'^Q[\s\-]?\d+[\.\:\)]\s+\S', re.I),
        re.compile(r'^Question\s+\d+[\.\:\)]\s+\S', re.I),
        re.compile(r'^No\.?\s*\d+[\.\:\)]\s+\S', re.I),
    ]
    ends_with_q   = re.compile(r'.{15,}\?\s*$')
    structural_re = re.compile(r'^(?:[A-Z][A-Z\s]{4,}|Section\s+\w|SECTION\s+\w|Part\s+[A-Z\d])')
    current_parts: List[str] = []

    def flush_question():
        nonlocal current_parts
        if current_parts:
            full = ' '.join(' '.join(p.split()) for p in current_parts)
            if len(full) > 10:
                questions.append(full.strip())
        current_parts = []

    for raw_line in full_text.split('\n'):
        line = raw_line.strip()
        if not line:
            flush_question()
            continue
        matched = False
        for pat in start_patterns:
            if pat.match(line):
                flush_question()
                clean = re.sub(
                    r'^(?:\d{1,3}[\.\)]|Q[\s\-]?\d+[\.\:\)]|'
                    r'Question\s+\d+[\.\:\)]|No\.?\s*\d+[\.\:\)])\s*',
                    '', line, flags=re.IGNORECASE).strip()
                if len(clean) > 5:
                    current_parts = [clean]
                matched = True
                break
        if not matched:
            if current_parts:
                if structural_re.match(line):
                    flush_question()
                else:
                    current_parts.append(line)
                    if ' '.join(current_parts).rstrip().endswith('?'):
                        flush_question()
            elif ends_with_q.match(line):
                flush_question()
                questions.append(line.strip())
    flush_question()
    return _deduplicate(questions)


def _deduplicate(questions: List[str]) -> List[str]:
    seen: set = set()
    unique: List[str] = []
    for q in questions:
        key = q.lower().strip()
        if key not in seen and key:
            seen.add(key)
            unique.append(q)
    return unique


# ── Chunking ───────────────────────────────────────────────────────────────

def chunk_text(text: str, chunk_size: int = CHUNK_SIZE,
               overlap: int = CHUNK_OVERLAP) -> List[str]:
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunks.append(' '.join(words[i:i + chunk_size]))
        i += chunk_size - overlap
    return [c for c in chunks if len(c.strip()) > 50]


# ── Embeddings ─────────────────────────────────────────────────────────────

def get_embeddings(texts: List[str], api_key: str) -> tuple:
    """Returns (embeddings_list, total_tokens_used)."""
    import urllib.request

    embeddings: List[List[float]] = []
    total_tokens = 0

    for i in range(0, len(texts), EMBED_BATCH):
        batch   = texts[i:i + EMBED_BATCH]
        payload = json.dumps({
            'model': 'text-embedding-3-small',
            'input': batch
        }).encode()
        req = urllib.request.Request(
            'https://api.openai.com/v1/embeddings',
            data=payload,
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
            }
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                for item in data['data']:
                    embeddings.append(item['embedding'])
                total_tokens += data.get('usage', {}).get('total_tokens', 0)
        except Exception as e:
            print(f"Embedding error: {e}")
            embeddings.extend([[0.0] * 1536] * len(batch))

    return embeddings, total_tokens


# ── Similarity ─────────────────────────────────────────────────────────────

def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    if not vec1 or not vec2:
        return 0.0
    dot   = sum(a * b for a, b in zip(vec1, vec2))
    norm1 = math.sqrt(sum(a * a for a in vec1))
    norm2 = math.sqrt(sum(b * b for b in vec2))
    return dot / (norm1 * norm2) if norm1 and norm2 else 0.0


# ── Retrieval ──────────────────────────────────────────────────────────────

def retrieve_relevant_chunks(
    question: str,
    chunks_with_embeddings: List[Tuple],
    api_key: str,
    top_k: int = RETRIEVAL_TOP_K,
) -> List[Tuple]:
    """Return top_k most relevant chunks via cosine similarity."""
    # get_embeddings returns (list_of_vectors, token_count)
    # We send one question → embeddings list has exactly one entry → [0][0]
    all_embeddings, _ = get_embeddings([question], api_key)
    q_emb = all_embeddings[0]   # the single question vector

    scored = []
    for chunk_id, chunk_text, embedding, doc_name, page_num in chunks_with_embeddings:
        if embedding:
            sim = cosine_similarity(q_emb, embedding)
            scored.append((sim, chunk_id, chunk_text, doc_name, page_num))

    scored.sort(key=lambda x: -x[0])
    return scored[:top_k]


# ── Answer generation ──────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are a security compliance analyst. "
    "Answer using ONLY the numbered sources below. "
    "Add inline citations [Source N] after each claim. "
    "If info is missing say 'Not specified in documents.' "
    "Be concise and professional. "
    "End with exactly: CONFIDENCE: <0-100>"
)


def generate_answer(
    question: str,
    relevant_chunks: List[Tuple],
    api_key: str,
    project_name: str = "",
) -> dict:
    """Generate answer via GPT-4o-mini with trimmed context."""
    import urllib.request

    if not relevant_chunks:
        return {
            'answer': 'No relevant information found in the reference documents.',
            'citations': [], 'confidence': 0.0,
            'usage': {'prompt_tokens': 0, 'completion_tokens': 0},
        }

    context_parts = []
    for i, (score, chunk_id, chunk_text, doc_name, page_num) in enumerate(relevant_chunks):
        page_ref = f", p.{page_num}" if page_num else ""
        snippet  = chunk_text[:CONTEXT_CHARS] + ('…' if len(chunk_text) > CONTEXT_CHARS else '')
        context_parts.append(f"[Source {i+1}: {doc_name}{page_ref}]\n{snippet}")
    context = '\n---\n'.join(context_parts)

    user_prompt = f"Q: {question}\n\nSOURCES:\n{context}"

    payload = json.dumps({
        'model': 'gpt-4o-mini',
        'messages': [
            {'role': 'system', 'content': _SYSTEM_PROMPT},
            {'role': 'user',   'content': user_prompt},
        ],
        'temperature': 0.0,
        'max_tokens':  MAX_ANSWER_TOKENS,
    }).encode()

    req = urllib.request.Request(
        'https://api.openai.com/v1/chat/completions',
        data=payload,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data        = json.loads(resp.read())
            full_answer = data['choices'][0]['message']['content']
            usage       = data.get('usage', {})

            confidence = 0.5
            conf_match = re.search(r'CONFIDENCE:\s*\[?(\d+)\]?', full_answer, re.IGNORECASE)
            if conf_match:
                confidence  = int(conf_match.group(1)) / 100
                full_answer = full_answer[:conf_match.start()].strip()

            citations = []
            for i, (score, chunk_id, chunk_text, doc_name, page_num) in enumerate(relevant_chunks):
                if f'[Source {i+1}]' in full_answer:
                    citations.append({
                        'number':          i + 1,
                        'document':        doc_name,
                        'page':            page_num,
                        'relevance_score': round(score, 3),
                        'excerpt':         chunk_text[:200] + ('…' if len(chunk_text) > 200 else ''),
                    })

            return {
                'answer':     full_answer,
                'citations':  citations,
                'confidence': confidence,
                'usage': {
                    'prompt_tokens':     usage.get('prompt_tokens', 0),
                    'completion_tokens': usage.get('completion_tokens', 0),
                },
            }
    except Exception as e:
        print(f"Generation error: {e}")
        return {
            'answer': f'Error generating answer: {e}',
            'citations': [], 'confidence': 0.0,
            'usage': {'prompt_tokens': 0, 'completion_tokens': 0},
        }


# ── Categorisation ─────────────────────────────────────────────────────────

def categorize_questions(questions: List[str], api_key: str) -> List[str]:
    """One API call to categorise all questions. Very low token usage."""
    import urllib.request

    if not questions or not api_key:
        return ['General'] * len(questions)

    q_lines = '\n'.join(f"{i+1}. {q[:80]}" for i, q in enumerate(questions[:50]))
    payload = json.dumps({
        'model': 'gpt-4o-mini',
        'messages': [{'role': 'user', 'content':
            f"Categorize each into 2-3 words. Return ONLY a JSON array.\n\n{q_lines}"}],
        'temperature': 0.0,
        'max_tokens':  200,
    }).encode()

    req = urllib.request.Request(
        'https://api.openai.com/v1/chat/completions',
        data=payload,
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data    = json.loads(resp.read())
            content = data['choices'][0]['message']['content']
            match   = re.search(r'\[.*\]', content, re.DOTALL)
            if match:
                cats = json.loads(match.group())
                while len(cats) < len(questions):
                    cats.append('General')
                return cats[:len(questions)]
    except Exception as e:
        print(f"Categorization error: {e}")

    return ['General'] * len(questions)