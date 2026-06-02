from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from pymongo import MongoClient
from bson.objectid import ObjectId
from bson.errors import InvalidId
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
import gridfs
import io
import re
import uuid
import string
import random

app = Flask(__name__)
CORS(app)

# MongoDB Atlas connection
client = MongoClient("mongodb+srv://ssp_user:SSP123456@cluster0.i7p03mi.mongodb.net/?appName=Cluster0")
db = client["smart_study_planner"]

# GridFS bucket for material file uploads
fs = gridfs.GridFS(db)

ALLOWED_EXTENSIONS = {"pdf", "doc", "docx", "ppt", "pptx", "txt", "png", "jpg", "jpeg"}

def allowed_file(filename):
    """Return True if the file extension is in the allowed set."""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def generate_join_code():
    """Generate a unique 6-character alphanumeric join code."""
    chars = string.ascii_uppercase + string.digits
    for _ in range(100):
        code = ''.join(random.choices(chars, k=6))
        if not db.courses.find_one({"join_code": code}):
            return code
    return ''.join(random.choices(chars, k=8))   # fallback to 8 chars


# ── Normalization helpers (for duplicate detection) ────────────────────────────

def normalize_text(value):
    """Lowercase, strip, collapse whitespace, remove punctuation except spaces."""
    v = value.strip().lower()
    v = re.sub(r"[^a-z0-9 ]", " ", v)
    v = re.sub(r" +", " ", v).strip()
    return v

def normalize_course_code(value):
    """Uppercase and keep only alphanumeric characters."""
    return re.sub(r"[^A-Z0-9]", "", value.strip().upper())

def ensure_indexes():
    """Unique index only when normalized institution and course code are real strings."""
    try:
        db.courses.drop_index("uniq_inst_code")
    except Exception:
        pass

    db.courses.create_index(
        [
            ("institution_normalized", 1),
            ("course_code_normalized", 1)
        ],
        unique=True,
        name="uniq_inst_code",
        partialFilterExpression={
            "institution_normalized": {"$type": "string"},
            "course_code_normalized": {"$type": "string"}
        }
    )
ensure_indexes()


def is_course_member(course_id, username):
    """Return True if username is a member of the given course_id."""
    return bool(db.course_members.find_one({"course_id": course_id, "username": username}))


# ─── Utility ──────────────────────────────────────────────────────────────────

def parse_oid(oid_str):
    """Return ObjectId or None if the string is invalid (prevents 500 errors)."""
    try:
        return ObjectId(oid_str)
    except (InvalidId, TypeError):
        return None


# ─── Health checks ────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return jsonify({"message": "Smart Study Planner backend is running"})


@app.route("/test-db")
def test_db():
    db.users.find_one({})
    return jsonify({"message": "MongoDB connection successful"})


# ─── USERS ────────────────────────────────────────────────────────────────────

