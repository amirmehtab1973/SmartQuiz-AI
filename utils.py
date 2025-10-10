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
    Robust parser: normalizes label forms, removes filler phrases,
    slices options by label positions, uses last Ans: in block.
    """
    import re

    if not text:
        return []

    # Normalize and clean
    text = text.replace("\r", "\n")
    text = re.sub(r"[*_]+", "", text)
    # Trim repeated spaces but keep line breaks for context
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    # Remove obvious filler/title lines
    ignore_phrases = [
        "compulsory quiz",
        "here are some more questions",
        "short quiz",
        "quiz on artificial intelligence",
    ]
    lines = [l for l in lines if not any(p in l.lower() for p in ignore_phrases)]

    combined = "\n".join(lines)

    # Ensure Ans: lines are on their own line to avoid swallowing next question
    combined = re.sub(r"(Ans(?:wer)?\s*[:\-]?\s*[A-Da-d])", r"\n\1\n", combined, flags=re.IGNORECASE)

    # Split into question blocks where a line starts with numbering like "1." or "Q1."
    q_blocks = re.split(r"(?m)(?=^(?:Q?\s*\d+[\).]\s+))", combined)
    q_blocks = [b.strip() for b in q_blocks if len(b.strip()) > 10]

    parsed = []
    for block in q_blocks:
        # Remove filler phrase occuring inside block (e.g. appended at end of an option)
        block = re.sub(r'(?i)\bhere (are|is) some more .*?questions.*', '', block, flags=re.IGNORECASE)

        # Standardize labels: convert variants like "A )", "A.", "A-" to "A) "
        block_norm = re.sub(r'\b([A-Da-d])\s*[\)\.\:\-]\s*', r'\1) ', block)

        # Find all option-label positions (A) B) C) D))
        label_re = re.compile(r'([A-Da-d])\)\s*')
        labels = list(label_re.finditer(block_norm))

        options = []
        if labels:
            # question text = everything before first label
            q_start = labels[0].start()
            q_text = block_norm[:q_start].strip()

            # slice text between successive labels
            for i, m in enumerate(labels):
                start = m.end()
                end = labels[i+1].start() if i+1 < len(labels) else len(block_norm)
                opt_text = block_norm[start:end].strip()

                # Remove trailing filler if present
                opt_text = re.sub(r'(?i)\bhere (are|is) some more .*?questions.*', '', opt_text, flags=re.IGNORECASE).strip()
                opt_text = re.sub(r'\s+', ' ', opt_text).strip()
                options.append(opt_text)
        else:
            # fallback: no explicit labels visible — try to infer question and inline options
            # remove leading numbering
            q_text = re.sub(r'^\s*(?:Q?\s*\d+[\).]?\s*){1,2}', '', block_norm).strip()

            # try splitting inline like "... A) option B) option ..."
            parts = re.split(r'(?i)\s([A-Da-d])\)\s', block_norm)
            # parts e.g. ['prefix', 'A', 'opt A', 'B', 'opt B', ...]
            if len(parts) >= 3:
                q_text = parts[0].strip()
                # gather option texts after labels
                opts = []
                for i in range(1, len(parts)-1, 2):
                    opts.append(parts[i+1].strip())
                options = opts

        # Ensure exactly 4 options (pad with N/A if necessary)
        options = [o for o in options if o != ""]
        while len(options) < 4:
            options.append("N/A")
        options = options[:4]

        # Extract the last Ans: letter if present
        ans_letters = re.findall(r'Ans(?:wer)?\s*[:\-]?\s*([A-Da-d])', block, flags=re.IGNORECASE)
        correct = ans_letters[-1].upper() if ans_letters else None

        # If no letter, try textual answer match after "Ans:" (e.g. "Ans: All of the above")
        if not correct:
            textual_ans = re.search(r'Ans(?:wer)?\s*[:\-]?\s*(.+)$', block, flags=re.IGNORECASE)
            if textual_ans:
                ans_text = textual_ans.group(1).strip()
                # match to option text
                for idx, opt in enumerate(options):
                    if ans_text.lower() in opt.lower() or opt.lower() in ans_text.lower():
                        correct = chr(65 + idx)
                        break

        # Heuristic: pick "All of the above" if present when no explicit answer
        if not correct:
            for idx, opt in enumerate(options):
                if 'all of the above' in (opt or '').lower():
                    correct = chr(65 + idx)
                    break

        # default fallback
        if not correct:
            correct = 'A'

        # Clean question text: strip leading numbering leftovers
        q_text = re.sub(r'^\s*(?:Q?\s*\d+[\).]?\s*){1,2}', '', q_text).strip()
        if len(q_text.split()) < 3:
            # skip if still too short to be a valid question
            continue

        parsed.append({
            "question": q_text,
            "options": options,
            "correct": correct
        })

    # deduplicate by question text
    final = []
    seen = set()
    for q in parsed:
        key = q["question"].lower()
        if key not in seen:
            seen.add(key)
            final.append(q)

    return final


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
