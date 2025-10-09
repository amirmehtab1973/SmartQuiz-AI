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
# CONFIG / SECRETS SECTION
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

# API Key check
if not OPENAI_API_KEY:
    st.error("⚠️ OpenAI API key not found in secrets.")
else:
    st.success("✅ OpenAI key loaded successfully.")

# ==========================
# GLOBAL STORAGE
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

quizzes = load_local_data(LOCAL_QUIZ_FILE)
results = load_local_data(LOCAL_RESULTS_FILE)

# ==========================
# STREAMLIT CONFIG
# ==========================
st.set_page_config(page_title="SmartQuiz AI", layout="wide")

st.markdown(
    """
    <style>
    .main {
        background-color: #0e1117;
        color: white;
    }
    .stButton>button {
        border-radius: 10px;
        font-weight: bold;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("🎓 SmartQuiz AI – Automated Quiz Generator")

# ==========================
# LOGIN & MODE SELECTION
# ==========================
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
                st.success("✅ Login successful!")
            else:
                st.error("❌ Invalid credentials.")
        st.stop()

    st.sidebar.success("Logged in as Admin")
    tabs = st.tabs(["📤 Upload Document", "📚 Manage Quizzes", "📊 Student Results"])

    # TAB 1: UPLOAD DOCUMENT
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
                        st.warning("⚙️ No clear MCQs found. Generating automatically via AI…")
                        mcqs = generate_mcqs_via_openai(text, n_questions=10)

                if mcqs and len(mcqs) > 0:
                    st.success(f"✅ {len(mcqs)} MCQs are ready!")
                    st.text_area("Parsed MCQs (Debug)", "\n\n".join([q["question"] for q in mcqs]), height=200)

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

    # TAB 2: MANAGE QUIZZES
    with tabs[1]:
        st.subheader("🗂 Manage Quizzes")
        if not quizzes:
            st.info("No quizzes uploaded yet.")
        else:
            for q in quizzes:
                with st.expander(q["title"]):
                    st.write(f"📅 Created: {q.get('created_at', '')}")
                    if st.button(f"🗑 Delete '{q['title']}'", key=f"del_{q['title']}"):
                        quizzes = [x for x in quizzes if x["title"] != q["title"]]
                        save_local_data(LOCAL_QUIZ_FILE, quizzes)
                        st.warning(f"Deleted quiz '{q['title']}'")
                        st.experimental_rerun()

    # TAB 3: STUDENT RESULTS
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

# ==========================
# STUDENT PANEL
# ==========================
# ==========================
# STUDENT PANEL (REPLACE THIS BLOCK)
# ==========================
# ==========================
# STUDENT PANEL
# ==========================
st.header("🎓 Student Quiz Panel")

quiz_titles = [q["title"] for q in quizzes] if quizzes else []
if not quiz_titles:
    st.warning("No quizzes available yet. Please ask admin to upload one.")
else:
    selected_quiz = st.selectbox("Choose a quiz:", quiz_titles)
    selected = next((q for q in quizzes if q["title"] == selected_quiz), None)

    if selected:
        mcqs = selected.get("questions", [])
        st.subheader(f"📘 Quiz: {selected['title']}")

        student_name = st.text_input("Your Name")
        student_email = st.text_input("Your Email")

        if student_name and student_email and mcqs:
            selected_answers = {}

            for i, q in enumerate(mcqs):
                st.markdown(f"**Q{i+1}. {q['question']}**")
                opts = q.get("options", [])
                labeled_options = [f"{chr(65+j)}) {opt}" for j, opt in enumerate(opts)]
                choice = st.radio("Choose answer:", labeled_options, key=f"q_{i}")
                sel_label = choice.split(")")[0].strip() if ")" in choice else ""
                sel_index = ord(sel_label) - 65 if sel_label else None
                selected_answers[i] = sel_index
                st.write("")

            if st.button("Submit Quiz"):
                score = 0
                for i, q in enumerate(mcqs):
                    if selected_answers.get(i) == q.get("correct_index"):
                        score += 1

                total = len(mcqs)
                st.success(f"✅ You scored {score} out of {total}")

                attempt = {
                    "student_name": student_name,
                    "student_email": student_email,
                    "quiz_title": selected_quiz,
                    "score": score,
                    "total": total,
                    "timestamp": datetime.utcnow().isoformat(),
                }
                results.append(attempt)
                save_local_data(LOCAL_RESULTS_FILE, results)

                send_result_email(student_email, student_name, selected_quiz, score, total)
                st.info("📧 Result emailed successfully!")
