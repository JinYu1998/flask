import html
import hashlib
import json
import os
import random
import re
import shutil
import sqlite3
import time
import zipfile
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

from flask import Flask, abort, flash, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "uploads"
EXPORT_DIR = BASE_DIR / "exports"
DB_PATH = DATA_DIR / "questions.sqlite3"

ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
OPTION_KEYS = ("A", "B", "C", "D")
IMAGE_REF_SEPARATOR = "\n"
LATEX_COMMANDS = (
    "frac",
    "times",
    "right",
    "left",
    "cdot",
    "sqrt",
    "leq",
    "geq",
    "neq",
    "div",
    "infty",
    "pi",
    "theta",
    "alpha",
    "beta",
    "gamma",
    "delta",
    "begin",
    "end",
)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "local-dev-question-bank")
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024
app.config["UPLOAD_FOLDER"] = str(UPLOAD_DIR)


def ensure_dirs():
    DATA_DIR.mkdir(exist_ok=True)
    UPLOAD_DIR.mkdir(exist_ok=True)
    EXPORT_DIR.mkdir(exist_ok=True)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    ensure_dirs()
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question_no TEXT NOT NULL DEFAULT '',
                question TEXT NOT NULL,
                option_a TEXT NOT NULL,
                option_b TEXT NOT NULL,
                option_c TEXT NOT NULL,
                option_d TEXT NOT NULL,
                answer TEXT NOT NULL DEFAULT '',
                explanation TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '',
                question_image TEXT,
                question_image_position TEXT NOT NULL DEFAULT 'before',
                explanation_image TEXT,
                source TEXT NOT NULL DEFAULT 'manual',
                created_at TEXT NOT NULL
            )
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(questions)").fetchall()}
        if "question_no" not in columns:
            conn.execute("ALTER TABLE questions ADD COLUMN question_no TEXT NOT NULL DEFAULT ''")
        if "source" not in columns:
            conn.execute("ALTER TABLE questions ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
        if "question_image_position" not in columns:
            conn.execute(
                "ALTER TABLE questions ADD COLUMN question_image_position TEXT NOT NULL DEFAULT 'before'"
            )
        conn.commit()


def today_string():
    return date.today().isoformat()


def display_day(value):
    dt = datetime.strptime(value, "%Y-%m-%d")
    return f"{dt.month:02d}月{dt.day:02d}日"


def deck_name_for_day(value):
    dt = datetime.strptime(value, "%Y-%m-%d")
    return f"数量关系题目::{dt.month:02d}月{dt.day:02d}日"


def export_stem_for_day(value):
    dt = datetime.strptime(value, "%Y-%m-%d")
    return f"{dt.month:02d}月{dt.day:02d}日_题目"


def allowed_image(filename):
    return Path(filename).suffix.lower() in ALLOWED_IMAGE_EXTENSIONS


def save_uploaded_file(file):
    if not file or not file.filename:
        return None
    if not allowed_image(file.filename):
        raise ValueError("只支持 png、jpg、jpeg、gif、webp、bmp 图片")
    original = secure_filename(file.filename)
    suffix = Path(original).suffix.lower()
    filename = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid4().hex}{suffix}"
    file.save(UPLOAD_DIR / filename)
    return filename


def save_upload(field_name):
    return save_uploaded_file(request.files.get(field_name))


def is_remote_image(value):
    return bool(re.match(r"^https?://", value or "", flags=re.I))


def pack_image_refs(values):
    return IMAGE_REF_SEPARATOR.join(value.strip() for value in values if value and value.strip())


def image_refs(value):
    if not value:
        return []
    return [part.strip() for part in str(value).split(IMAGE_REF_SEPARATOR) if part.strip()]


