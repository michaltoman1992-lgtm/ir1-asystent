import os
import json
import re
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from openai import OpenAI
from starlette.middleware.sessions import SessionMiddleware

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "02_dane" / "ir1_fts.sqlite"
JSONL_PATH = BASE_DIR / "02_dane" / "ir1_paragrafy.jsonl"

load_dotenv(BASE_DIR / ".env")

API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
MODEL = os.getenv("OPENAI_MODEL", "gpt-5").strip()

client = OpenAI(api_key=API_KEY)

app = FastAPI(title="IR-1 Asystent")
app.add_middleware(SessionMiddleware, secret_key="tajny_klucz_123_zmien_pozniej")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

PASSWORD = "kolej123"

SYSTEM_RULES = """
Jesteś asystentem instrukcji Ir-1 (prowadzenie ruchu pociągów).

Zasady bezwzględne:
1. Odpowiadasz WYŁĄCZNIE na podstawie dostarczonych fragmentów Ir-1.
2. Nie wolno dopowiadać wiedzy ogólnej ani własnych domysłów.
3. Jeśli brak danych, napisz:
   "Brak podstawy w dostarczonych fragmentach Ir-1."

Styl:
- krótka odpowiedź ogólna
- potem konkretne punkty
- cytuj paragrafy np. [IR1-§15]

Na końcu:
Podstawa: [IR1-§...]
""".strip()


# =========================
# BUDOWA BAZY
# =========================

def build_db_if_missing():
    """
    Jeśli baza SQLite już istnieje -> nic nie robi.
    Jeśli nie istnieje -> buduje ją z pliku ir1_paragrafy.jsonl.
    """
    if DB_PATH.exists():
        print(f"Baza już istnieje: {DB_PATH}")
        return

    if not JSONL_PATH.exists():
        raise RuntimeError(f"Brak pliku JSONL do budowy bazy: {JSONL_PATH}")

    print(f"Buduję bazę FTS z pliku: {JSONL_PATH}")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    cur.execute("DROP TABLE IF EXISTS docs")
    cur.execute("""
        CREATE TABLE docs(
            id TEXT PRIMARY KEY,
            rozdzial TEXT,
            paragraf TEXT,
            tytul TEXT,
            text TEXT
        )
    """)

    cur.execute("DROP TABLE IF EXISTS docs_fts")
    cur.execute("""
        CREATE VIRTUAL TABLE docs_fts USING fts5(
            id, rozdzial, paragraf, tytul, text,
            content='docs', content_rowid='rowid'
        )
    """)

    inserted = 0

    with JSONL_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            r = json.loads(line)
            cur.execute(
                "INSERT INTO docs(id, rozdzial, paragraf, tytul, text) VALUES (?,?,?,?,?)",
                (
                    r["id"],
                    r.get("rozdzial", ""),
                    r.get("paragraf", ""),
                    r.get("tytul", ""),
                    r.get("text", "")
                )
            )
            inserted += 1

    cur.execute("INSERT INTO docs_fts(docs_fts) VALUES('rebuild')")

    con.commit()
    con.close()

    print(f"OK. Zbudowano bazę FTS: {DB_PATH}")
    print(f"Rekordów: {inserted}")


# =========================
# LOGOWANIE
# =========================

def is_logged(request: Request) -> bool:
    return request.session.get("logged") is True


# =========================
# NORMALIZACJA PYTAŃ
# =========================

QUESTION_PREFIX_PATTERNS = [
    r"^czy można\s+",
    r"^czy mozna\s+",
    r"^czy się da\s+",
    r"^czy sie da\s+",
    r"^czy da się\s+",
    r"^czy da sie\s+",
    r"^czy wolno\s+",
    r"^czy trzeba\s+",
    r"^czy należy\s+",
    r"^czy nalezy\s+",
    r"^jak\s+",
    r"^jak się\s+",
    r"^jak sie\s+",
    r"^w jaki sposób\s+",
    r"^w jaki sposob\s+",
    r"^co jeśli\s+",
    r"^co jesli\s+",
    r"^a co jeśli\s+",
    r"^a co jesli\s+",
    r"^kiedy można\s+",
    r"^kiedy mozna\s+",
    r"^kiedy wolno\s+",
    r"^kiedy trzeba\s+",
    r"^kto może\s+",
    r"^kto moze\s+",
    r"^kto odpowiada za\s+",
]

