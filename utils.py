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
    """Extract text from PDF, DOCX, or TXT files."""
    name = filename.lower()
    text = ""
    try:
        if name.endswith(".pdf"):
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                pages = [p.extract_text() or "" for p in pdf.pages]
                text = "\n".join(pages)
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
    mcq_patterns = [r"Q\s*\d+", r"\d+\.", r"[A-D][).]", r"Answer\s*[:\-]"]
    return any(re.search(p, text, re.IGNORECASE) for p in mcq_patterns)

# ==========================
# PARSE EXISTING MCQs (robust)
# ==========================
def parse_mcqs(text):
    """
    Final robust MCQ parser.
    Handles numbered questions (1., 1), Q1), i)), ignores titles/instructions,
    extracts correct answers accurately (including text matches like 'Ans: D').
    """

    import re

    # Clean and normalize text
    text = (text or "").replace("\r", "\n")
    text = re.sub(r"\*+", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    blocks, cur_block = [], []

    def is_question_start(line: str) -> bool:
        """
        Detect start of a real question — must be numbered (1., Q1, etc.)
        and either end with '?' or contain typical question words.
        """
        if re.match(r"^(?:Q?\s*\d+[\).]|\d+\.)\s*", line, re.IGNORECASE):
            if re.match(r"^(?:Q?\s*\d+[\).]?\s*[A-D][).])", line, re.IGNORECASE):
                return False
            if "?" in line or re.search(r"\b(what|which|who|when|where|why|how)\b", line, re.IGNORECASE):
                return True
        return False

    for line in lines:
        if is_question_start(line):
            if cur_block:
                blocks.append("\n".join(cur_block))
                cur_block = []
            cur_block.append(line)
        else:
            cur_block.append(line)
    if cur_block:
        blocks.append("\n".join(cur_block))

    parsed = []
    for block in blocks:
        if not block.strip():
            continue

        # Extract question text
        q_match = re.match(r"^(?:Q?\s*\d+[\).]?\s*)(.*?)(?=\s+[A-Da-d][).:])", block)
        q_text = q_match.group(1).strip() if q_match else block.strip()

        # Skip non-question heading-type lines (no '?', no typical question words)
        if not ("?" in q_text or re.search(r"\b(what|which|who|when|where|why|how)\b", q_text, re.IGNORECASE)):
            continue

        # Extract options (A–D)
        options = []
        opt_re = re.compile(r"^[A-Da-d][).:\-]\s*(.+)$", re.MULTILINE)
        for m in opt_re.finditer(block):
            opt = re.sub(r"\s+", " ", m.group(1).strip())
            options.append(opt)
        if not options:
            options = ["N/A", "N/A", "N/A", "N/A"]
        while len(options) < 4:
            options.append("N/A")
        options = options[:4]

        # Detect correct answer letter or text
        ans_match = re.search(r"(?:Answer|Ans|Key)\s*[:\-]?\s*([A-Da-d])\b", block, re.IGNORECASE)
        correct = ans_match.group(1).upper() if ans_match else None

        # Textual fallback (if "Ans: All of the above")
        if not correct:
            ans_text_match = re.search(r"(?:Answer|Ans|Key)\s*[:\-]?\s*(.+)", block, re.IGNORECASE)
            if ans_text_match:
                ans_text = ans_text_match.group(1).strip()
                for i, o in enumerate(options):
                    if ans_text.lower() in o.lower() or o.lower() in ans_text.lower():
                        correct = chr(65 + i)
                        break

        # Heuristic: if "All of the above" exists, set that as correct if not found
        if not correct:
            for i, o in enumerate(options):
                if "all of the above" in o.lower():
                    correct = chr(65 + i)
                    break

        # Default fallback
        correct = correct or "A"

        parsed.append({
            "question": q_text,
            "options": options,
            "correct": correct
        })

    # De-duplicate by question text (avoid duplicates from page headers or OCR)
    unique = []
    seen = set()
    for q in parsed:
        key = q["question"][:100]
        if key not in seen:
            seen.add(key)
            unique.append(q)

    return unique



# ==========================
# GENERATE MCQs USING OPENAI
# ==========================
def generate_mcqs_via_openai(text, n_questions=8):
    """Generate MCQs from text using OpenAI API."""
    if not OPENAI_API_KEY:
        print("⚠️ No OpenAI API key found.")
        return []

    prompt = f"""
    You are an AI quiz generator.
    Create {n_questions} multiple-choice questions from the text below.
    Each must have 4 options (A,B,C,D) and one correct answer.
    Return valid JSON only in this exact format:
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
            max_tokens=1500,
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
            corr = (q.get("correct") or "A").strip().upper()
            m = re.search(r'([A-D])', corr, re.IGNORECASE)
            if m:
                corr_letter = m.group(1).upper()
                corr_index = ord(corr_letter) - 65
            else:
                corr_letter = "A"
                corr_index = 0
            normalized.append({
                "question": q.get("question", "").strip(),
                "options": opts[:4],
                "correct": corr_letter,
                "correct_index": int(max(0, min(corr_index, 3)))
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
        percent = round((score / total) * 100, 2) if total else 0
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
# RECORD ATTEMPT / RESULTS
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
