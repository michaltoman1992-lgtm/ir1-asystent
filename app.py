import os
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


def is_logged(request: Request) -> bool:
    return request.session.get("logged") is True


def search_fts(question: str, limit: int = 5):
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
        rows = cur.execute(sql, (question,)).fetchall()
    except sqlite3.OperationalError:
        rows = []

    con.close()
    return rows


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

    rows = search_fts(question, limit=5)

    if not rows:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "question": question,
                "answer": "Brak trafień w bazie",
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