# main.py (FULL — replace your current file)
import os
import json
import streamlit as st
import openai
import re
from datetime import datetime
from utils import (
    extract_text_from_file,
    detect_mcq,
    parse_mcqs,
    generate_mcqs_via_openai,
    send_result_email,
    export_results_to_excel_bytes,
    record_attempt,
    list_attempts,
)

# ==========================
# CONFIG / SECRETS
# ==========================
if hasattr(st, "secrets"):
    OPENAI_API_KEY = st.secrets.get("OPENAI_API_KEY", None)
    EMAIL_USER = st.secrets.get("EMAIL_USER", None)
    EMAIL_PASS = st.secrets.get("EMAIL_PASS", None)
    ADMIN_USER = st.secrets.get("ADMIN_USER", "admin")
    ADMIN_PASS = st.secrets.get("ADMIN_PASS", "admin123")
else:
    from dotenv import load_dotenv
    load_dotenv()
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    EMAIL_USER = os.getenv("EMAIL_USER")
    EMAIL_PASS = os.getenv("EMAIL_PASS")
    ADMIN_USER = os.getenv("ADMIN_USER", "admin")
    ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")

openai.api_key = OPENAI_API_KEY

if not OPENAI_API_KEY:
    st.warning("⚠️ OpenAI API key not found in secrets (diagnostic).")

# ==========================
# STORAGE / UTILITIES
# ==========================
LOCAL_QUIZ_FILE = "quizzes.json"
LOCAL_RESULTS_FILE = "results.json"

