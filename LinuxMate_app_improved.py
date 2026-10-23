import os
import re
import csv
import sqlite3
import json
import urllib.request
import urllib.error
from pathlib import Path

from flask import Flask, render_template, request, jsonify

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "linuxmate.db"
CSV_PATH = BASE_DIR / "data" / "linuxmate_dataset_300.csv"
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:latest")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")

app = Flask(__name__)

DANGEROUS_PATTERNS = [
    (r"\brm\s+.*(?:-rf|-fr|--recursive)", "High risk: recursive forced deletion can permanently remove many files."),
    (r"\bmkfs(?:\.|\s)", "High risk: formatting a device can erase its filesystem and data."),
    (r"\bdd\s+.*\bof=/dev/", "High risk: writing directly to a device can destroy data or overwrite a disk."),
    (r"\b(shutdown|reboot|poweroff)\b", "System-impacting: this can interrupt users and running services."),
    (r"\bchmod\s+(-R\s+)?777\b", "Security risk: world-readable/writable/executable permissions are usually unsafe."),
    (r"(?:curl|wget)\b.*\|\s*(?:sudo\s+)?sh\b", "Security risk: piping a downloaded script directly into a shell executes unreviewed code."),
    (r":\(\)\s*\{\s*:\s*\|", "Dangerous pattern: this resembles a fork bomb and can exhaust system resources."),
]

OS_TERMS = {
    "process", "thread", "deadlock", "scheduling", "scheduler", "paging",
    "virtual memory", "page replacement", "semaphore", "mutex", "synchronization",
    "operating system", "operating systems", "cpu scheduling", "memory management",
    "thrashing", "segmentation", "fragmentation", "banker's algorithm", "bankers algorithm",
    "critical section", "context switch", "context switching", "race condition",
    "producer consumer", "disk scheduling", "system call", "starvation"
}

COMMAND_TERMS = {
    "disk space", "filesystem space", "permissions", "list files", "current directory",
    "working directory", "running processes", "kill process", "delete file", "remove file",
    "copy file", "move file", "search file", "ip address", "memory usage", "cpu usage",
    "linux command", "command line", "terminal", "check disk", "disk usage"
}

STOP_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "do", "does", "did", "how",
    "what", "why", "when", "where", "which", "can", "could", "would", "should",
    "please", "explain", "tell", "me", "about", "in", "on", "for", "to", "of",
    "and", "or", "with", "give", "show", "i", "my", "it", "this", "that",
    "define", "describe", "mean", "difference", "between", "example", "use"
}


