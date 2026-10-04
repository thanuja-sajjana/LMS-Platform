import math
import os
import sqlite3
import uuid
from html import escape
from urllib.parse import parse_qs, urlparse
from flask import Flask, abort, g, has_app_context, render_template_string, request, redirect, url_for, session, flash, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IS_PRODUCTION = os.environ.get("APP_ENV", "development").strip().lower() == "production"
SECRET_KEY = os.environ.get("SECRET_KEY")
if IS_PRODUCTION and not SECRET_KEY:
    raise RuntimeError("SECRET_KEY must be set when APP_ENV=production")

DATA_DIR = os.environ.get("LMS_DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = SECRET_KEY or "local-development-secret-key"
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = IS_PRODUCTION
DB_NAME = os.path.join(DATA_DIR, "lms.db")

HTML_LAYOUT_PATH = os.path.join(BASE_DIR, "index.html")
UPLOAD_FOLDER = os.path.join(DATA_DIR, "uploads")

with open(HTML_LAYOUT_PATH, "r", encoding="utf-8") as layout_file:
    HTML_LAYOUT = layout_file.read()


@app.context_processor
def inject_notification_count():
    user_id = session.get("user_id")
    if user_id is None:
        return {"unread_notification_count": 0}

    count = get_db().execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read_at IS NULL",
        (user_id,),
    ).fetchone()[0]
    return {"unread_notification_count": count}


def get_db():
    if has_app_context():
        conn = g.get("_database")
        if conn is None:
            conn = sqlite3.connect(DB_NAME)
            conn.row_factory = sqlite3.Row
            g._database = conn
        return conn

    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


@app.teardown_appcontext
def close_db(error=None):
    conn = g.pop("_database", None)
    if conn is not None:
        conn.close()


def normalize_youtube_embed(video_url):
    if not video_url:
        return ""

    parsed = urlparse(video_url)
    host = parsed.netloc.lower()

    if "youtube.com" in host or "www.youtube.com" in host or "youtu.be" in host:
        if "youtube.com/watch" in video_url or "www.youtube.com/watch" in video_url:
            query = parse_qs(parsed.query)
            video_id = query.get("v", [""])[0]
            if video_id:
                return f"https://www.youtube.com/embed/{video_id}"
        if "youtu.be/" in video_url:
            video_id = video_url.split("youtu.be/")[-1].split("?")[0]
            if video_id:
                return f"https://www.youtube.com/embed/{video_id}"
        if "youtube.com/embed/" in video_url:
            return video_url

    return ""


def is_admin_user():
    username = session.get("username")
    role = session.get("role")
    return role == "instructor" or username in {"owner", "developer"}


