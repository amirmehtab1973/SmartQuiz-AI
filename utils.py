# (Paste the full utils.py content here)
# utils.py
import re, io, os, json
import pdfplumber, docx
from datetime import datetime
from email.mime.text import MIMEText
import smtplib
import pandas as pd

# Optional imports
try:
    import openai
except Exception:
    openai = None

# Environment
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or ""
MONGO_URI = os.getenv("MONGO_URI") or ""
EMAIL_USER = os.getenv("EMAIL_USER") or ""
EMAIL_PASS = os.getenv("EMAIL_PASS") or ""

if OPENAI_API_KEY and openai:
    openai.api_key = OPENAI_API_KEY

# In-memory fallback DB (for Colab/test without Mongo)
IN_MEM_DB = {"quizzes": [], "attempts": []}

# ----- Text extraction -----
def extract_text_from_file(file_bytes, filename):
    name = filename.lower()
    # PDF
    if name.endswith(".pdf"):
        try:
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                pages = [p.extract_text() or "" for p in pdf.pages]
                return "\n".join(pages)
        except Exception:
            pass
    # DOCX
    if name.endswith(".docx") or name.endswith(".doc"):
        try:
            doc = docx.Document(io.BytesIO(file_bytes))
            return "\n".join([p.text for p in doc.paragraphs])
        except Exception:
            pass
    # fallback to utf-8
    try:
        return file_bytes.decode("utf-8")
    except Exception:
        return ""

# ----- Detect MCQ format -----
def detect_mcq(text):
    if not text or len(text.strip()) < 50:
        return False
    sample = text[:8000]
    indicators = [r"\bQ\s*\d", r"Question\s*\d", r"\n\s*[A-D]\s*[).\-]", r"Answer\s*[:\-]"]
    for pat in indicators:
        if re.search(pat, sample, re.IGNORECASE):
            return True
    return False

# ----- Parse existing MCQs (heuristic) -----
def parse_mcqs(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    questions = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # question detection
        if re.match(r'^(?:Q\.?\s*\d+|Question\s*\d+)|\?$', line, re.IGNORECASE) or line.endswith('?'):
            q = re.sub(r'^(?:Q\.?\s*\d+[:.)]?\s*|Question\s*\d+[:.)]?\s*)', '', line, flags=re.IGNORECASE).strip()
            i += 1
            opts = []
            while i < len(lines) and len(opts) < 6:
                m = re.match(r'^([A-Da-d])[\.\)\-:]\s*(.*)$', lines[i])
                if m:
                    opts.append(m.group(2).strip()); i += 1; continue
                m2 = re.match(r'^\(\s*([A-Da-d])\s*\)\s*(.*)$', lines[i])
                if m2:
                    opts.append(m2.group(2).strip()); i += 1; continue
                if re.match(r'^(?:Q\.?\s*\d+|Question\s*\d+)\b', lines[i], re.IGNORECASE):
                    break
                i += 1
            correct = None
            if i < len(lines) and re.match(r'^(Answer|Ans|Key)\b', lines[i], re.IGNORECASE):
                mm = re.search(r'([A-Da-d])', lines[i])
                if mm: correct = mm.group(1).upper()
                i += 1
            if opts:
                while len(opts) < 4: opts.append("N/A")
                questions.append({"question": q, "options": opts[:4], "correct": correct or "A"})
        else:
            i += 1
    return questions

# ----- Generate MCQs via OpenAI -----
def generate_mcqs_via_openai(text, n_questions=8):
    if not OPENAI_API_KEY or openai is None:
        return []
    prompt = f"""You are an assistant that creates multiple-choice questions.
From the following text, create {n_questions} questions in JSON array form:
[{{"question":"...","options":["...","...","...","..."], "correct":"A"}}]
Text:
\"\"\"{text}\"\"\""""
    try:
        resp = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt}],
            temperature=0.0,
            max_tokens=1200
        )
        raw = resp.choices[0].message.content
        jstart = raw.find('[')
        raw_json = raw[jstart:] if jstart>=0 else raw
        parsed = json.loads(raw_json)
        out = []
        for q in parsed:
            out.append({
                "question": q.get("question",""),
                "options": (q.get("options") or ["N/A"]*4)[:4],
                "correct": (q.get("correct") or "A").upper()
            })
        return out
    except Exception as e:
        print("OpenAI error:", e)
        return []

