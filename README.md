# DocuRAG — AI-Powered Questionnaire Answering SaaS

## Assignment Context

For this implementation I created a fictional company and dataset to simulate a real questionnaire workflow.

Industry: SaaS Security & Compliance

Company:
SecureLayer Systems is a fictional SaaS company that provides cloud-based infrastructure monitoring and automated compliance tools for enterprises. Their platform helps organizations monitor system health, manage audit logs, and meet regulatory requirements.

To simulate a realistic scenario I created (Testing_DOCS):

A questionnaire containing 30 questions.

1 internal reference documents describing policies, infrastructure, and compliance practices.

These documents act as the source of truth used by the system to generate answers.
A production-ready Django SaaS that uses RAG (Retrieval-Augmented Generation) to automatically answer questionnaires using your reference documents — with full citations.

## Features

- **Smart Question Extraction** — Upload PDF/DOCX/TXT questionnaires; questions are auto-detected by regex + AI
- **Auto-Categorization** — GPT-4o-mini clusters questions into categories for organized review
- **Semantic RAG Engine** — OpenAI `text-embedding-3-small` + cosine similarity for precise chunk retrieval
- **Cited Answers** — Every answer includes inline [Source N] citations with document + page references
- **Confidence Scoring** — Each answer is rated 0-100% based on source relevance
- **Live Progress** — Real-time polling shows generation progress without page refresh
- **Inline Editor** — Edit answers directly in the browser; changes auto-save via AJAX
- **Export** — Download final answers as DOCX (formatted report) or JSON
- **Multi-tenant** — Each user has their own projects, docs, and API key

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set your OpenAI API key (or add it in your profile after login)
export OPENAI_API_KEY=sk-...

# 3. Run migrations
python manage.py migrate

# 4. Start server
python manage.py runserver

# 5. Visit http://localhost:8000
# Demo credentials: demo / demo1234
```

## Architecture

```
User uploads questionnaire.pdf
         ↓
rag_engine.extract_questions_from_file()
  → Regex patterns detect numbered/question-mark lines
  → AI categorizes questions into topic groups
         ↓
User uploads reference_docs/
         ↓ (background thread)
rag_engine.extract_text_from_pdf/docx/txt()
  → Chunked into 600-word overlapping segments
  → Each chunk embedded via text-embedding-3-small
  → Stored in DocumentChunk table
         ↓
generate_answers_task() (background thread)
  → For each question:
    1. Embed the question
    2. Cosine similarity against all document chunks
    3. Top-5 chunks passed to GPT-4o-mini as context
    4. Answer generated with [Source N] citations
    5. Confidence score extracted
         ↓
Review UI
  → Inline contenteditable editor
  → Citation tooltips with source excerpts
  → AJAX auto-save
         ↓
Export DOCX / JSON
```

## Project Structure

```
RAG_Document_Retrieval/
├── base/          # Auth, UserProfile, API key storage
├── questionnaire/
│   ├── models.py      # Project, ReferenceDocument, DocumentChunk, Question, Answer
│   ├── rag_engine.py  # Core AI: extraction, chunking, embeddings, retrieval, generation
│   ├── views.py       # All HTTP handlers + background tasks
│   └── exporter.py    # DOCX + JSON export
├── templates/
│   ├── base.html
│   ├── base/      # login, register, profile
│   └── questionnaire/ # dashboard, upload, generate, review
└── requirements.txt
└── Testing_DOCS  # Sample Documents
       ├── NexaTech_Security_Policy.pdf
       ├── Vendor_Security_Questionnaire.pdf
```

## Innovative Features

1. **Background Processing** — Reference doc embedding runs in threads; UI polls asynchronously
2. **Overlapping Chunks** — 100-word overlap between 600-word chunks prevents answer fragmentation
3. **Citation Tooltips** — Hover over source chips to see the exact excerpt that was used
4. **Confidence Coloring** — Green/amber/red badges show answer reliability at a glance
5. **Auto-Categorization** — AI groups questions by topic so reviewers can work section by section
6. **Edit + Track** — Edited answers are flagged separately; original AI output is preserved

## Customization

- Change chunk size in `rag_engine.chunk_text(chunk_size=600, overlap=100)`
- Swap `gpt-4o-mini` for `gpt-4o` in `generate_answer()` for higher quality
- Adjust `top_k=5` in `retrieve_relevant_chunks()` to pass more/fewer context chunks
- Add vector DB (Pinecone/Chroma) to replace SQLite embedding storage for scale

## About Me

Hi, I’m Shauryaman Sawai, an engineering student at NIT Rourkela with experience in full-stack development, data analytics, and applied AI Systems and AI Automations.

I enjoy building practical systems where machine learning and software engineering solve real workflow problems. This project reflects my interest in building AI tools that are structured, reliable, and usable in real operational settings.