def import_image_refs(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def question_display_no(question):
    return question["question_no"] if "question_no" in question.keys() and question["question_no"] else question["id"]


def question_value(question, key, default=""):
    return question[key] if key in question.keys() and question[key] is not None else default


def normalize_tags(tags):
    parts = re.split(r"[,，\s]+", tags.strip())
    return " ".join(part for part in parts if part)


def protect_latex_backslashes(raw_text):
    command_pattern = "|".join(re.escape(command) for command in LATEX_COMMANDS)
    return re.sub(rf"(?<!\\)\\(?=({command_pattern})\b)", r"\\\\", raw_text)


def repair_latex_escapes(value):
    if isinstance(value, str):
        replacements = {
            "\x0crac": r"\frac",
            "\times": r"\times",
            "\right": r"\right",
            "\b": r"\b",
        }
        for broken, fixed in replacements.items():
            value = value.replace(broken, fixed)
        return value
    if isinstance(value, list):
        return [repair_latex_escapes(item) for item in value]
    if isinstance(value, dict):
        return {key: repair_latex_escapes(item) for key, item in value.items()}
    return value


def load_import_json(file_storage):
    raw_text = file_storage.read().decode("utf-8-sig")
    try:
        return repair_latex_escapes(json.loads(protect_latex_backslashes(raw_text)))
    except json.JSONDecodeError:
        return repair_latex_escapes(json.loads(raw_text))


def fetch_questions(day=None, source="manual"):
    with get_db() as conn:
        if day:
            rows = conn.execute(
                "SELECT * FROM questions WHERE date(created_at) = ? AND source = ? ORDER BY id DESC",
                (day, source),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM questions WHERE source = ? ORDER BY id DESC", (source,)
            ).fetchall()
    return rows


def fetch_days(source="manual"):
    with get_db() as conn:
        return conn.execute(
            """
            SELECT date(created_at) AS day, COUNT(*) AS count
            FROM questions
            WHERE source = ?
            GROUP BY day
            ORDER BY day DESC
            """,
            (source,),
        ).fetchall()


def fetch_question(question_id):
    with get_db() as conn:
        return conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()


def inline_markdown(text):
    escaped = html.escape(text or "")
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    return escaped


def markdown_to_html(text):
    lines = (text or "").replace("\r\n", "\n").split("\n")
    output = []
    in_list = False
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            if in_list:
                output.append("</ul>")
                in_list = False
            continue
        heading = re.match(r"^(#{1,3})\s+(.+)$", line)
        bullet = re.match(r"^[-*]\s+(.+)$", line)
        if heading:
            if in_list:
                output.append("</ul>")
                in_list = False
            level = len(heading.group(1))
            output.append(f"<h{level}>{inline_markdown(heading.group(2))}</h{level}>")
        elif bullet:
            if not in_list:
                output.append("<ul>")
                in_list = True
            output.append(f"<li>{inline_markdown(bullet.group(1))}</li>")
        else:
            if in_list:
                output.append("</ul>")
                in_list = False
            output.append(f"<p>{inline_markdown(line)}</p>")
    if in_list:
        output.append("</ul>")
    return "\n".join(output)


def convert_dollar_math_for_anki(markup):
    placeholders = []

    def stash_display(match):
        placeholders.append(f"\\[{match.group(1)}\\]")
        return f"@@ANKI_MATH_{len(placeholders) - 1}@@"

    markup = re.sub(r"\$\$(.+?)\$\$", stash_display, markup, flags=re.S)
    markup = re.sub(r"(?<!\\)\$(.+?)(?<!\\)\$", r"\\(\1\\)", markup, flags=re.S)
    for index, value in enumerate(placeholders):
        markup = markup.replace(f"@@ANKI_MATH_{index}@@", value)
    return markup


def anki_markdown_to_html(text):
    return convert_dollar_math_for_anki(markdown_to_html(text))


@app.template_filter("md")
def md_filter(value):
    return markdown_to_html(value)


@app.template_filter("day_label")
def day_label(value):
    return display_day(value)


@app.template_filter("image_refs")
def image_refs_filter(value):
    return image_refs(value)


@app.template_filter("import_image_refs")
def import_image_refs_filter(value):
    return import_image_refs(value)


@app.template_filter("image_src")
def image_src(value):
    return value if is_remote_image(value) else url_for("uploaded_file", filename=value)


@app.template_filter("question_no")
def question_no_filter(value):
    return question_display_no(value)


@app.route("/")
def index():
    recent = fetch_questions(today_string())[:5]
    return render_template("index.html", recent=recent, today=today_string())


@app.route("/questions", methods=["POST"])
def create_question():
    try:
        question_image = save_upload("question_image")
        explanation_image = save_upload("explanation_image")
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("index"))

    form = request.form
    required = ["question", "option_a", "option_b", "option_c", "option_d", "answer", "explanation"]
    missing = [field for field in required if not form.get(field, "").strip()]
    if missing:
        flash("题目、ABCD 选项、答案和解析都需要填写", "error")
        return redirect(url_for("index"))

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO questions (
                question_no, question, option_a, option_b, option_c, option_d, answer,
                explanation, tags, question_image, explanation_image, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                form.get("question_no", "").strip(),
                form["question"].strip(),
                form["option_a"].strip(),
                form["option_b"].strip(),
                form["option_c"].strip(),
                form["option_d"].strip(),
                form.get("answer", "").strip(),
                form["explanation"].strip(),
                normalize_tags(form.get("tags", "")),
                question_image,
                explanation_image,
                "manual",
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    flash("题目已记录", "success")
    return redirect(url_for("today"))


@app.route("/questions/<int:question_id>/edit", methods=["GET", "POST"])
def edit_question(question_id):
    question = fetch_question(question_id)
    if not question:
        abort(404)

    if request.method == "GET":
        return render_template("edit.html", q=question)

    try:
        question_image = save_upload("question_image")
        explanation_image = save_upload("explanation_image")
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("edit_question", question_id=question_id))

    form = request.form
    required = ["question", "option_a", "option_b", "option_c", "option_d", "explanation"]
    missing = [field for field in required if not form.get(field, "").strip()]
    if missing:
        flash("题目、ABCD 选项和解析都需要填写", "error")
        return redirect(url_for("edit_question", question_id=question_id))

    with get_db() as conn:
        conn.execute(
            """
            UPDATE questions
            SET question_no = ?, question = ?, option_a = ?, option_b = ?, option_c = ?, option_d = ?,
                answer = ?, explanation = ?, tags = ?,
                question_image = ?, explanation_image = ?
            WHERE id = ?
            """,
            (
                form.get("question_no", "").strip(),
                form["question"].strip(),
                form["option_a"].strip(),
                form["option_b"].strip(),
                form["option_c"].strip(),
                form["option_d"].strip(),
                form.get("answer", "").strip().upper(),
                form["explanation"].strip(),
                normalize_tags(form.get("tags", "")),
                question_image or question["question_image"],
                explanation_image or question["explanation_image"],
                question_id,
            ),
        )
        conn.commit()
    flash(f"第 {question_id} 题已更新", "success")
    return redirect(request.form.get("next") or url_for("history_day", day=question["created_at"][:10]))


