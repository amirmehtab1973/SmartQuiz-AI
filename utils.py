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
    """
    Final stable MCQ parser – correctly extracts 1), 1., Q1, or *1 formats.
    Ensures correct answer alignment and skips stray option lines.
    """

    import re

    # Clean text
    text = text.replace("\r", "")
    text = re.sub(r"[*_]+", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    # Remove common noise
    ignore_lines = [
        "compulsory quiz", "here are some more questions", "day", "short quiz"
    ]
    lines = [l for l in lines if not any(p in l.lower() for p in ignore_lines)]

    # Join split lines correctly
    joined = " ".join(lines)

    # Split into question blocks using patterns like 1.  1)  Q1.  i)
    question_splits = re.split(r"(?:(?:^|\s)(?:Q?\s*\d+[\).]|[ivxlcdm]+\)))", joined)
    # the first split may be junk before first question
    question_splits = [q.strip() for q in question_splits if len(q.strip()) > 10]

    mcqs = []
    for qblock in question_splits:
        # Extract answer first
        ans_match = re.search(r"Ans(?:wer)?\s*[:\-]?\s*([A-Da-d])", qblock, re.IGNORECASE)
        correct = ans_match.group(1).upper() if ans_match else "A"
        qblock = re.sub(r"Ans(?:wer)?\s*[:\-]?\s*[A-Da-d]", "", qblock, flags=re.IGNORECASE)

        # Find all options (A-D)
        options = re.findall(r"(?:^|\s)([A-Da-d][).:\-]\s*[^A-Da-d]+)", qblock)
        options = [re.sub(r"^[A-Da-d][).:\-]\s*", "", o).strip() for o in options if o.strip()]

        # Extract question (text before first A)/B))
        qtext_match = re.split(r"[A-Da-d][).:\-]\s*", qblock, maxsplit=1)
        q_text = qtext_match[0].strip() if qtext_match else qblock

        # Remove filler lines
        if len(q_text.split()) < 3 or "quiz" in q_text.lower():
            continue

        # Normalize options
        while len(options) < 4:
            options.append("N/A")
        options = options[:4]

        mcqs.append({
            "question": q_text,
            "options": options,
            "correct": correct
        })

    # Remove duplicates by question text
    unique_mcqs = []
    seen = set()
    for q in mcqs:
        q_clean = q["question"].lower()
        if q_clean not in seen:
            seen.add(q_clean)
            unique_mcqs.append(q)

    return unique_mcqs


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
