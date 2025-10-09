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
MONGODB_URI = os.getenv("MONGODB_URI", "")

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
            # OCR fallback if text too short
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
    """Detect if document text is already in MCQ format."""
    if not text or len(text.strip()) < 50:
        return False
    mcq_patterns = [r"Q\s*\d+", r"Question\s*\d+", r"\d+\)", r"\d+\.", r"[A-D][).]", r"Answer\s*[:\-]"]
    return any(re.search(p, text, re.IGNORECASE) for p in mcq_patterns)


# ==========================
# PARSE EXISTING MCQs
# ==========================
def parse_mcqs(text):
    """Parses text to extract MCQs in multiple numbering formats."""
    text = text.replace("\r", "\n")
    text = re.sub(r"\*+", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    mcqs = []
    q = None
    options = []
    answer_raw = None

    question_pattern = re.compile(r"^(?:Q?\s*\d+[\).]|[ivx]+\)|[ivx]+\.)\s*(.*)", re.IGNORECASE)
    option_pattern = re.compile(r"^[A-Da-d][\).:\-]?\s*(.*)")
    answer_pattern = re.compile(r"^(?:Ans|Answer|Key)[:\-]?\s*(.*)", re.IGNORECASE)

    for line in lines:
        qm = question_pattern.match(line)
        om = option_pattern.match(line)
        am = answer_pattern.match(line)

        if qm:
            if q and options:
                mcqs.append(_finalize_mcq(q, options, answer_raw))
            q = qm.group(1).strip()
            options = []
            answer_raw = None
        elif om:
            options.append(om.group(1).strip())
        elif am:
            answer_raw = am.group(1).strip()
        else:
            if options:
                options[-1] = (options[-1] + " " + line.strip()).strip()
            elif q:
                q = (q + " " + line.strip()).strip()

    if q and options:
        mcqs.append(_finalize_mcq(q, options, answer_raw))

    return mcqs


def _finalize_mcq(q, options, answer_raw):
    """Cleans and formats MCQ block."""
    options = [re.sub(r'\s+', ' ', o).strip() for o in options]
    while len(options) < 4:
        options.append("N/A")

    correct_letter = "A"
    correct_index = 0
    if answer_raw:
        m = re.search(r'([A-Da-d])', answer_raw)
        if m:
            correct_letter = m.group(1).upper()
            correct_index = ord(correct_letter) - 65
        else:
            for i, opt in enumerate(options):
                if answer_raw.lower() in opt.lower() or opt.lower() in answer_raw.lower():
                    correct_index = i
                    correct_letter = chr(65 + i)
                    break
    correct_index = max(0, min(correct_index, 3))
    return {
        "question": q.strip(),
        "options": options[:4],
        "correct": correct_letter,
        "correct_index": correct_index
    }


# ==========================
# GENERATE MCQs USING OPENAI
# ==========================
def generate_mcqs_via_openai(text, n_questions=8):
    """Uses OpenAI API to generate MCQs from text."""
    if not OPENAI_API_KEY:
        print("⚠️ No OpenAI API key found.")
        return []

    prompt = f"""
    You are an AI quiz generator.
    Create {n_questions} multiple-choice questions based on the following content.
    Each question should have 4 options (A, B, C, D) and the correct answer.
    Return JSON in this exact format:
    [
      {{
        "question": "...",
        "options": ["...", "...", "...", "..."],
        "correct": "A"
      }}
    ]
    Text:
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

        questions = json.loads(content)
        normalized = []
        for q in questions:
            opts = [re.sub(r'\s+', ' ', (o or "")).strip() for o in q.get("options", [])]
            while len(opts) < 4:
                opts.append("N/A")
            corr = q.get("correct", "A")
            m = re.search(r'([A-Da-d])', str(corr))
            if m:
                corr_letter = m.group(1).upper()
                idx = ord(corr_letter) - 65
            else:
                corr_letter = "A"
                idx = 0
            normalized.append({
                "question": q.get("question", "").strip(),
                "options": opts[:4],
                "correct": corr_letter,
                "correct_index": idx
            })
        return normalized

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
# RECORD ATTEMPT
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
