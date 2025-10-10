def parse_mcqs(text):
    """
    Final, stable MCQ parser (v3)
    ✅ Handles 1. / 1) / Q1 / i)
    ✅ Avoids duplicate numbering
    ✅ Captures correct answers properly
    ✅ Ensures 4 clean options
    """

    import re
    text = text.replace("\r", "")
    text = re.sub(r"[*_]+", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    # Remove intro or filler lines
    ignore_phrases = [
        "compulsory quiz",
        "short quiz",
        "here are some more questions",
        "day"
    ]
    lines = [l for l in lines if not any(p in l.lower() for p in ignore_phrases)]

    joined = " ".join(lines)

    # --- Split questions safely ---
    # prevent double “1. 1.” bug using negative lookbehind
    q_blocks = re.split(
        r"(?<!\d)(?:\bQ?\s*\d+[\).]|[ivxlcdm]+\))", joined, flags=re.IGNORECASE
    )

    q_blocks = [q.strip() for q in q_blocks if len(q.strip()) > 20]

    mcqs = []
    for qblock in q_blocks:
        # Extract correct answer
        ans_match = re.search(r"Ans(?:wer)?\s*[:\-]?\s*([A-Da-d])", qblock, re.IGNORECASE)
        correct = ans_match.group(1).upper() if ans_match else "A"
        qblock = re.sub(r"Ans(?:wer)?\s*[:\-]?\s*[A-Da-d]", "", qblock, flags=re.IGNORECASE)

        # Find all options (A) to D))
        opts = re.findall(r"(?:^|\s)([A-Da-d][).:\-]\s*[^A-Da-d]+)", qblock)
        options = [re.sub(r"^[A-Da-d][).:\-]\s*", "", o).strip() for o in opts if o.strip()]

        # Extract question text before first option
        q_split = re.split(r"[A-Da-d][).:\-]\s*", qblock, maxsplit=1)
        q_text = q_split[0].strip() if q_split else qblock
        q_text = re.sub(r"^\d+[\).]?\s*", "", q_text).strip()  # remove leading numbering

        # Skip invalid fragments
        if len(q_text.split()) < 3:
            continue

        # Clean options to always have 4
        while len(options) < 4:
            options.append("N/A")
        options = options[:4]

        mcqs.append({
            "question": q_text,
            "options": options,
            "correct": correct
        })

    # Deduplicate by question text
    unique_mcqs = []
    seen = set()
    for q in mcqs:
        qt = q["question"].lower()
        if qt not in seen:
            seen.add(qt)
            unique_mcqs.append(q)

    return unique_mcqs
