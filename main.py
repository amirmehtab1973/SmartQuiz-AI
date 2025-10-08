# (Paste the full main.py content here)
# main.py
import streamlit as st
import os
import json
from utils import (
    extract_text_from_file, detect_mcq, parse_mcqs,
    generate_mcqs_via_openai, save_quiz_to_db, list_quizzes_from_db,
    get_quiz_by_id, record_attempt, send_result_email,
    list_attempts, export_results_to_excel_bytes
)
from io import BytesIO
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# Config / secrets
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")

st.set_page_config(page_title="SmartQuiz AI", layout="wide")

# Simple CSS for light/dark toggling (injected)
LIGHT_STYLE = """
:root {
  --bg: #f8fafc;
  --card: #ffffff;
  --text: #0f172a;
  --accent: #0ea5a3;
}
body { background: var(--bg)!important; color: var(--text)!important; }
.stButton>button { background-color: var(--accent)!important; color: white !important; }
.css-1d391kg { background: var(--card) !important; }
"""
DARK_STYLE = """
:root {
  --bg: #0b1220;
  --card: #071124;
  --text: #e6eef6;
  --accent: #06b6d4;
}
body { background: var(--bg)!important; color: var(--text)!important; }
.stButton>button { background-color: var(--accent)!important; color: white !important; }
.css-1d391kg { background: var(--card) !important; }
"""

def inject_style(dark_mode: bool):
    if dark_mode:
        st.markdown(f"<style>{DARK_STYLE}</style>", unsafe_allow_html=True)
    else:
        st.markdown(f"<style>{LIGHT_STYLE}</style>", unsafe_allow_html=True)

# --- Sidebar ---
with st.sidebar:
    st.title("SmartQuiz AI")
    role = st.radio("Mode", ["Student", "Admin"])
    dark = st.checkbox("Dark mode")
    inject_style(dark)
    st.markdown("---")
    st.markdown("Built with 💡 + Streamlit")
    st.markdown("")

# --- Admin Login State ---
if "admin_authenticated" not in st.session_state:
    st.session_state["admin_authenticated"] = False

# --- Admin Section ---
if role == "Admin":
    if not st.session_state["admin_authenticated"]:
        st.header("Admin Login")
        u = st.text_input("Username")
        p = st.text_input("Password", type="password")
        if st.button("Login"):
            if u == ADMIN_USER and p == ADMIN_PASS:
                st.session_state["admin_authenticated"] = True
                st.success("Logged in as admin")
            else:
                st.error("Invalid credentials")
        st.stop()
    # Admin authenticated UI
    st.header("Admin Dashboard")
    tab1, tab2, tab3 = st.tabs(["Upload / Generate", "Manage Quizzes", "Student Results"])

    # --- Upload / Generate ---
    with tab1:
        st.subheader("Upload Document (PDF / DOCX / TXT)")
        uploaded = st.file_uploader("Choose file", type=["pdf", "docx", "txt"])
        quiz_title = st.text_input("Quiz Title", value="")
        num_q = st.number_input("Number of questions (if generating)", min_value=3, max_value=30, value=10)

        if uploaded is not None:
            # read bytes
            file_bytes = uploaded.read()
            text = extract_text_from_file(file_bytes, uploaded.name)
            st.markdown("**Preview of extracted text:**")
            st.text_area("Text preview", value=text[:2000], height=200)

            if st.button("Detect & Prepare Quiz"):
                if detect_mcq(text):
                    st.info("Detected MCQ-format document. Parsing questions...")
                    parsed = parse_mcqs(text)
                    st.session_state["preview_questions"] = parsed
                    st.success(f"Parsed {len(parsed)} questions (preview).")
                else:
                    st.info("Detected paragraph/theory document. Generating MCQs via OpenAI...")
                    generated = generate_mcqs_via_openai(text, int(num_q))
                    st.session_state["preview_questions"] = generated
                    st.success(f"Generated {len(generated)} questions (preview).")
        # Preview & publish
        if "preview_questions" in st.session_state:
            st.subheader("Quiz Preview")
            for idx, q in enumerate(st.session_state["preview_questions"]):
                st.write(f"Q{idx+1}. {q.get('question')}")
                opts = q.get("options") or []
                for oidx, o in enumerate(opts):
                    st.write(f"  {chr(65+oidx)}. {o}")
                st.write(f"  **Answer:** {q.get('correct')}")
            if st.button("Publish Quiz"):
                title = quiz_title or f"Quiz-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
                quiz_obj = {
                    "title": title,
                    "questions": st.session_state["preview_questions"],
                    "created_at": datetime.utcnow()
                }
                saved = save_quiz_to_db(quiz_obj)
                st.success(f"Quiz published: {title}")
                # clear preview
                del st.session_state["preview_questions"]

    # --- Manage Quizzes ---
    with tab2:
        st.subheader("Published Quizzes")
        quizzes = list_quizzes_from_db()
        if not quizzes:
            st.info("No quizzes found.")
        else:
            for q in quizzes:
                st.write(f"• {q.get('title')}  (id: {q.get('_id')})")
                cols = st.columns([1,1,1,4])
                if cols[0].button("View", key=f"view-{q.get('_id')}"):
                    full = get_quiz_by_id(q.get('_id'))
                    st.write(full)
                if cols[1].button("Delete", key=f"del-{q.get('_id')}"):
                    db = __import__("pymongo").MongoClient(os.getenv("MONGO_URI")).get_default_database()
                    db.quizzes.delete_one({"_id": __import__("bson").ObjectId(q.get('_id'))})
                    st.experimental_rerun()
                if cols[2].button("Export CSV", key=f"exp-{q.get('_id')}"):
                    full = get_quiz_by_id(q.get('_id'))
                    # build CSV of questions
                    rows = []
                    for i,qq in enumerate(full.get("questions",[])):
                        rows.append({"quiz_title": full["title"], "q_index": i+1, "question": qq.get("question"), "options": "|".join(qq.get("options",[])), "correct": qq.get("correct")})
                    buf = BytesIO()
                    import pandas as pd
                    pd.DataFrame(rows).to_csv(buf, index=False)
                    buf.seek(0)
                    st.download_button("Download CSV", data=buf, file_name=f"{full['title']}_questions.csv")

    # --- Student Results ---
    with tab3:
        st.subheader("Student Attempts")
        attempts = list_attempts()
        if not attempts:
            st.info("No attempts recorded yet.")
        else:
            # Show table
            import pandas as pd
            rows = []
            for a in attempts:
                rows.append({
                    "Student": a.get("student_name"),
                    "Email": a.get("student_email"),
                    "Quiz": a.get("quiz_title"),
                    "Score": f"{a.get('score')}/{a.get('total')}",
                    "Date": a.get("timestamp").strftime("%Y-%m-%d %H:%M:%S") if a.get("timestamp") else ""
                })
            df = pd.DataFrame(rows)
            st.dataframe(df)
            # download excel
            if st.button("Download all results (Excel)"):
                buf = export_results_to_excel_bytes(rows, filetype="xlsx")
                st.download_button("Download .xlsx", data=buf, file_name="smartquiz_results.xlsx")