@app.route("/register", methods=["POST"])
def register():
    data = request.json or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")

    if not username or not password:
        return jsonify({"error": "Missing username or password"}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    if db.users.find_one({"username": username}):
        return jsonify({"error": "User already exists"}), 400

    # FIX: store hashed password, never plaintext
    db.users.insert_one({"username": username, "password": generate_password_hash(password)})
    return jsonify({"message": "User registered successfully"}), 201


@app.route("/login", methods=["POST"])
def login():
    data = request.json or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")

    if not username or not password:
        return jsonify({"error": "Missing username or password"}), 400

    user = db.users.find_one({"username": username})
    # FIX: use check_password_hash, not plain string comparison
    if user and check_password_hash(user["password"], password):
        return jsonify({"message": "Login successful", "username": username}), 200

    return jsonify({"error": "Invalid username or password"}), 401


# ─── COURSES ──────────────────────────────────────────────────────────────────

@app.route("/courses", methods=["POST"])
def add_course():
    """Create a course group, with two-tier duplicate detection:

    Tier 1 (hard block):  same institution_normalized + course_code_normalized
                          → 409 always, user must join the existing course.
    Tier 2 (soft warn):   same institution_normalized + course_name_normalized
                          → 409 with similar=True, client may resend with
                            force_create=true to override.
    """
    data         = request.json or {}
    username     = data.get("username", "").strip()
    course       = data.get("course", "").strip()
    institution  = data.get("institution", "").strip()
    course_code  = data.get("course_code", "").strip()
    force_create = bool(data.get("force_create", False))

    if not username or not course:
        return jsonify({"error": "Missing username or course name"}), 400

    inst_norm = normalize_text(institution) if institution else ""
    name_norm = normalize_text(course)
    code_norm = normalize_course_code(course_code) if course_code else ""

    # ── Tier 1: hard block on institution + course_code ──────────────────────
    if inst_norm and code_norm:
        existing = db.courses.find_one({
            "institution_normalized": inst_norm,
            "course_code_normalized": code_norm
        })
        if existing:
            mc = db.course_members.count_documents({"course_id": existing["course_id"]})
            return jsonify({
                "error": "This course already exists at your institution. Join the existing course instead.",
                "existing": {
                    "course_id":   existing["course_id"],
                    "course":      existing.get("course", ""),
                    "institution": existing.get("institution", ""),
                    "course_code": existing.get("course_code", ""),
                    "join_code":   existing.get("join_code", ""),
                    "member_count": mc,
                },
                "similar": False,
            }), 409

    # ── Tier 2: soft warn on institution + course name ───────────────────────
    if inst_norm and name_norm and not force_create:
        existing = db.courses.find_one({
            "institution_normalized": inst_norm,
            "course_normalized": name_norm
        })
        if existing:
            mc = db.course_members.count_documents({"course_id": existing["course_id"]})
            return jsonify({
                "error": "A course with a similar name already exists at your institution.",
                "existing": {
                    "course_id":   existing["course_id"],
                    "course":      existing.get("course", ""),
                    "institution": existing.get("institution", ""),
                    "course_code": existing.get("course_code", ""),
                    "join_code":   existing.get("join_code", ""),
                    "member_count": mc,
                },
                "similar": True,
            }), 409

    # ── No duplicate — create the course ─────────────────────────────────────
        # Per-user name uniqueness check (legacy guard)
    course_norm = normalize_text(course)
    institution_norm = normalize_text(institution)
    course_code_norm = normalize_course_code(course_code)

    if db.courses.find_one({
        "username": username,
        "course": course,
        "institution": institution
    }):
        return jsonify({"error": "You already created this exact course."}), 400

    course_id = str(uuid.uuid4())[:8]
    join_code = generate_join_code()
    now = datetime.utcnow().isoformat()

    doc = {
        "course_id": course_id,
        "course": course,
        "join_code": join_code,
        "username": username,
        "created_by": username,
        "created_at": now
    }

    if course_norm:
        doc["course_normalized"] = course_norm

    if institution_norm:
        doc["institution"] = institution
        doc["institution_normalized"] = institution_norm

    if course_code_norm:
        doc["course_code"] = course_code
        doc["course_code_normalized"] = course_code_norm

    db.courses.insert_one(doc)

    db.course_members.insert_one({
        "course_id": course_id,
        "username": username,
        "role": "owner",
        "joined_at": now,
    })

    return jsonify({
        "message": "Course created",
        "course_id": course_id,
        "join_code": join_code,
    }), 201

@app.route("/courses", methods=["GET"])
def search_courses_by_institution():
    institution = request.args.get("institution", "").strip()

    if not institution:
        return jsonify({"error": "institution is required"}), 400

    institution_norm = normalize_text(institution)

    courses = list(db.courses.find(
        {"institution_normalized": institution_norm},
        {"_id": 0}
    ))

    return jsonify(courses), 200

@app.route("/courses/<username>", methods=["GET"])
def get_courses(username):
    """Return all courses the user is a member of, including their role.
    Migrates legacy name-only records on the fly."""
    memberships = list(db.course_members.find({"username": username},
                                               {"course_id": 1, "role": 1, "_id": 0}))
    role_map   = {m["course_id"]: m.get("role", "member") for m in memberships}
    member_ids = list(role_map.keys())

    courses = []
    if member_ids:
        for c in db.courses.find({"course_id": {"$in": member_ids}}):
            c["_id"] = str(c["_id"])
            c["role"] = role_map.get(c["course_id"], "member")
            courses.append(c)

    # Migrate legacy courses (owned by user, no course_id) on the fly
    legacy = list(db.courses.find({"username": username, "course_id": {"$exists": False}}))
    for leg in legacy:
        cid = str(uuid.uuid4())[:8]
        jc  = generate_join_code()
        now = datetime.utcnow().isoformat()
        db.courses.update_one(
            {"_id": leg["_id"]},
            {"$set": {"course_id": cid, "join_code": jc,
                      "institution": "", "course_code": "", "created_at": now}}
        )
        if not db.course_members.find_one({"course_id": cid, "username": username}):
            db.course_members.insert_one({
                "course_id": cid, "username": username, "role": "owner", "joined_at": now
            })
        leg.pop("_id", None)
        leg.update({"course_id": cid, "join_code": jc,
                    "institution": "", "course_code": "", "role": "owner"})
        courses.append(leg)

    return jsonify(courses), 200


@app.route("/courses/join", methods=["POST"])
def join_course():
    """Join an existing course group using a join_code."""
    data      = request.json or {}
    username  = data.get("username", "").strip()
    join_code = data.get("join_code", "").strip().upper()

    if not username or not join_code:
        return jsonify({"error": "username and join_code are required"}), 400

    course = db.courses.find_one({"join_code": join_code})
    if not course:
        return jsonify({"error": "Invalid join code — no course found."}), 404

    course_id = course.get("course_id")
    if not course_id:
        return jsonify({"error": "Course is not yet migrated. Ask the owner to open their Courses page first."}), 422

    if db.course_members.find_one({"course_id": course_id, "username": username}):
        return jsonify({
            "message":     "Already a member of this course",
            "course_id":   course_id,
            "course":      course.get("course", ""),
            "institution": course.get("institution", ""),
            "join_code":   join_code
        }), 200

    db.course_members.insert_one({
        "course_id": course_id,
        "username":  username,
        "role":      "member",
        "joined_at": datetime.utcnow().isoformat()
    })
    return jsonify({
        "message":     "Joined successfully",
        "course_id":   course_id,
        "course":      course.get("course", ""),
        "institution": course.get("institution", ""),
        "join_code":   join_code
    }), 200


@app.route("/institutions/<path:institution>/courses", methods=["GET"])
def search_institution_courses(institution):
    """Return courses at the given institution, optionally filtered by query string.

    GET /institutions/Ramat%20Gan%20College/courses?q=algo
    Returns: list of course objects with member_count.
    """
    q         = request.args.get("q", "").strip()
    inst_norm = normalize_text(institution)

    mongo_filter = {"institution_normalized": inst_norm}

    if q:
        q_norm = normalize_text(q)
        # Match on normalized name OR normalized code prefix
        mongo_filter["$or"] = [
            {"course_normalized":       {"$regex": re.escape(q_norm)}},
            {"course_code_normalized":  {"$regex": "^" + re.escape(
                normalize_course_code(q)
            )}},
        ]

    cursor = db.courses.find(mongo_filter, {"_id": 0}).limit(30)
    results = []
    for c in cursor:
        mc = db.course_members.count_documents({"course_id": c["course_id"]})
        c["member_count"] = mc
        results.append(c)

    # Sort: most members first (helps students find the canonical hub)
    results.sort(key=lambda x: x["member_count"], reverse=True)
    return jsonify(results), 200


@app.route("/courses/join-existing", methods=["POST"])
def join_existing_course():
    """Join an already-known course by course_id (used by the search UI).
    Unlike /courses/join, no join_code is needed — the user found the course
    via institution search.
    """
    data      = request.json or {}
    username  = data.get("username", "").strip()
    course_id = data.get("course_id", "").strip()

    if not username or not course_id:
        return jsonify({"error": "username and course_id are required"}), 400

    course = db.courses.find_one({"course_id": course_id})
    if not course:
        return jsonify({"error": "Course not found"}), 404

    if db.course_members.find_one({"course_id": course_id, "username": username}):
        return jsonify({
            "message":   "You are already a member of this course.",
            "course_id": course_id,
            "course":    course.get("course", ""),
            "join_code": course.get("join_code", ""),
        }), 200

    now = datetime.utcnow().isoformat()
    db.course_members.insert_one({
        "course_id": course_id,
        "username":  username,
        "role":      "member",
        "joined_at": now,
    })
    return jsonify({
        "message":   "Joined course successfully.",
        "course_id": course_id,
        "course":    course.get("course", ""),
        "join_code": course.get("join_code", ""),
    }), 200


@app.route("/courses/<course_id>", methods=["DELETE"])
def delete_course(course_id):
    """
    Remove a user from a course, or fully delete it if they are the sole owner.

    Rules:
      - Non-owner member  -> remove only their course_members record (leave course).
      - Owner + other members exist -> block with 409; ask them to leave first.
      - Owner + no other members    -> full cascade delete:
          course record, all course_members, hub_messages, hub_materials
          (including GridFS files), hub_questions, hub_answers,
          hub_reports for this course.
    """
    data     = request.json or {}
    username = data.get("username", "").strip()

    if not username:
        return jsonify({"error": "username is required"}), 400

    # Verify the course exists
    course = db.courses.find_one({"course_id": course_id})
    if not course:
        return jsonify({"error": "Course not found"}), 404

    # Verify the requesting user is actually a member
    membership = db.course_members.find_one({"course_id": course_id, "username": username})
    if not membership:
        return jsonify({"error": "You are not a member of this course"}), 403

    role = membership.get("role", "member")

    # Non-owner: just leave
    if role != "owner":
        db.course_members.delete_one({"course_id": course_id, "username": username})
        return jsonify({"message": "You have left the course successfully"}), 200

    # Owner: check for other members
    other_count = db.course_members.count_documents(
        {"course_id": course_id, "username": {"$ne": username}}
    )
    if other_count > 0:
        return jsonify({
            "error": (
                f"Cannot delete \u2014 {other_count} other member(s) are still in this course. "
                "They must leave first before you can delete it."
            )
        }), 409

    # Owner with no other members: full cascade delete

    # 1. Collect GridFS file IDs before deleting materials
    for mat in db.hub_materials.find({"course_id": course_id}, {"file_id": 1}):
        fid = mat.get("file_id")
        if fid:
            oid = parse_oid(fid)
            if oid:
                try:
                    if fs.exists(oid):
                        fs.delete(oid)
                except Exception:
                    pass

    # 2. Collect question IDs so we can clean up their answers
    q_ids = [str(q["_id"]) for q in db.hub_questions.find(
        {"course_id": course_id}, {"_id": 1}
    )]

    # 3. Delete all hub content
    if q_ids:
        db.hub_answers.delete_many({"question_id": {"$in": q_ids}})
    db.hub_questions.delete_many({"course_id": course_id})
    db.hub_materials.delete_many({"course_id": course_id})
    db.hub_messages.delete_many({"course_id": course_id})
    db.hub_reports.delete_many({"course_id": course_id})

    # 4. Delete membership records and the course document itself
    db.course_members.delete_many({"course_id": course_id})
    db.courses.delete_one({"course_id": course_id})

    return jsonify({"message": "Course and all its hub content have been permanently deleted"}), 200


# ─── ASSIGNMENTS ──────────────────────────────────────────────────────────────

@app.route("/assignments", methods=["POST"])
def add_assignment():
    data = request.json or {}
    username = data.get("username", "").strip()
    course   = data.get("course", "").strip()
    title    = data.get("title", "").strip()
    deadline = data.get("deadline", "").strip()

    if not username or not course or not title or not deadline:
        return jsonify({"error": "Missing assignment data"}), 400

    # FIX: validate date format before saving
    try:
        datetime.strptime(deadline, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "deadline must be in YYYY-MM-DD format"}), 400

    db.assignments.insert_one({
        "username": username, "course": course,
        "title": title, "deadline": deadline, "completed": False
    })
    return jsonify({"message": "Assignment added successfully"}), 201


