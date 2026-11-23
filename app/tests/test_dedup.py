import sqlite3

from app import dedup


def test_salary_columns_are_added_to_an_existing_db_and_saved(tmp_path):
    db_path = str(tmp_path / "old.sqlite3")
    old = sqlite3.connect(db_path)
    old.execute("CREATE TABLE job_details (url TEXT PRIMARY KEY, company TEXT, title TEXT, location TEXT, "
                "posted_at TEXT, description TEXT, passed_filters INTEGER, fetched_at TEXT)")
    old.commit()
    old.close()

    job = {"url": "https://example.com/1", "company": "Acme", "title": "Frontend Engineer",
           "salary_min": 60000.0, "salary_max": 80000.0, "salary_currency": "EUR"}
    with dedup.connect(db_path) as conn:
        dedup.save_details(conn, job, passed_filters=True)
        row = conn.execute("SELECT salary_min, salary_max, salary_currency FROM job_details").fetchone()
    assert row == (60000.0, 80000.0, "EUR")


def test_legacy_ai_prefilter_table_is_renamed_keeping_rows(tmp_path):
    db_path = str(tmp_path / "legacy.sqlite3")
    old = sqlite3.connect(db_path)
    old.execute("CREATE TABLE ai_prefilter (url TEXT PRIMARY KEY, passed INTEGER, rejection_reason TEXT, "
                "answers TEXT, model TEXT, evaluated_at TEXT)")
    old.execute("INSERT INTO ai_prefilter (url, passed) VALUES ('https://example.com/1', 1)")
    old.commit()
    old.close()

    with dedup.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        rows = conn.execute("SELECT url, passed FROM ai_filter").fetchall()
    assert "ai_prefilter" not in tables
    assert rows == [("https://example.com/1", 1)]
