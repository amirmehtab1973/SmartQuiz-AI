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
    mcq_patterns = [r"Q\s*\d+", r"Question\s*\d+", r"[A-D][).]", r"Answer\s*[:\-]"]
    return any(re.search(p, text, re.IGNORECASE) for p in mcq_patterns)


# ==========================
# PARSE EXISTING MCQs
# ==========================
def parse_mcqs(text):
    """Ultra-flexible parser: handles *1., normal sentences, and interleaved text."""
    text = re.sub(r'\r', '', text)
    text = re.sub(r'\*+', '', text)  # remove asterisks
    lines = [l.strip() for l in text.split('\n') if l.strip()]

    blocks = []
    current = []
    for line in lines:
        # A new question starts if line begins with 1., *1., Q1., etc.
        if re.match(r"^(?:\*?\s*)?(?:Q?\s*\d+[\).])", line, re.IGNORECASE):
            if current:
                blocks.append(" ".join(current))
                current = []
            current.append(line)
        else:
            current.append(line)
    if current:
        blocks.append(" ".join(current))

    parsed = []
    for block in blocks:
        # Extract question
        q_match = re.match(r"^(?:\*?\s*)?(?:Q?\s*\d+[\).]?\s*)(.*?)(?=\s+[A-Da-d][).:])", block)
        question_text = q_match.group(1).strip() if q_match else block

        # Extract options
        opts = re.findall(r"[A-Da-d][).:\-]\s*([^A-Da-d]+?)(?=\s+[A-Da-d][).:\-]|$)", block)
        opts = [o.strip() for o in opts if o.strip()]

        # Detect correct answer
        ans_match = re.search(r"(?:Answer|Ans)\s*[:\-]?\s*([A-Da-d])", block, re.IGNORECASE)
        correct = ans_match.group(1).upper() if ans_match else "A"

        if len(opts) >= 2:
            while len(opts) < 4:
                opts.append("N/A")
            parsed.append({
                "question": question_text,
                "options": opts[:4],
                "correct": correct
            })

    return parsed


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

        # Debug print to check what OpenAI returned
        print("DEBUG: OpenAI response content:")
        print(content)

        start = content.find("[")
        if start != -1:
            content = content[start:]

        questions = json.loads(content)
        for q in questions:
            if "options" not in q or len(q["options"]) < 4:
                q["options"] = q.get("options", ["A", "B", "C", "D"])[:4]
        return questions

    except Exception as e:
        print(f"[ERROR] generate_mcqs_via_openai: {e}")
        return []


# ==========================
# SEND EMAIL RESULTS
# ==========================
def send_result_email(to_email, student_name, quiz_title, score, total):
    """Sends quiz results to student via Gmail SMTP."""
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
# RECORD ATTEMPT (LOCAL JSON FALLBACK)
# ==========================
LOCAL_RESULTS_FILE = "results.json"

def record_attempt(quiz_id, quiz_title, student_name, student_email, answers, score, total):
    """Saves student attempt locally (JSON fallback)."""
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
    """Returns list of saved attempts."""
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
    """Exports student results to an Excel file (bytes)."""
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
