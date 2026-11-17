"""
Telegram alerts + feedback.

Secrets: TELEGRAM_BOT_TOKEN (from @BotFather), TELEGRAM_CHAT_ID (your numeric id, from @userinfobot).

Each job is sent as its own message with buttons:
  👍 מתאים   👎 לא מתאים   ✅ הגשתי
Button presses and text replies are collected at the start of the next run
(Telegram keeps updates for 24h) and appended to feedback.jsonl, which is used
to tune the scoring weights.
"""

import os
import json
import time
from datetime import datetime
from html import escape

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FEEDBACK_FILE = os.path.join(BASE_DIR, "feedback.jsonl")
RLM = "‏"

VERDICT_LABELS = {"good": "👍 מתאים", "bad": "👎 לא מתאים", "applied": "✅ הגשתי"}


def _env(name):
    return os.getenv(name, "").strip().strip('"').strip("'")


def enabled():
    return bool(_env("TELEGRAM_BOT_TOKEN") and _env("TELEGRAM_CHAT_ID"))


def _api(method, payload=None, timeout=30):
    url = f"https://api.telegram.org/bot{_env('TELEGRAM_BOT_TOKEN')}/{method}"
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload or {}, timeout=timeout)
            data = r.json()
            if data.get("ok"):
                return data.get("result")
            if r.status_code == 429:
                time.sleep(int(data.get("parameters", {}).get("retry_after", 3)) + 1)
                continue
            print(f"[!] Telegram {method} failed: {str(data.get('description'))[:200]}")
            return None
        except (requests.RequestException, ValueError) as exc:
            print(f"[!] Telegram network error ({method}): {exc}")
            time.sleep(2)
    return None


def _days_ago(iso_date):
    from datetime import date
    try:
        d = (date.today() - date.fromisoformat(str(iso_date)[:10])).days
    except ValueError:
        return ""
    return "היום" if d <= 0 else "אתמול" if d == 1 else f"לפני {d} ימים"


def format_job(job):
    src = "לינקדאין" if job.get("source") == "linkedin" else "אתר החברה"
    lines = [
        f"{RLM}<b>{escape(job['title'])}</b>",
        f"{RLM}חברה: {escape(job['company'])} | מקור: {src}",
        f"{RLM}ציון: <b>{job.get('match_score', '?')}</b> | מיקום: {escape(str(job.get('location', '')))}",
    ]
    if job.get("posted"):
        lines.append(f"{RLM}פורסם: {_days_ago(job['posted'])}")
    if job.get("match_reason"):
        lines.append(f"{RLM}למה: {escape(job['match_reason'])}")
    if job.get("score_breakdown"):
        lines.append(f"{RLM}<i>פירוט: {escape(' | '.join(job['score_breakdown']))}</i>")
    lines.append(f"<a href=\"{escape(job['url'], quote=True)}\">{RLM}לפתיחת המשרה</a>")
    return "\n".join(lines)


def _buttons(job_id):
    return {"inline_keyboard": [[
        {"text": VERDICT_LABELS["good"], "callback_data": f"fb:good:{job_id}"},
        {"text": VERDICT_LABELS["bad"], "callback_data": f"fb:bad:{job_id}"},
        {"text": VERDICT_LABELS["applied"], "callback_data": f"fb:applied:{job_id}"},
    ]]}


def build_payloads(jobs, borderline):
    chat = _env("TELEGRAM_CHAT_ID")
    out = []

    def add_text(text):
        out.append({"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})

    def add_job(job):
        p = {"chat_id": chat, "text": format_job(job), "parse_mode": "HTML", "disable_web_page_preview": True}
        if job.get("job_id"):
            p["reply_markup"] = _buttons(job["job_id"])
        out.append(p)

    if jobs:
        add_text(f"{RLM}🔔 <b>{len(jobs)} משרות חדשות שמתאימות לך</b>")
        for j in jobs:
            add_job(j)
    if borderline:
        add_text(
            f"{RLM}🟡 <b>על הגבול ({len(borderline)})</b>\n"
            f"{RLM}לא עברו את הסף, נשלחות לבדיקה שלך. סמן 👍 על מה שכן היה מתאים, ונכוון את המשקלות."
        )
        for j in borderline:
            add_job(j)
    return out


def send_payloads(payloads):
    """Returns the payloads that failed to send."""
    failed = []
    for p in payloads:
        if _api("sendMessage", p) is None:
            failed.append(p)
        time.sleep(1.1)  # stay under Telegram's per-chat rate limit
    print(f"[✓] Telegram: {len(payloads) - len(failed)}/{len(payloads)} message(s) sent")
    return failed


def collect_feedback(state):
    """Reads button presses / replies since the last run and appends them to feedback.jsonl."""
    if not enabled():
        return 0
    my_chat = str(_env("TELEGRAM_CHAT_ID"))
    offset = state.get("telegram_offset")
    payload = {"timeout": 0, "allowed_updates": ["callback_query", "message"]}
    if offset:
        payload["offset"] = offset
    updates = _api("getUpdates", payload) or []
    records = []
    for u in updates:
        state["telegram_offset"] = u["update_id"] + 1
        cq = u.get("callback_query")
        if cq:
            msg = cq.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != my_chat:
                continue
            parts = str(cq.get("data", "")).split(":")
            if len(parts) == 3 and parts[0] == "fb" and parts[1] != "noop":
                verdict, job_id = parts[1], parts[2]
                records.append({"ts": datetime.now().isoformat(timespec="minutes"), "job_id": job_id,
                                "verdict": verdict, "message": (msg.get("text") or "")[:300]})
                _api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": "נרשם"})
                _api("editMessageReplyMarkup", {
                    "chat_id": msg["chat"]["id"], "message_id": msg["message_id"],
                    "reply_markup": {"inline_keyboard": [[{"text": f"נרשם: {VERDICT_LABELS.get(verdict, verdict)}",
                                                           "callback_data": "fb:noop:0"}]]},
                })
            continue
        m = u.get("message")
        if m and str(m.get("chat", {}).get("id")) == my_chat and m.get("text") and not m["text"].startswith("/"):
            replied = (m.get("reply_to_message") or {}).get("text", "")
            records.append({"ts": datetime.now().isoformat(timespec="minutes"), "verdict": "comment",
                            "comment": m["text"][:500], "message": replied[:300]})
    records = [r for r in records if r.get("verdict") != "noop"]
    if records:
        with open(FEEDBACK_FILE, "a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        _api("sendMessage", {"chat_id": my_chat, "text": f"{RLM}📝 נרשמו {len(records)} משובים. תודה!"})
    print(f"[*] Telegram feedback collected: {len(records)}")
    return len(records)
