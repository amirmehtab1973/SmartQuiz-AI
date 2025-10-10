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
def extract_text_from_file(file_bytes, filename):
    """Extracts text from PDF, DOCX, or TXT files."""
    name = filename.lower()
    text = ""
    try:
        if name.endswith(".pdf"):
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                pages = [p.extract_text() or "" for p in pdf.pages]
                text = "\n".join(pages)
        elif name.endswith(".docx"):
            document = docx.Document(io.BytesIO(file_bytes))
            text = "\n".join([p.text for p in document.paragraphs])
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
# PARSE EXISTING MCQs (Final)
# ==========================
def parse_mcqs(text):
    """
    Final bulletproof MCQ parser (v6)
    ✅ Handles 1., 1), Q1., i)
    ✅ Works even if 'Ans:' is on the same or next line
    ✅ Ensures all numbered questions are captured
    ✅ Prevents double numbering like '1. 1.'
    ✅ Cleans quiz titles and filler text
    """
    import re

    # Normalize
    text = text.replace("\r", "")
    text = re.sub(r"[*_]+", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    # Remove non-question filler
    ignore_phrases = [
        "compulsory quiz",
        "short quiz",
        "here are some more questions",
        "day",
        "quiz on artificial intelligence"
    ]
    lines = [l for l in lines if not any(p in l.lower() for p in ignore_phrases)]

    # Combine lines and fix broken "Ans:"
    combined = "\n".join(lines)
    combined = re.sub(r"(Ans\s*[:\-]?\s*[A-Da-d])", r"\n\1\n", combined, flags=re.IGNORECASE)

    # 🔹 Split based on numbered question patterns (even if preceded by text)
    q_blocks = re.split(r"(?m)(?=^(?:Q?\s*\d+[\).]\s+))", combined)
    q_blocks = [q.strip() for q in q_blocks if len(q.strip()) > 10]

    mcqs = []
    for block in q_blocks:
        # Detect correct answer (Ans: X)
        ans_match = re.search(r"Ans(?:wer)?\s*[:\-]?\s*([A-Da-d])", block, re.IGNORECASE)
        correct = ans_match.group(1).upper() if ans_match else "A"

        # Remove answer label from text
        block = re.sub(r"Ans(?:wer)?\s*[:\-]?\s*[A-Da-d]", "", block, flags=re.IGNORECASE)

        # Extract question text and options
        parts = re.split(r"(?m)(?=^[A-Da-d][).:\-]\s+)", block)
        if len(parts) > 1:
            q_text = parts[0].strip()
            opts = [re.sub(r"^[A-Da-d][).:\-]\s*", "", p).strip() for p in parts[1:]]
        else:
            q_text = block.strip()
            opts = []

        # Clean double numbering like "1. 1. What is..."
        q_text = re.sub(r"^(?:Q?\s*\d+[\).]\s*){1,2}", "", q_text).strip()

        # Skip lines too short to be valid questions
        if len(q_text.split()) < 3:
            continue

        # Fill missing options up to 4
        while len(opts) < 4:
            opts.append("N/A")
        opts = opts[:4]

        mcqs.append({
            "question": q_text,
            "options": opts,
            "correct": correct
        })

    # 🔹 Deduplicate by question text
    unique = []
    seen = set()
    for q in mcqs:
        qt = q["question"].lower()
        if qt not in seen and not qt.startswith("ans"):
            seen.add(qt)
            unique.append(q)

    return unique

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
# RECORD ATTEMPT (LOCAL JSON)
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
