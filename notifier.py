"""
Notifications for newly approved jobs.

Channels (each one is enabled only if its secrets are set):
  - WhatsApp via CallMeBot  -> CALLMEBOT_PHONE, CALLMEBOT_APIKEY
  - Email via Gmail SMTP    -> GMAIL_ADDRESS, GMAIL_APP_PASSWORD (optional NOTIFY_EMAIL_TO)

Messages that fail to send are kept in pending_notifications.json and retried next run.
Text is Hebrew-first: each line starts with a Hebrew word so RTL rendering stays readable.
"""

import os
import json
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from html import escape

import requests

CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"
WHATSAPP_MAX_CHARS = 1500
PENDING_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pending_notifications.json")
RLM = "‏"  # right-to-left mark: keeps lines that contain English in RTL order


def _env(name):
    return os.getenv(name, "").strip().strip('"').strip("'")


def _days_ago(iso_date):
    from datetime import date
    try:
        d = (date.today() - date.fromisoformat(iso_date[:10])).days
    except ValueError:
        return iso_date
    if d <= 0:
        return "היום"
    if d == 1:
        return "אתמול"
    return f"לפני {d} ימים"


def _source_label(job):
    return "לינקדאין" if job.get("source") == "linkedin" else "אתר החברה"


def _format_job_whatsapp(job, with_breakdown):
    lines = [
        f"{RLM}*{job['title']}*",
        f"{RLM}חברה: {job['company']} | מקור: {_source_label(job)}",
        f"{RLM}ציון: {job.get('match_score', '?')} | מיקום: {job.get('location', '')}",
    ]
    if job.get("posted"):
        lines.append(f"{RLM}פורסם: {_days_ago(job['posted'])}")
    if job.get("match_reason"):
        lines.append(f"{RLM}למה: {job['match_reason']}")
    if with_breakdown and job.get("score_breakdown"):
        lines.append(f"{RLM}פירוט: " + " | ".join(job["score_breakdown"]))
    lines.append(job["url"])
    return "\n".join(lines)


def _chunk_messages(header, blocks, limit=WHATSAPP_MAX_CHARS):
    messages, current = [], header
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > limit and current:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def build_whatsapp_messages(jobs, borderline):
    msgs = []
    if jobs:
        msgs += _chunk_messages(
            f"{RLM}🔔 {len(jobs)} משרות חדשות שמתאימות לך",
            [_format_job_whatsapp(j, with_breakdown=True) for j in jobs],
        )
    if borderline:
        msgs += _chunk_messages(
            f"{RLM}🟡 על הגבול ({len(borderline)}) - לא עברו את הסף, נשלחות לבדיקה שלך. "
            "אם משהו כאן כן היה מתאים, תגיד לי ונכוון את המשקלות.",
            [_format_job_whatsapp(j, with_breakdown=True) for j in borderline],
        )
    return msgs


def _send_whatsapp_text(msg):
    phone, apikey = _env("CALLMEBOT_PHONE"), _env("CALLMEBOT_APIKEY")
    try:
        r = requests.get(CALLMEBOT_URL, params={"phone": phone, "text": msg, "apikey": apikey}, timeout=30)
        if r.status_code == 200 and "ERROR" not in r.text.upper():
            return True
        print(f"[!] WhatsApp send failed (HTTP {r.status_code}): {r.text[:200]}")
    except requests.RequestException as exc:
        print(f"[!] WhatsApp network error: {exc}")
    return False


def send_whatsapp(messages):
    """Returns the list of messages that failed to send."""
    if not (_env("CALLMEBOT_PHONE") and _env("CALLMEBOT_APIKEY")):
        print("[i] WhatsApp disabled (CALLMEBOT_PHONE / CALLMEBOT_APIKEY not set)")
        return []
    failed = []
    for i, msg in enumerate(messages):
        if i:
            time.sleep(4)  # CallMeBot throttles rapid bursts
        if not _send_whatsapp_text(msg):
            failed.append(msg)
    print(f"[✓] WhatsApp: {len(messages) - len(failed)}/{len(messages)} message(s) sent")
    return failed


def _html_rows(jobs):
    rows = []
    for j in jobs:
        rows.append(
            "<tr>"
            f"<td><a href='{escape(j['url'])}'>{escape(j['title'])}</a></td>"
            f"<td>{escape(j['company'])}</td>"
            f"<td>{escape(str(j.get('match_score', '')))}</td>"
            f"<td>{escape(j.get('match_reason', ''))}</td>"
            f"<td>{escape(' | '.join(j.get('score_breakdown', [])))}</td>"
            "</tr>"
        )
    return "".join(rows)


def send_email(jobs, borderline):
    sender, password = _env("GMAIL_ADDRESS"), _env("GMAIL_APP_PASSWORD")
    if not (sender and password) or _env("NOTIFY_EMAIL").lower() in ("0", "false", "no"):
        print("[i] Email notifications disabled")
        return True
    to_addr = _env("NOTIFY_EMAIL_TO") or sender
    head = "<tr><th>משרה</th><th>חברה</th><th>ציון</th><th>למה</th><th>פירוט</th></tr>"
    table = "<table border='1' cellpadding='6' style='border-collapse:collapse'>"
    html = "<div dir='rtl'>"
    if jobs:
        html += f"<h3>משרות חדשות שמתאימות לך ({len(jobs)})</h3>{table}{head}{_html_rows(jobs)}</table>"
    if borderline:
        html += f"<h3>על הגבול ({len(borderline)})</h3>{table}{head}{_html_rows(borderline)}</table>"
    html += "</div>"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🔔 {len(jobs)} משרות חדשות" + (f" + {len(borderline)} על הגבול" if borderline else "")
    msg["From"] = sender
    msg["To"] = to_addr
    msg.attach(MIMEText(html, "html", "utf-8"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(sender, password)
            s.sendmail(sender, [to_addr], msg.as_string())
        print(f"[✓] Email sent to {to_addr}")
        return True
    except Exception as exc:
        print(f"[!] Email send failed: {exc}")
        return False


def notify_new_jobs(jobs, borderline=None):
    borderline = borderline or []
    jobs = sorted(jobs, key=lambda j: -(j.get("match_score") or 0))
    borderline = sorted(borderline, key=lambda j: -(j.get("match_score") or 0))

    pending = []
    if os.path.exists(PENDING_FILE):
        try:
            with open(PENDING_FILE, encoding="utf-8") as f:
                pending = json.load(f)
        except Exception:
            pending = []

    messages = list(pending) + build_whatsapp_messages(jobs, borderline)
    if not messages:
        print("[i] No new jobs to notify about.")
    failed = send_whatsapp(messages) if messages else []
    if jobs or borderline:
        send_email(jobs, borderline)

    with open(PENDING_FILE, "w", encoding="utf-8") as f:
        json.dump(failed[-20:], f, ensure_ascii=False, indent=2)
