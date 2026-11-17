"""
AI engine used to evaluate jobs. Returns parsed JSON from a prompt.

AI_ENGINE:
  "claude" - Claude Code CLI with your Claude subscription (CLAUDE_CODE_OAUTH_TOKEN
             from `claude setup-token`). No API key, no extra billing.
  "gemini" - Gemini API (GEMINI_API_KEY). Free tier, or your Google AI Pro monthly
             Cloud credits if you link them in AI Studio.
  unset    - "claude" if CLAUDE_CODE_OAUTH_TOKEN is set, else "gemini".
"""

import os
import re
import json
import time
import shutil
import subprocess

import requests


def _env(name, default=""):
    return os.getenv(name, default).strip().strip('"').strip("'")


GEMINI_API_KEY = _env("GEMINI_API_KEY")
GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-flash-latest")
CLAUDE_MODEL = _env("CLAUDE_MODEL", "sonnet")


def engine():
    e = _env("AI_ENGINE").lower()
    if e in ("claude", "gemini"):
        return e
    return "claude" if _env("CLAUDE_CODE_OAUTH_TOKEN") else "gemini"


def describe():
    return f"Claude Code ({CLAUDE_MODEL}, subscription)" if engine() == "claude" else f"Gemini API ({GEMINI_MODEL})"


def _extract_json(text):
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


# --- Claude Code (subscription) ---

def _claude(prompt, timeout):
    exe = shutil.which("claude")
    if not exe:
        raise RuntimeError("Claude Code CLI not installed (npm install -g @anthropic-ai/claude-code)")
    # Pure text task: no tools (prevents it from trying to browse the job URLs).
    cmd = [exe, "-p", "--output-format", "json", "--model", CLAUDE_MODEL, "--max-turns", "2",
           "--disallowedTools", "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit"]
    last_err = None
    for attempt in range(1, 4):
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
            if proc.returncode == 0:
                outer = json.loads(proc.stdout)
                if outer.get("is_error"):
                    raise RuntimeError(str(outer.get("result", ""))[:300])
                return outer.get("result", "")
            last_err = (proc.stderr or proc.stdout)[:300]
        except (subprocess.TimeoutExpired, json.JSONDecodeError, RuntimeError) as exc:
            last_err = str(exc)
        print(f"    [!] Claude call failed ({attempt}/3): {last_err}")
        time.sleep(10 * attempt)
    raise RuntimeError(f"Claude Code unavailable: {last_err}")


# --- Gemini API ---

def _gemini(prompt, timeout):
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"},
    }
    for attempt in range(1, 6):
        try:
            r = requests.post(url, json=payload, timeout=timeout, headers={"x-goog-api-key": GEMINI_API_KEY})
            if r.status_code == 200:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"]
            print(f"    [!] Gemini HTTP {r.status_code} ({attempt}/5)")
            if r.status_code not in (408, 429, 500, 502, 503, 504):
                raise RuntimeError(f"Gemini HTTP {r.status_code}: {r.text[:200]}")
        except requests.RequestException as exc:
            print(f"    [!] Gemini network error ({attempt}/5): {exc}")
        time.sleep(min(4 * 2 ** (attempt - 1), 45))
    raise RuntimeError("Gemini API unavailable after retries")


def complete_text(prompt, timeout=180):
    return _claude(prompt, timeout) if engine() == "claude" else _gemini(prompt, timeout)


def complete_json(prompt, timeout=180):
    return _extract_json(complete_text(prompt, timeout))