@app.route("/assignments/<username>", methods=["GET"])
def get_assignments(username):
    assignments = list(db.assignments.find({"username": username}))
    for a in assignments:
        a["_id"] = str(a["_id"])
    assignments.sort(key=lambda x: x.get("deadline", ""))
    return jsonify(assignments), 200


@app.route("/assignments/complete", methods=["PUT"])
def complete_assignment():
    data = request.json or {}
    assignment_id = data.get("id")

    if not assignment_id:
        return jsonify({"error": "Missing assignment id"}), 400

    # FIX: safe ObjectId parsing
    oid = parse_oid(assignment_id)
    if not oid:
        return jsonify({"error": "Invalid assignment id format"}), 400

    result = db.assignments.update_one({"_id": oid}, {"$set": {"completed": True}})
    if result.modified_count == 0:
        return jsonify({"error": "Assignment not found or already completed"}), 404

    return jsonify({"message": "Assignment marked as completed"}), 200


@app.route("/assignments", methods=["PUT"])
def update_assignment():
    data = request.json or {}
    assignment_id = data.get("id")
    new_title    = data.get("new_title", "").strip()
    new_course   = data.get("new_course", "").strip()
    new_deadline = data.get("new_deadline", "").strip()

    if not assignment_id or not new_title or not new_course or not new_deadline:
        return jsonify({"error": "Missing assignment update data"}), 400

    # FIX: validate date format
    try:
        datetime.strptime(new_deadline, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "new_deadline must be in YYYY-MM-DD format"}), 400

    # FIX: safe ObjectId parsing
    oid = parse_oid(assignment_id)
    if not oid:
        return jsonify({"error": "Invalid assignment id format"}), 400

    result = db.assignments.update_one(
        {"_id": oid},
        {"$set": {"title": new_title, "course": new_course, "deadline": new_deadline}}
    )
    if result.matched_count == 0:
        return jsonify({"error": "Assignment not found"}), 404

    return jsonify({"message": "Assignment updated successfully"}), 200