def load_local_data(file):
    if os.path.exists(file):
        try:
            with open(file, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return []
    return []

def save_local_data(file, data):
    with open(file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# Ensure a quiz question object is normalized (adds correct_index etc.)
def normalize_question_obj(q):
    # ensure options exist and are strings
    opts = q.get("options", [])
    opts = [re.sub(r'\s+', ' ', (o or "")).strip() for o in opts]
    while len(opts) < 4:
        opts.append("N/A")
    q["options"] = opts[:4]

    # normalize correct (letter)
    corr = q.get("correct", None)
    corr_idx = q.get("correct_index", None)

    # try to parse correct_index if present and numeric/string
    if corr_idx is not None:
        try:
            corr_idx = int(corr_idx)
            if corr_idx < 0 or corr_idx >= len(q["options"]):
                corr_idx = 0
        except:
            corr_idx = None

    # if corr letter provided
    if corr and isinstance(corr, str):
        m = re.search(r'([A-Da-d])', corr)
        if m:
            corr_letter = m.group(1).upper()
            corr_idx = ord(corr_letter) - 65
        else:
            corr_letter = None
    else:
        corr_letter = None

    # If still None, try to infer from correct_index or correct_text
    if corr_idx is None:
        # try to infer from corr text if it's not a single letter
        if corr and isinstance(corr, str) and len(corr.strip()) > 1:
            corr_text = corr.strip()
            found = False
            for i, opt in enumerate(q["options"]):
                if corr_text.lower() in opt.lower() or opt.lower() in corr_text.lower():
                    corr_idx = i
                    found = True
                    break
            if not found:
                corr_idx = 0
        else:
            corr_idx = 0

    corr_idx = max(0, min(int(corr_idx), 3))
    q["correct_index"] = corr_idx
    q["correct"] = q.get("correct", chr(65 + corr_idx)).upper()
    return q

def normalize_quiz_in_memory(quiz):
    qs = quiz.get("questions", [])
    for i,q in enumerate(qs):
        qs[i] = normalize_question_obj(q)
    quiz["questions"] = qs
    return quiz

# Load quizzes/results and normalize in-memory
quizzes = load_local_data(LOCAL_QUIZ_FILE)
for i,qq in enumerate(quizzes):
    quizzes[i] = normalize_quiz_in_memory(qq)

results = load_local_data(LOCAL_RESULTS_FILE)

# ==========================
# STREAMLIT CONFIG & UI
# ==========================
st.set_page_config(page_title="SmartQuiz AI", layout="wide")
st.title("🎓 SmartQuiz AI – Automated Quiz Generator")

if "is_admin" not in st.session_state:
    st.session_state["is_admin"] = False

mode = st.sidebar.radio("Choose Mode", ["Student", "Admin"])

# ==========================
# ADMIN PANEL
# ==========================
if mode == "Admin":
    if not st.session_state["is_admin"]:
        st.subheader("🔑 Admin Login")
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        if st.button("Login"):
            if username == ADMIN_USER and password == ADMIN_PASS:
                st.session_state["is_admin"] = True
                st.experimental_rerun()
            else:
                st.error("❌ Invalid credentials.")
        st.stop()

    # Admin logged in
    st.sidebar.success("Logged in as Admin")
    tabs = st.tabs(["📤 Upload Document", "📚 Manage Quizzes", "📊 Student Results", "⚙️ Admin Tools"])

    # Upload Document
    with tabs[0]:
        st.subheader("📄 Upload New Quiz Document")
        uploaded_file = st.file_uploader("Choose a document (PDF, DOCX, or TXT)", type=["pdf", "docx", "txt"])
        if uploaded_file:
            file_bytes = uploaded_file.read()
            text = extract_text_from_file(file_bytes, uploaded_file.name)
            st.text_area("Extracted Text (Debug – full)", text, height=300)

            if not text.strip():
                st.error("⚠️ Could not extract text from the file.")
            else:
                with st.spinner("Analyzing document..."):
                    is_mcq = detect_mcq(text)
                    if is_mcq:
                        mcqs = parse_mcqs(text)
                        st.info(f"🧾 Detected existing MCQs in document: {len(mcqs)} found.")
                    else:
                        mcqs = generate_mcqs_via_openai(text, n_questions=10)

                # Normalize each question before showing/saving
                mcqs = [normalize_question_obj(q) for q in mcqs]

                if mcqs and len(mcqs) > 0:
                    st.success(f"✅ {len(mcqs)} MCQs are ready!")
                    st.text_area("Parsed MCQs (Debug)", "\n\n".join([f"{i+1}. {q['question']} (Ans: {q.get('correct')})" for i,q in enumerate(mcqs)]), height=200)

                    quiz_title = st.text_input("Enter Quiz Title")
                    if st.button("💾 Save Quiz"):
                        if not quiz_title.strip():
                            st.error("Please enter a quiz title.")
                        else:
                            quiz_obj = {
                                "title": quiz_title.strip(),
                                "questions": mcqs,
                                "created_at": datetime.utcnow().isoformat(),
                            }
                            quizzes.append(quiz_obj)
                            save_local_data(LOCAL_QUIZ_FILE, quizzes)
                            st.success(f"✅ Quiz '{quiz_title}' saved successfully!")
                else:
                    st.error("❌ No MCQs could be detected or generated.")

    # Manage Quizzes
    with tabs[1]:
        st.subheader("🗂 Manage Quizzes")
        if not quizzes:
            st.info("No quizzes uploaded yet.")
        else:
            for qi, q in enumerate(quizzes):
                with st.expander(q["title"]):
                    st.write(f"📅 Created: {q.get('created_at', '')}")
                    # Show a quick preview of first question + correct index
                    if q.get("questions"):
                        st.write("Preview first Q:", q["questions"][0].get("question"))
                        st.write("Correct index:", q["questions"][0].get("correct_index"), "Correct letter:", q["questions"][0].get("correct"))
                    if st.button(f"🗑 Delete '{q['title']}'", key=f"del_{qi}"):
                        quizzes = [x for x in quizzes if x["title"] != q["title"]]
                        save_local_data(LOCAL_QUIZ_FILE, quizzes)
                        st.warning(f"Deleted quiz '{q['title']}'")
                        st.experimental_rerun()

    # Student Results
    with tabs[2]:
        st.subheader("📊 Student Results")
        results = load_local_data(LOCAL_RESULTS_FILE)
        if not results:
            st.info("No results available yet.")
        else:
            df_data = [
                {
                    "Student": r["student_name"],
                    "Email": r["student_email"],
                    "Quiz": r["quiz_title"],
                    "Score": f"{r['score']}/{r['total']}",
                    "Date": r["timestamp"],
                }
                for r in results
            ]
            st.dataframe(df_data)
            excel_bytes = export_results_to_excel_bytes(df_data)
            st.download_button(
                "📥 Download Results (Excel)",
                data=excel_bytes,
                file_name="student_results.xlsx",
            )

    # Admin Tools
    with tabs[3]:
        st.subheader("⚙️ Admin Tools")
        st.write("Use these tools to fix or normalize stored quizzes.")
        if st.button("Normalize saved quizzes (persist)"):
            changed = 0
            for i, quiz in enumerate(quizzes):
                updated = False
                for j, q in enumerate(quiz.get("questions", [])):
                    old = q.copy()
                    quiz["questions"][j] = normalize_question_obj(q)
                    if quiz["questions"][j] != old:
                        updated = True
                if updated:
                    changed += 1
            if changed:
                save_local_data(LOCAL_QUIZ_FILE, quizzes)
            st.success(f"Normalization complete. Quizzes changed: {changed}")

# ==========================
# STUDENT PANEL
# ==========================
elif mode == "Student":
    st.header("🎓 Student Quiz Panel")

    quiz_titles = [q["title"] for q in quizzes] if quizzes else []
    if not quiz_titles:
        st.warning("No quizzes available yet. Please ask admin to upload one.")
    else:
        selected_quiz = st.selectbox("Choose a quiz:", quiz_titles)
        selected = next((q for q in quizzes if q["title"] == selected_quiz), None)

        if selected:
            # Ensure in-memory normalization (handles old quizzes without correct_index)
            selected = normalize_quiz_in_memory(selected)
            mcqs = selected.get("questions", [])
            st.subheader(f"📘 Quiz: {selected['title']}")

            student_name = st.text_input("Your Name")
            student_email = st.text_input("Your Email")

            if student_name and student_email and mcqs:
                selected_answers = {}  # index -> selected_index (int)

                for i, q in enumerate(mcqs):
                    st.markdown(f"**Q{i+1}. {q.get('question','').strip()}**")
                    opts = q.get("options", [])
                    # labeled options with A) B) C) D)
                    labeled_options = [f"{chr(65 + j)}) {opts[j]}" for j in range(len(opts))]
                    # ensure 4 labels
                    while len(labeled_options) < 4:
                        idx = len(labeled_options)
                        labeled_options.append(f"{chr(65 + idx)}) N/A")
                        opts.append("N/A")

                    choice = st.radio("", labeled_options, key=f"q_{i}")
                    sel_label = choice.split(")")[0].strip() if ")" in choice else ""
                    try:
                        sel_index = ord(sel_label) - 65 if sel_label else None
                    except:
                        sel_index = None
                    selected_answers[i] = sel_index
                    st.write("")

                if st.button("Submit Quiz"):
                    score = 0
                    for i, q in enumerate(mcqs):
                        correct_idx = q.get("correct_index")
                        sel_idx = selected_answers.get(i)
                        if sel_idx is not None and correct_idx is not None and int(sel_idx) == int(correct_idx):
                            score += 1

                    total = len(mcqs)
                    percent = round((score / total) * 100, 2) if total else 0
                    st.success(f"✅ You scored {score}/{total} ({percent}%)")

                    # Save attempt (include per-question info)
                    answers_for_record = []
                    for i, q in enumerate(mcqs):
                        sel_idx = selected_answers.get(i)
                        sel_text = q["options"][sel_idx] if sel_idx is not None and 0 <= sel_idx < len(q["options"]) else ""
                        answers_for_record.append({
                            "question": q.get("question"),
                            "selected_index": sel_idx,
                            "selected_text": sel_text,
                            "correct_index": q.get("correct_index"),
                            "correct_text": q.get("options")[q.get("correct_index")] if q.get("options") and q.get("correct_index") is not None else ""
                        })

                    attempt = {
                        "student_name": student_name,
                        "student_email": student_email,
                        "quiz_title": selected_quiz,
                        "score": score,
                        "total": total,
                        "percent": percent,
                        "answers": answers_for_record,
                        "timestamp": datetime.utcnow().isoformat(),
                    }
                    results.append(attempt)
                    save_local_data(LOCAL_RESULTS_FILE, results)

                    # Email result (best-effort)
                    ok = send_result_email(student_email, student_name, selected_quiz, score, total)
                    if ok:
                        st.info("📧 Result emailed successfully!")
                    else:
                        st.info("Result saved locally (email not sent).")
