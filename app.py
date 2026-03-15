import fitz
import re
from flask import Flask, request, jsonify, render_template
from google_db import save_result
import threading

app = Flask(__name__)

# ── Text Extraction ────────────────────────────────────────────────────────────

def extract_text(file_stream):
    with fitz.open(stream=file_stream.read(), filetype="pdf") as doc:
        return "\n".join(page.get_text() for page in doc)

def extract_candidate_details(text):
    details = {"app_no": "Unknown", "roll_no": "Unknown", "name": "Unknown"}
    
    app_no_match = re.search(r"Application\s*(?:No\.?|Number)\s*:?\s*([A-Z0-9]+)", text, re.IGNORECASE)
    if app_no_match:
        details["app_no"] = app_no_match.group(1).strip()
        
    roll_no_match = re.search(r"Roll\s*(?:No\.?|Number)\s*:?\s*([A-Z0-9]+)", text, re.IGNORECASE)
    if roll_no_match:
        details["roll_no"] = roll_no_match.group(1).strip()
        
    # Attempt to extract candidate name, typically followed by test centre names or next fields
    name_match = re.search(r"Candidate'?s?\s*Name\s*:?\s*([A-Za-z\s]+?)(?=\n[A-Z]|$)", text, re.IGNORECASE)
    if name_match:
        details["name"] = name_match.group(1).strip()
        
    return details

# ── Answer Key Parser ──────────────────────────────────────────────────────────

def parse_answer_key(text):
    """
    Finds pairs of long numeric IDs on the same line.
    First ID = Question ID, Second = Correct Option ID.
    Works for any digit-based ID scheme (8–13 digits).
    """
    for pattern in [r"(\d{10})\s+(\d{10})", r"(\d{8,13})\s+(\d{8,13})"]:
        result = dict(re.findall(pattern, text))
        if result:
            return result
    return {}

# ── Response Sheet Parsers (3 strategies) ─────────────────────────────────────

# All known keyword variations for each field (case-insensitive regex)
_QID_LABELS = r"(?:question\s*id|q\.?\s*id|question\s*no\.?|ques(?:tion)?\.?\s*(?:no\.?|id)|qid)"
_OPT_LABELS = r"(?:option|opt\.?|choice|answer\s*option)\s*{n}\s*(?:id)?"
_CHO_LABELS = r"(?:chosen|selected|marked|answered?|your|given|attempted)\s*(?:option|answer|choice|resp(?:onse)?)?"
_NOT_ATT    = r"(?:not\s*(?:attempted|answered|available)|--|na\b|-)"

def _strategy_keyword(text):
    """
    Strategy 1: Label-based parsing. Tries many keyword variations.
    Reliable when PDF contains standard text labels.
    """
    lines = text.splitlines()
    blocks, block = [], []

    qid_start = re.compile(_QID_LABELS, re.I)

    for line in lines:
        stripped = line.strip()
        if qid_start.search(stripped) and block:
            blocks.append(block)
            block = []
        block.append(stripped)
    if block:
        blocks.append(block)

    response_map = {}
    opt_re  = [re.compile(_OPT_LABELS.replace("{n}", str(i+1)) + r"\s*[:\-]?\s*(\d{8,13})", re.I) for i in range(4)]
    qid_re  = re.compile(_QID_LABELS + r"\s*[:\-]?\s*(\d{8,13})", re.I)
    cho_re  = re.compile(_CHO_LABELS + r"\s*[:\-]?\s*(\d{1,2}|" + _NOT_ATT + r")", re.I)

    for block in blocks:
        full = "\n".join(block)
        qm = qid_re.search(full)
        if not qm:
            continue
        qid     = qm.group(1)
        options = [None] * 4
        for i, pat in enumerate(opt_re):
            m = pat.search(full)
            if m:
                options[i] = m.group(1)

        cm = cho_re.search(full)
        if not cm:
            response_map[qid] = "Unattempted"
            continue

        chosen_raw = cm.group(1).strip()
        if re.match(_NOT_ATT, chosen_raw, re.I) or not chosen_raw.isdigit():
            response_map[qid] = "Unattempted"
        else:
            idx = int(chosen_raw) - 1
            response_map[qid] = options[idx] if 0 <= idx < 4 else "Unattempted"

    return response_map


def _strategy_anchored(text, answer_qids):
    """
    Strategy 2: QID-Anchored — uses known question IDs from the answer key
    to anchor each block in the response sheet. Completely label-free.

    For each question ID found in the response text:
      - Extract the next 4 long numbers as option IDs
      - Search the surrounding text for a choice indicator (1-4 or 'not attempted')
    """
    if not answer_qids:
        return {}

    response_map = {}
    id_re  = re.compile(r'\b(\d{8,13})\b')
    not_re = re.compile(_NOT_ATT, re.I)

    # Build a list of all (position, number) in the document
    all_ids = [(m.start(), m.group(1)) for m in id_re.finditer(text)]

    for qid in answer_qids:
        # Find this QID in the text
        qid_positions = [pos for pos, num in all_ids if num == qid]
        if not qid_positions:
            response_map[qid] = "Unattempted"
            continue

        qpos = qid_positions[0]

        # The next 4 long numbers after QID are the option IDs
        following = [(pos, num) for pos, num in all_ids if pos > qpos]
        if len(following) < 4:
            response_map[qid] = "Unattempted"
            continue

        option_ids = [num for _, num in following[:4]]

        # Look at the text window after the 4th option for choice indicator
        window_start = following[3][0] + len(following[3][1])
        # End of window: start of next QID or +300 chars
        next_qid_pos = next(
            (pos for pos, num in following[4:] if num in answer_qids),
            window_start + 300
        )
        window = text[window_start:min(window_start + 300, next_qid_pos)]

        # Look for "not attempted" first
        if not_re.search(window):
            response_map[qid] = "Unattempted"
            continue

        # Look for a standalone digit 1-4
        chosen_match = re.search(r'\b([1-4])\b', window)
        if not chosen_match:
            response_map[qid] = "Unattempted"
        else:
            idx = int(chosen_match.group(1)) - 1
            response_map[qid] = option_ids[idx] if 0 <= idx < 4 else "Unattempted"

    return response_map