@app.route("/assignments", methods=["DELETE"])
def delete_assignment():
    data = request.json or {}
    assignment_id = data.get("id")

    if not assignment_id:
        return jsonify({"error": "Missing assignment id"}), 400

    # FIX: safe ObjectId parsing
    oid = parse_oid(assignment_id)
    if not oid:
        return jsonify({"error": "Invalid assignment id format"}), 400

    result = db.assignments.delete_one({"_id": oid})
    if result.deleted_count == 0:
        return jsonify({"error": "Assignment not found"}), 404

    return jsonify({"message": "Assignment deleted successfully"}), 200


# ─── EXAMS (US-05 — was completely missing) ───────────────────────────────────

@app.route("/exams", methods=["POST"])
def add_exam():
    data = request.json or {}
    username = data.get("username", "").strip()
    course   = data.get("course", "").strip()
    date     = data.get("date", "").strip()

    if not username or not course or not date:
        return jsonify({"error": "Missing exam data (username, course, date required)"}), 400

    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "date must be in YYYY-MM-DD format"}), 400

    db.exams.insert_one({"username": username, "course": course, "date": date})
    return jsonify({"message": "Exam added successfully"}), 201


@app.route("/exams/<username>", methods=["GET"])
def get_exams(username):
    exams = list(db.exams.find({"username": username}, {"_id": 0}))
    exams.sort(key=lambda x: x.get("date", ""))
    return jsonify(exams), 200