PHRASE_REPLACEMENTS = {
    "jak się": "",
    "jak sie": "",
    "czy można": "",
    "czy mozna": "",
    "czy się da": "",
    "czy sie da": "",
    "czy da się": "",
    "czy da sie": "",
    "czy wolno": "",
    "czy trzeba": "",
    "czy należy": "",
    "czy nalezy": "",
    "co jeśli": "",
    "co jesli": "",
    "a co jeśli": "",
    "a co jesli": "",
    "w jaki sposób": "",
    "w jaki sposob": "",
    "kiedy można": "",
    "kiedy mozna": "",
    "kiedy wolno": "",
    "kiedy trzeba": "",
    "kto może": "",
    "kto moze": "",
    "kto odpowiada za": "odpowiedzialność za",
    "kto prowadzi": "prowadzi",
    "gdzie się wpisuje": "wpis do",
    "gdzie sie wpisuje": "wpis do",
    "na jakich zasadach": "warunki",
    "czy jest możliwość": "",
    "czy jest mozliwosc": "",
}

STOPWORDS = {
    "czy", "sie", "się", "da", "mozna", "można", "jak", "co", "a", "jeśli", "jesli",
    "w", "na", "do", "z", "ze", "od", "pod", "nad", "oraz", "i", "lub", "albo",
    "to", "ten", "ta", "tego", "tej", "te", "jest", "są", "sa", "być", "byc",
    "ma", "mają", "maja", "mieć", "miec", "który", "ktory", "która", "ktora",
    "które", "ktore", "których", "ktorych", "którym", "ktorym", "kiedy", "gdzie",
    "jaki", "jaka", "jakie", "jakiego", "jakiej", "jakim", "wtedy"
}


def normalize_text(text: str) -> str:
    text = text.lower().strip()
    text = text.replace("ł", "ł")  # zostawiamy polskie znaki
    text = re.sub(r"[\"“”'`]", " ", text)
    text = re.sub(r"[^0-9a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ\s\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def strip_question_prefixes(text: str) -> str:
    out = text
    changed = True

    while changed:
        changed = False
        for pattern in QUESTION_PREFIX_PATTERNS:
            new_out = re.sub(pattern, "", out, flags=re.IGNORECASE).strip()
            if new_out != out:
                out = new_out
                changed = True

    return out.strip()


def replace_known_phrases(text: str) -> str:
    out = text
    for old, new in PHRASE_REPLACEMENTS.items():
        out = re.sub(rf"\b{re.escape(old)}\b", new, out, flags=re.IGNORECASE)
    out = re.sub(r"\s+", " ", out).strip()
    return out


def keyword_tokens(text: str):
    tokens = re.findall(r"[a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ0-9\-]+", text.lower())
    result = []

    for token in tokens:
        if len(token) < 3:
            continue
        if token in STOPWORDS:
            continue
        result.append(token)

    # usunięcie duplikatów z zachowaniem kolejności
    unique = []
    seen = set()
    for token in result:
        if token not in seen:
            unique.append(token)
            seen.add(token)

    return unique


def make_match_query_from_tokens(tokens):
    """
    Buduje bezpieczniejsze zapytanie FTS:
    słowa połączone AND, np.:
    prowadzenie AND ruchu AND szlaku
    """
    safe_tokens = []
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        # tylko normalne znaki
        if not re.fullmatch(r"[a-zA-ZąćęłńóśźżĄĆĘŁŃÓŚŹŻ0-9\-]+", token):
            continue
        safe_tokens.append(token)

    if not safe_tokens:
        return ""

    return " AND ".join(safe_tokens)


def build_search_queries(question: str):
    """
    Z jednego pytania robi kilka wariantów:
    1. oczyszczone pytanie
    2. po zamianie znanych fraz
    3. same słowa kluczowe
    4. krótsze rdzenie
    """
    queries = []

    q0 = normalize_text(question)
    if q0:
        queries.append(q0)

    q1 = strip_question_prefixes(q0)
    if q1 and q1 not in queries:
        queries.append(q1)

    q2 = replace_known_phrases(q1 or q0)
    if q2 and q2 not in queries:
        queries.append(q2)

    tokens_q0 = keyword_tokens(q0)
    tokens_q2 = keyword_tokens(q2)

    if tokens_q2:
        q3 = " ".join(tokens_q2)
        if q3 not in queries:
            queries.append(q3)

        if len(tokens_q2) >= 2:
            q4 = " ".join(tokens_q2[:4])
            if q4 not in queries:
                queries.append(q4)

    if tokens_q0 and len(tokens_q0) >= 2:
        q5 = " ".join(tokens_q0[:4])
        if q5 not in queries:
            queries.append(q5)

    # końcowe czyszczenie
    final_queries = []
    seen = set()

    for q in queries:
        q = re.sub(r"\s+", " ", q).strip()
        if len(q) < 2:
            continue
        if q not in seen:
            final_queries.append(q)
            seen.add(q)

    return final_queries


# =========================
# WYSZUKIWANIE FTS
# =========================

def search_fts_raw(match_query: str, limit: int = 5):
    if not match_query:
        return []

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    sql = f"""
    SELECT id, rozdzial, paragraf, tytul, text
    FROM docs_fts
    WHERE docs_fts MATCH ?
    ORDER BY bm25(docs_fts)
    LIMIT {limit}
    """

    try:
        rows = cur.execute(sql, (match_query,)).fetchall()
    except sqlite3.OperationalError:
        rows = []

    con.close()
    return rows


def search_fts_multi(question: str, total_limit: int = 8):
    """
    Szuka kilkoma wariantami pytania.
    Najpierw dokładniejsze wersje, potem bardziej ogólne.
    Scala wyniki i usuwa duplikaty.
    """
    search_variants = build_search_queries(question)

    collected = []
    seen_ids = set()

    for variant in search_variants:
        tokens = keyword_tokens(variant)

        # próba 1: wszystkie tokeny przez AND
        match_query = make_match_query_from_tokens(tokens)
        rows = search_fts_raw(match_query, limit=4)

        # próba 2: jeśli nic nie znalazło, spróbuj krócej
        if not rows and len(tokens) >= 2:
            shorter = make_match_query_from_tokens(tokens[:3])
            rows = search_fts_raw(shorter, limit=4)

        # próba 3: jeśli dalej nic, spróbuj jednym najmocniejszym słowem
        if not rows and len(tokens) >= 1:
            one_word = make_match_query_from_tokens(tokens[:1])
            rows = search_fts_raw(one_word, limit=3)

        for row in rows:
            row_id = row[0]
            if row_id not in seen_ids:
                collected.append(row)
                seen_ids.add(row_id)

            if len(collected) >= total_limit:
                return collected, search_variants

    return collected, search_variants


# =========================
# PAKIET DO OPENAI
# =========================

def build_pack(rows):
    pack = []
    for (id_, rozdz, par, tyt, text) in rows:
        short_text = text[:2500]
        pack.append(f"[{id_}] {rozdz} {par} {tyt}\n{short_text}\n")
    return "\n\n".join(pack)


def ask_openai(question: str, pack: str):
    prompt = f"""
{SYSTEM_RULES}

Pytanie:
{question}

Fragmenty:
{pack}
""".strip()

    resp = client.responses.create(
        model=MODEL,
        input=prompt,
    )

    return (resp.output_text or "").strip()


# Budowa bazy przy starcie aplikacji, jeśli jej nie ma
build_db_if_missing()


# =========================
# ROUTES
# =========================

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "error": ""
        }
    )


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, password: str = Form(...)):
    if password == PASSWORD:
        request.session["logged"] = True
        return RedirectResponse(url="/", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "error": "Złe hasło"
        }
    )


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if not is_logged(request):
        return RedirectResponse(url="/login", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "question": "",
            "answer": "",
            "used_rows": [],
            "error": ""
        }
    )


