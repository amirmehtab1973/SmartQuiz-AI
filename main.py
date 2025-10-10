import os
import json
import re
import streamlit as st
import openai
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

# ==========================
# BASIC UI CONFIG
# ==========================
st.set_page_config(page_title="🎓 SmartQuiz AI", layout="wide")
st.title("🎓 SmartQuiz AI – Automated Quiz Generator")

if not OPENAI_API_KEY:
    st.error("⚠️ OpenAI API key not found.")
else:
    st.success("✅ OpenAI API key loaded successfully.")

# ==========================
# LOCAL DATA STORAGE
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
# SIDEBAR – MODE SWITCH
# ==========================
mode = st.sidebar.radio("Choose Mode", ["Student", "Admin"])

# ==========================
# ADMIN PANEL
# ==========================
if mode == "Admin":
    if "is_admin" not in st.session_state:
        st.session_state["is_admin"] = False

    if not st.session_state["is_admin"]:
        st.subheader("🔑 Admin Login")
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        if st.button("Login"):
            if username == ADMIN_USER and password == ADMIN_PASS:
                st.session_state["is_admin"] = True
                st.rerun()
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
            st.text_area("Extracted Text (Debug – Full)", text, height=400)

            if text.strip():
                with st.spinner("🔍 Parsing document for MCQs..."):
                    is_mcq = detect_mcq(text)
                    mcqs = parse_mcqs(text) if is_mcq else generate_mcqs_via_openai(text)

                if not mcqs:
                    st.error("❌ No MCQs could be generated or detected.")
                else:
                    st.text_area("Parsed MCQs (Debug)", "\n\n".join([f"{i+1}. {q['question']} (Ans: {q['correct']})" for i, q in enumerate(mcqs)]), height=400)
                    st.success(f"✅ {len(mcqs)} MCQs ready to save.")

                    quiz_title = st.text_input("Enter Quiz Title:")
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
                        st.rerun()

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
            st.download_button("📥 Download Results (Excel)", data=excel_bytes, file_name="student_results.xlsx")

# ==========================
# STUDENT PANEL
# ==========================
elif mode == "Student":
    st.header("🎓 Student Quiz Panel")

    quiz_titles = [q["title"] for q in quizzes] if quizzes else []
    if not quiz_titles:
        st.warning("No quizzes available yet. Please ask the admin to upload one.")
        st.stop()

    selected_quiz = st.selectbox("Choose a quiz:", quiz_titles)
    selected = next((q for q in quizzes if q["title"] == selected_quiz), None)

    if not selected:
        st.error("Quiz not found.")
        st.stop()

    mcqs = selected["questions"]

    st.subheader(f"📘 Quiz: {selected['title']}")
    student_name = st.text_input("Your Name")
    student_email = st.text_input("Your Email")

    if student_name and student_email:
        selected_answers = {}

        for i, q in enumerate(mcqs):
            st.markdown(f"**Q{i+1}.** {q.get('question', '').strip()}")
            opts = q.get("options", [])
            while len(opts) < 4:
                opts.append("N/A")

            labeled_options = [f"{chr(65 + j)}) {opts[j]}" for j in range(4)]
            choice = st.radio("", labeled_options, key=f"q_{i}")

            sel_label = choice.split(")")[0].strip() if ")" in choice else ""
            sel_index = ord(sel_label) - 65 if sel_label else None
            selected_answers[i] = sel_index
            st.write("")

        if st.button("Submit Quiz"):
            score = 0
            total = len(mcqs)
            for i, q in enumerate(mcqs):
                correct_letter = q.get("correct", "A").strip().upper()
                correct_index = ord(correct_letter) - 65 if correct_letter in "ABCD" else 0
                if selected_answers.get(i) == correct_index:
                    score += 1

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