@app.route("/exams", methods=["DELETE"])
def delete_exam():
    data = request.json or {}
    username = data.get("username", "").strip()
    course   = data.get("course", "").strip()
    date     = data.get("date", "").strip()

    if not username or not course or not date:
        return jsonify({"error": "Missing exam data"}), 400

    result = db.exams.delete_one({"username": username, "course": course, "date": date})
    if result.deleted_count == 0:
        return jsonify({"error": "Exam not found"}), 404

    return jsonify({"message": "Exam deleted successfully"}), 200


# ─── DASHBOARD — Upcoming Deadlines (US-08 — was completely missing) ──────────

@app.route("/dashboard/<username>", methods=["GET"])
def dashboard(username):
    """Returns all assignments and exams due in the next 14 days, sorted by date."""
    today      = datetime.utcnow().date()
    cutoff     = today + timedelta(days=14)
    today_str  = today.isoformat()
    cutoff_str = cutoff.isoformat()

    upcoming = []

    for a in db.assignments.find({"username": username,
                                   "deadline": {"$gte": today_str, "$lte": cutoff_str}}):
        upcoming.append({
            "type": "assignment", "title": a.get("title"),
            "course": a.get("course"), "date": a.get("deadline"),
            "completed": a.get("completed", False), "id": str(a["_id"])
        })

    for e in db.exams.find({"username": username,
                             "date": {"$gte": today_str, "$lte": cutoff_str}}):
        upcoming.append({
            "type": "exam", "title": f"EXAM — {e.get('course')}",
            "course": e.get("course"), "date": e.get("date")
        })

    upcoming.sort(key=lambda x: x.get("date", ""))
    return jsonify({"username": username, "upcoming": upcoming}), 200


