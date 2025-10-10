import os
import io
import re
import json
import pdfplumber
import docx
import openai
import pandas as pd
import smtplib
from datetime import datetime
from email.mime.text import MIMEText

# ==========================
# CONFIG / SECRETS
# ==========================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")

if OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY

# ==========================
# TEXT EXTRACTION
# ==========================
try:
    import pytesseract
    from PIL import Image
    import pdf2image
except ImportError:
    pytesseract = None


def extract_text_from_file(file_bytes, filename):
    name = filename.lower()
    text = ""
    try:
        if name.endswith(".pdf"):
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                pages = [p.extract_text() or "" for p in pdf.pages]
                text = "\n".join(pages)
            if len(text.strip()) < 300 and pytesseract:
                images = pdf2image.convert_from_bytes(file_bytes)
                ocr_text = "\n".join(pytesseract.image_to_string(img) for img in images)
                text += "\n" + ocr_text
        elif name.endswith(".docx"):
            document = docx.Document(io.BytesIO(file_bytes))
            text = "\n".join(p.text for p in document.paragraphs)
        else:
            text = file_bytes.decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[ERROR] extract_text_from_file: {e}")
    return text


# ==========================
# DETECT MCQ FORMAT
# ==========================
def detect_mcq(text):
    if not text or len(text.strip()) < 50:
        return False
    patterns = [r"Q\s*\d+", r"\d+\)", r"\d+\.", r"[A-D][).]", r"Ans[:\- ]"]
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


# ==========================
# PARSE MCQs (IMPROVED)
# ==========================
def parse_mcqs(text):
    """Parses MCQs from text in mixed formats (PDF/DOCX/TXT)."""
    text = re.sub(r"[*_]+", "", text)
    text = text.replace("\r", "").strip()

    # Remove extra blank lines and headers like "Here are some more questions..."
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    lines = [l for l in lines if not re.search(r"Here are some more|Compulsory Quiz", l, re.I)]

    blocks = []
    current = []
    for line in lines:
        if re.match(r"^(?:Q?\s*\d+[\).]|[ivxlcdm]+\))", line.strip(), re.IGNORECASE):
            if current:
                blocks.append(" ".join(current))
                current = []
            current.append(line)
        else:
            current.append(line)
    if current:
        blocks.append(" ".join(current))

    mcqs = []
    for block in blocks:
        # Extract question text (everything before first A)/B)/C)/D) )
        q_match = re.match(r"^(?:Q?\s*\d+[\).]?\s*)(.*?)(?=\s+[A-Da-d][).:])", block)
        q_text = q_match.group(1).strip() if q_match else block

        # Find options
        options = re.findall(r"[A-Da-d][).:\-]\s*([^A-Da-d]+)", block)
        options = [o.strip() for o in options if o.strip()]
        if len(options) < 4:
            while len(options) < 4:
                options.append("N/A")

        # Detect correct answer (Ans: X)
        ans_match = re.search(r"Ans(?:wer)?\s*[:\-]?\s*([A-Da-d])", block, re.IGNORECASE)
        correct = ans_match.group(1).upper() if ans_match else "A"

        # Clean up any garbage like “Here are some…” after answers
        q_text = re.sub(r"Ans\s*[:\-]?\s*[A-Da-d].*", "", q_text, flags=re.IGNORECASE)

        if len(q_text.split()) < 3:
            continue  # skip non-question junk lines

        mcqs.append({
            "question": q_text.strip(),
            "options": options[:4],
            "correct": correct
        })

    # remove duplicates and misreads
    final_mcqs = []
    seen = set()
    for q in mcqs:
        qt = q["question"].lower()
        if qt not in seen and not qt.startswith("compulsory quiz"):
            final_mcqs.append(q)
            seen.add(qt)

    return final_mcqs


# ==========================
# GENERATE MCQs USING OPENAI
# ==========================
def generate_mcqs_via_openai(text, n_questions=8):
    if not OPENAI_API_KEY:
        print("⚠️ No OpenAI API key found.")
        return []

    prompt = f"""
    You are an AI quiz generator.
    Create {n_questions} multiple-choice questions (MCQs) from this content.
    Each question must have 4 options (A, B, C, D) and the correct answer.
    Return pure JSON:
    [
      {{
        "question": "...",
        "options": ["...", "...", "...", "..."],
        "correct": "A"
      }}
    ]
    Content:
    {text}
    """

    try:
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1200,
            temperature=0.5,
        )
        content = response.choices[0].message.content
        start = content.find("[")
        if start != -1:
            content = content[start:]
        return json.loads(content)
    except Exception as e:
        print(f"[ERROR] generate_mcqs_via_openai: {e}")
        return []


# ==========================
# SEND EMAIL RESULTS
# ==========================
def send_result_email(to_email, student_name, quiz_title, score, total):
    if not EMAIL_USER or not EMAIL_PASS:
        print("⚠️ Email credentials missing.")
        return False

    try:
        subject = f"SmartQuiz AI Results – {quiz_title}"
        percent = round((score / total) * 100, 2)
        body = (
            f"Hello {student_name},\n\n"
            f"Thank you for completing the quiz: {quiz_title}\n"
            f"Your Score: {score}/{total} ({percent}%)\n\n"
            "Keep learning!\nSmartQuiz AI"
        )
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = EMAIL_USER
        msg["To"] = to_email

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL_USER, EMAIL_PASS)
            server.send_message(msg)
        print(f"✅ Email sent to {to_email}")
        return True
    except Exception as e:
        print(f"[ERROR] send_result_email: {e}")
        return False


# ==========================
# RECORD ATTEMPTS
# ==========================
LOCAL_RESULTS_FILE = "results.json"


def record_attempt(quiz_id, quiz_title, student_name, student_email, answers, score, total):
    attempt = {
        "quiz_id": quiz_id,
        "quiz_title": quiz_title,
        "student_name": student_name,
        "student_email": student_email,
        "answers": answers,
        "score": score,
        "total": total,
        "timestamp": datetime.utcnow().isoformat(),
    }
    try:
        existing = []
        if os.path.exists(LOCAL_RESULTS_FILE):
            with open(LOCAL_RESULTS_FILE, "r", encoding="utf-8") as f:
                existing = json.load(f)
        existing.append(attempt)
        with open(LOCAL_RESULTS_FILE, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
        return attempt
    except Exception as e:
        print(f"[ERROR] record_attempt: {e}")
        return attempt


def list_attempts():
    if not os.path.exists(LOCAL_RESULTS_FILE):
        return []
    try:
        with open(LOCAL_RESULTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []


# ==========================
# EXPORT TO EXCEL
# ==========================
def export_results_to_excel_bytes(data):
    try:
        df = pd.DataFrame(data)
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            df.to_excel(writer, index=False)
        buf.seek(0)
        return buf
    except Exception as e:
        print(f"[ERROR] export_results_to_excel_bytes: {e}")
        return None