def _strategy_sequential(text):
    """
    Strategy 3: Sequential grouping — finds ALL long numbers in order,
    groups every 5 as [QID, Opt1, Opt2, Opt3, Opt4], then finds choice.
    Last resort if PDF has no recognizable labels and answer key is unavailable.
    """
    id_matches = list(re.finditer(r'\b(\d{8,13})\b', text))
    if len(id_matches) < 5:
        return {}

    response_map = {}
    not_re = re.compile(_NOT_ATT, re.I)

    for i in range(0, len(id_matches) - 4, 5):
        group   = id_matches[i:i+5]
        qid     = group[0].group(1)
        options = [g.group(1) for g in group[1:5]]

        # Text window after 4th option until next group starts
        win_start = group[4].end()
        win_end   = id_matches[i+5].start() if i+5 < len(id_matches) else win_start + 300
        window    = text[win_start:min(win_start + 300, win_end)]

        if not_re.search(window):
            response_map[qid] = "Unattempted"
            continue

        cm = re.search(r'\b([1-4])\b', window)
        if not cm:
            response_map[qid] = "Unattempted"
        else:
            idx = int(cm.group(1)) - 1
            response_map[qid] = options[idx] if 0 <= idx < 4 else "Unattempted"

    return response_map


def parse_response_sheet(text, answer_qids=None):
    """
    Tries three strategies in order, picks the one with the best coverage
    against the answer key question IDs.
    """
    candidates = []

    s1 = _strategy_keyword(text)
    candidates.append(("keyword", s1))

    if answer_qids:
        s2 = _strategy_anchored(text, answer_qids)
        candidates.append(("anchored", s2))

    s3 = _strategy_sequential(text)
    candidates.append(("sequential", s3))

    if answer_qids:
        # Pick the strategy that covers the most known question IDs
        def coverage(result):
            return sum(1 for q in answer_qids if q in result and result[q] != "Unattempted")
        best_name, best = max(candidates, key=lambda c: coverage(c[1]))
    else:
        best_name, best = max(candidates, key=lambda c: len(c[1]))

    return best, best_name


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/check", methods=["POST"])
def check():
    if "response_sheet" not in request.files or "answer_key" not in request.files:
        return jsonify({"error": "Both PDF files are required."}), 400

    try:
        response_text = extract_text(request.files["response_sheet"])
        answer_text   = extract_text(request.files["answer_key"])

        answer_map = parse_answer_key(answer_text)
        if not answer_map:
            return jsonify({
                "error": "Could not parse Answer Key PDF. Please check the file.",
                "debug_answer_sample": answer_text[:600],
            }), 400

        answer_qids = set(answer_map.keys())
        response_map, strategy_used = parse_response_sheet(response_text, answer_qids)

        if not response_map:
            return jsonify({
                "error": "Could not parse Response Sheet PDF. Please check the file.",
                "debug_response_sample": response_text[:600],
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

        score = correct * 4 - incorrect

        cand_details = extract_candidate_details(response_text)
        
        # Save to DB asynchronously to avoid blocking the response
        def save_to_gsheets():
            try:
                save_result(cand_details["app_no"], cand_details["roll_no"], cand_details["name"], score)
            except Exception as e:
                print(f"Error saving to db: {e}")
                
        threading.Thread(target=save_to_gsheets).start()

        return jsonify({
            "score":          correct * 4 - incorrect,
            "correct":        correct,
            "incorrect":      incorrect,
            "unattempted":    unattempted,
            "strategy_used":  strategy_used,
            "results":        results,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/debug", methods=["POST"])
def debug():
    if "response_sheet" not in request.files or "answer_key" not in request.files:
        return jsonify({"error": "Both files required"}), 400
    try:
        response_text = extract_text(request.files["response_sheet"])
        answer_text   = extract_text(request.files["answer_key"])

        answer_map  = parse_answer_key(answer_text)
        answer_qids = set(answer_map.keys())
        response_map, strategy = parse_response_sheet(response_text, answer_qids)

        attempted = sum(1 for v in response_map.values() if v != "Unattempted")

        return jsonify({
            "answer_key": {
                "questions_found": len(answer_map),
                "sample": list(answer_map.items())[:5],
                "raw_sample": answer_text[:600],
            },
            "response_sheet": {
                "strategy_used": strategy,
                "questions_found": len(response_map),
                "attempted": attempted,
                "sample": list(response_map.items())[:5],
                "raw_sample": response_text[:600],
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True)