def init_db():
    if not has_app_context():
        with app.app_context():
            init_db()
        return

    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT CHECK(role IN ('student', 'instructor')) NOT NULL
            );

            CREATE TABLE IF NOT EXISTS courses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                instructor_id INTEGER NOT NULL,
                FOREIGN KEY (instructor_id) REFERENCES users (id)
            );

            CREATE TABLE IF NOT EXISTS lessons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                video_url TEXT,
                FOREIGN KEY (course_id) REFERENCES courses (id)
            );

            CREATE TABLE IF NOT EXISTS enrollments (
                student_id INTEGER NOT NULL,
                course_id INTEGER NOT NULL,
                PRIMARY KEY (student_id, course_id),
                FOREIGN KEY (student_id) REFERENCES users (id),
                FOREIGN KEY (course_id) REFERENCES courses (id)
            );

            CREATE TABLE IF NOT EXISTS assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                course_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                instructions TEXT NOT NULL,
                FOREIGN KEY (course_id) REFERENCES courses (id)
            );

            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assignment_id INTEGER NOT NULL,
                student_id INTEGER NOT NULL,
                stored_filename TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                grade REAL CHECK (grade IS NULL OR (grade >= 0 AND grade <= 100)),
                feedback TEXT,
                graded_at TEXT,
                UNIQUE (assignment_id, student_id),
                FOREIGN KEY (assignment_id) REFERENCES assignments (id),
                FOREIGN KEY (student_id) REFERENCES users (id)
            );

            CREATE TABLE IF NOT EXISTS lesson_completions (
                student_id INTEGER NOT NULL,
                lesson_id INTEGER NOT NULL,
                completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (student_id, lesson_id),
                FOREIGN KEY (student_id) REFERENCES users (id),
                FOREIGN KEY (lesson_id) REFERENCES lessons (id)
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                course_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                read_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users (id),
                FOREIGN KEY (course_id) REFERENCES courses (id)
            );
        """)

        conn.execute(
            """
            DELETE FROM lessons
            WHERE id IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (PARTITION BY course_id, LOWER(title) ORDER BY id) AS row_num
                    FROM lessons
                )
                WHERE row_num > 1
            )
            """
        )

        conn.execute(
            """
            DELETE FROM courses
            WHERE id IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (PARTITION BY LOWER(title) ORDER BY id) AS row_num
                    FROM courses
                )
                WHERE row_num > 1
            )
            """
        )

        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_courses_title ON courses (LOWER(title))"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_lessons_course_title ON lessons (course_id, LOWER(title))"
        )

        columns = conn.execute("PRAGMA table_info(lessons)").fetchall()
        if not any(column[1] == "video_url" for column in columns):
            conn.execute("ALTER TABLE lessons ADD COLUMN video_url TEXT")

        submission_columns = {
            column[1] for column in conn.execute("PRAGMA table_info(submissions)").fetchall()
        }
        if "grade" not in submission_columns:
            conn.execute("ALTER TABLE submissions ADD COLUMN grade REAL")
        if "feedback" not in submission_columns:
            conn.execute("ALTER TABLE submissions ADD COLUMN feedback TEXT")
        if "graded_at" not in submission_columns:
            conn.execute("ALTER TABLE submissions ADD COLUMN graded_at TEXT")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_notifications_user_read ON notifications (user_id, read_at)"
        )

        admin_username = os.environ.get("LMS_ADMIN_USERNAME", "").strip()
        admin_password = os.environ.get("LMS_ADMIN_PASSWORD", "")
        if bool(admin_username) != bool(admin_password):
            raise RuntimeError("Set both LMS_ADMIN_USERNAME and LMS_ADMIN_PASSWORD to create an initial instructor account")
        if admin_username:
            existing_admin = conn.execute(
                "SELECT role FROM users WHERE username = ?",
                (admin_username,),
            ).fetchone()
            if existing_admin and existing_admin["role"] != "instructor":
                raise RuntimeError("LMS_ADMIN_USERNAME is already registered as a student")
            if not existing_admin:
                conn.execute(
                    "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
                    (admin_username, generate_password_hash(admin_password), "instructor"),
                )

        if not IS_PRODUCTION:
            conn.execute(
                "INSERT OR IGNORE INTO users (username, password, role) VALUES (?, ?, ?)",
                ("owner", generate_password_hash("pass123"), "instructor"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO users (username, password, role) VALUES (?, ?, ?)",
                ("developer", generate_password_hash("dev123"), "instructor"),
            )
            conn.execute(
                "INSERT OR IGNORE INTO users (username, password, role) VALUES (?, ?, ?)",
                ("instructor1", generate_password_hash("pass123"), "instructor"),
            )

            conn.execute(
                "INSERT OR IGNORE INTO courses (title, description, instructor_id) VALUES (?, ?, (SELECT id FROM users WHERE username = 'owner'))",
                ("Python Basics", "Learn Python from scratch with practical exercises."),
            )
            conn.execute(
                "INSERT OR IGNORE INTO courses (title, description, instructor_id) VALUES (?, ?, (SELECT id FROM users WHERE username = 'instructor1'))",
                ("Web Design Fundamentals", "Master HTML, CSS, and responsive design."),
            )

            course_rows = conn.execute("SELECT id, title FROM courses ORDER BY id").fetchall()
            for course_id, title in course_rows:
                if title == "Python Basics":
                    conn.execute(
                        "INSERT OR IGNORE INTO lessons (course_id, title, content, video_url) VALUES (?, ?, ?, ?)",
                        (course_id, "Python Intro", "Learn the basics of Python syntax and variables.", "https://www.youtube.com/watch?v=kqtD5dpn9C8"),
                    )
                elif title == "Web Design Fundamentals":
                    conn.execute(
                        "INSERT OR IGNORE INTO lessons (course_id, title, content, video_url) VALUES (?, ?, ?, ?)",
                        (course_id, "HTML Crash Course", "Understand HTML structure and the building blocks of web pages.", "https://www.youtube.com/watch?v=UB1O30fR-EE"),
                    )


@app.route("/")
def index():
    user_id = session.get("user_id")
    role = session.get("role")
    conn = get_db()
    query = request.args.get("q", "").strip()

    if query:
        search_term = f"%{query.lower()}%"
        courses = conn.execute(
            """
            SELECT c.*, u.username as instructor
            FROM courses c
            JOIN users u ON c.instructor_id = u.id
            WHERE LOWER(c.title) LIKE ?
               OR LOWER(c.description) LIKE ?
               OR LOWER(u.username) LIKE ?
            """,
            (search_term, search_term, search_term),
        ).fetchall()
    else:
        courses = conn.execute(
            """
            SELECT c.*, u.username as instructor
            FROM courses c
            JOIN users u ON c.instructor_id = u.id
            """
        ).fetchall()

    my_courses = []
    if user_id:
        if role == "instructor":
            my_courses = conn.execute(
                "SELECT * FROM courses WHERE instructor_id = ?",
                (user_id,),
            ).fetchall()
        else:
            my_courses = conn.execute(
                """
                SELECT c.* FROM courses c
                JOIN enrollments e ON c.id = e.course_id
                WHERE e.student_id = ?
                """,
                (user_id,),
            ).fetchall()

    content = f"""
    <div class="grid grid-cols-1 md:grid-cols-3 gap-6">
        <div class="md:col-span-2">
            <h2 class="text-2xl font-bold mb-4">Course Catalog</h2>
            <div class="grid grid-cols-1 gap-4">
                {''.join([f'''
                <div class="bg-white p-5 rounded-lg shadow-sm border border-gray-200">
                    <h3 class="text-xl font-semibold text-indigo-600">{c['title']}</h3>
                    <p class="text-gray-600 text-sm mt-1">{c['description']}</p>
                    <div class="mt-3 text-xs text-gray-500">Instructor: {c['instructor']}</div>
                    <a href="/courses/{c['id']}" class="inline-block mt-3 bg-indigo-50 text-indigo-600 px-3 py-1 rounded border border-indigo-200 text-sm hover:bg-indigo-100">View Course</a>
                </div>
                ''' for c in courses]) if courses else '<p class="text-gray-500">No courses available yet.</p>'}
            </div>
        </div>

        <div>
            {f'''
            <div class="bg-white p-5 rounded-lg shadow-sm border border-gray-200 mb-6">
                <h3 class="text-lg font-bold mb-3">{'My Taught Courses' if role == 'instructor' else 'Enrolled Courses'}</h3>
                <ul class="space-y-2">
                    {''.join([f'<li><a href="/courses/{mc["id"]}" class="text-indigo-600 hover:underline">{mc["title"]}</a></li>' for mc in my_courses]) if my_courses else '<li class="text-xs text-gray-500">No courses found.</li>'}
                </ul>
                {f'<a href="/dashboard" class="block mt-4 text-center bg-indigo-600 text-white py-2 rounded text-sm hover:bg-indigo-700">Open Dashboard</a>' if role == 'student' else ''}
            </div>
            ''' if user_id else ''}

            {f'''
            <div class="bg-white p-5 rounded-lg shadow-sm border border-gray-200">
                <h3 class="text-lg font-bold mb-3">Instructor Panel</h3>
                <div class="space-y-2">
                    {f'<a href="/admin" class="block text-center bg-indigo-600 text-white py-2 rounded text-sm hover:bg-indigo-700">Open Admin Panel</a>' if is_admin_user() else ''}
                    {f'<a href="/courses/create" class="block text-center border border-indigo-200 text-indigo-700 py-2 rounded text-sm hover:bg-indigo-50">Create New Course</a>' if is_admin_user() else ''}
                </div>
            </div>
            ''' if role == 'instructor' else ''}
        </div>
    </div>
    """
    return render_template_string(HTML_LAYOUT, content=content)


init_db()


@app.route("/register", methods=["GET", "POST"])
def register():
    allow_instructor_registration = os.environ.get(
        "ALLOW_INSTRUCTOR_REGISTRATION",
        "false" if IS_PRODUCTION else "true",
    ).strip().lower() in {"1", "true", "yes"}

    if request.method == "POST":
        username = request.form["username"].strip()
        raw_password = request.form["password"]
        requested_role = request.form.get("role", "student")
        role = requested_role if allow_instructor_registration else "student"

        if not username or not raw_password:
            flash("Username and password are required.", "error")
        elif role not in {"student", "instructor"}:
            flash("Choose a valid account role.", "error")
        else:
            password = generate_password_hash(raw_password)

            try:
                with get_db() as conn:
                    conn.execute(
                        "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
                        (username, password, role),
                    )
                flash("Registration successful. Please log in.", "success")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                flash("Username already exists.", "error")

    role_options = """
                    <option value="student">Student</option>
                    <option value="instructor">Instructor</option>
    """ if allow_instructor_registration else '<option value="student">Student</option>'
    content = """
    <div class="max-w-md mx-auto bg-white p-6 rounded-lg shadow-sm border border-gray-200">
        <h2 class="text-xl font-bold mb-4">Register Account</h2>
        <form method="POST" class="space-y-4">
            <div>
                <label class="block text-sm font-medium mb-1">Username</label>
                <input type="text" name="username" required class="w-full border p-2 rounded text-sm">
            </div>
            <div>
                <label class="block text-sm font-medium mb-1">Password</label>
                <input type="password" name="password" required class="w-full border p-2 rounded text-sm">
            </div>
            <div>
                <label class="block text-sm font-medium mb-1">Role</label>
                <select name="role" class="w-full border p-2 rounded text-sm">
                    {{ role_options | safe }}
                </select>
            </div>
            <button type="submit" class="w-full bg-indigo-600 text-white py-2 rounded text-sm hover:bg-indigo-700">Sign Up</button>
        </form>
    </div>
    """
    return render_template_string(HTML_LAYOUT, content=content, role_options=role_options)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE username = ?",
            (username,),
        ).fetchone()

        if user and check_password_hash(user["password"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            flash("Logged in successfully.", "success")
            return redirect(url_for("index"))
        else:
            flash("Invalid credentials.", "error")

    content = """
    <div class="max-w-md mx-auto bg-white p-6 rounded-lg shadow-sm border border-gray-200">
        <h2 class="text-xl font-bold mb-4">Login</h2>
        <form method="POST" class="space-y-4">
            <div>
                <label class="block text-sm font-medium mb-1">Username</label>
                <input type="text" name="username" required class="w-full border p-2 rounded text-sm">
            </div>
            <div>
                <label class="block text-sm font-medium mb-1">Password</label>
                <input type="password" name="password" required class="w-full border p-2 rounded text-sm">
            </div>
            <button type="submit" class="w-full bg-indigo-600 text-white py-2 rounded text-sm hover:bg-indigo-700">Login</button>
        </form>
    </div>
    """
    return render_template_string(HTML_LAYOUT, content=content)


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for("index"))


@app.route("/notifications")
def notifications():
    if not session.get("user_id"):
        flash("Please log in to view notifications.", "error")
        return redirect(url_for("login"))

    rows = get_db().execute(
        """
        SELECT n.*, c.title AS course_title
        FROM notifications n
        JOIN courses c ON c.id = n.course_id
        WHERE n.user_id = ?
        ORDER BY n.created_at DESC, n.id DESC
        """,
        (session["user_id"],),
    ).fetchall()
    unread_count = sum(notification["read_at"] is None for notification in rows)
    notification_cards = []
    for notification in rows:
        read_form = ""
        if notification["read_at"] is None:
            read_form = f"""
                <form action="/notifications/{notification['id']}/read" method="POST">
                    <button type="submit" class="whitespace-nowrap text-sm text-indigo-700 hover:underline">Mark as read</button>
                </form>
            """
        notification_cards.append(f'''
        <article class="rounded-lg border {'border-indigo-300 bg-indigo-50' if notification['read_at'] is None else 'border-gray-200 bg-white'} p-4">
            <div class="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-3">
                <div>
                    <a href="/courses/{notification['course_id']}" class="font-semibold text-indigo-700 hover:underline">{escape(notification['course_title'])}</a>
                    <p class="text-sm text-gray-700 mt-1">{escape(notification['message'])}</p>
                    <p class="text-xs text-gray-500 mt-2">{escape(notification['created_at'])}{' · Unread' if notification['read_at'] is None else ''}</p>
                </div>
                {read_form}
            </div>
        </article>
        ''')
    notification_cards = ''.join(notification_cards) if notification_cards else '<p class="text-sm text-gray-500">You have no notifications yet.</p>'

    content = f"""
    <div class="max-w-3xl mx-auto bg-white p-6 rounded-lg shadow-sm border border-gray-200">
        <div class="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 mb-5">
            <div>
                <p class="text-xs uppercase tracking-wide text-indigo-600 font-semibold">Updates</p>
                <h2 class="text-2xl font-bold mt-1">Notifications</h2>
                <p class="text-sm text-gray-500 mt-1">{unread_count} unread</p>
            </div>
            {f'''
            <form action="/notifications/read-all" method="POST">
                <button type="submit" class="border border-indigo-200 text-indigo-700 px-3 py-2 rounded text-sm hover:bg-indigo-50">Mark all as read</button>
            </form>
            ''' if unread_count else ''}
        </div>
        <div class="space-y-3">{notification_cards}</div>
    </div>
    """
    return render_template_string(HTML_LAYOUT, content=content)


@app.route("/notifications/<int:notification_id>/read", methods=["POST"])
def mark_notification_read(notification_id):
    if not session.get("user_id"):
        flash("Please log in to update notifications.", "error")
        return redirect(url_for("login"))

    with get_db() as conn:
        conn.execute(
            """
            UPDATE notifications
            SET read_at = CURRENT_TIMESTAMP
            WHERE id = ? AND user_id = ? AND read_at IS NULL
            """,
            (notification_id, session["user_id"]),
        )
    flash("Notification marked as read.", "success")
    return redirect(url_for("notifications"))


@app.route("/notifications/read-all", methods=["POST"])
def mark_all_notifications_read():
    if not session.get("user_id"):
        flash("Please log in to update notifications.", "error")
        return redirect(url_for("login"))

    with get_db() as conn:
        conn.execute(
            """
            UPDATE notifications
            SET read_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND read_at IS NULL
            """,
            (session["user_id"],),
        )
    flash("All notifications marked as read.", "success")
    return redirect(url_for("notifications"))


@app.route("/dashboard")
def student_dashboard():
    if not session.get("user_id"):
        flash("Please log in to view your dashboard.", "error")
        return redirect(url_for("login"))

    if session.get("role") != "student":
        flash("This dashboard is available for students only.", "error")
        return redirect(url_for("index"))

    conn = get_db()
    enrolled_courses = conn.execute(
        """
        SELECT c.*, u.username AS instructor,
               COUNT(DISTINCT l.id) AS lesson_count,
               COUNT(DISTINCT lc.lesson_id) AS completed_lesson_count,
               COUNT(DISTINCT a.id) AS assignment_count,
               COUNT(DISTINCT s.assignment_id) AS submitted_count
        FROM enrollments e
        JOIN courses c ON c.id = e.course_id
        JOIN users u ON u.id = c.instructor_id
        LEFT JOIN lessons l ON l.course_id = c.id
        LEFT JOIN lesson_completions lc
               ON lc.lesson_id = l.id AND lc.student_id = e.student_id
        LEFT JOIN assignments a ON a.course_id = c.id
        LEFT JOIN submissions s ON s.assignment_id = a.id AND s.student_id = e.student_id
        WHERE e.student_id = ?
        GROUP BY c.id, u.username
        ORDER BY c.title
        """,
        (session["user_id"],),
    ).fetchall()

    total_courses = len(enrolled_courses)
    total_lessons = sum(course["lesson_count"] for course in enrolled_courses)
    total_completed_lessons = sum(course["completed_lesson_count"] for course in enrolled_courses)
    overall_progress = round(total_completed_lessons * 100 / total_lessons) if total_lessons else 0

    course_cards = ''.join([
        f'''
        <div class="bg-white border border-gray-200 rounded-lg p-4 shadow-sm">
            <div class="flex items-start justify-between gap-3">
                <div>
                    <h3 class="text-lg font-semibold text-indigo-700">{course['title']}</h3>
                    <p class="text-xs text-gray-500 mt-1">Instructor: {course['instructor']}</p>
                </div>
                <span class="bg-indigo-100 text-indigo-700 text-xs font-medium px-2 py-1 rounded-full">{course['lesson_count']} lessons</span>
            </div>
            <p class="text-sm text-gray-600 mt-3 mb-4">{course['description']}</p>
            <div class="h-2 bg-gray-200 rounded-full overflow-hidden" role="progressbar" aria-label="{course['title']} progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{round(course['completed_lesson_count'] * 100 / course['lesson_count']) if course['lesson_count'] else 0}">
                <div class="h-full bg-indigo-600 rounded-full" style="width: {round(course['completed_lesson_count'] * 100 / course['lesson_count']) if course['lesson_count'] else 0}%"></div>
            </div>
            <div class="flex items-center justify-between mt-3 text-xs text-gray-500">
                <span>{course['completed_lesson_count']} of {course['lesson_count']} lessons complete</span>
                <span>{round(course['completed_lesson_count'] * 100 / course['lesson_count']) if course['lesson_count'] else 0}%</span>
            </div>
            <p class="text-xs text-gray-600 mt-2">{course['submitted_count']} of {course['assignment_count']} assignments submitted</p>
            <a href="/courses/{course['id']}" class="mt-4 inline-block bg-indigo-600 text-white px-3 py-2 rounded text-sm hover:bg-indigo-700">Continue Course</a>
        </div>
        ''' for course in enrolled_courses]) if enrolled_courses else '<p class="text-gray-500">You are not enrolled in any courses yet. Browse the catalog to get started.</p>'

    content = f"""
    <div class="max-w-5xl mx-auto space-y-6">
        <div class="bg-white rounded-lg border border-gray-200 shadow-sm p-6">
            <p class="text-xs uppercase tracking-wide text-indigo-600 font-semibold">Student Workspace</p>
            <h2 class="text-2xl font-bold mt-1">Student Dashboard</h2>
            <p class="text-gray-600 mt-2">Welcome back, {session['username']}! Keep moving through your learning path.</p>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div class="bg-indigo-50 border border-indigo-100 rounded-lg p-4">
                <div class="text-sm text-indigo-600">Enrolled Courses</div>
                <div class="text-3xl font-bold mt-2">{total_courses}</div>
            </div>
            <div class="bg-green-50 border border-green-100 rounded-lg p-4">
                <div class="text-sm text-green-600">Lessons Completed</div>
                <div class="text-3xl font-bold mt-2">{total_completed_lessons} / {total_lessons}</div>
            </div>
            <div class="bg-purple-50 border border-purple-100 rounded-lg p-4">
                <div class="text-sm text-purple-600">Overall Progress</div>
                <div class="text-3xl font-bold mt-2">{overall_progress}%</div>
            </div>
        </div>

        <div class="bg-white rounded-lg border border-gray-200 shadow-sm p-6">
            <div class="flex items-center justify-between gap-3 mb-4">
                <h3 class="text-xl font-bold">My Courses</h3>
                <a href="/" class="text-sm text-indigo-600 hover:underline">Browse catalog</a>
            </div>
            <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
                {course_cards}
            </div>
        </div>
    </div>
    """
    return render_template_string(HTML_LAYOUT, content=content)


@app.route("/admin", methods=["GET", "POST"])
def admin_panel():
    if not is_admin_user():
        flash("This admin panel is restricted to the owner and developer accounts.", "error")
        return redirect(url_for("index"))

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()

        if not title or not description:
            flash("Course title and description are required.", "error")
        else:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO courses (title, description, instructor_id) VALUES (?, ?, ?)",
                    (title, description, session["user_id"]),
                )
            flash("Course created successfully.", "success")
            return redirect(url_for("admin_panel"))

    conn = get_db()
    course_rows = conn.execute(
        "SELECT * FROM courses WHERE instructor_id = ? ORDER BY id",
        (session["user_id"],),
    ).fetchall()
    lesson_count = conn.execute(
        "SELECT COUNT(*) FROM lessons l JOIN courses c ON c.id = l.course_id WHERE c.instructor_id = ?",
        (session["user_id"],),
    ).fetchone()[0]
    student_count = conn.execute(
        "SELECT COUNT(DISTINCT e.student_id) FROM enrollments e JOIN courses c ON c.id = e.course_id WHERE c.instructor_id = ?",
        (session["user_id"],),
    ).fetchone()[0]

    course_cards = "".join([
        f'''<a href="/courses/{course['id']}" class="block p-4 rounded border border-gray-200 bg-white hover:border-indigo-300 hover:bg-indigo-50">
            <h3 class="font-semibold text-indigo-700">{course['title']}</h3>
            <p class="text-sm text-gray-600 mt-1">{course['description']}</p>
        </a>'''
        for course in course_rows
    ]) if course_rows else '<p class="text-gray-500">No courses yet.</p>'

    content = f"""
    <div class="max-w-5xl mx-auto bg-white p-6 rounded-lg shadow-sm border border-gray-200">
        <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-4 mb-6">
            <div>
                <p class="text-xs uppercase tracking-wide text-indigo-600 font-semibold">Instructor Workspace</p>
                <h2 class="text-2xl font-bold mt-1">Admin Panel</h2>
            </div>
            <a href="/courses/create" class="bg-indigo-600 text-white px-4 py-2 rounded text-sm hover:bg-indigo-700">Create New Course</a>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
            <div class="bg-indigo-50 rounded-lg p-4 border border-indigo-100">
                <div class="text-sm text-indigo-600">Courses</div>
                <div class="text-2xl font-bold mt-1">{len(course_rows)}</div>
            </div>
            <div class="bg-green-50 rounded-lg p-4 border border-green-100">
                <div class="text-sm text-green-600">Lessons</div>
                <div class="text-2xl font-bold mt-1">{lesson_count}</div>
            </div>
            <div class="bg-purple-50 rounded-lg p-4 border border-purple-100">
                <div class="text-sm text-purple-600">Students</div>
                <div class="text-2xl font-bold mt-1">{student_count}</div>
            </div>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <div class="border border-gray-200 rounded-lg p-4">
                <h3 class="text-lg font-bold mb-3">Create Course</h3>
                <form method="POST" class="space-y-4">
                    <div>
                        <label class="block text-sm font-medium mb-1">Course Title</label>
                        <input type="text" name="title" required class="w-full border p-2 rounded text-sm">
                    </div>
                    <div>
                        <label class="block text-sm font-medium mb-1">Description</label>
                        <textarea name="description" rows="4" required class="w-full border p-2 rounded text-sm"></textarea>
                    </div>
                    <button type="submit" class="w-full bg-indigo-600 text-white py-2 rounded text-sm hover:bg-indigo-700">Publish Course</button>
                </form>
            </div>

            <div class="border border-gray-200 rounded-lg p-4">
                <h3 class="text-lg font-bold mb-3">Your Courses</h3>
                <div class="space-y-3">
                    {course_cards}
                </div>
            </div>
        </div>
    </div>
    """
    return render_template_string(HTML_LAYOUT, content=content)


@app.route("/courses/create", methods=["GET", "POST"])
def create_course():
    if not is_admin_user():
        flash("This admin panel is restricted to the owner and developer accounts.", "error")
        return redirect(url_for("index"))

    if request.method == "POST":
        title = request.form["title"]
        description = request.form["description"]

        with get_db() as conn:
            conn.execute(
                "INSERT INTO courses (title, description, instructor_id) VALUES (?, ?, ?)",
                (title, description, session["user_id"]),
            )
        flash("Course created successfully.", "success")
        return redirect(url_for("admin_panel"))

    content = """
    <div class="max-w-lg mx-auto bg-white p-6 rounded-lg shadow-sm border border-gray-200">
        <h2 class="text-xl font-bold mb-4">Create New Course</h2>
        <form method="POST" class="space-y-4">
            <div>
                <label class="block text-sm font-medium mb-1">Course Title</label>
                <input type="text" name="title" required class="w-full border p-2 rounded text-sm">
            </div>
            <div>
                <label class="block text-sm font-medium mb-1">Description</label>
                <textarea name="description" rows="4" required class="w-full border p-2 rounded text-sm"></textarea>
            </div>
            <button type="submit" class="w-full bg-indigo-600 text-white py-2 rounded text-sm hover:bg-indigo-700">Publish Course</button>
        </form>
    </div>
    """
    return render_template_string(HTML_LAYOUT, content=content)


@app.route("/courses/<int:course_id>")
def view_course(course_id):
    conn = get_db()
    course = conn.execute(
        """
        SELECT c.*, u.username as instructor
        FROM courses c
        JOIN users u ON c.instructor_id = u.id
        WHERE c.id = ?
        """,
        (course_id,),
    ).fetchone()

    if course is None:
        flash("Course not found.", "error")
        return redirect(url_for("index"))

    lessons = conn.execute(
        "SELECT * FROM lessons WHERE course_id = ? ORDER BY id",
        (course_id,),
    ).fetchall()

    user_id = session.get("user_id")
    role = session.get("role")
    is_enrolled = False
    if user_id:
        is_enrolled = conn.execute(
            "SELECT 1 FROM enrollments WHERE student_id = ? AND course_id = ?",
            (user_id, course_id),
        ).fetchone() is not None

    is_owner = user_id == course["instructor_id"]
    completed_lesson_ids = set()
    if is_enrolled:
        completed_lesson_ids = {
            row["lesson_id"]
            for row in conn.execute(
                """
                SELECT lc.lesson_id
                FROM lesson_completions lc
                JOIN lessons l ON l.id = lc.lesson_id
                WHERE lc.student_id = ? AND l.course_id = ?
                """,
                (user_id, course_id),
            ).fetchall()
        }

    assignments = conn.execute(
        "SELECT * FROM assignments WHERE course_id = ? ORDER BY id",
        (course_id,),
    ).fetchall()
    student_submissions = {}
    instructor_submissions = {}
    if is_enrolled:
        student_submissions = {
            submission["assignment_id"]: submission
            for submission in conn.execute(
                "SELECT * FROM submissions WHERE student_id = ?",
                (user_id,),
            ).fetchall()
        }
    if is_owner:
        for submission in conn.execute(
            """
            SELECT s.*, u.username AS student_username
            FROM submissions s
            JOIN assignments a ON a.id = s.assignment_id
            JOIN users u ON u.id = s.student_id
            WHERE a.course_id = ?
            ORDER BY a.id, u.username
            """,
            (course_id,),
        ).fetchall():
            instructor_submissions.setdefault(submission["assignment_id"], []).append(submission)

    assignment_blocks = []
    for assignment in assignments:
        submitted = student_submissions.get(assignment["id"])
        if is_enrolled:
            if submitted:
                submission_status = f'''
                <p class="text-sm text-green-700 mt-3">Submitted {escape(submitted['submitted_at'])}:
                    <a class="underline" href="/submissions/{submitted['id']}/download">{escape(submitted['original_filename'])}</a>
                </p>
                '''
                if submitted["grade"] is not None:
                    submission_status += f'''
                    <p class="text-sm font-semibold text-indigo-700 mt-2">Grade: {submitted['grade']:g}/100</p>
                    <p class="text-sm text-gray-700 mt-1 whitespace-pre-line">{escape(submitted['feedback'] or 'No feedback provided.')}</p>
                    '''
                else:
                    submission_status += '<p class="text-sm text-gray-500 mt-2">Awaiting instructor review.</p>'
            else:
                submission_status = '<p class="text-sm text-amber-700 mt-3">Not submitted yet.</p>'
            student_form = f'''
            <form action="/assignments/{assignment['id']}/submit" method="POST" enctype="multipart/form-data" class="mt-3 space-y-2">
                <label class="block text-sm font-medium" for="assignment-file-{assignment['id']}">{'Replace submission' if submitted else 'Upload your submission'}</label>
                <input id="assignment-file-{assignment['id']}" type="file" name="file" required class="block w-full text-sm" aria-describedby="assignment-file-help-{assignment['id']}">
                <p id="assignment-file-help-{assignment['id']}" class="text-xs text-gray-500">Maximum file size: 16 MB.</p>
                <button type="submit" class="bg-indigo-600 text-white px-3 py-2 rounded text-sm hover:bg-indigo-700">{'Resubmit file' if submitted else 'Submit assignment'}</button>
            </form>
            '''
        else:
            submission_status = ""
            student_form = ""

        if is_owner:
            submitted_files = instructor_submissions.get(assignment["id"], [])
            submissions_html = ''.join([
                f'''
                <li class="rounded border border-gray-200 p-3">
                    <p class="text-sm font-medium">{escape(submission['student_username'])}</p>
                    <p class="text-xs text-gray-500 mt-1">Submitted {escape(submission['submitted_at'])}</p>
                    <a class="text-sm text-indigo-600 underline" href="/submissions/{submission['id']}/download">{escape(submission['original_filename'])}</a>
                    <form action="/submissions/{submission['id']}/grade" method="POST" class="mt-3 space-y-2">
                        <div>
                            <label class="block text-xs font-medium mb-1" for="grade-{submission['id']}">Score (out of 100)</label>
                            <input id="grade-{submission['id']}" type="number" name="grade" min="0" max="100" step="0.01" required value="{'' if submission['grade'] is None else submission['grade']}" class="w-full border p-2 rounded text-sm">
                        </div>
                        <div>
                            <label class="block text-xs font-medium mb-1" for="feedback-{submission['id']}">Feedback</label>
                            <textarea id="feedback-{submission['id']}" name="feedback" maxlength="2000" rows="3" class="w-full border p-2 rounded text-sm">{escape(submission['feedback'] or '')}</textarea>
                        </div>
                        <button type="submit" class="bg-indigo-600 text-white px-3 py-2 rounded text-sm hover:bg-indigo-700">{'Update grade' if submission['grade'] is not None else 'Save grade'}</button>
                    </form>
                </li>
                '''
                for submission in submitted_files
            ]) if submitted_files else '<li class="text-sm text-gray-500">No submissions yet.</li>'
            instructor_submissions_html = f'''
            <div class="mt-4 border-t pt-3">
                <h4 class="text-sm font-semibold mb-2">Student submissions</h4>
                <ul class="space-y-3">{submissions_html}</ul>
            </div>
            '''
        else:
            instructor_submissions_html = ""

        assignment_blocks.append(f'''
        <article class="rounded border border-gray-200 p-4">
            <h4 class="font-semibold text-indigo-700">{escape(assignment['title'])}</h4>
            <p class="text-sm text-gray-700 mt-2 whitespace-pre-line">{escape(assignment['instructions'])}</p>
            {submission_status}
            {student_form}
            {instructor_submissions_html}
        </article>
        ''')

    assignment_section = ""
    if is_owner or is_enrolled:
        assignment_section = f'''
        <section class="mt-8 border-t pt-5">
            <h3 class="text-lg font-bold mb-3">Assignments</h3>
            <div class="space-y-4">
                {''.join(assignment_blocks) if assignment_blocks else '<p class="text-sm text-gray-500">No assignments have been added yet.</p>'}
            </div>
            {f"""
            <form action="/courses/{course_id}/assignments" method="POST" class="mt-6 border-t pt-5 space-y-3">
                <h4 class="font-semibold">Create assignment</h4>
                <div>
                    <label class="block text-sm font-medium mb-1" for="assignment-title">Assignment title</label>
                    <input id="assignment-title" type="text" name="title" required maxlength="200" class="w-full border p-2 rounded text-sm">
                </div>
                <div>
                    <label class="block text-sm font-medium mb-1" for="assignment-instructions">Instructions</label>
                    <textarea id="assignment-instructions" name="instructions" rows="4" required class="w-full border p-2 rounded text-sm"></textarea>
                </div>
                <button type="submit" class="bg-indigo-600 text-white px-4 py-2 rounded text-sm hover:bg-indigo-700">Create assignment</button>
            </form>
            """ if is_owner else ''}
        </section>
        '''

    lesson_blocks = []
    for lesson in lessons:
        video_url = lesson["video_url"] if "video_url" in lesson.keys() else ""
        embed_url = normalize_youtube_embed(video_url)
        lesson_html = ""
        if embed_url:
            lesson_html = f'''
            <div class="mt-3 overflow-hidden rounded-lg border border-gray-200">
                <iframe width="100%" height="220" src="{embed_url}" title="{lesson["title"]}" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" referrerpolicy="strict-origin-when-cross-origin" allowfullscreen></iframe>
            </div>
            '''

        lesson_content = "[Enroll or be the instructor to view lesson content]" if not (is_enrolled or is_owner) else lesson["content"]
        completion_control = ""
        if is_enrolled:
            is_complete = lesson["id"] in completed_lesson_ids
            completion_control = f'''
            <form action="/lessons/{lesson['id']}/progress" method="POST" class="mt-3">
                <input type="hidden" name="completed" value="{'false' if is_complete else 'true'}">
                <button type="submit" class="px-3 py-2 rounded text-sm {'bg-green-100 text-green-800 hover:bg-green-200' if is_complete else 'bg-indigo-600 text-white hover:bg-indigo-700'}">
                    {'Mark as incomplete' if is_complete else 'Mark as complete'}
                </button>
            </form>
            '''
        lesson_blocks.append(f'''
        <div class="bg-white p-4 rounded border border-gray-200">
            <h3 class="font-bold text-lg text-indigo-600">{lesson['title']}</h3>
            <p class="text-gray-700 mt-2 text-sm whitespace-pre-line">{lesson_content}</p>
            {lesson_html}
            {completion_control}
        </div>
        ''')

    content = f"""
    <div class="max-w-4xl mx-auto bg-white p-6 rounded-lg shadow-sm border border-gray-200">
        <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-4 mb-6">
            <div>
                <h2 class="text-2xl font-bold text-indigo-700">{course['title']}</h2>
                <p class="text-sm text-gray-600 mt-1">Instructor: {course['instructor']}</p>
            </div>
            <div class="flex gap-2">
                <a href="/" class="bg-gray-200 text-gray-700 px-3 py-2 rounded text-sm">Back to catalog</a>
                {f'<a href="/courses/{course_id}/enroll" class="bg-indigo-600 text-white px-3 py-2 rounded text-sm hover:bg-indigo-700">' + ('Already Enrolled' if is_enrolled else 'Enroll Now') + '</a>' if user_id and role == 'student' else ''}
            </div>
        </div>

        <p class="text-gray-700 mb-6">{course['description']}</p>

        <div class="mb-8">
            <h3 class="text-lg font-bold mb-3">Course Lessons</h3>
            <div class="space-y-4">
                {''.join(lesson_blocks) if lessons else '<p class="text-gray-500">No lessons have been added yet.</p>'}
            </div>
        </div>

        {assignment_section}

        {f'''
        <div class="mt-6 border-t pt-5">
            <h3 class="text-lg font-bold mb-3">Add Lesson</h3>
            <form action="/courses/{course_id}/lessons" method="POST" class="space-y-3">
                <div>
                    <label class="block text-xs font-medium mb-1">Lesson Title</label>
                    <input type="text" name="title" required class="w-full border p-2 rounded text-sm">
                </div>
                <div>
                    <label class="block text-xs font-medium mb-1">Lesson Content</label>
                    <textarea name="content" rows="4" required class="w-full border p-2 rounded text-sm"></textarea>
                </div>
                <div>
                    <label class="block text-xs font-medium mb-1">YouTube URL (optional)</label>
                    <input type="url" name="video_url" placeholder="https://www.youtube.com/watch?v=..." class="w-full border p-2 rounded text-sm">
                </div>
                <button type="submit" class="w-full bg-indigo-600 text-white py-1.5 rounded text-sm hover:bg-indigo-700">Add Lesson</button>
            </form>
        </div>
        ''' if is_owner else ''}
    </div>
    """
    return render_template_string(HTML_LAYOUT, content=content)


@app.route("/lessons/<int:lesson_id>/progress", methods=["POST"])
def update_lesson_progress(lesson_id):
    if not session.get("user_id"):
        flash("Please log in to update your lesson progress.", "error")
        return redirect(url_for("login"))
    if session.get("role") != "student":
        abort(403)

    completed_value = request.form.get("completed", "")
    if completed_value not in {"true", "false"}:
        abort(400)

    with get_db() as conn:
        lesson = conn.execute(
            "SELECT id, course_id FROM lessons WHERE id = ?",
            (lesson_id,),
        ).fetchone()
        if lesson is None:
            abort(404)

        enrolled = conn.execute(
            "SELECT 1 FROM enrollments WHERE student_id = ? AND course_id = ?",
            (session["user_id"], lesson["course_id"]),
        ).fetchone()
        if enrolled is None:
            abort(403)

        if completed_value == "true":
            conn.execute(
                """
                INSERT OR IGNORE INTO lesson_completions (student_id, lesson_id)
                VALUES (?, ?)
                """,
                (session["user_id"], lesson_id),
            )
            flash("Lesson marked complete.", "success")
        else:
            conn.execute(
                "DELETE FROM lesson_completions WHERE student_id = ? AND lesson_id = ?",
                (session["user_id"], lesson_id),
            )
            flash("Lesson marked incomplete.", "info")

    return redirect(url_for("view_course", course_id=lesson["course_id"]))


@app.route("/courses/<int:course_id>/assignments", methods=["POST"])
def create_assignment(course_id):
    if session.get("role") != "instructor":
        abort(403)

    title = request.form.get("title", "").strip()
    instructions = request.form.get("instructions", "").strip()
    if not title or not instructions:
        flash("Assignment title and instructions are required.", "error")
        return redirect(url_for("view_course", course_id=course_id))

    with get_db() as conn:
        course = conn.execute(
            "SELECT id, title FROM courses WHERE id = ? AND instructor_id = ?",
            (course_id, session["user_id"]),
        ).fetchone()
        if course is None:
            abort(404)
        conn.execute(
            "INSERT INTO assignments (course_id, title, instructions) VALUES (?, ?, ?)",
            (course_id, title, instructions),
        )
        student_ids = conn.execute(
            "SELECT student_id FROM enrollments WHERE course_id = ?",
            (course_id,),
        ).fetchall()
        conn.executemany(
            "INSERT INTO notifications (user_id, course_id, message) VALUES (?, ?, ?)",
            [
                (student["student_id"], course_id, f"New assignment: {title}")
                for student in student_ids
            ],
        )

    flash("Assignment created successfully.", "success")
    return redirect(url_for("view_course", course_id=course_id))


@app.route("/assignments/<int:assignment_id>/submit", methods=["POST"])
def submit_assignment(assignment_id):
    if not session.get("user_id"):
        flash("Please log in to submit an assignment.", "error")
        return redirect(url_for("login"))
    if session.get("role") != "student":
        abort(403)

    conn = get_db()
    assignment = conn.execute(
        """
        SELECT a.id, a.course_id, a.title AS assignment_title,
               c.title AS course_title, c.instructor_id
        FROM assignments a
        JOIN courses c ON c.id = a.course_id
        WHERE a.id = ?
        """,
        (assignment_id,),
    ).fetchone()
    if assignment is None:
        abort(404)
    course_id = assignment["course_id"]
    enrolled = conn.execute(
        "SELECT 1 FROM enrollments WHERE student_id = ? AND course_id = ?",
        (session["user_id"], course_id),
    ).fetchone()
    if enrolled is None:
        abort(403)

    uploaded_file = request.files.get("file")
    if uploaded_file is None or not uploaded_file.filename:
        flash("Choose a file to submit.", "error")
        return redirect(url_for("view_course", course_id=course_id))

    original_filename = secure_filename(uploaded_file.filename)
    if not original_filename:
        flash("The selected filename is not valid.", "error")
        return redirect(url_for("view_course", course_id=course_id))

    _, extension = os.path.splitext(original_filename)
    stored_filename = f"{uuid.uuid4().hex}{extension.lower()}"
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    uploaded_file.save(os.path.join(UPLOAD_FOLDER, stored_filename))

    with get_db() as conn:
        old_submission = conn.execute(
            "SELECT stored_filename FROM submissions WHERE assignment_id = ? AND student_id = ?",
            (assignment_id, session["user_id"]),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO submissions
                (assignment_id, student_id, stored_filename, original_filename, submitted_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT (assignment_id, student_id) DO UPDATE SET
                stored_filename = excluded.stored_filename,
                original_filename = excluded.original_filename,
                submitted_at = CURRENT_TIMESTAMP
            """,
            (assignment_id, session["user_id"], stored_filename, original_filename),
        )
        conn.execute(
            """
            INSERT INTO notifications (user_id, course_id, message)
            VALUES (?, ?, ?)
            """,
            (
                assignment["instructor_id"],
                course_id,
                f"New submission for {assignment['assignment_title']} from {session['username']}",
            ),
        )

    if old_submission:
        old_path = os.path.join(UPLOAD_FOLDER, old_submission["stored_filename"])
        if os.path.isfile(old_path):
            os.remove(old_path)

    flash("Assignment submitted successfully.", "success")
    return redirect(url_for("view_course", course_id=course_id))