# ─── STUDY PLAN — Auto-generate (US-06) ───────────────────────────────────────

@app.route("/study-plan/<username>", methods=["GET"])
def study_plan(username):
    """Schedules 3 study blocks before each upcoming deadline in the next 30 days."""
    today      = datetime.utcnow().date()
    cutoff     = today + timedelta(days=30)
    today_str  = today.isoformat()
    cutoff_str = cutoff.isoformat()

    items = []
    for a in db.assignments.find({"username": username,
                                   "deadline": {"$gte": today_str, "$lte": cutoff_str},
                                   "completed": False}):
        items.append({"title": a["title"], "course": a["course"],
                      "due": a["deadline"], "type": "assignment"})

    for e in db.exams.find({"username": username,
                             "date": {"$gte": today_str, "$lte": cutoff_str}}):
        items.append({"title": f"Exam: {e['course']}", "course": e["course"],
                      "due": e["date"], "type": "exam"})

    items.sort(key=lambda x: x["due"])

    plan = []
    for item in items:
        due_date = datetime.strptime(item["due"], "%Y-%m-%d").date()
        for days_before in range(3, 0, -1):
            study_date = due_date - timedelta(days=days_before)
            if study_date >= today:
                plan.append({
                    "study_date": study_date.isoformat(),
                    "activity": f"Study for: {item['title']}",
                    "course": item["course"],
                    "type": item["type"],
                    "due": item["due"]
                })

    plan.sort(key=lambda x: x["study_date"])
    return jsonify({"username": username, "study_plan": plan}), 200


# ---------------------------------------------------------------------------
# COURSE COMMUNITY HUB  (v2 — course_id based, with membership checks)
# Collections: hub_messages, hub_materials, hub_questions, hub_answers,
#              hub_reports, course_members
# ---------------------------------------------------------------------------

# == Chat ====================================================================

@app.route("/hub/<course_id>/messages", methods=["GET"])
def get_hub_messages(course_id):
    """Return up to 50 messages for a course, oldest first."""
    username = request.args.get("username", "").strip()
    if username and not is_course_member(course_id, username):
        return jsonify({"error": "Not a member of this course"}), 403
    messages = list(
        db.hub_messages
        .find({"course_id": course_id})
        .sort("created_at", 1)
        .limit(50)
    )
    for m in messages:
        m["_id"] = str(m["_id"])
    return jsonify(messages), 200


@app.route("/hub/<course_id>/messages", methods=["POST"])
def post_hub_message(course_id):
    """Post a new message to a course hub."""
    data     = request.json or {}
    username = data.get("username", "").strip()
    text     = data.get("text", "").strip()

    if not username or not text:
        return jsonify({"error": "username and text are required"}), 400
    if not is_course_member(course_id, username):
        return jsonify({"error": "Not a member of this course"}), 403
    if len(text) > 1000:
        return jsonify({"error": "Message too long (max 1000 characters)"}), 400

    db.hub_messages.insert_one({
        "course_id":  course_id,
        "username":   username,
        "text":       text,
        "created_at": datetime.utcnow().isoformat()
    })
    return jsonify({"message": "Message posted"}), 201


# == Materials ================================================================

