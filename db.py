import json
import sqlite3
import time
from pathlib import Path

DB = Path(__file__).parent / "data.db"


def _c():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _c() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, name TEXT, created INTEGER,
            claim_credits INTEGER DEFAULT 0, court_credits INTEGER DEFAULT 0,
            shield_until INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS cases(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, title TEXT,
            data TEXT, status TEXT DEFAULT 'претензия готова', created INTEGER);
        CREATE TABLE IF NOT EXISTS reminders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, case_id INTEGER,
            due INTEGER, text TEXT, sent INTEGER DEFAULT 0);
        """)


def user(uid, name=""):
    with _c() as c:
        c.execute("INSERT OR IGNORE INTO users(id,name,created) VALUES(?,?,?)", (uid, name, int(time.time())))
        return dict(c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())


def add_credits(uid, claim=0, court=0):
    with _c() as c:
        c.execute("UPDATE users SET claim_credits=claim_credits+?, court_credits=court_credits+? WHERE id=?",
                  (claim, court, uid))


def use_credit(uid, kind):
    col = "claim_credits" if kind == "claim" else "court_credits"
    with _c() as c:
        r = c.execute(f"UPDATE users SET {col}={col}-1 WHERE id=? AND {col}>0", (uid,))
        return r.rowcount > 0


def add_shield(uid, days):
    u = user(uid)
    base = max(u["shield_until"], int(time.time()))
    with _c() as c:
        c.execute("UPDATE users SET shield_until=? WHERE id=?", (base + days * 86400, uid))


def add_case(uid, title, data):
    with _c() as c:
        cur = c.execute("INSERT INTO cases(user_id,title,data,created) VALUES(?,?,?,?)",
                        (uid, title, json.dumps(data, ensure_ascii=False, default=str), int(time.time())))
        return cur.lastrowid


def cases(uid):
    with _c() as c:
        return [dict(r) for r in c.execute("SELECT * FROM cases WHERE user_id=? ORDER BY id DESC LIMIT 20", (uid,))]


def case(cid, uid):
    with _c() as c:
        r = c.execute("SELECT * FROM cases WHERE id=? AND user_id=?", (cid, uid)).fetchone()
        if not r:
            return None
        r = dict(r)
        r["data"] = json.loads(r["data"])
        return r


def set_status(cid, status):
    with _c() as c:
        c.execute("UPDATE cases SET status=? WHERE id=?", (status, cid))


def add_reminder(uid, cid, days, text):
    with _c() as c:
        c.execute("INSERT INTO reminders(user_id,case_id,due,text) VALUES(?,?,?,?)",
                  (uid, cid, int(time.time()) + int(days * 86400), text))


def due_reminders():
    with _c() as c:
        return [dict(r) for r in c.execute("SELECT * FROM reminders WHERE sent=0 AND due<=?", (int(time.time()),))]


def mark_sent(rid):
    with _c() as c:
        c.execute("UPDATE reminders SET sent=1 WHERE id=?", (rid,))


def cancel_reminders(cid):
    with _c() as c:
        c.execute("UPDATE reminders SET sent=1 WHERE case_id=? AND sent=0", (cid,))


def update_case_data(cid, **kw):
    with _c() as c:
        row = c.execute("SELECT data FROM cases WHERE id=?", (cid,)).fetchone()
        d = json.loads(row["data"])
        d.update(kw)
        c.execute("UPDATE cases SET data=? WHERE id=?", (json.dumps(d, ensure_ascii=False, default=str), cid))
