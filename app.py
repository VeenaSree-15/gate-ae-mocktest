import os
import json
import time
import uuid
import random
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, request, jsonify, session, Response
from flask import send_file
from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()

USE_LOCAL_LLM = os.getenv("USE_LOCAL_LLM", "true").lower() == "true"
LOCAL_LLM_BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "http://localhost:11434/v1").strip()
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "llama3.1:8b").strip()

USE_LOCAL_LLM = os.getenv("USE_LOCAL_LLM", "false").lower() == "true"

if USE_LOCAL_LLM:
    LOCAL_LLM_BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "http://localhost:11434/v1")
    LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "llama3.1:8b")

    client = OpenAI(
        base_url=LOCAL_LLM_BASE_URL,
        api_key="ollama"
    )

    ACTIVE_MODEL = LOCAL_LLM_MODEL

else:
    client = OpenAI(
        api_key=os.getenv("OPENAI_API_KEY")
    )

    ACTIVE_MODEL = "gpt-4o-mini"
# --------------------------
# Flask app setup
# --------------------------
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")



# --------------------------
# Demo users (replace with DB later)
# --------------------------
USERS = {
    "student1": {"password": "gate123", "name": "Student 1"},
    "student2": {"password": "gate123", "name": "Student 2"},
}

MOCK_TESTS = [
    {"id": "MT-1", "name": "Mock Test 1 (GATE AE Style)", "durationMin": 180},
    {"id": "MT-2", "name": "Mock Test 2 (GATE AE Style)", "durationMin": 180},
    {"id": "MT-3", "name": "Mock Test 3 (GATE AE Style)", "durationMin": 180},
]

BLUEPRINT = {
    "totalQ": 65,
    "marks1": 30,
    "marks2": 35,
    "typeCounts": {"MCQ": 35, "MSQ": 10, "NAT": 20},
    "diffCounts": {"E": 20, "M": 35, "H": 10},
}

# How many variants to generate per paper
VARIANT_TARGETS = {"count_total": 8, "count_1mark": 4, "count_2mark": 4}

# In-memory attempts (demo). Use DB/Redis in production.
ATTEMPTS: Dict[str, Dict[str, Any]] = {}


# ==========================
# Utility
# ==========================
def now_ts() -> int:
    return int(time.time())


def require_login() -> Tuple[bool, Optional[Dict[str, Any]]]:
    uid = session.get("userId")
    if not uid or uid not in USERS:
        return False, None
    return True, {"userId": uid, "name": USERS[uid]["name"]}


def shuffle_list(xs: List[Any]) -> List[Any]:
    ys = xs[:]
    random.shuffle(ys)
    return ys


def normalize_msq(v: Any) -> List[str]:
    if not isinstance(v, list):
        return []
    return sorted([str(x) for x in v])


def arrays_equal(a: List[str], b: List[str]) -> bool:
    return a == b


def is_correct(q: Dict[str, Any], resp_val: Any) -> bool:
    if resp_val is None:
        return False
    qtype = q["type"]
    if qtype == "MCQ":
        return str(resp_val) == str(q["answer"])
    if qtype == "MSQ":
        return arrays_equal(normalize_msq(resp_val), normalize_msq(q["answer"]))
    # NAT
    tol = float(q.get("tolerance", 0.01))
    try:
        return abs(float(resp_val) - float(q["answer"])) <= tol
    except Exception:
        return False


def calc_score(paper_full: List[Dict[str, Any]], responses: Dict[str, Any]) -> Dict[str, Any]:
    total_marks = 0.0
    obtained = 0.0
    correct = wrong = unattempted = 0

    for q in paper_full:
        total_marks += float(q["marks"])
        qid = q["id"]
        resp = responses.get(qid, None)

        if resp is None:
            unattempted += 1
            continue

        ok = is_correct(q, resp)
        if ok:
            correct += 1
            obtained += float(q["marks"])
        else:
            wrong += 1
            # Negative marking only for MCQ (GATE-like)
            if q["type"] == "MCQ":
                obtained -= (1.0 / 3.0) if int(q["marks"]) == 1 else (2.0 / 3.0)

    return {
        "totalMarks": round(total_marks, 2),
        "obtained": round(obtained, 2),
        "correct": correct,
        "wrong": wrong,
        "unattempted": unattempted,
    }


def validate_pool(pool: List[Dict[str, Any]]) -> None:
    if not isinstance(pool, list) or len(pool) < 200:
        raise ValueError("LOCAL_POOL must be an array with at least 200 questions.")

    required = {"id", "topic", "difficulty", "type", "marks", "question", "answer"}
    for i, q in enumerate(pool[:200]):
        missing = required - set(q.keys())
        if missing:
            raise ValueError(f"Pool question missing fields at index {i}: {sorted(list(missing))}")

        if q["type"] in ("MCQ", "MSQ"):
            if not isinstance(q.get("options", None), list) or len(q["options"]) != 4:
                raise ValueError(f"MCQ/MSQ must have exactly 4 options (index {i})")

        if q["type"] == "NAT":
            q.setdefault("tolerance", 0.01)
            q.setdefault("decimals", 2)


def make_targets() -> List[Dict[str, Any]]:
    diff_list = (
        ["E"] * BLUEPRINT["diffCounts"]["E"]
        + ["M"] * BLUEPRINT["diffCounts"]["M"]
        + ["H"] * BLUEPRINT["diffCounts"]["H"]
    )
    type_list = (
        ["MCQ"] * BLUEPRINT["typeCounts"]["MCQ"]
        + ["MSQ"] * BLUEPRINT["typeCounts"]["MSQ"]
        + ["NAT"] * BLUEPRINT["typeCounts"]["NAT"]
    )
    marks_list = ([1] * BLUEPRINT["marks1"] + [2] * BLUEPRINT["marks2"])

    targets = []
    for i in range(BLUEPRINT["totalQ"]):
        targets.append({"difficulty": diff_list[i], "type": type_list[i], "marks": marks_list[i]})
    return shuffle_list(targets)


def pick_question(pool: List[Dict[str, Any]], used: set, filt) -> Optional[Dict[str, Any]]:
    candidates = [q for q in pool if q["id"] not in used and filt(q)]
    return random.choice(candidates) if candidates else None