@app.post("/ask", response_class=HTMLResponse)
def ask(request: Request, question: str = Form(...)):
    if not is_logged(request):
        return RedirectResponse(url="/login", status_code=303)

    question = question.strip()

    if not question:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "question": "",
                "answer": "",
                "used_rows": [],
                "error": "Wpisz pytanie"
            }
        )

    if not API_KEY:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "question": question,
                "answer": "",
                "used_rows": [],
                "error": "Brak OPENAI_API_KEY"
            }
        )

    if not DB_PATH.exists():
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "question": question,
                "answer": "",
                "used_rows": [],
                "error": f"Brak bazy FTS: {DB_PATH}"
            }
        )

    rows, search_variants = search_fts_multi(question, total_limit=8)

    if not rows:
        variants_text = ", ".join(search_variants[:5]) if search_variants else "brak"
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "question": question,
                "answer": f"Brak trafień w bazie.\n\nPróbowano wariantów: {variants_text}",
                "used_rows": [],
                "error": ""
            }
        )

    pack = build_pack(rows)

    try:
        answer = ask_openai(question, pack)
    except Exception as e:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "question": question,
                "answer": "",
                "used_rows": [],
                "error": f"Błąd OpenAI: {e}"
            }
        )

    used_rows = [
        {
            "id": row[0],
            "rozdzial": row[1],
            "paragraf": row[2],
            "tytul": row[3],
        }
        for row in rows
    ]

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "question": question,
            "answer": answer,
            "used_rows": used_rows,
            "error": ""
        }
    )