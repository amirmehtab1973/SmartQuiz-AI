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
    Robust parser using span-based extraction:
    - Finds question start positions (1., 1) , Q1.), builds block = [start..next_start)
    - Extracts options A)-D) from block (supports multi-line options)
    - Finds last Ans: inside block and maps to the correct option
    - Filters out headings / short non-question blocks
    """
    import re

    if not text:
        return []

    # normalize line endings
    txt = text.replace("\r", "\n")
    # remove repeated asterisks used for bold in some docs
    txt = re.sub(r"\*+", "", txt)
    # keep original text for span slicing
    full = txt

    # candidate question headers (numbering) - do not treat "2. C)" as header because of lookahead
    header_re = re.compile(r"(?m)^\s*(?:Q\s*)?(\d{1,3})[\).]\s*(?![A-D][\).:])")

    # find all header matches with their start index
    headers = [(m.start(), m.end(), int(m.group(1))) for m in header_re.finditer(full)]

    # If no headers found, fallback to older approach
    if not headers:
        # fallback: split by lines that start with number and a dot
        lines = [l for l in full.split("\n") if l.strip()]
        blocks = []
        cur = []
        for line in lines:
            if re.match(r"^\s*\d+[\).]\s*", line):
                if cur:
                    blocks.append("\n".join(cur))
                    cur = []
                cur.append(line)
            else:
                cur.append(line)
        if cur:
            blocks.append("\n".join(cur))
    else:
        # build blocks using header spans
        blocks = []
        for i, (s, e, n) in enumerate(headers):
            start = s
            end = headers[i + 1][0] if i + 1 < len(headers) else len(full)
            block = full[start:end].strip()
            # skip if header line looks like title (no question words and no '?')
            header_line = full[s:full.find("\n", s) if full.find("\n", s) != -1 else end].strip()
            if not ("?" in header_line or re.search(r"\b(what|which|who|when|where|why|how)\b", header_line, re.IGNORECASE)):
                # if header line is short and next lines contain options, still accept it as question
                # otherwise skip the block
                # check if block contains option markers A) B) C)
                if not re.search(r"\b[A-D][\).:\-]\s", block):
                    # likely not a real question header (could be document title)
                    continue
            blocks.append(block)

    parsed = []
    for block in blocks:
        # short guard
        if not block or len(block.strip()) < 8:
            continue

        # Question text: take header line, remove the leading number
        first_line = block.split("\n", 1)[0]
        qtext = re.sub(r"^\s*(?:Q\s*)?\d{1,3}[\).]\s*", "", first_line).strip()

        # If question text is very short, try to include following lines up to first option
        if len(qtext) < 6:
            # try up to first option marker
            m_optpos = re.search(r"\b[A-D][\).:\-]\s", block)
            if m_optpos:
                qtext = block[:m_optpos.start()].strip()
                qtext = re.sub(r"^\s*(?:Q\s*)?\d{1,3}[\).]\s*", "", qtext).strip()

        # --- Extract options robustly: find each A)/B)/C)/D) and slice between labels ---
        options = []
        # find all option label occurrences with their indices
        opt_label_re = re.compile(r"([A-Da-d])[\).:\-]", re.IGNORECASE)
        all_labels = list(opt_label_re.finditer(block))

        if all_labels:
            # find position of answer markers (to avoid swallowing Answer: text)
            ans_marker = re.search(r"(?:Answer|Ans|Key)\s*[:\-]?", block, re.IGNORECASE)
            ans_pos = ans_marker.start() if ans_marker else len(block)
            for j, lab in enumerate(all_labels):
                start_idx = lab.end()
                if j + 1 < len(all_labels):
                    end_idx = all_labels[j + 1].start()
                else:
                    end_idx = ans_pos
                opt_text = block[start_idx:end_idx].strip()
                opt_text = re.sub(r"\s+", " ", opt_text).strip()
                options.append(opt_text)
        else:
            # fallback: lines starting with A) B) etc.
            opts = []
            for line in block.split("\n"):
                m = re.match(r"^\s*([A-Da-d])[\).:\-]\s*(.*)", line)
                if m:
                    opts.append(m.group(2).strip())
            options = [re.sub(r"\s+", " ", o).strip() for o in opts]

        # ensure exactly 4 options
        while len(options) < 4:
            options.append("N/A")
        options = options[:4]

        # --- Find last Answer letter in the block ---
        ans_letter_re = re.compile(r"(?:Answer|Ans|Key)\s*[:\-]?\s*([A-Da-d])\b", re.IGNORECASE)
        ans_matches = list(ans_letter_re.finditer(block))
        correct_letter = None
        correct_index = None
        if ans_matches:
            last_ans = ans_matches[-1]
            correct_letter = last_ans.group(1).upper()
            idx = ord(correct_letter) - 65
            if 0 <= idx < len(options):
                correct_index = idx

        # If no letter, try textual answer after Answer: and match to option text
        if correct_index is None:
            ans_text_re = re.compile(r"(?:Answer|Ans|Key)\s*[:\-]?\s*(.+)", re.IGNORECASE | re.DOTALL)
            m_alt = ans_text_re.search(block)
            if m_alt:
                ans_text = m_alt.group(1).strip()
                # cut if 'Here are' etc. appears after
                ans_text = re.split(r"(?:Here are|Here is|Here\'s)", ans_text, flags=re.IGNORECASE)[0].strip()
                ans_text = re.sub(r"\s+", " ", ans_text)
                # try to match to options
                matched = False
                for i, opt in enumerate(options):
                    if not opt or opt == "N/A":
                        continue
                    if ans_text.lower() in opt.lower() or opt.lower() in ans_text.lower():
                        correct_index = i
                        correct_letter = chr(65 + i)
                        matched = True
                        break

        # Heuristic: prefer "All of the above"
        if correct_index is None:
            for i, opt in enumerate(options):
                if "all of the above" in (opt or "").lower():
                    correct_index = i
                    correct_letter = chr(65 + i)
                    break

        # Final fallback: choose longest non-N/A option
        if correct_index is None:
            non_na = [(i, o) for i, o in enumerate(options) if o and o != "N/A"]
            if non_na:
                best = max(non_na, key=lambda x: len(x[1]))
                correct_index = best[0]
                correct_letter = chr(65 + correct_index)
            else:
                correct_index = 0
                correct_letter = "A"

        # add to parsed
        parsed.append({
            "question": re.sub(r"\s+", " ", qtext).strip(),
            "options": options,
            "correct": correct_letter,
            "correct_index": int(correct_index)
        })

    # cleanup: remove very short question texts and deduplicate
    final = []
    seen = set()
    for q in parsed:
        key = q["question"][:120]
        if len(q["question"]) < 6:
            continue
        if key in seen:
            continue
        seen.add(key)
        final.append(q)

    return final


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