def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = connect()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS qa (
      id INTEGER PRIMARY KEY,
      category TEXT,
      topic TEXT,
      question TEXT,
      command TEXT,
      answer TEXT,
      risk_level TEXT,
      example_or_note TEXT
    );
    CREATE TABLE IF NOT EXISTS history (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      question TEXT NOT NULL,
      answer TEXT NOT NULL,
      source TEXT,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS feedback (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      history_id INTEGER,
      rating TEXT NOT NULL,
      comment TEXT,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)

    count = con.execute("SELECT COUNT(*) FROM qa").fetchone()[0]
    if count == 0 and CSV_PATH.exists():
        with open(CSV_PATH, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for r in reader:
                con.execute(
                    """INSERT OR REPLACE INTO qa
                    (id, category, topic, question, command, answer, risk_level, example_or_note)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        r.get("id"), r.get("category", ""), r.get("topic", ""),
                        r.get("question", ""), r.get("command", ""), r.get("answer", ""),
                        r.get("risk_level", ""), r.get("example_or_note", "")
                    )
                )
        con.commit()
    con.close()


def normalize(text):
    text = (text or "").lower().replace("_", " ")
    text = re.sub(r"[^\w./+-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokens(text):
    return {
        t for t in re.findall(r"[a-z0-9_./+-]+", normalize(text))
        if t not in STOP_WORDS and len(t) > 1
    }


def classify_question(question):
    q = normalize(question)

    os_markers = (
        "operating system", "os concept", "deadlock", "process", "thread",
        "cpu scheduling", "scheduling", "scheduler", "paging", "virtual memory",
        "page replacement", "semaphore", "mutex", "synchronization",
        "critical section", "context switch", "race condition", "thrashing",
        "segmentation", "fragmentation", "producer consumer", "disk scheduling",
        "system call", "starvation", "kernel", "file system", "filesystem"
    )
    command_markers = (
        "linux command", "terminal", "command line", "current directory",
        "hidden files", "disk space", "disk usage", "memory usage",
        "ip address", "running processes", "file permissions", "copy file",
        "move file", "delete file", "search for a file", "create a directory"
    )

    if any(term in q for term in os_markers):
        # "processes" as a request to list running processes is a command intent.
        if any(term in q for term in ("list processes", "show processes", "running processes", "process list")):
            return "command"
        return "os"

    if any(term in q for term in command_markers):
        return "command"

    if re.search(
        r"\b(df|du|ls|pwd|cd|ps|kill|chmod|chown|grep|find|rm|cp|mv|mkdir|touch|cat|head|tail|free|top|ip|ping|whoami|uname|date|man|wc|sort|uniq|tar|curl|wget)\b",
        q
    ):
        return "command"

    return "unknown"

def record_kind(record):
    # Trust the dataset's explicit category instead of guessing from words.
    # This prevents command topics such as "ps" / "processes" being mistaken
    # for Operating Systems theory.
    category = normalize(record.get("category", ""))
    if category in {"operating systems", "operating system", "os"}:
        return "os"
    if category in {"linux commands", "linux command", "commands"}:
        return "command"

    # Fallback for custom datasets without a category field.
    topic = normalize(record.get("topic", ""))
    command = normalize(record.get("command", ""))
    if command and command not in {"n/a", "na", "none"}:
        return "command"
    if any(term in topic for term in OS_TERMS):
        return "os"
    return "unknown"


def record_text(record):
    return " ".join(
        str(record.get(key, "") or "")
        for key in ("category", "topic", "question", "command", "answer", "example_or_note")
    )


TOPIC_ALIASES = {
    # Linux command intents
    "pwd": ("current directory", "working directory", "where am i", "present directory"),
    "ls": ("list files", "list directory", "show files", "hidden files", "directory contents"),
    "df": ("disk space", "filesystem space", "file system space", "free disk space"),
    "du": ("folder size", "directory size", "disk usage of a folder"),
    "free": ("memory usage", "available ram", "ram usage", "free memory"),
    "whoami": ("current user", "logged in user", "username"),
    "ip": ("ip address", "network address", "network interface"),
    "ps": ("running processes", "list processes", "process list"),
    "top": ("live processes", "interactive process monitor", "cpu usage live"),
    "grep": ("search text", "search inside file", "find text in file"),
    "find": ("find files", "locate files", "search for files"),
    "mv": ("move file", "rename file", "rename a file"),
    "cp": ("copy file", "copy files"),
    "mkdir": ("create directory", "make directory", "create folder"),
    "touch": ("create empty file", "create a file"),
    "rm": ("delete file", "remove file"),
    "chmod": ("file permissions", "change permissions", "permission mode"),
    "tail": ("last lines", "end of file", "last 20 lines"),
    "head": ("first lines", "beginning of file"),
    "wc": ("count lines", "word count", "count words"),
    # OS concepts
    "process": ("process in operating system", "program in execution"),
    "thread": ("process vs thread", "thread versus process", "lightweight process"),
    "scheduling": ("cpu scheduling", "cpu scheduler", "schedule processes"),
    "deadlock": ("deadlock conditions", "deadlock prevention", "deadlock avoidance"),
    "paging": ("memory pages", "paging in memory management"),
    "virtual memory": ("virtual memory", "memory larger than ram"),
    "page replacement": ("fifo page replacement", "lru page replacement", "optimal page replacement"),
    "synchronization": ("process synchronization", "synchronize processes"),
    "semaphore": ("binary semaphore", "counting semaphore"),
    "critical section": ("critical section problem", "mutual exclusion"),
    "file system": ("file system in os", "filesystem in operating system"),
    "system call": ("system calls", "user mode kernel mode request"),
    "context switch": ("context switching", "switch cpu between processes"),
    "kernel": ("operating system kernel", "core of operating system"),
    "fragmentation": ("internal fragmentation", "external fragmentation"),
}

def _topic_intent(question):
    q = normalize(question)
    # Prefer the longest matching alias/topic to avoid matching broad terms first.
    candidates = []
    for topic, aliases in TOPIC_ALIASES.items():
        variants = (topic,) + aliases
        for phrase in variants:
            p = normalize(phrase)
            if p and p in q:
                candidates.append((len(p), topic))
    if not candidates:
        return None
    return max(candidates)[1]


def score_record(question, record, question_kind):
    q = normalize(question)
    q_tokens = tokens(question)
    topic = normalize(record.get("topic", ""))
    stored_question = normalize(record.get("question", ""))
    command = normalize(record.get("command", ""))
    r_tokens = tokens(record_text(record))

    if not q_tokens:
        return 0.0

    # Weight uncommon query terms more than generic words; avoid long answer text
    # dominating the score.
    overlap = len(q_tokens & r_tokens)
    precision = overlap / max(len(q_tokens), 1)
    jaccard = overlap / max(len(q_tokens | r_tokens), 1)
    score = 0.65 * precision + 0.20 * jaccard

    intent_topic = _topic_intent(question)
    if intent_topic and topic == intent_topic:
        score += 1.25
    elif intent_topic and topic and topic != intent_topic:
        score -= 0.35

    if stored_question and (stored_question in q or q in stored_question):
        score += 0.55
    if command and re.search(r"(?<!\w)" + re.escape(command) + r"(?!\w)", q):
        score += 0.45

    kind = record_kind(record)
    if question_kind != "unknown":
        if kind == question_kind:
            score += 0.30
        elif kind != "unknown":
            score -= 1.0

    return score


def retrieve(question, limit=4):
    question_kind = classify_question(question)
    con = connect()
    records = [dict(row) for row in con.execute("SELECT * FROM qa").fetchall()]
    con.close()

    if not records:
        return [], 0.0, question_kind

    intent_topic = _topic_intent(question)
    ranked = sorted(
        ((score_record(question, record, question_kind), record) for record in records),
        key=lambda item: item[0],
        reverse=True
    )

    best_score = ranked[0][0]

    # Only return same-category candidates; don't contaminate the prompt with
    # unrelated command records when the question is an OS theory question.
    candidates = [
        (score, record) for score, record in ranked
        if question_kind == "unknown" or record_kind(record) == question_kind
    ]

    # If a clear topic was recognized, require the matching topic rather than
    # returning a high lexical-overlap record from another topic.
    if intent_topic:
        topic_matches = [(score, record) for score, record in candidates
                         if normalize(record.get("topic", "")) == intent_topic]
        if topic_matches:
            candidates = topic_matches

    threshold = 0.72 if intent_topic else (0.34 if question_kind != "unknown" else 0.48)
    matches = [record for score, record in candidates if score >= threshold][:limit]

    # A clear intent with topic matches is useful even if the wording differs.
    if not matches and intent_topic:
        matches = [record for score, record in candidates
                   if normalize(record.get("topic", "")) == intent_topic][:limit]

    return matches, best_score, question_kind

def safety_warnings(text):
    return [
        message for pattern, message in DANGEROUS_PATTERNS
        if re.search(pattern, text or "", re.IGNORECASE)
    ]


def ask_ollama(question, examples, question_kind):
    context = "\n\n".join(
        f"Category: {e.get('category', '')}\n"
        f"Topic: {e.get('topic', '')}\n"
        f"Question: {e.get('question', '')}\n"
        f"Command: {e.get('command', '')}\n"
        f"Answer: {e.get('answer', '')}\n"
        f"Note: {e.get('example_or_note', '')}"
        for e in examples[:3]
    ) or "No sufficiently relevant local records were found."

    if question_kind == "os":
        instruction = (
            "This is an Operating Systems concept question. Answer the concept directly. "
            "Do not substitute a Linux command for a theory explanation."
        )
    elif question_kind == "command":
        instruction = (
            "This is a Linux command question. Give the appropriate command, explain its flags, "
            "and state its risk level."
        )
    else:
        instruction = (
            "Determine whether the user asks about a Linux command or an OS concept. "
            "If ambiguous, briefly ask for clarification."
        )

    prompt = (
        "You are LinuxMate, a careful Linux commands and Operating Systems tutor for a university student.\n"
        f"{instruction}\n"
        "Use retrieved records only if they are relevant. Ignore unrelated records instead of copying them. "
        "Never claim that a command was executed. Do not encourage dangerous commands. "
        "Explain risks and safer alternatives when appropriate.\n\n"
        f"Relevant local dataset records:\n{context}\n\n"
        f"Student question: {question}\n\n"
        "Give a clear, direct, student-friendly answer. For OS concepts, include a definition and example. "
        "For Linux commands, include the command, explanation, and an example."
    )

    payload = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.1}
    }).encode("utf-8")

    req = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"}
    )

    with urllib.request.urlopen(req, timeout=45) as response:
        data = json.loads(response.read().decode("utf-8"))

    answer = (data.get("response") or "").strip()
    if not answer:
        raise RuntimeError("Ollama returned an empty response.")
    return answer


def fallback_answer(question, examples, question_kind):
    if not examples:
        return (
            "I couldn't find a sufficiently relevant answer in the local dataset. "
            "Try rephrasing your question or enable Ollama for a broader response. "
            "I won't return an unrelated command as an answer.",
            "no confident dataset match"
        )

    record = examples[0]
    kind = record_kind(record)

    if question_kind == "os" and kind == "command":
        return (
            "I couldn't find a sufficiently relevant Operating Systems concept in the local dataset. "
            "I won't return a Linux command because it would not answer your question. "
            "Enable Ollama or rephrase the concept.",
            "no confident dataset match"
        )

    if question_kind == "command" and kind == "os":
        return (
            "I couldn't find a sufficiently relevant Linux command in the local dataset. "
            "Enable Ollama or ask using a more specific command/topic.",
            "no confident dataset match"
        )

    if kind == "os" or question_kind == "os":
        answer = (
            f"**Topic:** {record.get('topic', 'Operating Systems')}\n\n"
            f"**Explanation:** {record.get('answer', '')}\n\n"
            f"**Example / note:** {record.get('example_or_note', '')}"
        )
    else:
        answer = (
            f"**Topic:** {record.get('topic', '')}\n\n"
            f"**Suggested command:** `{record.get('command', '')}`\n\n"
            f"**Explanation:** {record.get('answer', '')}\n\n"
            f"**Example / note:** {record.get('example_or_note', '')}\n\n"
            f"**Risk level:** {record.get('risk_level', 'Check before running')}."
        )

    return answer, "local dataset retrieval"


@app.route("/")
def index():
    con = connect()
    count = con.execute("SELECT COUNT(*) FROM qa").fetchone()[0]
    history_count = con.execute("SELECT COUNT(*) FROM history").fetchone()[0]
    con.close()
    return render_template(
        "index.html",
        dataset_count=count,
        history_count=history_count,
        model=OLLAMA_MODEL
    )


@app.post("/api/ask")
def api_ask():
    body = request.get_json(silent=True) or request.form
    question = (body.get("question") or "").strip()
    use_model = str(body.get("use_model", "true")).lower() not in (
        "false", "0", "off", "no"
    )

    if not question:
        return jsonify({"error": "Please enter a question."}), 400

    examples, best_score, question_kind = retrieve(question)
    warnings = safety_warnings(question)

    if use_model:
        try:
            answer = ask_ollama(question, examples, question_kind)
            source = (
                f"Ollama ({OLLAMA_MODEL}) + relevant dataset context"
                if examples else f"Ollama ({OLLAMA_MODEL})"
            )
        except (OSError, urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, ValueError, RuntimeError):
            answer, source = fallback_answer(question, examples, question_kind)
            source += " (Ollama unavailable)"
    else:
        answer, source = fallback_answer(question, examples, question_kind)

    for warning in safety_warnings(answer):
        if warning not in warnings:
            warnings.append(warning)

    con = connect()
    cursor = con.execute(
        "INSERT INTO history(question, answer, source) VALUES (?, ?, ?)",
        (question, answer, source)
    )
    history_id = cursor.lastrowid
    con.commit()
    con.close()

    return jsonify({
        "answer": answer,
        "source": source,
        "warnings": warnings,
        "history_id": history_id,
        "question_type": question_kind,
        "best_match_score": round(best_score, 3),
        "retrieved": [
            {
                "topic": record.get("topic", ""),
                "question": record.get("question", ""),
                "score": round(score_record(question, record, question_kind), 3)
            }
            for record in examples[:3]
        ]
    })


@app.get("/api/history")
def api_history():
    con = connect()
    rows = con.execute(
        "SELECT id, question, answer, source, created_at "
        "FROM history ORDER BY id DESC LIMIT 25"
    ).fetchall()
    con.close()
    return jsonify([dict(row) for row in rows])


@app.post("/api/feedback")
def api_feedback():
    body = request.get_json(silent=True) or request.form

    try:
        history_id = int(body.get("history_id"))
    except (ValueError, TypeError):
        return jsonify({"error": "Missing history ID"}), 400

    rating = (body.get("rating") or "").strip()
    if rating not in ("helpful", "not_helpful"):
        return jsonify({"error": "Invalid rating"}), 400

    comment = (body.get("comment") or "").strip()[:1000]
    con = connect()
    con.execute(
        "INSERT INTO feedback(history_id, rating, comment) VALUES (?, ?, ?)",
        (history_id, rating, comment)
    )
    con.commit()
    con.close()
    return jsonify({"ok": True})


@app.get("/api/stats")
def api_stats():
    con = connect()
    total = con.execute("SELECT COUNT(*) FROM qa").fetchone()[0]
    topics = con.execute("SELECT COUNT(DISTINCT topic) FROM qa").fetchone()[0]
    history = con.execute("SELECT COUNT(*) FROM history").fetchone()[0]
    feedback = con.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
    con.close()
    return jsonify({
        "dataset_records": total,
        "topics": topics,
        "questions_asked": history,
        "feedback_count": feedback
    })


@app.get("/health")
def health():
    con = connect()
    count = con.execute("SELECT COUNT(*) FROM qa").fetchone()[0]
    con.close()
    return jsonify({"status": "ok", "dataset_records": count})


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
