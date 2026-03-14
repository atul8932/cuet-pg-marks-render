import fitz
import re
from flask import Flask, request, jsonify, render_template

app = Flask(__name__)

# ── Parsers ──────────────────────────────────────────────────────────────────

def extract_text(file_stream):
    with fitz.open(stream=file_stream.read(), filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc)

def parse_answer_key(text):
    """
    Tries to match pairs of numeric IDs (8-12 digits) on the same line,
    which is the CUET answer key format: <QuestionID>  <CorrectOptionID>
    """
    # Primary: strict 10-digit pairs
    result = dict(re.findall(r"(\d{10})\s+(\d{10})", text))
    if result:
        return result
    # Fallback: any 8-12 digit pair on the same line
    result = dict(re.findall(r"(\d{8,12})\s+(\d{8,12})", text))
    return result

def parse_response_sheet(text):
    """Returns {question_id: chosen_option_id | 'Unattempted'}"""
    lines = text.splitlines()
    blocks, block = [], []

    for line in lines:
        line = line.strip()
        if "Question ID" in line and block:
            blocks.append(block)
            block = []
        block.append(line)
    if block:
        blocks.append(block)

    response_map = {}
    for block in blocks:
        qid, chosen, options = None, None, [None] * 4
        for line in block:
            if "Question ID" in line:
                m = re.search(r"(\d{8,12})", line)
                qid = m.group(1) if m else None
            for idx in range(4):
                if f"Option {idx+1} ID" in line:
                    m = re.search(r"(\d{8,12})", line)
                    options[idx] = m.group(1) if m else None
            if "Chosen Option" in line:
                m = re.search(r"(\d+|Not Attempted)", line, re.IGNORECASE)
                chosen = m.group(1) if m else "Not Attempted"

        if qid:
            if not chosen or str(chosen).lower().startswith("not") or not str(chosen).isdigit():
                response_map[qid] = "Unattempted"
            else:
                idx = int(chosen) - 1
                response_map[qid] = options[idx] if 0 <= idx < 4 else "Unattempted"

    return response_map

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/check", methods=["POST"])
def check():
    if "response_sheet" not in request.files or "answer_key" not in request.files:
        return jsonify({"error": "Both PDF files are required."}), 400

    try:
        response_file = request.files["response_sheet"]
        answer_file   = request.files["answer_key"]

        response_text = extract_text(response_file)
        answer_text   = extract_text(answer_file)

        answer_map   = parse_answer_key(answer_text)
        response_map = parse_response_sheet(response_text)

        if not answer_map:
            return jsonify({
                "error": "Could not parse Answer Key PDF. Please check the file and try again.",
                "debug_answer_sample": answer_text[:500]
            }), 400

        if not response_map:
            return jsonify({
                "error": "Could not parse Response Sheet PDF. Please check the file and try again.",
                "debug_response_sample": response_text[:500]
            }), 400

        correct = incorrect = unattempted = 0
        results = []

        for qid, correct_code in answer_map.items():
            user_code = response_map.get(qid, "Unattempted")
            if user_code == "Unattempted":
                status = "Unattempted"
                unattempted += 1
            elif user_code == correct_code:
                status = "Correct"
                correct += 1
            else:
                status = "Incorrect"
                incorrect += 1

            results.append({
                "qid":     qid,
                "yours":   user_code,
                "correct": correct_code,
                "status":  status,
            })

        return jsonify({
            "score":       correct * 4 - incorrect,
            "correct":     correct,
            "incorrect":   incorrect,
            "unattempted": unattempted,
            "results":     results,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/debug", methods=["POST"])
def debug():
    """Upload both PDFs and get a diagnostic snapshot of what was parsed."""
    if "response_sheet" not in request.files or "answer_key" not in request.files:
        return jsonify({"error": "Both files required"}), 400
    try:
        response_text = extract_text(request.files["response_sheet"])
        answer_text   = extract_text(request.files["answer_key"])

        answer_map   = parse_answer_key(answer_text)
        response_map = parse_response_sheet(response_text)

        return jsonify({
            "answer_key": {
                "total_questions_parsed": len(answer_map),
                "sample_pairs":           list(answer_map.items())[:5],
                "raw_text_sample":        answer_text[:800],
            },
            "response_sheet": {
                "total_questions_parsed": len(response_map),
                "sample_pairs":           list(response_map.items())[:5],
                "raw_text_sample":        response_text[:800],
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True)