def build_paper_from_pool(pool: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    used = set()
    result = []
    targets = make_targets()

    for tgt in targets:
        d, t, m = tgt["difficulty"], tgt["type"], tgt["marks"]

        q = pick_question(pool, used, lambda q: q["difficulty"] == d and q["type"] == t and int(q["marks"]) == m)
        if not q:
            q = pick_question(pool, used, lambda q: q["type"] == t and int(q["marks"]) == m)
        if not q:
            q = pick_question(pool, used, lambda q: q["difficulty"] == d and int(q["marks"]) == m)
        if not q:
            q = pick_question(pool, used, lambda q: int(q["marks"]) == m)
        if not q:
            q = pick_question(pool, used, lambda q: True)
        if not q:
            raise RuntimeError("Pool too small to build a paper.")

        used.add(q["id"])
        result.append(q)

    return shuffle_list(result)


def strip_answers_for_exam(q: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": q["id"],
        "topic": q["topic"],
        "difficulty": q.get("difficulty", "M"),
        "type": q["type"],
        "marks": q["marks"],
        "question": q["question"],
        "options": q.get("options", None),
        "decimals": q.get("decimals", 2),
        "tolerance": q.get("tolerance", 0.01),
        "isVariant": bool(q.get("isVariant", False)),
        "baseId": q.get("baseId", None),
    }

    return make_json_safe(cleaned)

def compute_simple_feedback(paper_full: List[Dict[str, Any]], responses: Dict[str, Any]) -> Dict[str, float]:
    positive_marks = 0.0
    negative_marks = 0.0

    for q in paper_full:
        qid = q["id"]
        resp = responses.get(qid, None)

        if resp is None:
            continue

        if is_correct(q, resp):
            positive_marks += float(q["marks"])
        else:
            if q["type"] == "MCQ":
                if float(q["marks"]) == 1:
                    negative_marks += (1.0 / 3.0)
                elif float(q["marks"]) == 2:
                    negative_marks += (2.0 / 3.0)

    total_marks = round(positive_marks - negative_marks, 2)

    return {
        "totalMarks": total_marks,
        "positiveMarks": round(positive_marks, 2),
        "negativeMarks": round(negative_marks, 2),
    }

def compute_simple_feedback(paper_full: List[Dict[str, Any]], responses: Dict[str, Any]) -> Dict[str, float]:
    positive_marks = 0.0
    negative_marks = 0.0

    for q in paper_full:
        qid = q["id"]
        resp = responses.get(qid, None)

        if resp is None:
            continue

        if is_correct(q, resp):
            positive_marks += float(q["marks"])
        else:
            if q["type"] == "MCQ":
                if float(q["marks"]) == 1:
                    negative_marks += (1.0 / 3.0)
                elif float(q["marks"]) == 2:
                    negative_marks += (2.0 / 3.0)

    total_marks = round(positive_marks - negative_marks, 2)

    return {
        "totalMarks": total_marks,
        "positiveMarks": round(positive_marks, 2),
        "negativeMarks": round(negative_marks, 2),
    }

# ==========================
# Variant mode (optional)
# ==========================

def choose_variant_candidates(paper: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    one_mark = [q for q in paper if int(q["marks"]) == 1]
    two_mark = [q for q in paper if int(q["marks"]) == 2]
    one_pick = shuffle_list(one_mark)[:VARIANT_TARGETS["count_1mark"]]
    two_pick = shuffle_list(two_mark)[:VARIANT_TARGETS["count_2mark"]]
    return one_pick + two_pick


def _extract_text_from_openai_response(resp: Any) -> str:
    # chat.completions path
    try:
        return resp.choices[0].message.content or ""
    except Exception:
        return ""


def llm_variant_questions(base_questions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Generates variants for base_questions using OpenAI if configured.
    Returns a list of variant dicts with keys:
      id, baseId, isVariant, question, options?, answer, solution, type, marks, topic, difficulty, tolerance?, decimals?
    """
    if client is None or not base_questions:
        return []

    payload = []
    for q in base_questions:
        payload.append(
            {
                "id": q["id"],
                "topic": q["topic"],
                "difficulty": q.get("difficulty", "M"),
                "type": q["type"],
                "marks": q["marks"],
                "question": q["question"],
                "options": q.get("options", None),
                "answer": q.get("answer", None),
                "tolerance": q.get("tolerance", 0.01),
                "decimals": q.get("decimals", 2),
                "solution": q.get("solution", ""),
            }
        )

    system_msg = (
        "You generate ORIGINAL GATE Aerospace mock-test variants.\n"
        "You will receive BASE questions as JSON.\n"
        "For each base question, create ONE variant:\n"
        "- Keep same topic, type, marks, difficulty approximately.\n"
        "- Make small numeric changes or slight conceptual twists.\n"
        "- Recompute answer and solution.\n"
        "- MCQ/MSQ must have EXACTLY 4 options.\n"
        "- NAT must be numeric with decimals=2 and tolerance=0.01.\n"
        "- Output ONLY valid JSON array (no markdown, no extra text).\n"
        "Schema per variant: {baseId, topic, difficulty, type, marks, question, options?, answer, solution, tolerance?, decimals?}"
    )
    user_msg = f"BASE:\n{json.dumps(payload, ensure_ascii=False)}"

    try:
        resp = client.chat.completions.create(
            model=ACTIVE_MODEL,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
        )
    except Exception as e:
        print("VARIANT MODE FAILED (chat.completions.create):", repr(e))
        return []

    text = _extract_text_from_openai_response(resp)
    if not text:
        print("VARIANT MODE FAILED: empty response content")
        return []

    # Parse JSON
    try:
        data = json.loads(text)
    except Exception as e:
        print("VARIANT MODE FAILED: JSON parse error:", repr(e))
        print("RAW LLM OUTPUT (first 1000 chars):", text[:1000])
        return []

    if not isinstance(data, list) or len(data) != len(base_questions):
        print("VARIANT MODE FAILED: expected list of same length.")
        return []

    # Normalize variants
    out: List[Dict[str, Any]] = []
    for i, v in enumerate(data):
        if not isinstance(v, dict):
            print("VARIANT MODE FAILED: variant not a dict:", v)
            return []

        base_id = str(v.get("baseId") or base_questions[i]["id"])

        vv = dict(v)
        vv["id"] = f"VAR-{uuid.uuid4().hex[:10]}"
        vv["isVariant"] = True
        vv["baseId"] = base_id

        # Ensure required fields exist
        for k in ["topic", "difficulty", "type", "marks", "question", "answer", "solution"]:
            if k not in vv:
                print("VARIANT MODE FAILED: missing key", k, "in", vv)
                return []

        if vv.get("type") in ("MCQ", "MSQ"):
            opts = vv.get("options", [])
            if not isinstance(opts, list) or len(opts) != 4:
                print("VARIANT MODE FAILED: MCQ/MSQ must have 4 options:", vv)
                return []

            # normalize to A/B/C/D prefix
            normalized = []
            for j, ot in enumerate(opts):
                ot_str = str(ot)
                if ot_str.strip().startswith(("A:", "B:", "C:", "D:")):
                    normalized.append(ot_str)
                else:
                    normalized.append(f"{['A','B','C','D'][j]}: {ot_str}")
            vv["options"] = normalized

            if vv["type"] == "MCQ":
                vv["answer"] = str(vv["answer"]).strip()
                if vv["answer"] not in ("A", "B", "C", "D"):
                    # allow "A:" forms
                    vv["answer"] = vv["answer"].replace(":", "").strip()[:1]
                if vv["answer"] not in ("A", "B", "C", "D"):
                    print("VARIANT MODE FAILED: MCQ answer must be one of A/B/C/D:", vv["answer"])
                    return []

            if vv["type"] == "MSQ":
                if not isinstance(vv["answer"], list):
                    print("VARIANT MODE FAILED: MSQ answer must be list:", vv["answer"])
                    return []
                vv["answer"] = sorted([str(x).strip()[:1] for x in vv["answer"]])
                for x in vv["answer"]:
                    if x not in ("A", "B", "C", "D"):
                        print("VARIANT MODE FAILED: MSQ answer invalid:", vv["answer"])
                        return []

        if vv.get("type") == "NAT":
            vv["decimals"] = 2
            vv["tolerance"] = float(vv.get("tolerance", 0.01))
            try:
                vv["answer"] = round(float(vv["answer"]), 2)
            except Exception:
                print("VARIANT MODE FAILED: NAT answer not numeric:", vv["answer"])
                return []

        out.append(vv)

    return out


def apply_variants(paper: List[Dict[str, Any]], variants: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not variants:
        return paper
    by_base = {v["baseId"]: v for v in variants if "baseId" in v}
    return [by_base.get(q["id"], q) for q in paper]


# ==========================
# Frontend HTML
# ==========================
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>GATE AE Mock Tests</title>

  <!-- MathJax -->
  <script>
    window.MathJax = {
      tex: { inlineMath: [['\\(','\\)'], ['$', '$']], displayMath: [['\\[','\\]']] },
      svg: { fontCache: 'global' }
    };
  </script>
  <script defer src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"></script>

  <style>
    :root{
      --bg:#ffffff; --card:#ffffff; --text:#000; --muted:#333;
      --border:rgba(0,0,0,0.12);
      --primary:#2563eb; --danger:#dc2626; --warn:#f59e0b;
      --c-notvisited:#e5e7eb; --c-visited:#93c5fd; --c-answered:#86efac; --c-review:#fcd34d;
      --shadow:0 10px 24px rgba(0,0,0,0.08); --radius:16px;
    }
    *{box-sizing:border-box}
    body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial;background:var(--bg);color:var(--text)}
    .topbar{position:sticky;top:0;z-index:10;display:flex;align-items:center;justify-content:space-between;padding:14px 18px;background:#fff;border-bottom:1px solid var(--border)}
    .brand{display:flex;gap:12px;align-items:center}
    .logo{width:44px;height:44px;border-radius:12px;display:grid;place-items:center;font-weight:900;color:#fff;background:linear-gradient(135deg,#2563eb,#16a34a);box-shadow:var(--shadow)}
    .title{font-weight:900;font-size:16px}
    .subtitle{color:var(--muted);font-size:12px;margin-top:2px}
    .top-actions{display:flex;align-items:center;gap:12px}
    .timer-box{padding:10px 14px;border:1px solid var(--border);border-radius:14px;background:#fff}
    .timer-label{font-size:11px;color:var(--muted)}
    .timer{font-size:18px;font-weight:900;margin-top:3px}
    .layout{display:grid;grid-template-columns:340px 1fr;gap:16px;padding:16px;max-width:1200px;margin:0 auto}
    .card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);padding:14px}
    .card h3{margin:0 0 12px 0;font-size:16px}
    .row{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:10px 0}
    label{color:var(--muted);font-size:13px}
    input{width:100%;background:#fff;color:var(--text);border:1px solid var(--border);border-radius:12px;padding:10px;outline:none}
    .btn{border:1px solid var(--border);background:#fff;color:var(--text);padding:10px 12px;border-radius:14px;cursor:pointer;font-weight:800}
    .btn.primary{background:rgba(37,99,235,0.10);border-color:rgba(37,99,235,0.35)}
    .btn.danger{background:rgba(220,38,38,0.10);border-color:rgba(220,38,38,0.35);color:#7f1d1d}
    .btn.warn{background:rgba(245,158,11,0.16);border-color:rgba(245,158,11,0.40)}
    .btn:disabled{opacity:.55;cursor:not-allowed}
    .hidden{display:none !important}

    .legend{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px}
    .tag{font-size:11px;padding:6px 8px;border-radius:999px;border:1px solid var(--border);font-weight:800;color:#111}
    .tag.notvisited{background:var(--c-notvisited)}
    .tag.visited{background:var(--c-visited)}
    .tag.answered{background:var(--c-answered)}
    .tag.review{background:var(--c-review)}
    .palette{display:grid;grid-template-columns:repeat(8,1fr);gap:8px}
    .pal-btn{height:34px;border-radius:12px;border:1px solid var(--border);background:var(--c-notvisited);color:#111;cursor:pointer;font-weight:900}
    .pal-btn.active{outline:2px solid rgba(37,99,235,0.55)}
    .pal-btn.notvisited{background:var(--c-notvisited)}
    .pal-btn.visited{background:var(--c-visited)}
    .pal-btn.answered{background:var(--c-answered)}
    .pal-btn.review{background:var(--c-review)}

    .q-top{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}
    .q-meta{display:flex;gap:8px;flex-wrap:wrap}
    .pill{font-size:12px;padding:7px 10px;border-radius:999px;border:1px solid var(--border);background:#fff;font-weight:900;color:#111}
    .pill.ghost{color:var(--muted);font-weight:800}
    .q-nav{display:flex;gap:8px}
    .q-text{font-size:16px;line-height:1.55;padding:12px 10px;border-radius:14px;background:#fff;border:1px solid var(--border)}
    .options{margin-top:12px;display:flex;flex-direction:column;gap:10px}
    .option{padding:10px 10px;border-radius:14px;border:1px solid var(--border);background:#fff;display:flex;gap:10px;align-items:flex-start}
    .option input{margin-top:4px;width:auto}
    .option .opt-label{width:26px;height:26px;border-radius:10px;display:grid;place-items:center;background:#f3f4f6;border:1px solid var(--border);font-weight:900;flex:0 0 auto}
    .option .opt-text{color:#111;line-height:1.5}
    .nat{display:flex;gap:10px;align-items:center;padding:12px 10px;border-radius:14px;border:1px solid var(--border);background:#fff}
    .nat input{width:240px}
    .q-actions{display:flex;gap:10px;margin-top:14px;flex-wrap:wrap}
    .feedback{margin-top:12px;padding:12px;border-radius:14px;border:1px solid var(--border);background:#fff;color:var(--muted);line-height:1.55}

    .result-summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:12px}
    .stat{padding:12px;border-radius:16px;border:1px solid var(--border);background:#fff}
    .stat .k{color:var(--muted);font-size:12px}
    .stat .v{font-size:18px;font-weight:900;margin-top:5px}
    .solution-item{border:1px solid var(--border);border-radius:16px;padding:12px;background:#fff;margin:10px 0}
    .solution-item .head{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:6px}
    .badge{font-size:12px;font-weight:900;padding:6px 10px;border-radius:999px;border:1px solid var(--border)}
    .badge.correct{background:var(--c-answered)}
    .badge.wrong{background:#fecaca}
    .badge.unattempted{background:var(--c-notvisited)}
    .badge.variant{background:#ddd6fe}

    @media (max-width:980px){
      .layout{grid-template-columns:1fr}
      .palette{grid-template-columns:repeat(10,1fr)}
      .row{flex-direction:column;align-items:stretch}
    }
  </style>

  <script src="/local_pool.js"></script>
</head>

<body>
<!-- SASTRA UNIVERSITY BANNER -->
<div style="width:100%;text-align:center;background:white;border-bottom:2px solid #ddd;">
  <img src="/sastra_banner.jpg" style="width:100%;max-height:90px;object-fit:contain;">
</div>
  <header class="topbar">
    <div class="brand">
      <div class="logo">G</div>
      <div>
        <div class="title">GATE AE Mock Tests</div>
        <div class="subtitle">Login → Choose Mock → Attempt → Submit → Result Published (MathJax)</div>
      </div>
    </div>

    <div class="top-actions">
      <div class="timer-box hidden" id="timerBox">
        <div class="timer-label">Time Left</div>
        <div id="timer" class="timer">03:00:00</div>
      </div>
      <button id="logoutBtn" class="btn hidden" type="button">Logout</button>
      <button id="submitBtn" class="btn danger hidden" type="button">Submit Test</button>
    </div>
  </header>

  <main class="layout">
    <aside class="card">
      <section id="loginView">
        <h3>Student Login</h3>
        <div class="row"><label>User ID</label></div>
        <input id="loginUser" placeholder="e.g., student1" />
        <div class="row" style="margin-top:12px;"><label>Password</label></div>
        <input id="loginPass" type="password" placeholder="e.g., gate123" />
        <button id="loginBtn" class="btn primary" style="width:100%;margin-top:12px;" type="button">Login</button>
        <div class="feedback">
          Demo accounts (server-side):<br>
          <b>student1</b> / gate123<br>
          <b>student2</b> / gate123
        </div>
      </section>

      <section id="testListView" class="hidden">
        <h3>Available Mock Tests</h3>
        <div class="feedback" id="welcomeBox"></div>

        <div class="feedback" style="margin-top:10px;">
          <label style="display:flex;gap:10px;align-items:center;cursor:pointer;">
            <input id="variantModeChk" type="checkbox" checked style="width:auto;">
            <span><b>Variant Mode</b>: Small numeric changes + updated solution (new each attempt)</span>
          </label>
          <div style="margin-top:6px;color:var(--muted);font-size:12px;">
            Variant Mode works only if backend has <b>OPENAI_API_KEY</b>; otherwise paper is generated from LOCAL_POOL only.
          </div>
        </div>

        <div id="testList"></div>

        <div class="feedback">
          Each attempt is generated server-side:
          <br>✅ 65 questions
          <br>✅ total marks 100 (30×1 + 35×2)
          <br>✅ shuffled every attempt
        </div>
      </section>

      <section id="paletteView" class="hidden">
        <h3>Question Palette</h3>
        <div class="legend">
          <span class="tag answered">Answered</span>
          <span class="tag review">Review</span>
          <span class="tag notvisited">Not Visited</span>
          <span class="tag visited">Visited</span>
        </div>
        <div id="palette" class="palette"></div>
      </section>
    </aside>

    <section class="card">
      <section id="testView" class="hidden">
        <div class="q-top">
          <div class="q-meta">
            <span id="qNo" class="pill">Q1</span>
            <span id="qType" class="pill ghost">MCQ</span>
            <span id="qTopic" class="pill ghost">—</span>
            <span id="qMarks" class="pill ghost">—</span>
            <span id="paperInfo" class="pill ghost">65Q | 100</span>
          </div>
          <div class="q-nav">
            <button id="prevBtn" class="btn" type="button">Prev</button>
            <button id="nextBtn" class="btn" type="button">Next</button>
          </div>
        </div>

        <div id="questionText" class="q-text"></div>
        <div id="optionsBox" class="options"></div>

        <div class="q-actions">
          <button id="saveBtn" class="btn primary" type="button">Save Answer</button>
          <button id="clearBtn" class="btn" type="button">Clear</button>
          <button id="reviewBtn" class="btn warn" type="button">Mark for Review</button>
        </div>

        <div id="feedback" class="feedback"></div>
      </section>

      <section id="resultsView" class="hidden">
        <h3>Result Summary</h3>
        <div id="resultSummary" class="result-summary"></div>
        <div id="aiFeedbackBox" class="feedback" style="margin-bottom:12px;"></div>
        <div id="solutionList"></div>

        <div id="doubtAssistantBox" class="feedback" style="margin-top:16px;">
          <h3 style="margin-top:0;">AI Doubt Assistant</h3>
          <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px;">
            <input id="doubtQuestionNo" type="number" min="1" placeholder="Question No (e.g. 12)" style="width:180px;">
            <input id="doubtInput" type="text" placeholder="Ask your doubt here..." style="flex:1;min-width:240px;">
            <button id="doubtSendBtn" class="btn primary" type="button">Ask AI</button>
          </div>
          <div id="doubtChatLog" style="max-height:300px;overflow-y:auto;border:1px solid var(--border);border-radius:12px;padding:10px;background:#fff;"></div>
        </div>

        <button id="backBtn" class="btn primary" style="margin-top:10px;" type="button">Back to Test List</button>
      </section>

      <section id="emptyView">
        <div class="feedback">Login to see your mock tests.</div>
      </section>
    </section>
  </main>

  <script>
    function typesetMath(){
      if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise();
    }

    const loginView = document.getElementById("loginView");
    const testListView = document.getElementById("testListView");
    const paletteView = document.getElementById("paletteView");
    const testView = document.getElementById("testView");
    const resultsView = document.getElementById("resultsView");
    const emptyView = document.getElementById("emptyView");

    const timerBox = document.getElementById("timerBox");
    const timerEl = document.getElementById("timer");
    const submitBtn = document.getElementById("submitBtn");
    const logoutBtn = document.getElementById("logoutBtn");

    const loginUser = document.getElementById("loginUser");
    const loginPass = document.getElementById("loginPass");
    const loginBtn = document.getElementById("loginBtn");

    const welcomeBox = document.getElementById("welcomeBox");
    const testListEl = document.getElementById("testList");

    const variantModeChk = document.getElementById("variantModeChk");

    const paletteEl = document.getElementById("palette");
    const qNoEl = document.getElementById("qNo");
    const qTypeEl = document.getElementById("qType");
    const qTopicEl = document.getElementById("qTopic");
    const qMarksEl = document.getElementById("qMarks");
    const paperInfoEl = document.getElementById("paperInfo");
    const questionTextEl = document.getElementById("questionText");
    const optionsBoxEl = document.getElementById("optionsBox");
    const feedbackEl = document.getElementById("feedback");

    const prevBtn = document.getElementById("prevBtn");
    const nextBtn = document.getElementById("nextBtn");
    const saveBtn = document.getElementById("saveBtn");
    const clearBtn = document.getElementById("clearBtn");
    const reviewBtn = document.getElementById("reviewBtn");

    const resultSummaryEl = document.getElementById("resultSummary");
    const solutionListEl = document.getElementById("solutionList");
    const aiFeedbackBox = document.getElementById("aiFeedbackBox");
    const backBtn = document.getElementById("backBtn");

    const doubtQuestionNoEl = document.getElementById("doubtQuestionNo");
    const doubtInputEl = document.getElementById("doubtInput");
    const doubtSendBtn = document.getElementById("doubtSendBtn");
    const doubtChatLog = document.getElementById("doubtChatLog");

    function appendChatMessage(sender, text){
      const div = document.createElement("div");
      div.style.marginBottom = "10px";
      div.style.padding = "10px";
      div.style.borderRadius = "10px";
      div.style.background = sender === "You" ? "#eef6ff" : "#f8f8f8";
      div.innerHTML = `<b>${sender}:</b><div style="white-space:pre-line;margin-top:4px;">${text}</div>`;
      doubtChatLog.appendChild(div);
      doubtChatLog.scrollTop = doubtChatLog.scrollHeight;
      typesetMath();
    }

    doubtSendBtn.addEventListener("click", async ()=>{

  const message = doubtInputEl.value.trim();
  const questionNo = doubtQuestionNoEl.value.trim();

  if (!attemptId){
    alert("No submitted attempt found.");
    return;
  }

  if (!message){
    alert("Please type your doubt.");
    return;
  }

  appendChatMessage("You", message);
  doubtInputEl.value = "";

  try{

    console.log("Sending doubt request...");

    const data = await api("/api/ask-doubt","POST",{
      attemptId: attemptId,
      questionNo: questionNo ? Number(questionNo) : null,
      message: message
    });

    console.log("AI RESPONSE:", data);

    if (data && data.reply){
      appendChatMessage("AI Tutor", data.reply);
    }
    else{
      appendChatMessage("AI Tutor", "No reply received from AI.");
    }

  }
  catch(e){

    console.error("AI ERROR:", e);

    appendChatMessage(
      "AI Tutor",
      e.message || "Sorry, I could not process your doubt right now."
    );

  }

});

    let user = null;
    let tests = [];

    let attemptId = null;
    let testMeta = null;

    let questions = [];
    let currentIndex = 0;
    let timerId = null;
    let timeLeftSec = 0;

    const status = {};
    const responses = {};

    function show(el){ el.classList.remove("hidden"); }
    function hide(el){ el.classList.add("hidden"); }
    function setFeedback(msg){ feedbackEl.innerHTML = msg; typesetMath(); }

    function fmtTime(sec){
      const h = String(Math.floor(sec/3600)).padStart(2,"0");
      const m = String(Math.floor((sec%3600)/60)).padStart(2,"0");
      const s = String(sec%60).padStart(2,"0");
      return `${h}:${m}:${s}`;
    }

    async function api(path, method="GET", body=null){
      const res = await fetch(path, {
        method,
        headers: body ? {"Content-Type":"application/json"} : {},
        body: body ? JSON.stringify(body) : null,
        credentials: "include"
      });
      const data = await res.json().catch(()=>({ok:false,error:"Bad JSON"}));
      if (!res.ok) throw new Error(data.error || "Request failed");
      return data;
    }

    function resetAll(){
      attemptId=null; testMeta=null;
      questions=[]; currentIndex=0;
      for (const k of Object.keys(status)) delete status[k];
      for (const k of Object.keys(responses)) delete responses[k];
      clearInterval(timerId); timerId=null;
      paletteEl.innerHTML="";
      optionsBoxEl.innerHTML="";
      questionTextEl.innerHTML="";
    }

    function showLogin(){
      user=null; tests=[];
      resetAll();

      show(loginView);
      hide(testListView);
      hide(paletteView);
      hide(testView);
      hide(resultsView);
      show(emptyView);

      hide(timerBox);
      hide(submitBtn);
      hide(logoutBtn);
    }

    async function showTestList(){
      const data = await api("/api/tests","GET");
      user = data.user;
      tests = data.tests;

      hide(loginView);
      hide(testView);
      hide(resultsView);
      hide(paletteView);

      show(testListView);
      show(emptyView);

      hide(timerBox);
      hide(submitBtn);
      show(logoutBtn);

      welcomeBox.innerHTML = `Welcome, <b>${user.name}</b> (${user.userId}).`;

      testListEl.innerHTML = "";
      tests.forEach(t=>{
        const div=document.createElement("div");
        div.className="card";
        div.style.margin="10px 0";
        div.innerHTML = `
          <div style="display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;">
            <div>
              <div style="font-weight:900;">${t.name}</div>
              <div style="color:var(--muted);font-size:12px;margin-top:3px;">
                Duration: ${t.durationMin} min
              </div>
            </div>
            <div style="display:flex;gap:8px;align-items:center;">
              <button class="btn primary" type="button">Start Attempt</button>
            </div>
          </div>
        `;
        div.querySelector("button").addEventListener("click", ()=> startAttempt(t.id));
        testListEl.appendChild(div);
      });
    }

    loginBtn.addEventListener("click", async ()=>{
      try{
        await api("/api/login","POST",{userId:loginUser.value.trim(), password:loginPass.value});
        await showTestList();
      }catch(e){ alert(e.message); }
    });

    logoutBtn.addEventListener("click", async ()=>{
      try{ await api("/api/logout","POST",{}); }catch(e){}
      showLogin();
    });

    function startTimer(minutes){
      clearInterval(timerId);
      timeLeftSec = minutes*60;
      timerEl.textContent = fmtTime(timeLeftSec);

      timerId=setInterval(()=>{
        timeLeftSec--;
        timerEl.textContent = fmtTime(timeLeftSec);
        if(timeLeftSec<=0){
          clearInterval(timerId); timerId=null;
          timerEl.textContent="00:00:00";
          submitNow(true);
        }
      },1000);
    }

    function renderPalette(){
      paletteEl.innerHTML="";
      questions.forEach((q,idx)=>{
        const btn=document.createElement("button");
        btn.className="pal-btn notvisited";
        btn.textContent=String(idx+1);
        btn.addEventListener("click",()=>goTo(idx));
        paletteEl.appendChild(btn);
      });
      updatePalette();
    }

    function updatePalette(){
      const buttons=paletteEl.querySelectorAll(".pal-btn");
      buttons.forEach((btn,idx)=>{
        const q=questions[idx];
        const st=status[q.id];

        btn.classList.remove("active","answered","review","visited","notvisited");
        if(!st.visited) btn.classList.add("notvisited"); else btn.classList.add("visited");
        if(st.answered) btn.classList.add("answered");
        if(st.review) btn.classList.add("review");
        if(idx===currentIndex) btn.classList.add("active");
      });
    }

    function applyLocking(){
      const q=questions[currentIndex];
      const st=status[q.id];
      const lock = st.answered && !st.review;

      optionsBoxEl.querySelectorAll("input").forEach(inp=> inp.disabled=lock);

      saveBtn.disabled = lock;
      clearBtn.disabled = lock;
      reviewBtn.disabled = false;
    }

    function renderQuestion(){
      const q=questions[currentIndex];
      status[q.id].visited=true;

      qNoEl.textContent=`Q${currentIndex+1}`;
      qTypeEl.textContent=q.type + (q.isVariant ? " • VAR" : "");
      qTopicEl.textContent=q.topic;
      qMarksEl.textContent=`${q.marks} mark${q.marks>1?"s":""}`;

      let badge = "";
      if(q.isVariant) badge = `<span class="badge variant" style="margin-left:8px;">AI-Variant</span>`;
      questionTextEl.innerHTML = q.question + badge;

      optionsBoxEl.innerHTML="";
      const saved = responses[q.id];

      if(q.type==="MCQ"){
        q.options.forEach((optText,i)=>{
          const letter=["A","B","C","D"][i];
          const row=document.createElement("label");
          row.className="option";

          const radio=document.createElement("input");
          radio.type="radio"; radio.name="mcq"; radio.value=letter;
          if(saved===letter) radio.checked=true;

          const b=document.createElement("div");
          b.className="opt-label"; b.textContent=letter;

          const txt=document.createElement("div");
          txt.className="opt-text";
          txt.innerHTML = optText.replace(/^([A-D]:\s*)/, "");

          row.appendChild(radio); row.appendChild(b); row.appendChild(txt);
          optionsBoxEl.appendChild(row);
        });
      } else if(q.type==="MSQ"){
        q.options.forEach((optText,i)=>{
          const letter=["A","B","C","D"][i];
          const row=document.createElement("label");
          row.className="option";

          const chk=document.createElement("input");
          chk.type="checkbox"; chk.name="msq"; chk.value=letter;
          if(Array.isArray(saved) && saved.includes(letter)) chk.checked=true;

          const b=document.createElement("div");
          b.className="opt-label"; b.textContent=letter;

          const txt=document.createElement("div");
          txt.className="opt-text";
          txt.innerHTML = optText.replace(/^([A-D]:\s*)/, "");

          row.appendChild(chk); row.appendChild(b); row.appendChild(txt);
          optionsBoxEl.appendChild(row);
        });
      } else {
        const wrap=document.createElement("div");
        wrap.className="nat";

        const label=document.createElement("div");
        label.style.color="var(--muted)";
        label.style.fontWeight="800";
        label.textContent="Enter numeric answer (2 decimals):";

        const inp=document.createElement("input");
        inp.type="number"; inp.step="0.01";
        inp.placeholder="e.g., 12.34";
        inp.value=(saved!==null && saved!==undefined) ? String(saved) : "";

        wrap.appendChild(label); wrap.appendChild(inp);
        optionsBoxEl.appendChild(wrap);
      }

      updatePalette();
      applyLocking();
      typesetMath();
    }

    function goTo(idx){
      if(idx<0||idx>=questions.length) return;
      currentIndex=idx;
      renderQuestion();
    }
    function next(){ if(currentIndex<questions.length-1) goTo(currentIndex+1); }
    function prev(){ if(currentIndex>0) goTo(currentIndex-1); }

    prevBtn.addEventListener("click", prev);
    nextBtn.addEventListener("click", next);

    function readCurrentSelection(){
      const q=questions[currentIndex];

      if(q.type==="MCQ"){
        const checked=optionsBoxEl.querySelector('input[type="radio"]:checked');
        return checked ? checked.value : null;
      }
      if(q.type==="MSQ"){
        const checked=[...optionsBoxEl.querySelectorAll('input[type="checkbox"]:checked')].map(x=>x.value);
        return checked.length ? checked.sort() : null;
      }
      const inp=optionsBoxEl.querySelector('input[type="number"]');
      const v=inp.value.trim();
      if(!v) return null;
      const num=Number(v);
      if(Number.isNaN(num)) return null;
      const rounded=Math.round(num*100)/100;
      inp.value=rounded.toFixed(2);
      return rounded;
    }

    saveBtn.addEventListener("click", ()=>{
      const q=questions[currentIndex];
      const val=readCurrentSelection();
      responses[q.id]=val;
      status[q.id].answered = (val!==null);
      setFeedback(val===null ? "No answer selected." : "Answer saved. (Locked unless marked for review)");
      updatePalette();
      applyLocking();
    });

    clearBtn.addEventListener("click", ()=>{
      const q=questions[currentIndex];
      responses[q.id]=null;
      status[q.id].answered=false;

      optionsBoxEl.querySelectorAll("input").forEach(inp=>{
        if(inp.type==="radio"||inp.type==="checkbox") inp.checked=false;
        else inp.value="";
      });

      setFeedback("Cleared.");
      updatePalette();
      applyLocking();
    });

    reviewBtn.addEventListener("click", ()=>{
      const q=questions[currentIndex];
      status[q.id].review=!status[q.id].review;

      setFeedback(status[q.id].review
        ? "Marked for review. Editing unlocked for this question."
        : "Review removed. If already saved, it will be locked again.");

      updatePalette();
      applyLocking();
    });

    async function startAttempt(testId){
  resetAll();

  try{
    if (!window.LOCAL_POOL || !Array.isArray(window.LOCAL_POOL) || window.LOCAL_POOL.length < 200) {
      throw new Error("LOCAL_POOL not loaded properly. Hard refresh (Ctrl+Shift+R) and check /local_pool.js in Network tab.");
    }

    const data = await api("/api/generate-paper","POST",{
      testId,
      pool: window.LOCAL_POOL,
      variantMode: variantModeChk ? variantModeChk.checked : true
    });

    attemptId = data.attemptId;
    testMeta = data.test;
    questions = data.paper;

    questions.forEach(q=>{
      status[q.id]={visited:false,answered:false,review:false};
      responses[q.id]=null;
    });

    hide(testListView); hide(resultsView); hide(emptyView);
    show(testView); show(paletteView);

    show(timerBox); show(submitBtn); show(logoutBtn);

    paperInfoEl.textContent = `65Q | 100 • ${testMeta.id} • Variants: ${data.variantsApplied}`;

    renderPalette();
    renderQuestion();
    startTimer(testMeta.durationMin);

    setFeedback(`Attempt started. <b>Save</b> locks answers unless you <b>Mark for Review</b>.`);
  }catch(e){
    console.error(e);
    alert(e.message);
  }
}
window.startAttempt = startAttempt;

    submitBtn.addEventListener("click", ()=>{
      if(confirm("Submit test now? You cannot change answers after submit.")) submitNow(false);
    });

    async function submitNow(auto=false){
      if(!attemptId) return;
      clearInterval(timerId); timerId=null;

      try{
        const data = await api("/api/submit","POST",{ attemptId, responses });

        hide(testView); hide(paletteView); hide(emptyView);
        show(resultsView);

        hide(timerBox); hide(submitBtn);

        const sc=data.score;
        const pf = data.performanceFeedback || {
          totalMarks: sc.obtained,
          positiveMarks: sc.obtained,
          negativeMarks: 0
        };
        const answered = sc.correct + sc.wrong;
        const accuracy = answered ? ((sc.correct/answered)*100).toFixed(1) : "0.0";

        resultSummaryEl.innerHTML = `
          <div class="stat"><div class="k">Status</div><div class="v">${auto ? "Auto-submitted" : "Submitted"}</div></div>
          <div class="stat"><div class="k">Score</div><div class="v">${sc.obtained.toFixed(2)} / ${sc.totalMarks}</div></div>
          <div class="stat"><div class="k">Accuracy</div><div class="v">${accuracy}%</div></div>
          <div class="stat"><div class="k">Correct</div><div class="v">${sc.correct}</div></div>
          <div class="stat"><div class="k">Wrong</div><div class="v">${sc.wrong}</div></div>
          <div class="stat"><div class="k">Unattempted</div><div class="v">${sc.unattempted}</div></div>
        `;

        aiFeedbackBox.innerHTML = `
          <h3 style="margin-top:0;color:#800020;">Performance Feedback</h3>
          <div style="line-height:1.8;">
            <b>Total Marks:</b> ${pf.totalMarks}<br>
            <b>Total Positive Marks:</b> ${pf.positiveMarks}<br>
            <b>Total Negative Marks:</b> ${pf.negativeMarks}
          </div>
        `;

        doubtChatLog.innerHTML = "";
        doubtQuestionNoEl.value = "";
        doubtInputEl.value = "";

        solutionListEl.innerHTML="";
        data.solutions.forEach(s=>{
          const badgeClass = (s.status==="Correct") ? "correct" : (s.status==="Wrong" ? "wrong" : "unattempted");
          const correctText = Array.isArray(s.correctAnswer)
            ? s.correctAnswer.join(", ")
            : (typeof s.correctAnswer === "number" ? s.correctAnswer.toFixed(2) : String(s.correctAnswer));
          const yourText = (s.yourAnswer===null || s.yourAnswer===undefined)
            ? "-"
            : (Array.isArray(s.yourAnswer) ? s.yourAnswer.join(", ") : String(s.yourAnswer));

          const varBadge = s.isVariant ? `<span class="badge variant">AI-Variant</span>` : "";

          const div=document.createElement("div");
          div.className="solution-item";
          div.style.cursor = "pointer";
          div.innerHTML=`
            <div class="head">
              <div><b>Q${s.qNo}</b> • ${s.topic} • ${s.type} • ${s.marks}M • Diff ${s.difficulty} ${varBadge}</div>
              <div class="badge ${badgeClass}">${s.status}</div>
            </div>
            <div style="margin:6px 0 10px 0;">${s.question}</div>
            <div style="font-size:13px;line-height:1.6;">
              <b>Your answer:</b> ${yourText} &nbsp; | &nbsp; <b>Correct:</b> ${correctText}
            </div>
            <div style="margin-top:8px;line-height:1.6;">
              <b>Solution:</b> ${s.solution}
            </div>
          `;
          div.addEventListener("click", ()=>{
            doubtQuestionNoEl.value = s.qNo;
            doubtInputEl.focus();
          });
          solutionListEl.appendChild(div);
        });

        typesetMath();
        alert("Result published. You can go back and start another attempt.");
      }catch(e){
        console.error(e);
        alert(e.message);
      }
    }

    backBtn.addEventListener("click", async ()=>{ await showTestList(); });

    document.addEventListener("keydown",(e)=>{
      if(testView.classList.contains("hidden")) return;
      if(e.key==="ArrowRight") next();
      if(e.key==="ArrowLeft") prev();
    });

    // Initial
    showLogin();
  </script>
  <footer style="
      margin-top:30px;
      padding:14px;
      text-align:center;
      font-size:14px;
      color:white;
      border-top:2px solid #ddd;
      background:#800020;
  ">
      GATE Mock Test Companion <br>
      Curated by Dr. Veena and Dr. Sreehari VM, SASTRA Deemed University.
  </footer>
</body>
</html>
"""


# ==========================
# local_pool.js (same as your current bank)
# NOTE: This is NOT secure because answers are in browser. OK for demo.
# ==========================
LOCAL_POOL_JS = r"""/* local_pool.js
   Creates LOCAL_POOL with exactly 200 questions
   Topics: Aptitude, Engineering Maths, Aerodynamics, Propulsion, Structures,
           Gas Dynamics, Turbomachinery, Flight Mechanics
   Each topic: Easy 8, Medium 8, Hard 9 = 25; total 200
*/
function buildLocalPool() {
  const pool = [];
  const ABCD = ["A", "B", "C", "D"];

  const r2 = (x) => Math.round(Number(x) * 100) / 100;

  function add(q) { pool.push(q); }
  function mkId(topicCode, diff, n) { return `${topicCode}-${diff}-${String(n).padStart(2, "0")}`; }

  function mcq({ id, topic, difficulty, marks, question, opts, ansIndex, solution }) {
    add({
      id, topic, difficulty, type: "MCQ", marks,
      question,
      options: opts.map((t, i) => `${ABCD[i]}: ${t}`),
      answer: ABCD[ansIndex],
      solution
    });
  }
  function msq({ id, topic, difficulty, marks, question, opts, ansIndices, solution }) {
    add({
      id, topic, difficulty, type: "MSQ", marks,
      question,
      options: opts.map((t, i) => `${ABCD[i]}: ${t}`),
      answer: ansIndices.map(i => ABCD[i]),
      solution
    });
  }
  function nat({ id, topic, difficulty, marks, question, answer, tolerance = 0.01, solution }) {
    add({
      id, topic, difficulty, type: "NAT", marks,
      question,
      answer: r2(answer),
      tolerance,
      decimals: 2,
      solution
    });
  }

  // =========================
  // APTITUDE (APT) 25
  // =========================
  (function(){
    const T="Aptitude", C="APT";
    mcq({id:mkId(C,"E",1),topic:T,difficulty:"E",marks:1,question:"If 5 pens cost ₹75, cost of 12 pens (₹) is",
      opts:["150","160","180","200"],ansIndex:2,solution:"Unit cost=75/5=15. Cost=12×15=180."});
    mcq({id:mkId(C,"E",2),topic:T,difficulty:"E",marks:1,question:"Average of 10 and 20 is",
      opts:["10","15","20","25"],ansIndex:1,solution:"(10+20)/2=15."});
    nat({id:mkId(C,"E",3),topic:T,difficulty:"E",marks:1,question:"A shop offers 10% discount on ₹2400. Selling price (2 decimals) = ____",
      answer:2160,solution:"SP=2400(1−0.10)=2160.00"});
    mcq({id:mkId(C,"E",4),topic:T,difficulty:"E",marks:1,question:"If \\(x\\) is even, then \\(x^2\\) is",
      opts:["odd","even","prime","negative"],ansIndex:1,solution:"Even squared remains even."});
    nat({id:mkId(C,"E",5),topic:T,difficulty:"E",marks:1,question:"A train travels 90 km in 1.5 hours. Speed (km/h, 2 decimals) = ____",
      answer:60,solution:"Speed=90/1.5=60.00"});
    mcq({id:mkId(C,"E",6),topic:T,difficulty:"E",marks:1,question:"Choose the correct word: The conclusions were ____ by new evidence.",
      opts:["affected","effected","accepted","afflicted"],ansIndex:0,solution:"Affected = influenced."});
    mcq({id:mkId(C,"E",7),topic:T,difficulty:"E",marks:1,question:"If A is mother of B and B is sister of C, A is ____ of C.",
      opts:["aunt","mother","sister","grandmother"],ansIndex:1,solution:"A is mother of both siblings."});
    mcq({id:mkId(C,"E",8),topic:T,difficulty:"E",marks:1,question:"Simplify: \\(3\\times 4 + 5\\times 2\\) equals",
      opts:["12","16","22","24"],ansIndex:2,solution:"12+10=22."});

    mcq({id:mkId(C,"M",1),topic:T,difficulty:"M",marks:2,question:"Two pipes fill a tank in 10 h and 15 h. Time to fill together (hours) is closest to",
      opts:["5.0","6.0","6.5","7.5"],ansIndex:1,solution:"Rate=1/10+1/15=1/6. Time=6 h."});
    nat({id:mkId(C,"M",2),topic:T,difficulty:"M",marks:2,question:"If \\(\\log_{10}(x)=1.7\\), then \\(x\\) (2 decimals) = ____",
      answer:Math.pow(10,1.7),tolerance:0.05,solution:"x=10^{1.7}≈50.12"});
    mcq({id:mkId(C,"M",3),topic:T,difficulty:"M",marks:1,question:"A number leaves remainder 5 when divided by 9. Remainder when divided by 3 is",
      opts:["0","1","2","Cannot be determined"],ansIndex:2,solution:"n=9k+5 ⇒ n mod 3 = 2."});
    mcq({id:mkId(C,"M",4),topic:T,difficulty:"M",marks:2,question:"In a class, ratio of boys:girls = 3:2. If total students = 50, girls =",
      opts:["10","20","25","30"],ansIndex:1,solution:"Girls=2/5×50=20."});
    nat({id:mkId(C,"M",5),topic:T,difficulty:"M",marks:1,question:"Simple interest on ₹5000 at 8% for 1.5 years (₹, 2 decimals) = ____",
      answer:600,solution:"SI=PRT=5000×0.08×1.5=600.00"});
    mcq({id:mkId(C,"M",6),topic:T,difficulty:"M",marks:2,question:"Average of 8 numbers is 12. If one number 20 is replaced by 10, new average is",
      opts:["10.75","11.25","11.75","12.00"],ansIndex:0,solution:"Sum=96. New sum=86. Avg=86/8=10.75"});
    mcq({id:mkId(C,"M",7),topic:T,difficulty:"M",marks:1,question:"If \\(2x-3=7\\), then \\(x\\) equals",
      opts:["2","3","4","5"],ansIndex:3,solution:"2x=10 ⇒ x=5."});
    mcq({id:mkId(C,"M",8),topic:T,difficulty:"M",marks:2,question:"A work is completed by A in 12 days and B in 18 days. Together they finish in (days) closest to",
      opts:["6.0","7.2","7.5","8.0"],ansIndex:1,solution:"Rate=1/12+1/18=5/36. Time=36/5=7.2."});

    mcq({id:mkId(C,"H",1),topic:T,difficulty:"H",marks:2,question:"If \\(x\\) and \\(y\\) are positive and \\(\\frac{1}{x}+\\frac{1}{y}=\\frac{1}{6}\\), then minimum of \\(x+y\\) is",
      opts:["12","18","24","36"],ansIndex:2,solution:"Minimum at x=y=12 ⇒ x+y=24."});
    nat({id:mkId(C,"H",2),topic:T,difficulty:"H",marks:2,question:"Milk:water = 7:3. If 10 L water is added to 40 L mixture, new milk fraction (2 decimals) = ____",
      answer:(40*(7/10))/(50),solution:"Milk=28L, total=50L ⇒ fraction=0.56"});
    mcq({id:mkId(C,"H",3),topic:T,difficulty:"H",marks:2,question:"If \\(n\\) is an integer, \\(n^2\\) mod 4 can be",
      opts:["0 only","1 only","0 or 1","2 or 3"],ansIndex:2,solution:"Even⇒0, odd⇒1 mod 4."});
    nat({id:mkId(C,"H",4),topic:T,difficulty:"H",marks:2,question:"Mean of 5 numbers is 14 and mean of 3 of them is 12. Mean of remaining 2 (2 decimals) = ____",
      answer:(5*14-3*12)/2,solution:"Remaining sum=34 ⇒ mean=17.00"});
    mcq({id:mkId(C,"H",5),topic:T,difficulty:"H",marks:2,question:"How many integers in [1,100] are divisible by 2 or 5?",
      opts:["50","60","65","70"],ansIndex:1,solution:"50+20−10=60."});
    msq({id:mkId(C,"H",6),topic:T,difficulty:"H",marks:2,question:"Select all statements always true for real \\(a,b\\):",
      opts:["\\((a+b)^2\\ge 0\\)","\\(a^2+b^2\\ge 2ab\\)","\\(a^2+b^2\\le (a+b)^2\\)","\\(|a+b|\\le |a|+|b|\\)"],
      ansIndices:[0,1,2,3],solution:"All true (nonnegativity, inequality, expansion, triangle)."});
    nat({id:mkId(C,"H",7),topic:T,difficulty:"H",marks:2,question:"If success probability is 0.2 per trial, expected successes in 15 trials (2 decimals) = ____",
      answer:3,solution:"E=np=15×0.2=3.00"});
    mcq({id:mkId(C,"H",8),topic:T,difficulty:"H",marks:2,question:"If \\(\\sin\\theta=3/5\\) in first quadrant, \\(\\cos\\theta\\) is",
      opts:["3/5","4/5","5/3","5/4"],ansIndex:1,solution:"3-4-5 triangle ⇒ cos=4/5."});
    mcq({id:mkId(C,"H",9),topic:T,difficulty:"H",marks:2,question:"A cube has surface area 150 \\(\\text{cm}^2\\). Volume (\\(\\text{cm}^3\\)) is",
      opts:["125","150","216","250"],ansIndex:0,solution:"6a^2=150 ⇒ a=5 ⇒ V=125."});
  })();

  // =========================
  // ENGINEERING MATHS (MTH) 25
  // =========================
  (function(){
    const T="Engineering Maths", C="MTH";
    mcq({id:mkId(C,"E",1),topic:T,difficulty:"E",marks:1,question:"If \\(f(x)=x^3\\), then \\(f'(2)\\) equals",
      opts:["4","6","8","12"],ansIndex:3,solution:"f'(x)=3x^2 ⇒ 12"});
    mcq({id:mkId(C,"E",2),topic:T,difficulty:"E",marks:1,question:"Determinant of \\(\\begin{bmatrix}1&2\\\\3&4\\end{bmatrix}\\) is",
      opts:["-2","2","-1","1"],ansIndex:0,solution:"1·4−2·3=−2"});
    nat({id:mkId(C,"E",3),topic:T,difficulty:"E",marks:1,question:"Compute \\(\\int_0^2 x\\,dx\\) (2 decimals) = ____",
      answer:2,solution:"[x^2/2]_0^2=2.00"});
    mcq({id:mkId(C,"E",4),topic:T,difficulty:"E",marks:1,question:"\\(\\mathcal{L}\\{1\\}\\) equals",
      opts:["1/s","s","1/s^2","e^s"],ansIndex:0,solution:"Integral gives 1/s."});
    mcq({id:mkId(C,"E",5),topic:T,difficulty:"E",marks:1,question:"If A is symmetric, then",
      opts:["A^T=A","A^T=-A","A^2=I","det(A)=0"],ansIndex:0,solution:"Definition."});
    nat({id:mkId(C,"E",6),topic:T,difficulty:"E",marks:1,question:"\\(|3+4i|\\) (2 decimals) = ____",
      answer:5,solution:"sqrt(9+16)=5.00"});
    mcq({id:mkId(C,"E",7),topic:T,difficulty:"E",marks:1,question:"Solution of \\(x^2-9=0\\) (positive root) is",
      opts:["1","3","-3","9"],ansIndex:1,solution:"x=3"});
    nat({id:mkId(C,"E",8),topic:T,difficulty:"E",marks:1,question:"\\(\\lim_{x\\to 0}\\frac{\\sin(2x)}{x}\\) (2 decimals) = ____",
      answer:2,solution:"=2.00"});

    mcq({id:mkId(C,"M",1),topic:T,difficulty:"M",marks:2,question:"Eigenvalues of \\(\\begin{bmatrix}2&0\\\\0&5\\end{bmatrix}\\) are",
      opts:["2 and 5","0 and 7","1 and 6","-2 and -5"],ansIndex:0,solution:"Diagonal entries."});
    nat({id:mkId(C,"M",2),topic:T,difficulty:"M",marks:2,question:"Compute \\(\\int_0^1 6x(1-x)\\,dx\\) (2 decimals) = ____",
      answer:1,solution:"6(1/2−1/3)=1.00"});
    mcq({id:mkId(C,"M",3),topic:T,difficulty:"M",marks:2,question:"For \\(y'+y=e^x\\), integrating factor is",
      opts:["e^x","e^{-x}","x","1"],ansIndex:0,solution:"IF=e^{∫1dx}=e^x"});
    nat({id:mkId(C,"M",4),topic:T,difficulty:"M",marks:2,question:"If \\(\\mathbf{a}\\cdot\\mathbf{b}=12\\), \\(|a|=3\\), \\(|b|=5\\). Angle (deg,2 decimals)=____",
      answer:(Math.acos(0.8)*180/Math.PI),tolerance:0.2,solution:"cosθ=0.8 ⇒ θ≈36.87°"});
    mcq({id:mkId(C,"M",5),topic:T,difficulty:"M",marks:1,question:"If \\(\\sum a_n\\) converges absolutely, then it converges",
      opts:["always","never","only if positive","only if monotone"],ansIndex:0,solution:"Absolute convergence ⇒ convergence."});
    nat({id:mkId(C,"M",6),topic:T,difficulty:"M",marks:2,question:"Solve \\(2x+3y=12\\), \\(x-y=1\\). \\(x\\) (2 decimals)=____",
      answer:3,solution:"x=3.00"});
    mcq({id:mkId(C,"M",7),topic:T,difficulty:"M",marks:2,question:"For continuous random variable, \\(P(X=a)\\) equals",
      opts:["0","1","depends on a","infinite"],ansIndex:0,solution:"Point probability is 0."});
    nat({id:mkId(C,"M",8),topic:T,difficulty:"M",marks:2,question:"\\(\\nabla\\cdot (x\\hat i + y\\hat j + z\\hat k)\\) (2 decimals)=____",
      answer:3,solution:"=3.00"});

    mcq({id:mkId(C,"H",1),topic:T,difficulty:"H",marks:2,question:"If \\(A\\) is orthogonal, then \\(A^{-1}\\) equals",
      opts:["A","A^T","-A","0"],ansIndex:1,solution:"Orthogonal ⇒ A^{-1}=A^T"});
    nat({id:mkId(C,"H",2),topic:T,difficulty:"H",marks:2,question:"\\(\\int_0^{\\pi/2}\\sin x\\,dx\\) (2 decimals)=____",
      answer:1,solution:"=1.00"});
    mcq({id:mkId(C,"H",3),topic:T,difficulty:"H",marks:2,question:"For \\(y''+4y=0\\), general solution is",
      opts:["A(e^{2x}+e^{-2x})","A(\\sin 2x + \\cos 2x)","A(\\sin x+\\cos x)","Ae^{4x}"],ansIndex:1,
      solution:"r^2+4=0 ⇒ r=±2i"});
    nat({id:mkId(C,"H",4),topic:T,difficulty:"H",marks:2,question:"\\(\\sum_{k=1}^{10} k\\) (2 decimals)=____",
      answer:55,solution:"10×11/2=55.00"});
    mcq({id:mkId(C,"H",5),topic:T,difficulty:"H",marks:2,question:"If \\(X\\sim N(0,1)\\), then \\(E[X^2]\\) equals",
      opts:["0","1","2","\\(\\pi\\)"],ansIndex:1,solution:"Var=1, mean=0 ⇒ E[X^2]=1"});
    nat({id:mkId(C,"H",6),topic:T,difficulty:"H",marks:2,question:"Rank of \\(\\begin{bmatrix}1&1\\\\1&1\\end{bmatrix}\\) (2 decimals)=____",
      answer:1,solution:"Dependent rows ⇒ rank=1.00"});
    msq({id:mkId(C,"H",7),topic:T,difficulty:"H",marks:2,question:"Select all true statements:",
      opts:["If \\(\\nabla f=0\\), it is stationary point","All stationary points are minima","Minimum can occur at boundary","Hessian positive definite ⇒ local minimum"],
      ansIndices:[0,2,3],solution:"(1),(3),(4) true."});
    nat({id:mkId(C,"H",8),topic:T,difficulty:"H",marks:2,question:"For 2×2 matrix, if det(A)=3, then det(2A) (2 decimals)=____",
      answer:12,solution:"det(2A)=2^2 det(A)=12.00"});
    mcq({id:mkId(C,"H",9),topic:T,difficulty:"H",marks:2,question:"If \\(u=e^x\\cos y\\), then \\(u_{xx}\\) equals",
      opts:["\\(e^x\\cos y\\)","\\(e^x\\sin y\\)","\\(-e^x\\cos y\\)","0"],ansIndex:0,solution:"Differentiate twice in x."});
  })();

  // =========================
  // Remaining 6 topics (programmatic) 25 each = 150
  // =========================
  function fillTopic(topic, code, easyCount, medCount, hardCount, maker) {
    for (let i=1;i<=easyCount;i++) maker("E", i);
    for (let i=1;i<=medCount;i++) maker("M", i);
    for (let i=1;i<=hardCount;i++) maker("H", i);
  }

  // AERODYNAMICS
  fillTopic("Aerodynamics","AERO",8,8,9,(D,i)=>{
    const id=mkId("AERO",D,i);
    const marks=(D==="E")?1:2;
    if (i%5===0) {
      const alphaDeg = (D==="E") ? (3+i%3) : (4+i%4);
      const ans = 2*Math.PI*(alphaDeg*Math.PI/180);
      nat({id,topic:"Aerodynamics",difficulty:D,marks,question:`Thin airfoil: \\(C_L\\approx 2\\pi\\alpha\\). For \\(\\alpha=${alphaDeg}^\\circ\\), \\(C_L\\) (2 decimals)=____`,
        answer:ans,tolerance:0.03,solution:`\\(\\alpha\\) in rad = ${alphaDeg}π/180. \\(C_L=2\\pi\\alpha\\).`});
    } else if (i%4===0) {
      msq({id,topic:"Aerodynamics",difficulty:D,marks,question:"Select all that reduce induced drag for same lift:",
        opts:["Increase aspect ratio","Decrease aspect ratio","Increase Oswald efficiency","Decrease Oswald efficiency"],
        ansIndices:[0,2],solution:"\\(C_{D_i}\\propto 1/(eAR)\\)."});
    } else {
      mcq({id,topic:"Aerodynamics",difficulty:D,marks,question:"For inviscid incompressible potential flow, vorticity is",
        opts:["zero","nonzero","equal to pressure","equal to density"],ansIndex:0,solution:"Potential flow is irrotational ⇒ vorticity 0."});
    }
  });

  // PROPULSION
  fillTopic("Propulsion","PROP",8,8,9,(D,i)=>{
    const id=mkId("PROP",D,i);
    const marks=(D==="E")?1:2;
    if (i%5===1) {
      nat({id,topic:"Propulsion",difficulty:D,marks,question:"Turbojet thrust (ignore pressure thrust): \\(T=\\dot m(V_e-V_0)\\). If \\(\\dot m=30\\), \\(V_e=650\\), \\(V_0=250\\), T (kN,2 decimals)=____",
        answer:(30*(650-250))/1000,tolerance:0.05,solution:"T=30×400=12000 N=12.00 kN"});
    } else if (i%4===2) {
      mcq({id,topic:"Propulsion",difficulty:D,marks,question:"Choked flow at nozzle throat occurs at",
        opts:["M=0","M=0.5","M=1","M>1"],ansIndex:2,solution:"Choking at M=1 at throat."});
    } else if (i%3===0) {
      msq({id,topic:"Propulsion",difficulty:D,marks,question:"Select all true for ideal nozzle:",
        opts:["Isentropic","Can choke","\\(T_0\\) constant (no work)","Always subsonic exit"],
        ansIndices:[0,1,2],solution:"Ideal nozzle: isentropic, may choke, T0 constant; exit may be supersonic in CD nozzle."});
    } else {
      mcq({id,topic:"Propulsion",difficulty:D,marks,question:"For perfectly expanded rocket nozzle (\\(p_e=p_a\\)), thrust is",
        opts:["\\(\\dot mV_e\\)","\\(\\dot m(V_e-V_0)\\)","\\(\\dot mV_0\\)","0"],ansIndex:0,solution:"T=ṁVe+(pe−pa)Ae ⇒ ṁVe."});
    }
  });

  // STRUCTURES
  fillTopic("Structures","STR",8,8,9,(D,i)=>{
    const id=mkId("STR",D,i);
    const marks=(D==="E")?1:2;
    if (i%5===0) {
      nat({id,topic:"Structures",difficulty:D,marks,question:"Simply supported beam: \\(M_{max}=wL^2/8\\). If \\(w=3\\,kN/m\\), \\(L=4\\,m\\). \\(M_{max}\\) (kN·m,2 decimals)=____",
        answer:3*16/8,tolerance:0.05,solution:"Mmax=3×16/8=6.00"});
    } else if (i%4===0) {
      msq({id,topic:"Structures",difficulty:D,marks,question:"Euler buckling: \\(P_{cr}=\\pi^2EI/(KL)^2\\). Select all that increase \\(P_{cr}\\):",
        opts:["Increase E","Increase L","Decrease K","Increase I"],ansIndices:[0,2,3],solution:"Pcr increases with E,I and decreases with (KL)^2."});
    } else {
      mcq({id,topic:"Structures",difficulty:D,marks,question:"For thin-walled closed single-cell section, Bredt–Batho gives",
        opts:["\\(T=2Aq\\)","\\(T=Aq\\)","\\(T=2Atq\\)","\\(T=q/t\\)"],ansIndex:0,solution:"T=2Aq ⇒ q=T/(2A)."});
    }
  });

  // GAS DYNAMICS
  fillTopic("Gas Dynamics","GAS",8,8,9,(D,i)=>{
    const id=mkId("GAS",D,i);
    const marks=(D==="E")?1:2;
    if (i%5===2) {
      const M=(D==="E")?0.8:(D==="M"?1.6:2.0);
      nat({id,topic:"Gas Dynamics",difficulty:D,marks,question:`For \\(\\gamma=1.4\\), compute \\(T_0/T=1+0.2M^2\\) for \\(M=${M}\\). (2 decimals)=____`,
        answer:1+0.2*M*M,tolerance:0.02,solution:"Use isentropic relation."});
    } else if (i%4===1) {
      msq({id,topic:"Gas Dynamics",difficulty:D,marks,question:"Across a normal shock (perfect gas), select all true:",
        opts:["Mach decreases","Static pressure increases","Stagnation pressure increases","Stagnation temperature ~ constant (adiabatic)"],
        ansIndices:[0,1,3],solution:"p0 decreases; T0 approx constant."});
    } else {
      mcq({id,topic:"Gas Dynamics",difficulty:D,marks,question:"Speed of sound in ideal gas is",
        opts:["\\(\\sqrt{RT}\\)","\\(\\sqrt{\\gamma RT}\\)","\\(\\gamma RT\\)","\\(RT/\\gamma\\)"],ansIndex:1,solution:"a=√(γRT)."});
    }
  });

  // TURBOMACHINERY
  fillTopic("Turbomachinery","TURBO",8,8,9,(D,i)=>{
    const id=mkId("TURBO",D,i);
    const marks=(D==="E")?1:2;
    if (i%5===3) {
      nat({id,topic:"Turbomachinery",difficulty:D,marks,question:"Euler: \\(\\Delta h_0=U\\Delta V_w\\). If U=260 m/s and \\(\\Delta V_w=70\\) m/s, \\(\\Delta h_0\\) (kJ/kg,2 decimals)=____",
        answer:260*70/1000,tolerance:0.2,solution:"=18.20 kJ/kg"});
    } else if (i%4===0) {
      mcq({id,topic:"Turbomachinery",difficulty:D,marks,question:"Degree of reaction 0.5 implies",
        opts:["all enthalpy drop in stator","all in rotor","equal split in stator and rotor","no enthalpy drop"],
        ansIndex:2,solution:"R=0.5 means equal split."});
    } else {
      msq({id,topic:"Turbomachinery",difficulty:D,marks,question:"Select all true about compressors:",
        opts:["Increase stagnation pressure","Increase stagnation temperature","Extract shaft work","Can be axial or centrifugal"],
        ansIndices:[0,1,3],solution:"Compressors need shaft work input (so 3 false)."});
    }
  });

  // FLIGHT MECHANICS
  fillTopic("Flight Mechanics","FM",8,8,9,(D,i)=>{
    const id=mkId("FM",D,i);
    const marks=(D==="E")?1:2;
    if (i%5===4) {
      const W = (D==="E")?50000:(D==="M"?60000:70000);
      const S = 30;
      const rho = (D==="H")?0.9:1.0;
      const CL = (D==="E")?0.5:0.6;
      nat({id,topic:"Flight Mechanics",difficulty:D,marks,question:`Level flight: \\(L=W=\\tfrac12\\rho V^2SC_L\\). For W=${(W/1000)} kN, \\(\\rho=${rho}\\), S=${S} m^2, \\(C_L=${CL}\\). V (m/s,2 decimals)=____`,
        answer:Math.sqrt((2*W)/(rho*S*CL)),tolerance:0.7,solution:"V=√(2W/(ρSC_L))."});
    } else if (i%4===2) {
      mcq({id,topic:"Flight Mechanics",difficulty:D,marks,question:"Longitudinal static stability requires CG be",
        opts:["ahead of neutral point","behind neutral point","at neutral point always","at wing tip"],
        ansIndex:0,solution:"CG ahead ⇒ positive static margin."});
    } else {
      msq({id,topic:"Flight Mechanics",difficulty:D,marks,question:"Select all true for steady level flight:",
        opts:["\\(L=W\\)","\\(T=D\\)","\\(L=D\\)","\\(T=W\\)"],
        ansIndices:[0,1],solution:"Force balance gives L=W and T=D."});
    }
  });

  if (pool.length !== 200) {
    throw new Error(`LOCAL_POOL size is ${pool.length}, expected 200.`);
  }
  return pool;
}

window.LOCAL_POOL = buildLocalPool();
"""
def make_json_safe(value):
    if isinstance(value, dict):
        return {str(k): make_json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [make_json_safe(v) for v in value]
    if isinstance(value, tuple):
        return [make_json_safe(v) for v in value]
    if isinstance(value, set):
        return [make_json_safe(v) for v in sorted(value)]
    return value
def make_json_safe(obj):
    return json.loads(json.dumps(obj, default=str))
# ==========================
# Routes
# ==========================
@app.get("/sastra_banner.jpg")
def sastra_banner():
        return send_file("sastra_banner.jpg", mimetype="image/jpeg")

@app.get("/")
def root():
    return Response(INDEX_HTML, mimetype="text/html; charset=utf-8")


@app.get("/local_pool.js")
def local_pool():
        return Response(LOCAL_POOL_JS, mimetype="application/javascript; charset=utf-8")
 

@app.post("/api/login")
def api_login():
    data = request.get_json(force=True)
    user_id = str(data.get("userId", "")).strip()
    password = str(data.get("password", "")).strip()

    if user_id not in USERS or USERS[user_id]["password"] != password:
        return jsonify({"ok": False, "error": "Invalid credentials"}), 401

    session["userId"] = user_id
    return jsonify({"ok": True, "user": {"userId": user_id, "name": USERS[user_id]["name"]}})


@app.post("/api/logout")
def api_logout():
    session.pop("userId", None)
    return jsonify({"ok": True})


@app.get("/api/tests")
def api_tests():
    ok, user = require_login()
    if not ok:
        return jsonify({"ok": False, "error": "Not logged in"}), 401
    return jsonify({"ok": True, "tests": MOCK_TESTS, "user": user})


@app.post("/api/generate-paper")
def api_generate_paper():
    ok, user = require_login()
    if not ok:
        return jsonify({"ok": False, "error": "Not logged in"}), 401

    data = request.get_json(force=True)
    test_id = str(data.get("testId", "")).strip()
    pool = data.get("pool", [])
    variant_mode = bool(data.get("variantMode", True))

    test = next((t for t in MOCK_TESTS if t["id"] == test_id), None)
    if not test:
        return jsonify({"ok": False, "error": "Invalid testId"}), 400

    try:
        validate_pool(pool)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    paper = build_paper_from_pool(pool)

    variants_applied = 0
    if variant_mode and client is not None:
        try:
            bases = choose_variant_candidates(paper)
            variants = llm_variant_questions(bases)
            paper = apply_variants(paper, variants)
            variants_applied = len(variants)
        except Exception as e:
            print("ERROR DURING VARIANT GENERATION:", repr(e))
            variants_applied = 0

    attempt_id = f"ATT-{uuid.uuid4().hex}"
    ATTEMPTS[attempt_id] = {
        "userId": user["userId"],
        "testId": test_id,
        "startedAt": now_ts(),
        "durationMin": test["durationMin"],
        "paperFull": paper,
        "submitted": False,
        "variantMode": variant_mode,
        "variantsApplied": variants_applied,
    }

    paper_exam = make_json_safe([strip_answers_for_exam(q) for q in paper])
    return jsonify(
        {
            "ok": True,
            "attemptId": attempt_id,
            "test": test,
            "variantMode": variant_mode,
            "variantsApplied": variants_applied,
            "paper": paper_exam,
            "blueprint": BLUEPRINT,
        }
    )


@app.post("/api/submit")
def api_submit():
    ok, user = require_login()
    if not ok:
        return jsonify({"ok": False, "error": "Not logged in"}), 401

    data = request.get_json(force=True)
    attempt_id = str(data.get("attemptId", "")).strip()
    responses = data.get("responses", {})

    if attempt_id not in ATTEMPTS:
        return jsonify({"ok": False, "error": "Invalid attemptId"}), 400

    att = ATTEMPTS[attempt_id]
    if att["userId"] != user["userId"]:
        return jsonify({"ok": False, "error": "Not your attempt"}), 403
    if att["submitted"]:
        return jsonify({"ok": False, "error": "Already submitted"}), 400

    paper_full = att["paperFull"]
    score = calc_score(paper_full, responses)
    performance_feedback = compute_simple_feedback(paper_full, responses)

    att["submitted"] = True
    att["submittedAt"] = now_ts()
    att["responses"] = responses
    att["score"] = score

    solutions = []
    for idx, q in enumerate(paper_full, start=1):
        qid = q["id"]
        resp = responses.get(qid, None)
        attempted = resp is not None
        ok_q = attempted and is_correct(q, resp)

        if q["type"] == "NAT":
            correct_ans = round(float(q["answer"]), 2)
        elif q["type"] == "MSQ":
            correct_ans = normalize_msq(q["answer"])
        else:
            correct_ans = q["answer"]

        solutions.append(
            {
                "qNo": idx,
                "id": qid,
                "baseId": q.get("baseId", None),
                "isVariant": bool(q.get("isVariant", False)),
                "topic": q["topic"],
                "difficulty": q.get("difficulty", "M"),
                "type": q["type"],
                "marks": q["marks"],
                "question": q["question"],
                "options": q.get("options", None),
                "yourAnswer": resp,
                "correctAnswer": correct_ans,
                "status": ("Unattempted" if not attempted else ("Correct" if ok_q else "Wrong")),
                "solution": q.get("solution", "—"),
            }
        )

    return jsonify({
        "ok": True,
        "score": score,
        "solutions": solutions,
        "performanceFeedback": performance_feedback,
        "meta": {
            "attemptId": attempt_id,
            "testId": att["testId"],
            "startedAt": att["startedAt"],
            "submittedAt": att["submittedAt"],
            "variantMode": att.get("variantMode", False),
            "variantsApplied": att.get("variantsApplied", 0),
        },
    })


@app.post("/api/ask-doubt")
def api_ask_doubt():
    ok, user = require_login()
    if not ok:
        return jsonify({"ok": False, "error": "Not logged in"}), 401

    data = request.get_json(force=True)
    attempt_id = str(data.get("attemptId", "")).strip()
    question_no = data.get("questionNo", None)
    user_message = str(data.get("message", "")).strip()

    if not attempt_id or attempt_id not in ATTEMPTS:
        return jsonify({"ok": False, "error": "Invalid attemptId"}), 400
    if not user_message:
        return jsonify({"ok": False, "error": "Message is empty"}), 400

    att = ATTEMPTS[attempt_id]
    if att["userId"] != user["userId"]:
        return jsonify({"ok": False, "error": "Not your attempt"}), 403
    if not att.get("submitted"):
        return jsonify({"ok": False, "error": "Submit the test first to use the doubt assistant."}), 400

    paper_full = att["paperFull"]
    responses = att.get("responses", {})

    selected_q = None
    q_index = None

    if question_no is not None and str(question_no).strip() != "":
        try:
            q_index = int(question_no) - 1
            if 0 <= q_index < len(paper_full):
                selected_q = paper_full[q_index]
        except Exception:
            selected_q = None

    if selected_q is None:
        import re
        m = re.search(r"\bQ(?:uestion)?\s*(\d+)\b", user_message, re.IGNORECASE)
        if m:
            try:
                q_index = int(m.group(1)) - 1
                if 0 <= q_index < len(paper_full):
                    selected_q = paper_full[q_index]
            except Exception:
                selected_q = None

    if selected_q is None:
        return jsonify({"ok": False, "error": "Please provide a valid question number, for example: Q12 or Question 12."}), 400

    qid = selected_q["id"]
    your_answer = responses.get(qid, None)

    if selected_q["type"] == "NAT":
        correct_answer = round(float(selected_q["answer"]), 2)
    elif selected_q["type"] == "MSQ":
        correct_answer = normalize_msq(selected_q["answer"])
    else:
        correct_answer = selected_q["answer"]

    context = {
        "qNo": q_index + 1,
        "topic": selected_q["topic"],
        "difficulty": selected_q.get("difficulty", "M"),
        "type": selected_q["type"],
        "marks": selected_q["marks"],
        "question": selected_q["question"],
        "options": selected_q.get("options", None),
        "correctAnswer": correct_answer,
        "yourAnswer": your_answer,
        "officialSolution": selected_q.get("solution", ""),
        "isVariant": bool(selected_q.get("isVariant", False)),
    }
    print("ASK_DOUBT -> client is None:", client is None, "ACTIVE_MODEL:", ACTIVE_MODEL)
    if client is None:
                   return jsonify({
                            "ok": True,
                            "reply": (
                                     f"AI tutor is not available right now.\n\n"
                                     f"Official Solution:\n{context['officialSolution']}"
                            )
                   })

    prompt = f"""
You are an academic doubt-clearing assistant for a GATE Aerospace mock test platform.

Follow these rules strictly:
1. Use the provided question context as the PRIMARY source of truth.
2. You may use your general subject knowledge to explain concepts, formulas, theory, and reasoning behind this question.
3. Do NOT reveal unrelated hidden questions or the full question bank.
4. Do NOT invent missing question data.
5. If the student's answer is wrong, explain why it is wrong.
6. If useful, explain why the correct answer is correct and why other options are incorrect.
7. Keep the answer focused on THIS question only.
8. No web search, no external websites.
9. Be clear, step-by-step, accurate, and student-friendly.
10. If the official solution is brief, expand it using general knowledge, but do not contradict the given context.

QUESTION CONTEXT:
{json.dumps(context, ensure_ascii=False, indent=2)}

STUDENT DOUBT:
{user_message}
"""

    try:
        resp = client.chat.completions.create(
            model=ACTIVE_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a careful, accurate, and helpful aerospace exam tutor. "
                        "Use the provided question context as the main reference. "
                        "You may use general engineering and mathematics knowledge to explain concepts, "
                        "but you must stay consistent with the given question context. "
                        "Do not reveal unrelated hidden questions."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
       
        print("OLLAMA RAW RESPONSE:", resp)

        reply = ""   

        if resp and hasattr(resp, "choices") and len(resp.choices) > 0:
           msg = resp.choices[0].message
           if msg and hasattr(msg, "content") and msg.content:
                                    reply = msg.content.strip()
        if not reply:
            reply = "Official Solution:\n" + str(context["officialSolution"])
 
        return jsonify({"ok": True, "reply": reply})


        
    except Exception as e:
        print("ASK DOUBT FAILED:", repr(e))
        return jsonify({
            "ok": True,
            "reply": (
                "AI tutor is temporarily unavailable.\n\n"
                + "Official Solution:\n"
                + str(context["officialSolution"])
                + "\n\nSystem error: "
                + str(e)
            )
        })



if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)