@app.route("/questions/<int:question_id>/delete", methods=["POST"])
def delete_question(question_id):
    question = fetch_question(question_id)
    if not question:
        abort(404)
    with get_db() as conn:
        conn.execute("DELETE FROM questions WHERE id = ?", (question_id,))
        conn.commit()
    flash(f"已删除第 {question_id} 题", "success")
    next_url = request.form.get("next") or url_for("today")
    return redirect(next_url)


@app.route("/today")
def today():
    day = today_string()
    questions = fetch_questions(day)
    return render_template("day.html", questions=questions, day=day, is_today=True)


@app.route("/history")
def history():
    return render_template("history.html", days=fetch_days(), json_days=fetch_days("json"))


@app.route("/history/<day>")
def history_day(day):
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        abort(404)
    questions = fetch_questions(day)
    return render_template("day.html", questions=questions, day=day, is_today=(day == today_string()))


@app.route("/history/json/<day>")
def json_history_day(day):
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        abort(404)
    questions = fetch_questions(day, "json")
    return render_template("day.html", questions=questions, day=day, is_today=False, is_json=True)


@app.route("/import-json", methods=["GET", "POST"])
def import_json():
    if request.method == "GET":
        return render_template("import_json.html", imported=None)

    import_file = request.files.get("json_file")
    if import_file and import_file.filename:
        try:
            payload = load_import_json(import_file)
        except (UnicodeDecodeError, json.JSONDecodeError):
            flash("JSON 文件读取失败，请确认格式正确", "error")
            return redirect(url_for("import_json"))
        if not isinstance(payload, list):
            flash("JSON 顶层需要是题目数组", "error")
            return redirect(url_for("import_json"))
        return render_template("import_json.html", imported=payload)

    raw_items = request.form.getlist("item_json")
    if not raw_items:
        flash("请先选择 JSON 文件", "error")
        return redirect(url_for("import_json"))

    explanations = request.form.getlist("explanation")
    question_nos = request.form.getlist("question_no")
    imported_tags = request.form.getlist("tags")
    imported_questions = request.form.getlist("question")
    option_as = request.form.getlist("option_a")
    option_bs = request.form.getlist("option_b")
    option_cs = request.form.getlist("option_c")
    option_ds = request.form.getlist("option_d")
    answers = request.form.getlist("answer")
    question_image_positions = request.form.getlist("question_image_position")
    questions = []
    created_at = datetime.now().isoformat(timespec="seconds")
    for index, raw_item in enumerate(raw_items):
        try:
            item = json.loads(raw_item)
        except json.JSONDecodeError:
            continue
        images = item.get("图片") or []
        if isinstance(images, str):
            images = [images]
        answer = answers[index].strip().upper() if index < len(answers) else str(item.get("答案", "")).strip().upper()
        if answer not in OPTION_KEYS:
            answer = ""
        image_position = (
            question_image_positions[index].strip().lower()
            if index < len(question_image_positions)
            else "before"
        )
        if image_position not in {"before", "after"}:
            image_position = "before"
        try:
            uploaded_question_image = save_uploaded_file(
                request.files.get(f"question_image_{index}")
            )
            uploaded_explanation_image = save_uploaded_file(
                request.files.get(f"explanation_image_{index}")
            )
        except ValueError as exc:
            flash(f"第 {index + 1} 题：{exc}", "error")
            return redirect(url_for("import_json"))
        questions.append(
            {
                "id": index + 1,
                "question_no": (question_nos[index] if index < len(question_nos) else str(item.get("题号", index + 1))).strip(),
                "question": (imported_questions[index] if index < len(imported_questions) else str(item.get("题目", ""))).strip(),
                "option_a": (option_as[index] if index < len(option_as) else str(item.get("A", ""))).strip(),
                "option_b": (option_bs[index] if index < len(option_bs) else str(item.get("B", ""))).strip(),
                "option_c": (option_cs[index] if index < len(option_cs) else str(item.get("C", ""))).strip(),
                "option_d": (option_ds[index] if index < len(option_ds) else str(item.get("D", ""))).strip(),
                "answer": answer,
                "explanation": explanations[index].strip() if index < len(explanations) else "",
                "tags": normalize_tags(imported_tags[index] if index < len(imported_tags) else str(item.get("标签", ""))),
                "question_image": pack_image_refs(
                    [str(value) for value in images] + [uploaded_question_image]
                ),
                "question_image_position": image_position,
                "explanation_image": uploaded_explanation_image,
                "source": "json",
                "created_at": created_at,
            }
        )
    if not questions:
        flash("没有可导出的题目", "error")
        return redirect(url_for("import_json"))
    with get_db() as conn:
        conn.executemany(
            """
            INSERT INTO questions (
                question_no, question, option_a, option_b, option_c, option_d, answer,
                explanation, tags, question_image, question_image_position,
                explanation_image, source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    question["question_no"],
                    question["question"],
                    question["option_a"],
                    question["option_b"],
                    question["option_c"],
                    question["option_d"],
                    question["answer"],
                    question["explanation"],
                    question["tags"],
                    question["question_image"],
                    question["question_image_position"],
                    question["explanation_image"],
                    question["source"],
                    question["created_at"],
                )
                for question in questions
            ],
        )
        conn.commit()
    flash(f"已导入 {len(questions)} 道 JSON 题目，可在历史页继续编辑", "success")
    path = build_apkg(today_string(), questions, request.form.get("deck_name"))
    return send_file(path, as_attachment=True, download_name=path.name)


@app.route("/uploads/<filename>")
def uploaded_file(filename):
    return send_file(UPLOAD_DIR / secure_filename(filename))


@app.route("/exports/today")
def export_today():
    day = today_string()
    questions = fetch_questions(day)
    if not questions:
        flash("今天还没有题目，无法导出 Anki 包", "error")
        return redirect(url_for("today"))
    path = build_apkg(day, questions, request.args.get("deck_name"))
    return send_file(path, as_attachment=True, download_name=path.name)


@app.route("/exports/<day>")
def export_day(day):
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        abort(404)
    questions = fetch_questions(day)
    if not questions:
        flash("这一天还没有题目，无法导出 Anki 包", "error")
        return redirect(url_for("history_day", day=day))
    path = build_apkg(day, questions, request.args.get("deck_name"))
    return send_file(path, as_attachment=True, download_name=path.name)


@app.route("/exports/json/<day>")
def export_json_day(day):
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        abort(404)
    questions = fetch_questions(day, "json")
    if not questions:
        flash("这一天还没有 JSON 导入题目，无法导出 Anki 包", "error")
        return redirect(url_for("json_history_day", day=day))
    path = build_apkg(day, questions, request.args.get("deck_name") or f"JSON导入题目::{display_day(day)}")
    return send_file(path, as_attachment=True, download_name=path.name)


def field_html(label, value):
    return f"<section><h3>{html.escape(label)}</h3>{anki_markdown_to_html(value)}</section>"


def image_html(filename):
    return "".join(f'<p><img src="{html.escape(ref)}" /></p>' for ref in image_refs(filename))


def option_html(key, value, answer):
    correct = "1" if answer and key == answer else "0"
    return (
        f'<button class="choice" type="button" data-choice="{key}" data-correct="{correct}">'
        f'<span class="choice-key">{key}</span>'
        f'<span class="choice-text">{anki_markdown_to_html(value)}</span>'
        "</button>"
    )


def build_front(question):
    answer = (question["answer"] or "").strip().upper()
    question_content = field_html("题目", question["question"])
    question_images = image_html(question["question_image"])
    if question_value(question, "question_image_position", "before") == "after":
        parts = [question_content, question_images, "<div class='choices'>"]
    else:
        parts = [question_images, question_content, "<div class='choices'>"]
    for key in OPTION_KEYS:
        value = question[f"option_{key.lower()}"]
        parts.append(option_html(key, value, answer))
    parts.append("</div><p class='choice-feedback'></p>")
    return "".join(parts)


def build_back(question):
    answer = html.escape(question["answer"] or "未标注")
    tags = html.escape(question["tags"] or "")
    parts = [
        f"<p><strong>答案：</strong>{answer}</p>",
        field_html("解析", question["explanation"]),
        image_html(question["explanation_image"]),
    ]
    if tags:
        parts.append(f"<p class='tags'>{tags}</p>")
    return "".join(parts)


def anki_guid(source):
    value = int(hashlib.sha1(source.encode("utf-8")).hexdigest()[:12], 16)
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
    result = []
    while value:
        value, idx = divmod(value, len(alphabet))
        result.append(alphabet[idx])
    return "".join(result) or "0"


def build_apkg(day, questions, deck_name=None):
    deck_name = (deck_name or "").strip() or deck_name_for_day(day)
    timestamp = int(time.time())
    export_stem = export_stem_for_day(day)
    package_dir = EXPORT_DIR / f"{export_stem}_{timestamp}_{uuid4().hex}"
    media_dir = package_dir / "media_files"
    collection_path = package_dir / "collection.anki2"
    package_dir.mkdir(parents=True, exist_ok=True)
    media_dir.mkdir(parents=True, exist_ok=True)

    deck_id = random.randrange(10**12, 10**13)
    model_id = random.randrange(10**12, 10**13)
    media = {}
    media_index = 0

    def collect_media(filename):
        nonlocal media_index
        refs = image_refs(filename)
        if not refs:
            return None
        for ref in refs:
            if is_remote_image(ref):
                continue
            src = UPLOAD_DIR / ref
            if not src.exists():
                continue
            dest = media_dir / str(media_index)
            shutil.copyfile(src, dest)
            media[str(media_index)] = ref
            media_index += 1
        return None

    for question in questions:
        collect_media(question["question_image"])
        collect_media(question["explanation_image"])

    conn = sqlite3.connect(collection_path)
    try:
        create_anki_schema(conn)
        insert_anki_collection(conn, deck_id, deck_name, model_id, timestamp)
        for question in questions:
            insert_anki_card(conn, deck_id, model_id, question, timestamp)
        conn.commit()
    finally:
        conn.close()

    media_json = package_dir / "media"
    media_json.write_text(json.dumps(media, ensure_ascii=False), encoding="utf-8")

    apkg_path = EXPORT_DIR / f"{export_stem}.apkg"
    with zipfile.ZipFile(apkg_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(collection_path, "collection.anki2")
        zf.write(media_json, "media")
        for media_file in media_dir.iterdir():
            zf.write(media_file, media_file.name)
    shutil.rmtree(package_dir)
    return apkg_path


def create_anki_schema(conn):
    conn.executescript(
        """
        CREATE TABLE col (
            id integer primary key,
            crt integer not null,
            mod integer not null,
            scm integer not null,
            ver integer not null,
            dty integer not null,
            usn integer not null,
            ls integer not null,
            conf text not null,
            models text not null,
            decks text not null,
            dconf text not null,
            tags text not null
        );
        CREATE TABLE notes (
            id integer primary key,
            guid text not null,
            mid integer not null,
            mod integer not null,
            usn integer not null,
            tags text not null,
            flds text not null,
            sfld text not null,
            csum integer not null,
            flags integer not null,
            data text not null
        );
        CREATE TABLE cards (
            id integer primary key,
            nid integer not null,
            did integer not null,
            ord integer not null,
            mod integer not null,
            usn integer not null,
            type integer not null,
            queue integer not null,
            due integer not null,
            ivl integer not null,
            factor integer not null,
            reps integer not null,
            lapses integer not null,
            left integer not null,
            odue integer not null,
            odid integer not null,
            flags integer not null,
            data text not null
        );
        CREATE TABLE revlog (
            id integer primary key,
            cid integer not null,
            usn integer not null,
            ease integer not null,
            ivl integer not null,
            lastIvl integer not null,
            factor integer not null,
            time integer not null,
            type integer not null
        );
        CREATE TABLE graves (
            usn integer not null,
            oid integer not null,
            type integer not null
        );
        CREATE INDEX ix_notes_usn on notes (usn);
        CREATE INDEX ix_cards_usn on cards (usn);
        CREATE INDEX ix_cards_nid on cards (nid);
        CREATE INDEX ix_cards_sched on cards (did, queue, due);
        CREATE INDEX ix_revlog_usn on revlog (usn);
        CREATE INDEX ix_revlog_cid on revlog (cid);
        """
    )


def insert_anki_collection(conn, deck_id, deck_name, model_id, timestamp):
    decks = {
        str(deck_id): {
            "id": deck_id,
            "mod": timestamp,
            "name": deck_name,
            "usn": 0,
            "lrnToday": [0, 0],
            "revToday": [0, 0],
            "newToday": [0, 0],
            "timeToday": [0, 0],
            "collapsed": False,
            "browserCollapsed": False,
            "desc": "",
            "dyn": 0,
            "conf": 1,
            "extendNew": 10,
            "extendRev": 50,
        }
    }
    models = {
        str(model_id): {
            "id": model_id,
            "name": "题目记录",
            "type": 0,
            "mod": timestamp,
            "usn": 0,
            "sortf": 0,
            "did": deck_id,
            "tmpls": [
                {
                    "name": "Card 1",
                    "ord": 0,
                    "qfmt": "{{Front}}\n<script>\n(function(){\n  var root = document.currentScript ? document.currentScript.parentElement : document;\n  var buttons = root.querySelectorAll('.choice');\n  var feedback = root.querySelector('.choice-feedback');\n  for (var i = 0; i < buttons.length; i++) {\n    buttons[i].onclick = function(){\n      for (var j = 0; j < buttons.length; j++) {\n        buttons[j].classList.remove('selected', 'right', 'wrong');\n        if (buttons[j].getAttribute('data-correct') === '1') {\n          buttons[j].classList.add('right');\n        }\n      }\n      this.classList.add('selected');\n      if (this.getAttribute('data-correct') === '1') {\n        this.classList.add('right');\n        feedback.textContent = '回答正确';\n      } else {\n        this.classList.add('wrong');\n        feedback.textContent = '回答错误，正确答案已高亮';\n      }\n    };\n  }\n})();\n</script>",
                    "afmt": "{{FrontSide}}<hr id=answer>{{Back}}",
                    "did": None,
                    "bqfmt": "",
                    "bafmt": "",
                }
            ],
            "flds": [
                {"name": "Front", "ord": 0, "sticky": False, "rtl": False, "font": "Arial", "size": 20},
                {"name": "Back", "ord": 1, "sticky": False, "rtl": False, "font": "Arial", "size": 20},
                {"name": "Answer", "ord": 2, "sticky": False, "rtl": False, "font": "Arial", "size": 20},
            ],
            "css": """
.card { font-family: Arial, sans-serif; font-size: 18px; line-height: 1.55; text-align: left; color: #1f2937; }
img { max-width: 100%; border-radius: 6px; }
h3 { margin: 0.8em 0 0.2em; font-size: 18px; }
.choices { display: grid; grid-template-columns: 1fr; gap: 10px; margin-top: 14px; }
.choice { display: grid; grid-template-columns: 28px 1fr; align-items: center; gap: 8px; width: 100%; min-height: 54px; padding: 10px 12px; border: 1px solid #dbe3ef; border-radius: 7px; background: #fbfcff; color: #1f2937; font: inherit; text-align: left; cursor: pointer; }
.choice-key { display: inline-flex; align-items: center; justify-content: center; width: 28px; height: 28px; border-radius: 999px; background: #eef4ff; color: #2563eb; font-weight: 700; }
.choice-text p { margin: 0; }
.choice.selected { border-width: 2px; }
.choice.right { border-color: #16a34a; background: #ecfdf3; }
.choice.wrong { border-color: #dc2626; background: #fef2f2; }
.choice-feedback { min-height: 24px; color: #2563eb; font-weight: 700; }
.tags { color: #64748b; font-size: 14px; }
code { background: #f1f5f9; padding: 0.1em 0.25em; border-radius: 4px; }
""",
            "latexPre": "\\documentclass[12pt]{article}\n\\special{papersize=3in,5in}\n\\usepackage{amssymb,amsmath}\n\\pagestyle{empty}\n\\begin{document}",
            "latexPost": "\\end{document}",
            "req": [[0, "any", [0]]],
        }
    }
    conf = {"nextPos": 1, "estTimes": True, "activeDecks": [deck_id], "sortType": "noteFld"}
    dconf = {
        "1": {
            "id": 1,
            "name": "Default",
            "mod": 0,
            "usn": 0,
            "maxTaken": 60,
            "autoplay": True,
            "timer": 0,
            "replayq": True,
            "new": {"delays": [1, 10], "ints": [1, 4, 0], "initialFactor": 2500, "perDay": 20},
            "rev": {"perDay": 200, "ease4": 1.3, "fuzz": 0.05, "minSpace": 1, "ivlFct": 1},
            "lapse": {"delays": [10], "mult": 0, "minInt": 1, "leechFails": 8, "leechAction": 0},
        }
    }
    conn.execute(
        "INSERT INTO col VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            1,
            timestamp,
            timestamp,
            timestamp,
            11,
            0,
            0,
            0,
            json.dumps(conf),
            json.dumps(models, ensure_ascii=False),
            json.dumps(decks, ensure_ascii=False),
            json.dumps(dconf),
            "{}",
        ),
    )


def checksum(text):
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return int(digest, 16)


def insert_anki_card(conn, deck_id, model_id, question, timestamp):
    note_id = int(time.time() * 1000) + question["id"]
    card_id = note_id + 100000
    front = build_front(question)
    back = build_back(question)
    fields = f"{front}\x1f{back}\x1f{html.escape(question['answer'] or '')}"
    tags = " ".join(question["tags"].split())
    conn.execute(
        "INSERT INTO notes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            note_id,
            anki_guid(f"{question_value(question, 'question_no') or question['id']}-{question['created_at']}"),
            model_id,
            timestamp,
            0,
            f" {tags} " if tags else "",
            fields,
            html.escape(question["question"])[:100],
            checksum(question["question"]),
            0,
            "",
        ),
    )
    conn.execute(
        "INSERT INTO cards VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            card_id,
            note_id,
            deck_id,
            0,
            timestamp,
            0,
            0,
            0,
            question["id"],
            0,
            2500,
            0,
            0,
            0,
            0,
            0,
            0,
            "",
        ),
    )


init_db()



if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
