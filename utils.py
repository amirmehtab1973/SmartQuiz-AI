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
    Robust parser tuned for exam-style documents.
    Handles question numbering like 1., 1), Q1., i), ii) etc.
    Extracts A)-D) options even when lines wrap, and finds the last Ans: marker.
    """
    # normalize
    text = (text or "").replace("\r", "\n")
    text = re.sub(r"\*+", "", text)
    # split into lines and remove empty
    lines = [l for l in text.split("\n")]

    # group lines into question blocks: start a new block when a line looks like a question header
    blocks = []
    cur = []
    q_start_re = re.compile(r"^\s*(?:Q?\s*\d+[\).]|[ivxlcdm]+\)|\d+\.)\s*", re.IGNORECASE)
    for line in lines:
        if q_start_re.match(line):
            if cur:
                blocks.append("\n".join(cur).strip())
            cur = [line.strip()]
        else:
            # keep lines that are not empty
            if line.strip() != "":
                cur.append(line.strip())
    if cur:
        blocks.append("\n".join(cur).strip())

    parsed = []
    for block in blocks:
        if not block or len(block.strip()) < 5:
            continue

        # Question text: remove leading numbering from the first line only
        first_line = block.split("\n", 1)[0]
        qtext = re.sub(r"^\s*(?:Q?\s*\d+[\).]|\d+\.)\s*", "", first_line, flags=re.IGNORECASE).strip()

        # If the block has more lines, append them to question text until options start,
        # but we will extract options separately so keep full block for option search.
        # Extract option positions by finding all A)/A. tokens
        option_label_re = re.compile(r"([A-Da-d])[\).:\-]", re.IGNORECASE)
        matches = list(option_label_re.finditer(block))

        options = []
        if matches:
            # For each option label, take the text between its end and the next label or Ans/Answer or end
            # Find position of answer markers (first occurrence) to limit slicing
            ans_marker_re = re.compile(r"(?:Answer|Ans|Key)\s*[:\-]?", re.IGNORECASE)
            ans_marker_match = ans_marker_re.search(block)
            ans_pos = ans_marker_match.start() if ans_marker_match else len(block)

            for i, m in enumerate(matches):
                start = m.end()
                if i + 1 < len(matches):
                    end = matches[i + 1].start()
                else:
                    end = ans_pos
                opt_text = block[start:end].strip()
                opt_text = re.sub(r"\s+", " ", opt_text).strip()
                options.append(opt_text)
        else:
            # fallback: try to pull lines that start with A), B) etc on separate lines
            opts = []
            for line in block.split("\n"):
                m = re.match(r"^\s*([A-Da-d])[\).:\-]\s*(.*)", line)
                if m:
                    opts.append(m.group(2).strip())
            options = [re.sub(r"\s+", " ", o).strip() for o in opts]

        # ensure 4 options
        while len(options) < 4:
            options.append("N/A")
        options = options[:4]

        # Find ALL answer-letter matches in the block and pick the last one (closest to end)
        ans_letter_re = re.compile(r"(?:Answer|Ans|Key)\s*[:\-]?\s*([A-Da-d])", re.IGNORECASE)
        ans_letters = list(ans_letter_re.finditer(block))
        correct_letter = None
        correct_index = None

        if ans_letters:
            last = ans_letters[-1]
            correct_letter = last.group(1).upper()
            idx = ord(correct_letter) - 65
            if 0 <= idx < len(options):
                correct_index = idx
            else:
                correct_index = None

        # If no letter found, try to capture textual answer after 'Answer:' and match to option texts
        if correct_index is None:
            ans_text_re = re.compile(r"(?:Answer|Ans|Key)\s*[:\-]?\s*(.+)$", re.IGNORECASE | re.MULTILINE)
            m_alt = ans_text_re.search(block)
            if m_alt:
                ans_text = m_alt.group(1).strip()
                # often there could be trailing phrases, cut at line break or 'Here are' etc.
                ans_text = re.split(r"(?:\n|Here are|Here\'s|Here is)", ans_text, flags=re.IGNORECASE)[0].strip()
                ans_text = re.sub(r"\s+", " ", ans_text)
                # try to match to options
                found = False
                for i, opt in enumerate(options):
                    if not opt or opt == "N/A":
                        continue
                    if ans_text.lower() in opt.lower() or opt.lower() in ans_text.lower():
                        correct_index = i
                        correct_letter = chr(65 + i)
                        found = True
                        break
                if not found:
                    # heuristic: if "All of the above" is in ans_text, pick option containing that
                    for i, opt in enumerate(options):
                        if "all of the above" in opt.lower() or "all of the above" in ans_text.lower():
                            correct_index = i
                            correct_letter = chr(65 + i)
                            found = True
                            break

        # Additional heuristic: if still None but an option explicitly contains 'all of the above', use it
        if correct_index is None:
            for i, opt in enumerate(options):
                if "all of the above" in (opt or "").lower():
                    correct_index = i
                    correct_letter = chr(65 + i)
                    break

        # Final fallback: if still None, attempt to pick option that seems longest or not 'N/A'
        if correct_index is None:
            non_na = [(i, o) for i, o in enumerate(options) if o and o != "N/A"]
            if non_na:
                # pick the option with the longest length (heuristic)
                best = max(non_na, key=lambda x: len(x[1]))
                correct_index = best[0]
                correct_letter = chr(65 + correct_index)
                print(f"[WARN] No explicit answer letter found; guessed index {correct_index} ({correct_letter}) for question: {qtext[:60]}")
            else:
                correct_index = 0
                correct_letter = "A"
                print(f"[WARN] No options available; defaulting to A for question: {qtext[:60]}")

        parsed.append({
            "question": qtext,
            "options": options,
            "correct": correct_letter,
            "correct_index": int(correct_index)
        })

    # remove obvious junk blocks: question length > 5 and at least one non-N/A option
    parsed = [q for q in parsed if len(q["question"]) > 5 and any(o != "N/A" for o in q["options"])]
    return parsed

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