@app.route("/submissions/<int:submission_id>/grade", methods=["POST"])
def grade_submission(submission_id):
    if not session.get("user_id"):
        flash("Please log in to grade submissions.", "error")
        return redirect(url_for("login"))
    if session.get("role") != "instructor":
        abort(403)

    with get_db() as conn:
        submission = conn.execute(
            """
            SELECT s.id, s.student_id, a.course_id, a.title AS assignment_title
            FROM submissions s
            JOIN assignments a ON a.id = s.assignment_id
            JOIN courses c ON c.id = a.course_id
            WHERE s.id = ? AND c.instructor_id = ?
            """,
            (submission_id, session["user_id"]),
        ).fetchone()
        if submission is None:
            abort(404)

    grade_value = request.form.get("grade", "").strip()
    feedback = request.form.get("feedback", "").strip()
    try:
        grade = float(grade_value)
    except ValueError:
        flash("Enter a valid score between 0 and 100.", "error")
        return redirect(url_for("view_course", course_id=submission["course_id"]))
    if not math.isfinite(grade) or not 0 <= grade <= 100:
        flash("Enter a valid score between 0 and 100.", "error")
        return redirect(url_for("view_course", course_id=submission["course_id"]))
    if len(feedback) > 2000:
        flash("Feedback must be 2000 characters or fewer.", "error")
        return redirect(url_for("view_course", course_id=submission["course_id"]))

    with get_db() as conn:
        conn.execute(
            """
            UPDATE submissions
            SET grade = ?, feedback = ?, graded_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (grade, feedback or None, submission_id),
        )
        conn.execute(
            """
            INSERT INTO notifications (user_id, course_id, message)
            VALUES (?, ?, ?)
            """,
            (
                submission["student_id"],
                submission["course_id"],
                f"Your submission for {submission['assignment_title']} has been graded.",
            ),
        )

    flash("Grade saved successfully.", "success")
    return redirect(url_for("view_course", course_id=submission["course_id"]))


@app.route("/submissions/<int:submission_id>/download")
def download_submission(submission_id):
    if not session.get("user_id"):
        flash("Please log in to download submissions.", "error")
        return redirect(url_for("login"))

    conn = get_db()
    submission = conn.execute(
        """
        SELECT s.*, a.course_id, c.instructor_id
        FROM submissions s
        JOIN assignments a ON a.id = s.assignment_id
        JOIN courses c ON c.id = a.course_id
        WHERE s.id = ?
        """,
        (submission_id,),
    ).fetchone()
    if submission is None:
        abort(404)

    can_download = (
        session.get("role") == "student"
        and submission["student_id"] == session["user_id"]
    ) or (
        session.get("role") == "instructor"
        and submission["instructor_id"] == session["user_id"]
    )
    if not can_download:
        abort(403)

    return send_from_directory(
        UPLOAD_FOLDER,
        submission["stored_filename"],
        as_attachment=True,
        download_name=submission["original_filename"],
    )


@app.route("/courses/<int:course_id>/enroll")
def enroll_course(course_id):
    if not session.get("user_id"):
        flash("Please log in to enroll.", "error")
        return redirect(url_for("login"))

    if session.get("role") != "student":
        flash("Only students can enroll in courses.", "error")
        return redirect(url_for("index"))

    conn = get_db()
    existing = conn.execute(
        "SELECT 1 FROM enrollments WHERE student_id = ? AND course_id = ?",
        (session["user_id"], course_id),
    ).fetchone()

    if existing is None:
        conn.execute(
            "INSERT INTO enrollments (student_id, course_id) VALUES (?, ?)",
            (session["user_id"], course_id),
        )
        conn.commit()
        flash("Enrollment successful.", "success")
    else:
        flash("You are already enrolled in this course.", "info")

    return redirect(url_for("view_course", course_id=course_id))


@app.route("/courses/<int:course_id>/lessons", methods=["POST"])
def add_lesson(course_id):
    if session.get("role") != "instructor":
        flash("Only instructors can add lessons.", "error")
        return redirect(url_for("index"))

    title = request.form.get("title", "").strip()
    content = request.form.get("content", "").strip()
    video_url = request.form.get("video_url", "").strip()

    if not title or not content:
        flash("Lesson title and content are required.", "error")
        return redirect(url_for("view_course", course_id=course_id))

    with get_db() as conn:
        conn.execute(
            "INSERT INTO lessons (course_id, title, content, video_url) VALUES (?, ?, ?, ?)",
            (course_id, title, content, video_url or None),
        )

    flash("Lesson added successfully.", "success")
    return redirect(url_for("view_course", course_id=course_id))


if __name__ == "__main__":
    init_db()
    app.run(debug=not IS_PRODUCTION)