# ----- Email -----
def send_result_email(to_email, student_name, quiz_title, score, total):
    if not EMAIL_USER or not EMAIL_PASS:
        print("Email creds not configured.")
        return False
    try:
        subject = f"SmartQuiz AI Result — {quiz_title}"
        percent = round((score/total)*100,2) if total else 0
        body = f"Hello {student_name or ''},\n\nYou completed the quiz: {quiz_title}\nYour Score: {score}/{total}\nPercentage: {percent}%\n\nRegards,\nSmartQuiz AI"
        msg = MIMEText(body)
        msg["Subject"] = subject; msg["From"] = EMAIL_USER; msg["To"] = to_email
        server = smtplib.SMTP("smtp.gmail.com", 587, timeout=10)
        server.ehlo(); server.starttls()
        server.login(EMAIL_USER, EMAIL_PASS)
        server.sendmail(EMAIL_USER, [to_email], msg.as_string()); server.quit()
        return True
    except Exception as e:
        print("Email error:", e); return False

# ----- DB helpers: in-memory fallback -----
def _new_id(prefix="local"):
    return f"{prefix}-{int(datetime.utcnow().timestamp()*1000)}"

def save_quiz_to_db(quiz_obj):
    # if MONGO_URI present, user can expand to store in Mongo
    if not MONGO_URI:
        quiz = dict(quiz_obj)
        quiz["_id"] = _new_id("quiz")
        IN_MEM_DB["quizzes"].append(quiz)
        return quiz
    # placeholder for Mongo logic (left minimal for Colab flow)
    try:
        from pymongo import MongoClient
        client = MongoClient(MONGO_URI)
        db = client.get_default_database() or client["smartquiz"]
        res = db.quizzes.insert_one(quiz_obj)
        saved = db.quizzes.find_one({"_id": res.inserted_id})
        saved["_id"] = str(saved["_id"])
        return saved
    except Exception as e:
        print("Mongo save error:", e)
        quiz = dict(quiz_obj); quiz["_id"]=_new_id("quiz"); IN_MEM_DB["quizzes"].append(quiz); return quiz

def list_quizzes_from_db():
    if not MONGO_URI:
        return IN_MEM_DB["quizzes"]
    try:
        from pymongo import MongoClient
        client = MongoClient(MONGO_URI)
        db = client.get_default_database() or client["smartquiz"]
        out = list(db.quizzes.find({}, {"questions":0}))
        for q in out: q["_id"] = str(q["_id"])
        return out
    except Exception as e:
        print("Mongo list error:", e); return IN_MEM_DB["quizzes"]

def get_quiz_by_id(qid):
    if not MONGO_URI:
        for q in IN_MEM_DB["quizzes"]:
            if str(q.get("_id")) == str(qid): return q
        return None
    try:
        from pymongo import MongoClient
        from bson.objectid import ObjectId
        client = MongoClient(MONGO_URI)
        db = client.get_default_database() or client["smartquiz"]
        q = db.quizzes.find_one({"_id": ObjectId(qid)})
        if q: q["_id"]=str(q["_id"])
        return q
    except Exception as e:
        print("Mongo get error:", e); return None

def record_attempt(quiz_id, quiz_title, student_name, student_email, answers, score, total):
    attempt = {
        "_id": _new_id("att"),
        "quiz_id": quiz_id, "quiz_title": quiz_title,
        "student_name": student_name, "student_email": student_email,
        "answers": answers, "score": score, "total": total,
        "timestamp": datetime.utcnow()
    }
    if not MONGO_URI:
        IN_MEM_DB["attempts"].append(attempt); return attempt
    try:
        from pymongo import MongoClient
        client = MongoClient(MONGO_URI)
        db = client.get_default_database() or client["smartquiz"]
        res = db.attempts.insert_one(attempt)
        a = db.attempts.find_one({"_id": res.inserted_id}); a["_id"]=str(a["_id"]); return a
    except Exception as e:
        print("Mongo record error:", e); IN_MEM_DB["attempts"].append(attempt); return attempt

def list_attempts():
    if not MONGO_URI:
        return IN_MEM_DB["attempts"]
    try:
        from pymongo import MongoClient
        client = MongoClient(MONGO_URI)
        db = client.get_default_database() or client["smartquiz"]
        out = list(db.attempts.find().sort("timestamp", -1))
        for a in out: a["_id"]=str(a["_id"])
        return out
    except Exception as e:
        print("Mongo attempts error:", e); return IN_MEM_DB["attempts"]

def export_results_to_excel_bytes(results, filetype="xlsx"):
    df = pd.DataFrame(results)
    buf = io.BytesIO()
    if filetype=="xlsx":
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False)
    else:
        df.to_csv(buf, index=False)
    buf.seek(0); return buf
