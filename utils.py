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
    Defensive MCQ parser:
    - Finds question starts by numbering OR by question words / '?'
    - Detects cases where numbering is followed immediately by an option (e.g. "2. D) ...")
      and looks at previous/next lines for the real question text
    - Collects the block between question starts, extracts A-D options robustly,
      picks the last Ans: occurrence inside that block, and returns cleaned MCQs.
    """
    import re

    if not text:
        return []

    # Normalize & split into lines
    text = text.replace("\r", "\n")
    text = re.sub(r"[*_]+", "", text)
    # preserve original line order, but trim blanks
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    def is_option_line(l):
        return bool(re.match(r'^[A-Da-d][\).:\-]\s+', l))

    def is_ans_line(l):
        return bool(re.search(r'\bAns(?:wer)?\b', l, re.IGNORECASE))

    def is_numbered_header(l):
        return bool(re.match(r'^(?:Q?\s*\d+[\).]|\d+\.)\s*', l, re.IGNORECASE))

    def has_question_word(l):
        return bool(re.search(r'\b(what|which|who|when|where|why|how)\b', l, re.IGNORECASE))

    # Build candidate question start indices
    candidates = []
    for i, ln in enumerate(lines):
        # skip pure option or answer lines as starts
        if is_option_line(ln) or is_ans_line(ln):
            continue

        if is_numbered_header(ln):
            # if number is immediately followed by an option label (e.g. "2. C) ...")
            if re.match(r'^(?:Q?\s*\d+[\).])\s*[A-Da-d][\).:\-]', ln):
                # prefer the previous line if it looks like the question
                if i - 1 >= 0 and has_question_word(lines[i - 1]):
                    idx = i - 1
                    if idx not in candidates:
                        candidates.append(idx)
                    continue
                # prefer the next line if it looks like the question
                if i + 1 < len(lines) and has_question_word(lines[i + 1]):
                    idx = i + 1
                    if idx not in candidates:
                        candidates.append(idx)
                    continue
                # otherwise only accept this line as header if it contains a question word or '?'
                if has_question_word(ln) or '?' in ln:
                    if i not in candidates:
                        candidates.append(i)
                    continue
                # else it's likely an inline-options-only line: skip starting here
                continue
            else:
                if i not in candidates:
                    candidates.append(i)
                continue

        # Not a numbered header: treat as question start if contains '?' or question word
        if has_question_word(ln) or '?' in ln:
            if i not in candidates:
                candidates.append(i)

    # Always sort and ensure first candidate is earliest
    candidates = sorted(set(candidates))

    # If no candidates found, try a fallback: treat any line containing '?' as candidate
    if not candidates:
        for i, ln in enumerate(lines):
            if '?' in ln:
                candidates.append(i)
        candidates = sorted(set(candidates))

    # Build blocks between candidate starts
    blocks = []
    for idx_pos, start_idx in enumerate(candidates):
        end_idx = candidates[idx_pos + 1] if idx_pos + 1 < len(candidates) else len(lines)
        block_lines = lines[start_idx:end_idx]
        # also include following option lines if block ends before next candidate
        # (they should already be included but this ensures we capture trailing A)/B) lines)
        j = end_idx
        while j < len(lines) and is_option_line(lines[j]):
            block_lines.append(lines[j])
            j += 1
        block_text = " ".join(block_lines).strip()
        if block_text:
            blocks.append(block_text)

    # If still no blocks, fallback to entire text as single block
    if not blocks:
        blocks = [" ".join(lines)]

    mcqs = []
    for block in blocks:
        # Extract the last Ans: letter in the block (if any)
        all_ans = re.findall(r'Ans(?:wer)?\s*[:\-]?\s*([A-Da-d])', block, re.IGNORECASE)
        correct = all_ans[-1].upper() if all_ans else None

        # Remove any Ans: tokens from block for cleaner parsing
        block_clean = re.sub(r'Ans(?:wer)?\s*[:\-]?\s*[A-Da-d]', '', block, flags=re.IGNORECASE)

        # Extract options by locating A) B) C) D) positions
        opt_matches = list(re.finditer(r'([A-Da-d])[\).:\-]\s*', block_clean))
        options = []
        if opt_matches:
            for i, m in enumerate(opt_matches):
                start = m.end()
                end = opt_matches[i + 1].start() if i + 1 < len(opt_matches) else len(block_clean)
                opt_text = block_clean[start:end].strip()
                # remove stray leading numbering or punctuation
                opt_text = re.sub(r'^\s*[\d\).\-\:]+', '', opt_text)
                opt_text = re.sub(r'\s+', ' ', opt_text).strip()
                options.append(opt_text)
        else:
            # If no labeled options found, try splitting at common separators ( " A) " already removed)
            # as final fallback: look for "A) " tokens inline with lookahead across the text
            # Nothing to do here; options will be padded later
            options = []

        # Extract question text: everything before the first option label (if exists), else whole block
        if opt_matches:
            q_text = block_clean[:opt_matches[0].start()].strip()
        else:
            # try to remove leading numbering
            q_text = re.sub(r'^\s*(?:Q?\s*\d+[\).]?\s*){1,2}', '', block_clean).strip()

        # Heuristic: If q_text is too short (like 1-2 words), attempt to find nearest line containing question words
        if len(q_text.split()) < 3:
            # try to find 'what/which' phrase inside the block_clean
            m_qw = re.search(r'(.{0,200}\b(what|which|who|when|where|why|how)\b.*?)(?=[A-D][\).:\-]|$)', block_clean, re.IGNORECASE)
            if m_qw:
                q_text = m_qw.group(1).strip()

        # Normalize options length to 4
        while len(options) < 4:
            options.append("N/A")
        options = options[:4]

        # Final attempt: if correct letter wasn't found, try to detect textual answer like "all of the above"
        if not correct:
            for i, opt in enumerate(options):
                if opt and 'all of the above' in opt.lower():
                    correct = chr(65 + i)
                    break

        # Default to A if still unknown
        if not correct:
            correct = "A"

        # Clean question text
        q_text = re.sub(r'^\s*\d+[\).]?\s*', '', q_text).strip()
        if len(q_text) < 3:
            # skip if still useless
            continue

        mcqs.append({
            "question": q_text,
            "options": options,
            "correct": correct
        })

    # de-duplicate by question text and return
    seen = set()
    final = []
    for q in mcqs:
        key = q["question"].strip().lower()
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