@app.route("/hub/<course_id>/materials/upload", methods=["POST"])
def upload_hub_file(course_id):
    """Upload a file to GridFS and store metadata in hub_materials."""
    username    = request.form.get("username", "").strip()
    title       = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()

    if not username or not title:
        return jsonify({"error": "username and title are required"}), 400
    if not is_course_member(course_id, username):
        return jsonify({"error": "Not a member of this course"}), 403
    if len(title) > 200:
        return jsonify({"error": "Title too long (max 200 characters)"}), 400
    if "file" not in request.files:
        return jsonify({"error": "No file attached"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400
    if not allowed_file(file.filename):
        return jsonify({"error": "File type not allowed. Accepted: pdf, doc, docx, ppt, pptx, txt, png, jpg, jpeg"}), 400

    safe_name    = secure_filename(file.filename)
    content_type = file.content_type or "application/octet-stream"
    file_id      = fs.put(file.stream, filename=safe_name, content_type=content_type)

    db.hub_materials.insert_one({
        "course_id":    course_id,
        "username":     username,
        "title":        title,
        "description":  description,
        "file_id":      str(file_id),
        "filename":     safe_name,
        "content_type": content_type,
        "upvotes":      0,
        "created_at":   datetime.utcnow().isoformat()
    })
    return jsonify({"message": "File uploaded successfully", "file_id": str(file_id)}), 201


@app.route("/hub/materials/file/<file_id>", methods=["GET"])
def serve_hub_file(file_id):
    """Stream a file from GridFS back to the browser."""
    oid = parse_oid(file_id)
    if not oid:
        return jsonify({"error": "Invalid file id"}), 400
    if not fs.exists(oid):
        return jsonify({"error": "File not found"}), 404

    grid_out = fs.get(oid)
    return send_file(
        io.BytesIO(grid_out.read()),
        mimetype=grid_out.content_type or "application/octet-stream",
        as_attachment=False,
        download_name=grid_out.filename
    )


@app.route("/hub/<course_id>/materials", methods=["GET"])
def get_hub_materials(course_id):
    """Return all shared materials for a course, sorted by upvotes descending."""
    username = request.args.get("username", "").strip()
    if username and not is_course_member(course_id, username):
        return jsonify({"error": "Not a member of this course"}), 403
    materials = list(
        db.hub_materials
        .find({"course_id": course_id})
        .sort("upvotes", -1)
    )
    for m in materials:
        m["_id"] = str(m["_id"])
    return jsonify(materials), 200


@app.route("/hub/<course_id>/materials", methods=["POST"])
def post_hub_material(course_id):
    """Add a shared resource link to a course hub."""
    data        = request.json or {}
    username    = data.get("username", "").strip()
    title       = data.get("title", "").strip()
    description = data.get("description", "").strip()
    link        = data.get("link", "").strip()

    if not username or not title or not link:
        return jsonify({"error": "username, title, and link are required"}), 400
    if not is_course_member(course_id, username):
        return jsonify({"error": "Not a member of this course"}), 403
    if len(title) > 200:
        return jsonify({"error": "Title too long (max 200 characters)"}), 400

    db.hub_materials.insert_one({
        "course_id":   course_id,
        "username":    username,
        "title":       title,
        "description": description,
        "link":        link,
        "upvotes":     0,
        "created_at":  datetime.utcnow().isoformat()
    })
    return jsonify({"message": "Material added"}), 201


@app.route("/hub/materials/<material_id>/upvote", methods=["PUT"])
def upvote_material(material_id):
    """Increment upvote count for a shared material."""
    oid = parse_oid(material_id)
    if not oid:
        return jsonify({"error": "Invalid material id"}), 400
    result = db.hub_materials.update_one({"_id": oid}, {"$inc": {"upvotes": 1}})
    if result.matched_count == 0:
        return jsonify({"error": "Material not found"}), 404
    mat = db.hub_materials.find_one({"_id": oid}, {"upvotes": 1})
    return jsonify({"upvotes": mat["upvotes"]}), 200


# == Q&A ======================================================================

@app.route("/hub/<course_id>/questions", methods=["GET"])
def get_hub_questions(course_id):
    """Return all questions for a course, newest first, with answer count."""
    username = request.args.get("username", "").strip()
    if username and not is_course_member(course_id, username):
        return jsonify({"error": "Not a member of this course"}), 403
    questions = list(
        db.hub_questions
        .find({"course_id": course_id})
        .sort("created_at", -1)
    )
    for q in questions:
        q["_id"] = str(q["_id"])
        q["answer_count"] = db.hub_answers.count_documents({"question_id": q["_id"]})
        if q.get("anonymous"):
            q["username"] = "Anonymous"
    return jsonify(questions), 200


@app.route("/hub/<course_id>/questions", methods=["POST"])
def post_hub_question(course_id):
    """Post a new question to a course hub."""
    data      = request.json or {}
    username  = data.get("username", "").strip()
    question  = data.get("question", "").strip()
    anonymous = bool(data.get("anonymous", False))

    if not username or not question:
        return jsonify({"error": "username and question are required"}), 400
    if not is_course_member(course_id, username):
        return jsonify({"error": "Not a member of this course"}), 403
    if len(question) > 2000:
        return jsonify({"error": "Question too long (max 2000 characters)"}), 400

    db.hub_questions.insert_one({
        "course_id":  course_id,
        "username":   username,
        "question":   question,
        "anonymous":  anonymous,
        "created_at": datetime.utcnow().isoformat()
    })
    return jsonify({"message": "Question posted"}), 201


@app.route("/hub/questions/<question_id>/answers", methods=["GET"])
def get_hub_answers(question_id):
    """Return all answers for a question, most helpful first."""
    answers = list(
        db.hub_answers
        .find({"question_id": question_id})
        .sort("helpful_count", -1)
    )
    for a in answers:
        a["_id"] = str(a["_id"])
    return jsonify(answers), 200


@app.route("/hub/questions/<question_id>/answers", methods=["POST"])
def post_hub_answer(question_id):
    """Post an answer to a Q&A question."""
    data     = request.json or {}
    username = data.get("username", "").strip()
    answer   = data.get("answer", "").strip()

    if not username or not answer:
        return jsonify({"error": "username and answer are required"}), 400
    if len(answer) > 3000:
        return jsonify({"error": "Answer too long (max 3000 characters)"}), 400

    oid = parse_oid(question_id)
    if not oid:
        return jsonify({"error": "Invalid question id"}), 400
    if not db.hub_questions.find_one({"_id": oid}):
        return jsonify({"error": "Question not found"}), 404

    db.hub_answers.insert_one({
        "question_id":   question_id,
        "username":      username,
        "answer":        answer,
        "helpful_count": 0,
        "created_at":    datetime.utcnow().isoformat()
    })
    return jsonify({"message": "Answer posted"}), 201


@app.route("/hub/answers/<answer_id>/helpful", methods=["PUT"])
def mark_answer_helpful(answer_id):
    """Increment helpful_count for an answer."""
    oid = parse_oid(answer_id)
    if not oid:
        return jsonify({"error": "Invalid answer id"}), 400
    result = db.hub_answers.update_one({"_id": oid}, {"$inc": {"helpful_count": 1}})
    if result.matched_count == 0:
        return jsonify({"error": "Answer not found"}), 404
    ans = db.hub_answers.find_one({"_id": oid}, {"helpful_count": 1})
    return jsonify({"helpful_count": ans["helpful_count"]}), 200


# == Activity Feed ============================================================

@app.route("/hub/<course_id>/activity", methods=["GET"])
def hub_activity(course_id):
    """Unified activity feed: combines chat, materials, questions — newest first."""
    username = request.args.get("username", "").strip()
    if username and not is_course_member(course_id, username):
        return jsonify({"error": "Not a member of this course"}), 403

    limit = 30
    feed  = []

    for m in db.hub_messages.find({"course_id": course_id}).sort("created_at", -1).limit(limit):
        feed.append({
            "type":       "chat",
            "id":         str(m["_id"]),
            "username":   m.get("username", ""),
            "text":       m.get("text", ""),
            "created_at": m.get("created_at", ""),
            "course_id":  course_id
        })

    for m in db.hub_materials.find({"course_id": course_id}).sort("created_at", -1).limit(limit):
        item_type = "file" if m.get("file_id") else "url"
        feed.append({
            "type":         item_type,
            "id":           str(m["_id"]),
            "username":     m.get("username", ""),
            "title":        m.get("title", ""),
            "description":  m.get("description", ""),
            "link":         m.get("link", ""),
            "file_id":      m.get("file_id", ""),
            "filename":     m.get("filename", ""),
            "content_type": m.get("content_type", ""),
            "upvotes":      m.get("upvotes", 0),
            "created_at":   m.get("created_at", ""),
            "course_id":    course_id
        })

    for q in db.hub_questions.find({"course_id": course_id}).sort("created_at", -1).limit(limit):
        display_name = "Anonymous" if q.get("anonymous") else q.get("username", "")
        feed.append({
            "type":         "question",
            "id":           str(q["_id"]),
            "username":     display_name,
            "question":     q.get("question", ""),
            "created_at":   q.get("created_at", ""),
            "course_id":    course_id,
            "answer_count": db.hub_answers.count_documents({"question_id": str(q["_id"])})
        })

    feed.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return jsonify(feed[:limit]), 200


# == Reports ======================
if __name__ == "__main__":
    app.run(debug=True)