# --- Student Section ---
else:
    st.header("Student Portal")
    st.subheader("Take a Quiz")
    quizzes = list_quizzes_from_db()
    if not quizzes:
        st.warning("No quizzes available at the moment. Please ask the admin to upload one.")
        st.stop()

    # choose quiz
    quiz_titles = {q["title"]: q["_id"] for q in quizzes}
    chosen = st.selectbox("Select Quiz", options=list(quiz_titles.keys()))
    quiz_id = quiz_titles[chosen]
    # fetch quiz
    quiz = get_quiz_by_id(quiz_id)
    if not quiz:
        st.error("Selected quiz could not be loaded.")
        st.stop()

    # student info
    student_name = st.text_input("Your name")
    student_email = st.text_input("Your email")

    # quiz questions
    st.markdown(f"### {quiz.get('title')}")
    answers = [None] * len(quiz.get("questions", []))
    for i, q in enumerate(quiz.get("questions", [])):
        st.write(f"**Q{i+1}. {q.get('question')}**")
        opts = q.get("options", [])
        # radio options
        choice = st.radio(f"Select (Q{i+1})", opts, key=f"q{i}")
        answers[i] = choice

    if st.button("Submit Answers"):
        # grading (compare selected option text to the correct option label or text)
        score = 0
        total = len(quiz.get("questions", []))
        for i, q in enumerate(quiz.get("questions", [])):
            sel = answers[i] or ""
            # If stored correct is label (A/B/C) map to options
            correct_label = q.get("correct", "").strip().upper()
            correct_text = None
            if correct_label in ["A","B","C","D"]:
                idx = ord(correct_label) - 65
                try:
                    correct_text = q.get("options", [])[idx]
                except Exception:
                    correct_text = None
            # compare by text fallback
            if correct_text:
                if sel.strip().lower() == correct_text.strip().lower():
                    score += 1
            else:
                # compare to stored correct string
                if sel.strip().lower() == str(q.get("correct","")).strip().lower():
                    score += 1
        # record attempt
        attempt = record_attempt(quiz_id, quiz.get("title"), student_name, student_email, answers, score, total)
        # send email
        try:
            sent = send_result_email(student_email, student_name, quiz.get("title"), score, total)
            if sent:
                st.success(f"Submitted! Your score: {score}/{total}. Email sent.")
            else:
                st.warning(f"Submitted! Your score: {score}/{total}. Email could not be sent (check server logs).")
        except Exception as e:
            st.success(f"Submitted! Your score: {score}/{total}. Email attempt raised error; check logs.")
        st.balloons()
        st.write("Thank you — your attempt has been recorded.")
