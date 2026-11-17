#!/usr/bin/env python3
"""
Autonomous Israeli Tech Job Crawler (v4.0 - Hybrid ATS API + Playwright + Strict YOE Guard).

Key Upgrades:
1. Hybrid Scraping Engine:
   - Direct ATS JSON APIs (Greenhouse, Lever, Ashby) for 100% structured data, location, and full descriptions.
   - Fast parallel `requests` for standard HTML career pages (extracting card titles even behind "Read More" links).
   - Optional Playwright Headless Chromium fallback for JS-rendered SPAs (Wix, Workday, React sites) when `requests` fails.
2. Strict Anti-Guessing & Minimum-YOE Guard:
   - NEVER sends empty/missing job descriptions to AI (minimum 150 chars required, zero blind title guessing).
   - Deterministic `has_excessive_min_yoe()` filter: keeps ranges like "1-3 years" or "2-3 years" (min <= max_yoe),
     while strictly rejecting postings whose minimum required experience is >= max_yoe + 1 (e.g. "3+ years", "3-5 years", "at least 3 years").
3. High-Precision Small-Batch AI Verification:
   - Evaluates batches of 4 jobs at a time with full clean text (up to 6,000 chars) so requirements are never lost in the middle.
4. Config-Driven (`config.json`) & Referral-Aware:
   - Reads `min_yoe`, `max_yoe`, `location`, `target_roles`, `excluded_roles`, `include_student_and_graduate`.
"""

import os
import sys
import re
import json
import time
import hashlib
from datetime import datetime
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
load_dotenv()

import requests
from bs4 import BeautifulSoup
import openpyxl

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# --- Paths & Configuration ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

if os.path.exists(os.path.join(BASE_DIR, "master_companies_with_referrals.json")):
    MASTER_FILE = os.path.join(BASE_DIR, "master_companies_with_referrals.json")
elif os.path.exists(os.path.join(BASE_DIR, "master_companies.json")):
    MASTER_FILE = os.path.join(BASE_DIR, "master_companies.json")
else:
    MASTER_FILE = os.path.join(BASE_DIR, "companies_registry_public.json")

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
SEEN_JOBS_FILE = os.path.join(BASE_DIR, "seen_jobs.json")
# Hashes of every job already sent to the AI (approved or rejected), so rejected jobs
# are not re-evaluated (and re-billed) on every run.
EVALUATED_FILE = os.path.join(BASE_DIR, "evaluated_jobs.json")
EVALUATED_TTL_DAYS = 45
PROFILE_FILE = os.path.join(BASE_DIR, "profile.md")
# Every AI evaluation with its score breakdown - used to tune the weights from your feedback.
EVAL_LOG_FILE = os.path.join(BASE_DIR, "evaluations_log.jsonl")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
# "fast" = no headless browser (runs often); "full" = also renders JS-heavy career sites
SCAN_MODE = os.getenv("SCAN_MODE", "full").strip().lower()


def title_key(company, title):
    """Cross-source identity (same job on the career page and on LinkedIn)."""
    norm = lambda x: re.sub(r"[^a-z0-9\u0590-\u05ff]+", " ", str(x).lower()).strip()
    return "ct:" + norm(company) + "|" + norm(title)


def url_key(url):
    u = re.sub(r"[?#].*$", "", str(url).strip().lower()).rstrip("/")
    return "url:" + re.sub(r"^https?://(www\.)?", "", u)


def append_eval_log(job, ev):
    rec = {
        "job_id": compute_job_hash(job["company"], job["title"], job["url"]),
        "ts": datetime.now().isoformat(timespec="minutes"),
        "company": job["company"], "title": job["title"], "url": job["url"],
        "source": job.get("source", "career_page"),
        "score": ev.get("match_score"), "approved": ev.get("approved"),
        "breakdown": ev.get("score_breakdown", []), "reason": ev.get("match_reason", ""),
        "rejection": ev.get("rejection_reason", ""),
        "facts": {k: ev.get(k) for k in ("min_years_required", "core_stack_match", "must_have_missing_core",
                                         "must_have_missing_other", "fullstack_in_title", "ai_involvement", "realistic_chance")},
    }
    with open(EVAL_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
CHECKPOINT_FILE = os.path.join(BASE_DIR, "progress_checkpoint.json")
OUTPUT_EXCEL = os.path.join(BASE_DIR, f"new_jobs_{datetime.now().strftime('%Y%m%d')}.xlsx")

import llm

DEFAULT_CONFIG = {
    "min_yoe": 0,
    "max_yoe": 2,
    "location": "Israel",
    "include_student_and_graduate": True,
    "target_roles": [
        "Software Development",
        "Backend",
        "Frontend",
        "Fullstack",
        "Mobile",
        "AI/ML",
        "Data Science",
        "Algorithms",
        "MLOps",
        "Software Automation"
    ],
    "excluded_roles": [
        "Manual QA",
        "DevOps",
        "Customer Success",
        "Operations",
        "Technical Support",
        "Sales",
        "Marketing",
        "HR",
        "Finance"
    ]
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,he;q=0.8",
}


def load_json(filepath, default_val):
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[!] Error loading {filepath}: {e}")
    return default_val


def save_json(filepath, data):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_user_config():
    raw = load_json(CONFIG_FILE, {})
    cfg = DEFAULT_CONFIG.copy()
    if isinstance(raw, dict):
        for k, v in raw.items():
            if v is not None:
                cfg[k] = v
    return cfg


def load_candidate_profile():
    """CV text: CANDIDATE_PROFILE secret first, then a local profile.md file."""
    env_profile = os.getenv("CANDIDATE_PROFILE", "").strip()
    if env_profile:
        return env_profile
    if os.path.exists(PROFILE_FILE):
        with open(PROFILE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


USER_CONFIG = load_user_config()
CANDIDATE_PROFILE = load_candidate_profile()
MIN_MATCH_SCORE = int(USER_CONFIG.get("min_match_score", 60))
TARGET_LOCATION = str(USER_CONFIG.get("location", "Israel")).strip() or "Israel"
IS_ISRAEL_TARGET = TARGET_LOCATION.lower() in ("israel", "ישראל")

# --- Strict Deterministic Regex Rules (Config-Aware) ---

EUROPE_GENDER_REGEX = re.compile(
    r"\b(m/f/d|m/w/d|d/m/w|m/f/x|w/m/d)\b",
    re.IGNORECASE
)

FOREIGN_LOCATION_REGEX = re.compile(
    r"\b(bangkok|thailand|singapore|australia|sydney|melbourne|london|uk\b|united kingdom|"
    r"germany|deutschland|berlin|munich|walldorf|potsdam|frankfurt|dresden|hamburg|"
    r"poland|warsaw|krakow|india|bangalore|bengaluru|hyderabad|pune|gurgaon|"
    r"canada|toronto|vancouver|new[\s\-_]+york|nyc\b|san[\s\-_]+francisco|california|seattle|"
    r"chicago|austin|texas|boston|paris|france|amsterdam|netherlands|dublin|ireland|"
    r"tokyo|japan|spain|madrid|barcelona|brazil|mexico|colombia|philippines|vietnam|"
    r"china|shenzhen|shanghai|beijing|taiwan|taipei|korea|seoul|us government|usa\b|united states|romania|bucharest|ukraine|kyiv|portugal|lisbon)\b",
    re.IGNORECASE
)

ISRAEL_LOCATION_REGEX = re.compile(
    r"\b(israel|ישראל|tel[\s\-_]+aviv|תל[\s\-_]+אביב|haifa|חיפה|herzliya|הרצליה|jerusalem|ירושלים|"
    r"beer[\s\-_]+sheva|באר[\s\-_]+שבע|petah[\s\-_]+tikva|פתח[\s\-_]+תקווה|raanana|רעננה|rehovot|רחובות|"
    r"ramat[\s\-_]+gan|רמת[\s\-_]+גן|yokneam|יוקנעם|glil[\s\-_]+yam|גליל[\s\-_]+ים|netanya|נתניה|"
    r"kfar[\s\-_]+saba|כפר[\s\-_]+סבא|holon|חולון|remote\s*\(?israel\)?)\b",
    re.IGNORECASE
)

NON_JOB_URL_PATTERNS = re.compile(
    r"("
    r"[?&]search="
    r"|/search\?"
    r"|searchresults"
    r"|/jobslobby/"
    r"|/categories/"
    r"|/all-jobs/"
    r"|/all_jobs/"
    r"|/alljobs"
    r"|/career-opportunities/students-internship"
    r"|developer-zone|developer_zone|developers|documentation|/docs/|/blog/|/resources/|/community/|/about/|/contact/|/news/"
    r"|/products?/|/solutions?/|/platform/|/features?/|/use-cases?/|/customers?/|/events?/|/webinars?/|/glossary/"
    r")",
    re.IGNORECASE
)

AGENCY_CATEGORY_REGEX = re.compile(
    r"(gotfriends\.co\.il/jobslobby/|"
    r"nisha\.co\.il/positions/[a-z\-]+/?$|"
    r"alljobs\.co\.il/search|"
    r"experis\.co\.il/search)",
    re.IGNORECASE
)

NAV_EXCLUDE_REGEX = re.compile(
    r"^(developer zone|developer portal|developer community|developer guide|developer hub|"
    r"developers|for developers|developer docs|documentation|api documentation|resources|"
    r"community|blog|news|about us|contact us|sign in|log in|sign up|products|solutions|privacy policy|terms|view all jobs|search jobs)$",
    re.IGNORECASE
)

GENERIC_CTA_REGEX = re.compile(
    r"^(read more|learn more|apply now|apply|view job|see details|more details|details|לפרטים|הגש מועמדות|למשרה|קרא עוד)$",
    re.IGNORECASE
)


def build_reject_title_regex(cfg):
    excluded_lower = " ".join(str(x).lower() for x in cfg.get("excluded_roles", []))
    max_yoe = int(cfg.get("max_yoe", 2))

    patterns = [
        r"legal", r"counsel", r"office manager", r"administrative",
        r"receptionist", r"executive assistant", r"payroll", r"accounting",
        r"מדריך", r"קורס", r"הכשרה", r"הכשרות", r"סדנה", r"טיפים", r"איך להצליח"
    ]

    if "customer success" in excluded_lower or "csm" in excluded_lower:
        patterns.extend([r"customer success", r"csm\b"])
    if "support" in excluded_lower:
        patterns.extend([r"support engineer", r"support specialist", r"it support", r"helpdesk", r"technical support"])
    if "operations" in excluded_lower:
        patterns.extend([r"operations", r"supply chain", r"logistics", r"procurement", r"supply operation"])
    if "sales" in excluded_lower:
        patterns.extend([r"sales", r"account executive", r"bdr\b", r"sdr\b", r"business development"])
    if "marketing" in excluded_lower:
        patterns.extend([r"marketing", r"content writer", r"growth", r"copywriter", r"seo\b"])
    if "hr" in excluded_lower or "recruiter" in excluded_lower:
        patterns.extend([r"recruiter", r"talent acquisition", r"hr\b", r"human resources"])
    if "finance" in excluded_lower:
        patterns.append(r"finance")
    if "qa" in excluded_lower:
        patterns.extend([r"qa\b", r"manual tester", r"qc\b", r"quality assurance"])
    if "devops" in excluded_lower:
        patterns.extend([r"devops", r"sre\b", r"infrastructure engineer", r"sysadmin", r"system administrator"])

    if max_yoe <= 3:
        patterns.extend([
            r"vp\b", r"director", r"chief", r"head of", r"lead\b",
            r"principal", r"staff\b", r"architect", r"manager\b", r"senior", r"sr\b", r"expert", r"leader"
        ])

    return re.compile(r"\b(" + "|".join(patterns) + r")\b", re.IGNORECASE)


SEP = r"[\s\-_/\.]*"  # "full stack", "full-stack", "full_stack", "fullstack", "full/stack"

# A real job title must name a ROLE, not just a topic ("AI Security" is a product page, not a job).
ROLE_EN_REGEX = re.compile(
    r"\b(" + "|".join([
        r"engineers?", r"developers?", r"programmers?", r"devs?", r"swe",
        rf"full{SEP}stack", rf"back{SEP}end", rf"front{SEP}end",
        r"scientists?", r"researchers?",
    ]) + r")\b",
    re.IGNORECASE,
)
# Hebrew: no \b, so prefixes (ו/ל/ה/ב) and gender forms (מפתח/ת, מפתחת) still match.
ROLE_HE_REGEX = re.compile(
    r"(מפתח|מתכנת|מהנדס|פיתוח|חוקר|" + rf"פול{SEP}סטא?ק|בק{SEP}אנד|פרונט{SEP}אנד)"
)
# Roles that are clearly not a fit for a web/AI developer.
REJECT_DOMAIN_REGEX = re.compile(
    r"\b(vlsi|asic|soc|fpga|rf|analog|mixed[\s\-]signal|physical[\s\-]design|layout|chip|silicon|"
    r"hardware|mechanical|electrical|photonic\w*|optic\w*|laser|firmware|embedded|kernel|bsp|dsp|"
    r"verification|validation|manufacturing|process|equipment|reliability|yield|test[\s\-]engineer|"
    r"network[\s\-]engineer|it[\s\-]engineer|solutions?[\s\-]engineer|sales[\s\-]engineer|pre[\s\-]?sales|"
    r"support|customer|field|application[\s\-]engineer|forward[\s\-]deployed|government|data[\s\-]analyst|"
    r"devops|dev[\s\-]ops|sre|site[\s\-]reliability|security[\s\-]engineers?|cloud[\s\-]engineer|cloud[\s\-]monitoring|"
    r"data[\s\-]engineer|data[\s\-]scientist|bi|etl|test[\s\-]development|qa|student|intern|internship)\b",
    re.IGNORECASE,
)
REJECT_HE_REGEX = re.compile(r"(בכיר|ראש צוות|ראש קבוצת|מנהל|מנהלת|סטודנט|מתמח|חומרה|אנלוג|שיווק|מכירות|תמיכה)")


def build_candidate_title_regex(cfg):
    """Kept for compatibility: a title is a candidate if it names a role (EN or HE)."""
    return ROLE_EN_REGEX



REJECT_TITLE_REGEX = build_reject_title_regex(USER_CONFIG)
CANDIDATE_TITLE_REGEX = build_candidate_title_regex(USER_CONFIG)


def has_excessive_min_yoe(text, max_allowed_yoe):
    """
    Checks if the job text explicitly requires a minimum number of years > max_allowed_yoe.
    - Allows ranges like '1-3 years' or '2-4 years' (because minimum is <= 2).
    - Rejects '3+ years', '3-5 years', 'at least 3 years', 'minimum 3 years', '4+ years of experience', etc.
    """
    if not text:
        return False

    # Match English and Hebrew minimum-experience patterns:
    # e.g., "3+ years", "3-5 years", "at least 3 years", "minimum of 4 years", "לפחות 3 שנות ניסיון", "3+ שנים"
    pattern = re.compile(
        r"(?:at\s+least|minimum(?:\s+of)?|min\.?|לפחות)?\s*"
        r"(\d{1,2})\s*(?:\+|(?:\s*[-–—to]+\s*\d{1,2}))?\s*"
        r"(?:years?|yrs?|שנים|שנות)\s*(?:of)?\s*(?:experience|industry|hands-on|ניסיון)?",
        re.IGNORECASE
    )

    nice_to_have = re.compile(r"advantage|nice[\s\-]to[\s\-]have|bonus|preferred|a plus|\bplus\b|יתרון", re.IGNORECASE)

    for match in pattern.finditer(text):
        full_phrase = match.group(0).lower()
        # Skip years mentioned only as an advantage (same line)
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        line = text[line_start:line_end if line_end != -1 else len(text)]
        if nice_to_have.search(line):
            continue
        min_years = int(match.group(1))
        # Ignore company age / founding references like "25 years of experience in insurance"
        if "company" in full_phrase or "founded" in full_phrase or "industry leader" in full_phrase:
            continue
        # Only trigger if it clearly refers to experience or has a '+' / range / 'at least' / 'minimum'
        has_qualifier = any(q in full_phrase for q in ["+", "-", "–", "—", "to", "at least", "minimum", "min", "לפחות", "experience", "ניסיון"])
        if has_qualifier and min_years > max_allowed_yoe and min_years <= 15:
            return True, match.group(0).strip()

    return False


# --- Helper Functions ---

def compute_job_hash(company, title, url):
    normalized = f"{company.strip().lower()}::{title.strip().lower()}::{url.strip().lower()}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def test_ai_preflight():
    print("=" * 60)
    print(f" [*] PRE-FLIGHT CHECK: Testing AI engine: {llm.describe()}...")
    print(f" [*] Active Config: YOE {USER_CONFIG['min_yoe']}-{USER_CONFIG['max_yoe']} | Location: {TARGET_LOCATION}")
    print(f" [*] Playwright Headless Fallback: {'ENABLED' if PLAYWRIGHT_AVAILABLE else 'DISABLED'}")
    print("=" * 60)
    try:
        llm.complete_text("Reply with strictly the word: OK", timeout=60)
    except Exception as exc:
        print(f"[!] ERROR: AI engine not working: {exc}")
        sys.exit(1)
    print("[✓] AI Pre-flight Check SUCCESSFUL!\n")
    return True


# --- ATS API Direct Scrapers ---

def try_ats_api_scrape(company_info, url):
    """
    Detects Greenhouse, Lever, or Ashby URLs and queries their official public JSON APIs directly.
    Returns (handled: bool, candidates: list)
    """
    name = company_info["name"]
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path_parts = [p for p in parsed.path.split("/") if p]

    # 1. Greenhouse API
    if "greenhouse.io" in host and path_parts:
        board = path_parts[0]
        api_url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
        try:
            r = requests.get(api_url, timeout=10)
            if r.status_code == 200:
                jobs_data = r.json().get("jobs", [])
                candidates = []
                for j in jobs_data:
                    title = j.get("title", "")
                    loc = (j.get("location") or {}).get("name", "Unspecified")
                    job_url = j.get("absolute_url", "")
                    if not is_title_candidate(title, job_url):
                        continue
                    if IS_ISRAEL_TARGET and FOREIGN_LOCATION_REGEX.search(loc) and not ISRAEL_LOCATION_REGEX.search(loc):
                        continue
                    raw_html = j.get("content", "")
                    clean_text = BeautifulSoup(raw_html, "html.parser").get_text(separator="\n", strip=True)
                    candidates.append({
                        "company": name,
                        "title": title,
                        "url": job_url,
                        "context_snippet": f"{title} - {loc}",
                        "requirements_text": clean_text[:6000],
                        "page_location": loc,
                        "referral_contact": company_info.get("referral_contact", "הגשה ישירה באתר (ללא רפרל)"),
                        "referral_type": company_info.get("referral_type", "הגשה ישירה")
                    })
                return True, candidates
        except Exception:
            pass

    # 2. Lever API
    if "jobs.lever.co" in host and path_parts:
        slug = path_parts[0]
        api_url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
        try:
            r = requests.get(api_url, timeout=10)
            if r.status_code == 200:
                jobs_data = r.json()
                candidates = []
                for j in jobs_data:
                    title = j.get("text", "")
                    loc = (j.get("categories") or {}).get("location", "Unspecified")
                    job_url = j.get("hostedUrl", "")
                    if not is_title_candidate(title, job_url):
                        continue
                    if IS_ISRAEL_TARGET and FOREIGN_LOCATION_REGEX.search(loc) and not ISRAEL_LOCATION_REGEX.search(loc):
                        continue
                    desc = j.get("descriptionPlain", "") + "\n" + "\n".join(
                        lst.get("text", "") + "\n" + BeautifulSoup(lst.get("content", ""), "html.parser").get_text("\n", strip=True)
                        for lst in j.get("lists", [])
                    )
                    candidates.append({
                        "company": name,
                        "title": title,
                        "url": job_url,
                        "context_snippet": f"{title} - {loc}",
                        "requirements_text": desc.strip()[:6000],
                        "page_location": loc,
                        "referral_contact": company_info.get("referral_contact", "הגשה ישירה באתר (ללא רפרל)"),
                        "referral_type": company_info.get("referral_type", "הגשה ישירה")
                    })
                return True, candidates
        except Exception:
            pass

    # 3. Ashby API
    if "jobs.ashbyhq.com" in host and path_parts:
        slug = path_parts[0]
        api_url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
        try:
            r = requests.get(api_url, timeout=10)
            if r.status_code == 200:
                jobs_data = r.json().get("jobs", [])
                candidates = []
                for j in jobs_data:
                    title = j.get("title", "")
                    loc = j.get("location", "Unspecified")
                    job_url = j.get("jobUrl", "")
                    if not is_title_candidate(title, job_url):
                        continue
                    if IS_ISRAEL_TARGET and FOREIGN_LOCATION_REGEX.search(loc) and not ISRAEL_LOCATION_REGEX.search(loc):
                        continue
                    desc = j.get("descriptionPlain", "") or BeautifulSoup(j.get("descriptionHtml", ""), "html.parser").get_text("\n", strip=True)
                    candidates.append({
                        "company": name,
                        "title": title,
                        "url": job_url,
                        "context_snippet": f"{title} - {loc}",
                        "requirements_text": desc.strip()[:6000],
                        "page_location": loc,
                        "referral_contact": company_info.get("referral_contact", "הגשה ישירה באתר (ללא רפרל)"),
                        "referral_type": company_info.get("referral_type", "הגשה ישירה")
                    })
                return True, candidates
        except Exception:
            pass

    return False, []


# --- HTML Scraping Logic ---

def is_title_candidate(title, url=""):
    t = re.sub(r"\s+", " ", title).strip()
    if len(t) < 4 or len(t) > 110:
        return False
    if NAV_EXCLUDE_REGEX.match(t):
        return False
    if NON_JOB_URL_PATTERNS.search(url):
        return False
    if AGENCY_CATEGORY_REGEX.search(url):
        return False
    if IS_ISRAEL_TARGET:
        if EUROPE_GENDER_REGEX.search(t):
            return False
        if FOREIGN_LOCATION_REGEX.search(f"{t} {url}") and not ISRAEL_LOCATION_REGEX.search(f"{t} {url}"):
            return False
    if REJECT_TITLE_REGEX.search(t) or REJECT_HE_REGEX.search(t) or REJECT_DOMAIN_REGEX.search(t):
        return False
    return bool(ROLE_EN_REGEX.search(t) or ROLE_HE_REGEX.search(t))


def extract_candidates_from_soup(soup, base_url, company_info):
    name = company_info["name"]
    links = soup.find_all("a", href=True)
    found_candidates = []

    for a in links:
        raw_title = a.get_text(separator=" ", strip=True)
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue

        title = raw_title
        parent_context = a.parent.get_text(separator=" ", strip=True) if a.parent else ""

        # If link text is just a generic CTA like "Read More" / "Apply", inspect nearby heading in parent card
        if GENERIC_CTA_REGEX.match(title) and a.parent:
            heading = a.parent.find(["h1", "h2", "h3", "h4", "strong"])
            if heading:
                title = heading.get_text(separator=" ", strip=True)
            elif a.parent.parent:
                heading = a.parent.parent.find(["h1", "h2", "h3", "h4", "strong"])
                if heading:
                    title = heading.get_text(separator=" ", strip=True)
                    parent_context = a.parent.parent.get_text(separator=" ", strip=True)

        full_url = urljoin(base_url, href)
        if is_title_candidate(title, full_url):
            if IS_ISRAEL_TARGET:
                if (FOREIGN_LOCATION_REGEX.search(parent_context) or EUROPE_GENDER_REGEX.search(parent_context)) and not ISRAEL_LOCATION_REGEX.search(parent_context):
                    continue

            found_candidates.append({
                "company": name,
                "title": title,
                "url": full_url,
                "context_snippet": parent_context[:300],
                "referral_contact": company_info.get("referral_contact", "הגשה ישירה באתר (ללא רפרל)"),
                "referral_type": company_info.get("referral_type", "הגשה ישירה")
            })

    return found_candidates


def scrape_single_company(company_info):
    name = company_info["name"]
    url = (
        company_info.get("official_scrape_url") or
        company_info.get("career_url") or
        company_info.get("link", "")
    ).strip()

    if not url:
        return name, False, [], "Missing career URL", company_info
    if "linkedin.com/in/" in url or "linkedin.com/company/" in url:
        return name, False, [], "LinkedIn profile/company page (direct outreach recommended)", company_info

    # Try direct ATS JSON API first (Greenhouse, Lever, Ashby)
    ats_handled, ats_candidates = try_ats_api_scrape(company_info, url)
    if ats_handled:
        return name, True, ats_candidates, "ATS API scrape successful", company_info

    try:
        resp = requests.get(url, headers=HEADERS, timeout=8)
        if resp.status_code in [403, 429, 503]:
            return name, False, [], f"Blocked by server (HTTP {resp.status_code})", company_info
        if resp.status_code != 200:
            return name, False, [], f"HTTP error {resp.status_code}", company_info
        if "challenge-running" in resp.text or "cf-browser-verification" in resp.text:
            return name, False, [], "Cloudflare bot challenge detected", company_info

        soup = BeautifulSoup(resp.text, "html.parser")
        found_candidates = extract_candidates_from_soup(soup, url, company_info)

        body_len = len(soup.get_text(strip=True))
        if len(found_candidates) == 0 and body_len < 600:
            return name, False, [], "Client-side rendered SPA (requires JS/browser)", company_info

        return name, True, found_candidates, "Scrape successful", company_info

    except requests.exceptions.RequestException as e:
        return name, False, [], f"Network exception: {type(e).__name__}", company_info


def scrape_spa_with_playwright(failed_list, companies_by_name):
    """Uses Headless Chromium to render JS-heavy career pages that failed static requests."""
    if not PLAYWRIGHT_AVAILABLE or not failed_list:
        return [], failed_list

    retryable = [
        f for f in failed_list
        if "SPA" in f.get("reason", "") or "403" in f.get("reason", "")
    ]
    non_retryable = [f for f in failed_list if f not in retryable]

    if not retryable:
        return [], failed_list

    print(f"\n=== STAGE 1B: Headless Browser (Playwright) Fallback ({len(retryable)} JS/protected sites) ===")
    recovered_candidates = []
    still_failed = list(non_retryable)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = context.new_page()

            for item in retryable:
                c_name = item["name"]
                url = item["link"]
                c_info = companies_by_name.get(c_name, {"name": c_name})
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(2500)
                    html = page.content()
                    soup = BeautifulSoup(html, "html.parser")
                    cands = extract_candidates_from_soup(soup, url, c_info)
                    if cands:
                        recovered_candidates.extend(cands)
                        print(f"  [+] Playwright recovered {len(cands)} candidates from {c_name}")
                    else:
                        still_failed.append(item)
                except Exception:
                    still_failed.append(item)

            browser.close()
    except Exception as exc:
        print(f"  [!] Playwright stage error: {exc}")
        return recovered_candidates, failed_list

    return recovered_candidates, still_failed


def extract_clean_text_from_soup(soup):
    extracted_text = ""
    detected_loc = "Unspecified"

    for script_tag in soup.find_all("script", type="application/ld+json"):
        try:
            js_data = json.loads(script_tag.string or "{}")
            if isinstance(js_data, list):
                js_data = js_data[0] if js_data else {}
            if js_data.get("@type") == "JobPosting":
                loc_obj = js_data.get("jobLocation", {})
                if isinstance(loc_obj, list):
                    loc_obj = loc_obj[0] if loc_obj else {}
                addr = loc_obj.get("address", {})
                locality = addr.get("addressLocality") or ""
                country = addr.get("addressCountry") or ""
                if locality or country:
                    detected_loc = f"{locality}, {country}".strip(", ")
                desc = js_data.get("description", "")
                if desc:
                    clean_desc = BeautifulSoup(desc, "html.parser").get_text(separator="\n", strip=True)
                    if len(clean_desc) > 200:
                        extracted_text = clean_desc[:6000]
                        break
        except Exception:
            pass

    if detected_loc == "Unspecified":
        loc_candidates = []
        for tag in soup.find_all(["span", "div", "p", "li"], class_=re.compile(r"locat|workplace|city|country", re.I)):
            txt = tag.get_text(strip=True)
            if 2 < len(txt) < 60:
                loc_candidates.append(txt)
        if loc_candidates:
            detected_loc = " / ".join(loc_candidates[:2])

    if not extracted_text:
        for elem in soup(["script", "style", "nav", "footer", "noscript", "svg"]):
            elem.decompose()
        body_text = soup.get_text(separator="\n", strip=True)
        lines = [line.strip() for line in body_text.splitlines() if len(line.strip()) > 3]
        extracted_text = "\n".join(lines)[:6000]

    return extracted_text, detected_loc


def fetch_job_details(job):
    # If ATS API already populated full requirements text, keep it!
    if len(job.get("requirements_text", "")) >= 200:
        return job

    url = job["url"]
    extracted_text = ""
    detected_loc = job.get("page_location", "Unspecified")

    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        if r.status_code == 200:
            soup = BeautifulSoup(r.text, "html.parser")
            extracted_text, detected_loc = extract_clean_text_from_soup(soup)
    except Exception:
        pass

    job["requirements_text"] = extracted_text
    job["page_location"] = detected_loc
    return job


def fetch_missing_details_with_playwright(jobs_list):
    """For candidate jobs where static requests returned empty/JS shell, render with Playwright."""
    if not PLAYWRIGHT_AVAILABLE:
        return jobs_list

    missing = [j for j in jobs_list if len(j.get("requirements_text", "")) < 150]
    if not missing:
        return jobs_list

    print(f"[*] Rendering {len(missing)} JS job pages with Playwright to read full requirements...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = context.new_page()

            for job in missing:
                try:
                    page.goto(job["url"], wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(2000)
                    soup = BeautifulSoup(page.content(), "html.parser")
                    txt, loc = extract_clean_text_from_soup(soup)
                    if len(txt) > len(job.get("requirements_text", "")):
                        job["requirements_text"] = txt
                    if loc != "Unspecified":
                        job["page_location"] = loc
                except Exception:
                    pass

            browser.close()
    except Exception as exc:
        print(f"  [!] Playwright job detail fetch error: {exc}")

    return jobs_list


# --- Strict 4-Way Config-Driven Batch AI Evaluation ---

DEFAULT_SCORING = {
    "base": 50,
    "years_ok": 10,             # required minimum <= candidate_years + 0.5
    "years_unstated": 5,        # posting doesn't say
    "years_stretch": 0,         # required minimum between that and max_yoe (e.g. 3)
    "stack_strong": 15,         # the role's main stack is what the candidate works with
    "stack_partial": 5,
    "stack_none": -10,
    "missing_core": -20,        # each missing MUST-HAVE that is central to the role (main language/framework)
    "missing_other": -6,        # each missing MUST-HAVE that is secondary (a tool, a DB, a cloud service)
    "missing_cap": -40,
    "fullstack_title": 15,
    "fullstack_description": 5,
    "ai_product": 12,           # builds AI/LLM features (agents, RAG, LLM integrations) as an engineer
    "ai_tools": 3,              # only mentions AI-assisted development / AI tools
    "chance_high": 5,
    "chance_low": -10,
}

def compute_match_score(ev):
    """
    Deterministic, explainable score from the AI's structured findings.
    Returns (score or None if disqualified, breakdown list of Hebrew strings).
    """
    w = dict(DEFAULT_SCORING)
    w.update(USER_CONFIG.get("scoring") or {})
    max_yoe = int(USER_CONFIG.get("max_yoe", 3))
    candidate_years = float(USER_CONFIG.get("candidate_years", 1.5))
    parts = []

    min_req = ev.get("min_years_required")
    try:
        min_req = None if min_req in (None, "") else float(min_req)
    except (TypeError, ValueError):
        min_req = None
    if min_req is not None and min_req > max_yoe:
        return None, [f"דורש {int(min_req)} שנים ומעלה"]

    score = w["base"]
    if min_req is None:
        score += w["years_unstated"]; parts.append(f"שנים לא צוינו {w['years_unstated']:+d}")
    elif min_req <= candidate_years + 0.5:
        score += w["years_ok"]; parts.append(f"דורש {min_req:g} שנים {w['years_ok']:+d}")
    else:
        score += w["years_stretch"]; parts.append(f"דורש {min_req:g} שנים {w['years_stretch']:+d}")

    stack = str(ev.get("core_stack_match", "")).lower()
    if stack in ("strong", "partial", "none"):
        pts = w["stack_" + stack]
        score += pts
        label = {"strong": "סטאק תואם", "partial": "סטאק תואם חלקית", "none": "סטאק לא תואם"}[stack]
        parts.append(f"{label} {pts:+d}")

    core = ev.get("must_have_missing_core") or []
    other = ev.get("must_have_missing_other") or []
    missing_pts = max(w["missing_cap"], w["missing_core"] * len(core) + w["missing_other"] * len(other))
    if core or other:
        score += missing_pts
        names = ", ".join(list(core) + list(other))
        parts.append(f"חסר כחובה: {names} {missing_pts:+d}")

    if ev.get("fullstack_in_title"):
        score += w["fullstack_title"]; parts.append(f"פול סטאק בכותרת {w['fullstack_title']:+d}")
    elif ev.get("fullstack_in_description"):
        score += w["fullstack_description"]; parts.append(f"פול סטאק בתיאור {w['fullstack_description']:+d}")
    ai = str(ev.get("ai_involvement", "")).lower()
    if ai == "product":
        score += w["ai_product"]; parts.append(f"בונה מוצר בינה מלאכותית {w['ai_product']:+d}")
    elif ai == "tools":
        score += w["ai_tools"]; parts.append(f"עבודה עם כלי בינה מלאכותית {w['ai_tools']:+d}")

    chance = str(ev.get("realistic_chance", "")).lower()
    if chance == "high":
        score += w["chance_high"]; parts.append(f"סיכוי גבוה לזימון {w['chance_high']:+d}")
    elif chance == "low":
        score += w["chance_low"]; parts.append(f"סיכוי נמוך לזימון {w['chance_low']:+d}")

    return max(0, min(100, int(score))), parts


def batch_verify_jobs_with_ai(jobs_batch):
    max_yoe = int(USER_CONFIG.get("max_yoe", 3))
    candidate_years = USER_CONFIG.get("candidate_years", 1.5)
    target_roles_str = ", ".join(USER_CONFIG.get("target_roles", []))
    excluded_roles_str = ", ".join(USER_CONFIG.get("excluded_roles", []))

    location_clause = (
        f"MUST be located in {TARGET_LOCATION} (any Israeli city, Hybrid, or Remote {TARGET_LOCATION}). "
        f"REJECT positions located outside {TARGET_LOCATION}."
        if IS_ISRAEL_TARGET else f"MUST match the target location: {TARGET_LOCATION}."
    )

    items_text = []
    for idx, j in enumerate(jobs_batch, 1):
        items_text.append(
            f"=== Job #{idx} ===\n"
            f"Company: {j['company']}\nTitle: {j['title']}\nURL: {j['url']}\n"
            f"Detected Page Location: {j.get('page_location', 'Unspecified')}\n"
            f"Full Job Description & Requirements:\n{j.get('requirements_text', '').strip()[:5500]}\n"
        )

    profile_block = (
        "=== CANDIDATE PROFILE ===\n" + CANDIDATE_PROFILE[:6000] + "\n=== END PROFILE ===\n\n"
        if CANDIDATE_PROFILE else ""
    )

    prompt = (
        "You are an expert technical recruiter in the Israeli tech market. For each job posting below, "
        "extract structured facts. Do NOT compute any score yourself.\n\n"
        f"{profile_block}"
        f"The candidate has about {candidate_years} years of professional experience.\n\n"
        "For each job decide:\n"
        "- is_single_specific_job: a real, individual open vacancy (not a category page, search page, talent pool or 'send CV' form).\n"
        f"- is_target_role: matches one of [{target_roles_str}] and is NOT one of [{excluded_roles_str}].\n"
        f"- is_matching_location: {location_clause}\n"
        "- min_years_required: the MINIMUM years of professional experience the posting REQUIRES overall "
        "(e.g. '3-5 years' -> 3, '2+ years' -> 2). Ignore years mentioned only as an advantage/nice-to-have/bonus/'יתרון'. "
        "If it requires N years in one specific technology as a must, use that N if it is higher. null if not stated.\n"
        "- core_stack_match: 'strong' if the role's MAIN languages/frameworks are ones the candidate works with "
        "(e.g. TypeScript/Node/React/Vue/Python), 'partial' if some of them, 'none' if the main stack is something else.\n"
        "- must_have_missing_core: REQUIRED/MUST skills the candidate lacks that are CENTRAL to the role "
        "(the main programming language or main framework, e.g. Java, C#, Go, C++, Angular).\n"
        "- must_have_missing_other: REQUIRED/MUST skills the candidate lacks that are SECONDARY "
        "(a specific tool, database, queue, cloud service, testing framework).\n"
        "  For both lists: do NOT include anything under advantage / nice to have / bonus / plus / 'יתרון'. "
        "Treat close equivalents as known (React~Vue, Node~NestJS, AWS~any cloud, MongoDB~PostgreSQL~SQL, Docker~containers). "
        "Use short tech names. Empty list if nothing missing.\n"
        "- nice_to_have_missing: advantage-only skills the candidate lacks (informational).\n"
        "- fullstack_in_title: the job TITLE says Full Stack / Fullstack / Full-Stack.\n"
        "- fullstack_in_description: the description describes full-stack work (both frontend and backend).\n"
        "- ai_involvement: 'product' if the engineer will BUILD AI features in the product (LLM integrations, agents, RAG, "
        "AI APIs, AI-powered workflows) as an application/software engineer; 'research' if it is model training / ML research / "
        "data science that needs an ML/math background; 'tools' if the posting only mentions using AI coding tools "
        "(Cursor, Copilot, Claude Code) or 'AI-first engineering culture'; 'none' otherwise.\n"
        "- realistic_chance: 'high' / 'medium' / 'low' - your honest estimate that a recruiter would call this candidate "
        "for an interview, given his experience and the must-haves.\n"
        "- match_reason: ONE short sentence IN HEBREW: what fits and what is missing.\n\n"
        "Candidate Postings:\n"
        f"{''.join(items_text)}\n"
        "Output ONLY valid JSON, no prose, with this exact structure:\n"
        '{"evaluations": [{"job_index": 1, "is_single_specific_job": true, "is_target_role": true, '
        '"is_matching_location": true, "min_years_required": 2, "core_stack_match": "partial", '
        '"must_have_missing_core": ["Java"], "must_have_missing_other": ["Kafka"], '
        '"nice_to_have_missing": ["GraphQL"], "fullstack_in_title": true, "fullstack_in_description": true, '
        '"ai_involvement": "tools", "realistic_chance": "medium", "experience_summary": "2+ years", '
        '"detected_city": "Tel Aviv", "match_reason": "..."}]}'
    )

    try:
        parsed = llm.complete_json(prompt)
    except Exception as e:
        print(f"    [!] Error during batch AI verification: {e}")
        return []

    evaluations = parsed.get("evaluations", []) if isinstance(parsed, dict) else []
    for ev in evaluations:
        basic_ok = all(ev.get(k) for k in ("is_single_specific_job", "is_target_role", "is_matching_location"))
        try:
            ev["job_index"] = int(ev.get("job_index"))
        except (TypeError, ValueError):
            pass
        score, breakdown = compute_match_score(ev)
        ev["match_score"] = score
        ev["score_breakdown"] = breakdown
        ev["approved"] = bool(basic_ok and score is not None)
        if not basic_ok:
            failed = [k for k in ("is_single_specific_job", "is_target_role", "is_matching_location") if not ev.get(k)]
            ev["rejection_reason"] = "failed: " + ", ".join(failed)
        elif score is None:
            ev["rejection_reason"] = f"requires {ev.get('min_years_required')}+ years"
    return evaluations


# --- Excel Generation ---

def export_to_excel(confirmed_jobs, unreached_companies, filepath):
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "New Jobs with Referrals"
    ws1.append([
        "שם החברה",
        "שם המשרה",
        "איש קשר להגשת רפרל",
        "סוג רפרל / הגשה",
        "מיקום",
        "קישור למשרה",
        "דרישות ניסיון",
        "תאריך גילוי"
    ])

    default_exp_label = f"{USER_CONFIG['min_yoe']}-{USER_CONFIG['max_yoe']} YOE (AI Verified)"
    for nj in confirmed_jobs:
        ws1.append([
            nj["company"],
            nj["title"],
            nj.get("referral_contact", "הגשה ישירה באתר (ללא רפרל)"),
            nj.get("referral_type", "הגשה ישירה"),
            nj.get("location", TARGET_LOCATION),
            nj["url"],
            nj.get("experience", default_exp_label),
            nj.get("discovered_at", datetime.now().strftime("%Y-%m-%d"))
        ])

    if unreached_companies:
        ws2 = wb.create_sheet(title="לבדיקה ידנית ורפרל ישיר")
        ws2.append(["שם החברה", "איש קשר לרפרל", "קישור / פרופיל", "סיבה"])
        for uc in unreached_companies:
            ws2.append([
                uc["name"],
                uc.get("referral_contact", ""),
                uc.get("link", ""),
                uc.get("reason", "Requires manual check")
            ])

    for sheet in wb.worksheets:
        for col in sheet.columns:
            max_len = max(len(str(cell.value or '')) for cell in col)
            col_letter = openpyxl.utils.get_column_letter(col[0].column)
            sheet.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 55)

    wb.save(filepath)
    print(f"[✓] Real-time Excel exported: {len(confirmed_jobs)} confirmed job(s) -> {filepath}")


# --- Main Execution ---

def main():
    test_ai_preflight()

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Loading companies registry: {os.path.basename(MASTER_FILE)}...")

    companies = load_json(MASTER_FILE, [])
    companies_by_name = {c["name"]: c for c in companies}
    seen_jobs = load_json(SEEN_JOBS_FILE, [])
    seen_hashes = {compute_job_hash(j["company"], j["title"], j["url"]) for j in seen_jobs}

    # Previously evaluated (incl. rejected) jobs - pruned after EVALUATED_TTL_DAYS
    evaluated = load_json(EVALUATED_FILE, {})
    cutoff = time.time() - EVALUATED_TTL_DAYS * 86400
    evaluated = {h: ts for h, ts in evaluated.items() if ts >= cutoff}
    seen_hashes |= set(evaluated.keys())
    notify_this_run = []
    borderline_this_run = []
    state = load_json(STATE_FILE, {})
    backfill = not state.get("backfill_done", False)
    try:
        import telegram_bot
        telegram_bot.collect_feedback(state)
        save_json(STATE_FILE, state)
    except Exception as exc:
        print(f"[!] Telegram feedback step failed: {exc}")
    max_ai_jobs = int(USER_CONFIG.get("max_ai_jobs_per_run", 80))
    backfill_left = False
    print(f"[*] Scan mode: {SCAN_MODE} | LinkedIn backfill (last month): {'yes' if backfill else 'no'} | AI cap per run: {max_ai_jobs}")

    if CANDIDATE_PROFILE:
        print(f"[*] Candidate profile loaded ({len(CANDIDATE_PROFILE)} chars) - min match score: {MIN_MATCH_SCORE}")
    else:
        print("[i] No candidate profile (CANDIDATE_PROFILE / profile.md) - CV scoring disabled")

    checkpoint = load_json(CHECKPOINT_FILE, {
        "scraped_companies": [],
        "confirmed_jobs": [],
        "failed_companies": [],
        "pending_candidates": []
    })

    scraped_set = set(checkpoint.get("scraped_companies", []))
    confirmed_new_jobs = checkpoint.get("confirmed_jobs", [])
    failed_companies = checkpoint.get("failed_companies", [])
    direct_candidates = checkpoint.get("pending_candidates", [])

    if scraped_set:
        print(f"[*] Resuming from checkpoint: {len(scraped_set)}/{len(companies)} processed, {len(confirmed_new_jobs)} jobs found.")

    # --- STAGE 1: Parallel Deterministic & ATS API Scrape ---
    remaining_companies = [c for c in companies if c["name"] not in scraped_set]

    if remaining_companies:
        print(f"\n=== STAGE 1: High-Speed Parallel Scraping ({len(remaining_companies)} companies) ===")
        max_workers = 12

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_company = {
                executor.submit(scrape_single_company, c): c for c in remaining_companies
            }

            completed_count = 0
            for future in as_completed(future_to_company):
                c = future_to_company[future]
                completed_count += 1
                try:
                    c_name, success, jobs, reason, c_info = future.result()
                    if not success:
                        failed_companies.append({
                            "name": c_name,
                            "link": c_info.get("career_url") or c_info.get("link", ""),
                            "referral_contact": c_info.get("referral_contact", ""),
                            "reason": reason
                        })
                    else:
                        if jobs:
                            direct_candidates.extend(jobs)

                    scraped_set.add(c["name"])

                except Exception as exc:
                    failed_companies.append({
                        "name": c["name"],
                        "link": c.get("career_url") or c.get("link", ""),
                        "referral_contact": c.get("referral_contact", ""),
                        "reason": f"Execution error: {exc}"
                    })
                    scraped_set.add(c["name"])

                if completed_count % 30 == 0 or completed_count == len(remaining_companies):
                    checkpoint["scraped_companies"] = list(scraped_set)
                    checkpoint["failed_companies"] = failed_companies
                    checkpoint["pending_candidates"] = direct_candidates
                    save_json(CHECKPOINT_FILE, checkpoint)
                    print(f"  -> Progress: {completed_count}/{len(remaining_companies)} companies scraped ({len(direct_candidates)} candidates found)...")

        # STAGE 1B: Playwright Headless Fallback for JS/SPA sites
        recovered = []
        if SCAN_MODE == "full":
            recovered, failed_companies = scrape_spa_with_playwright(failed_companies, companies_by_name)
        if recovered:
            direct_candidates.extend(recovered)
            checkpoint["failed_companies"] = failed_companies
            checkpoint["pending_candidates"] = direct_candidates
            save_json(CHECKPOINT_FILE, checkpoint)

        export_to_excel(confirmed_new_jobs, failed_companies, OUTPUT_EXCEL)

    print(f"\n[Stage 1 Summary] Successful: {len(companies) - len(failed_companies)} | Un-scraped/LinkedIn: {len(failed_companies)}")
    print(f"[*] Total candidate links collected: {len(direct_candidates)}")

    # --- STAGE 1C: LinkedIn public job search ---
    try:
        from linkedin_source import collect_linkedin_candidates
        li_candidates = collect_linkedin_candidates(
            USER_CONFIG,
            is_title_candidate,
            lambda co, ti, u: compute_job_hash(co, ti, u) in seen_hashes or title_key(co, ti) in seen_hashes,
            backfill=backfill,
        )
        direct_candidates.extend(li_candidates)
    except Exception as exc:
        print(f"[!] LinkedIn stage failed (continuing without it): {exc}")

    unique_candidates = []
    candidate_hashes = set()
    for cand in direct_candidates:
        h = compute_job_hash(cand["company"], cand["title"], cand["url"])
        tk = title_key(cand["company"], cand["title"])
        uk = url_key(cand["url"])
        keys = (h, tk, uk)
        if any(k in seen_hashes or k in candidate_hashes for k in keys):
            continue
        candidate_hashes.update(keys)
        unique_candidates.append(cand)

    print(f"[*] Brand-new unseen candidates to process: {len(unique_candidates)}")

    # --- STAGE 2: Parallel Page Details Fetching + Playwright + Deterministic YOE Guard ---
    if unique_candidates:
        print(f"\n=== STAGE 2: Deep Fetching Job Details ({len(unique_candidates)} jobs) ===")
        with ThreadPoolExecutor(max_workers=8) as executor:
            fetched_candidates = list(executor.map(fetch_job_details, unique_candidates))

        if SCAN_MODE == "full":
            fetched_candidates = fetch_missing_details_with_playwright(fetched_candidates)

        max_allowed_yoe = int(USER_CONFIG.get("max_yoe", 2))
        ai_ready_candidates = []

        for job in fetched_candidates:
            txt = job.get("requirements_text", "").strip()
            # Strict Anti-Guessing Rule: Reject if description could not be extracted
            if len(txt) < 150:
                print(f"  [X] SKIPPED (No readable description): {job['company']} - {job['title']}")
                continue
            # Strict Minimum-YOE Regex Guard (keeps 1-3, rejects 3+, 3-5, 4+, 5+)
            excessive = has_excessive_min_yoe(txt, max_allowed_yoe)
            if excessive:
                print(f"  [X] REJECTED (Min YOE > {max_allowed_yoe}: '{excessive[1]}'): {job['company']} - {job['title']}")
                evaluated[compute_job_hash(job["company"], job["title"], job["url"])] = time.time()
                continue
            ai_ready_candidates.append(job)

        print(f"[*] Candidates passing description & YOE pre-checks for AI review: {len(ai_ready_candidates)}")
        if len(ai_ready_candidates) > max_ai_jobs:
            # Not marked as evaluated -> picked up again by the next run.
            print(f"[*] AI cap: evaluating {max_ai_jobs} now, {len(ai_ready_candidates) - max_ai_jobs} left for next runs")
            ai_ready_candidates = ai_ready_candidates[:max_ai_jobs]
            backfill_left = True
        else:
            backfill_left = False

        # --- STAGE 3: High-Precision Small-Batch AI Verification ---
        if ai_ready_candidates:
            print(f"\n=== STAGE 3: Precision Batch AI Verification ({len(ai_ready_candidates)} jobs) ===")
            batch_size = 4
            batches = [ai_ready_candidates[i:i + batch_size] for i in range(0, len(ai_ready_candidates), batch_size)]
            print(f"[*] Grouped into {len(batches)} batches of up to {batch_size} jobs each.\n")

            default_exp_label = f"{USER_CONFIG['min_yoe']}-{USER_CONFIG['max_yoe']} YOE (AI Verified)"

            for b_idx, batch in enumerate(batches, 1):
                print(f"[*] Sending Batch {b_idx}/{len(batches)} ({len(batch)} jobs) to AI...")
                evaluations = batch_verify_jobs_with_ai(batch)

                eval_map = {ev.get("job_index"): ev for ev in evaluations}

                for idx, job in enumerate(batch, 1):
                    ev = eval_map.get(idx)
                    h = compute_job_hash(job["company"], job["title"], job["url"])
                    if ev:
                        evaluated[h] = time.time()
                        evaluated[title_key(job["company"], job["title"])] = time.time()
                        evaluated[url_key(job["url"])] = time.time()
                        append_eval_log(job, ev)

                    score = ev.get("match_score") if ev else None
                    low_score = score is not None and score < MIN_MATCH_SCORE
                    calib = USER_CONFIG.get("calibration") or {}
                    is_borderline = (
                        calib.get("enabled") and low_score and score >= int(calib.get("min_score", 40))
                    )

                    if ev and ev.get("approved") and low_score:
                        print(f"  [X] LOW FIT ({score}): {job['company']} - {job['title']} ({ev.get('match_reason', '')})")
                        if is_borderline:
                            borderline_this_run.append({
                                "company": job["company"], "title": job["title"], "url": job["url"],
                                "location": ev.get("detected_city") or TARGET_LOCATION,
                                "experience": ev.get("experience_summary", ""),
                                "source": job.get("source", "career_page"),
                                "match_score": score, "match_reason": ev.get("match_reason", ""),
                                "score_breakdown": ev.get("score_breakdown", []),
                                "posted": job.get("posted", ""),
                                "job_id": h,
                            })
                    elif ev and ev.get("approved"):
                        location = ev.get("detected_city") or TARGET_LOCATION
                        exp_sum = ev.get("experience_summary") or default_exp_label

                        entry = {
                            "company": job["company"],
                            "title": job["title"],
                            "referral_contact": job.get("referral_contact", "הגשה ישירה באתר (ללא רפרל)"),
                            "referral_type": job.get("referral_type", "הגשה ישירה"),
                            "location": location,
                            "url": job["url"],
                            "experience": exp_sum,
                            "discovered_at": datetime.now().isoformat(),
                            "source": job.get("source", "career_page"),
                            "match_score": score,
                            "match_reason": ev.get("match_reason", ""),
                            "score_breakdown": ev.get("score_breakdown", []),
                            "posted": job.get("posted", ""),
                            "job_id": h,
                        }
                        confirmed_new_jobs.append(entry)
                        notify_this_run.append(entry)
                        seen_jobs.append(entry)
                        seen_hashes.add(h)
                        print(f"  [✓] APPROVED: {job['company']} - {job['title']} [{location}] -> [Referral: {entry['referral_contact']}]")
                    else:
                        reason = ev.get("rejection_reason") if ev else "Unconfirmed by AI"
                        print(f"  [X] REJECTED: {job['company']} - {job['title']} (Reason: {reason})")

                checkpoint["confirmed_jobs"] = confirmed_new_jobs
                checkpoint["pending_candidates"] = []
                save_json(CHECKPOINT_FILE, checkpoint)
                save_json(SEEN_JOBS_FILE, seen_jobs)
                save_json(EVALUATED_FILE, evaluated)
                export_to_excel(confirmed_new_jobs, failed_companies, OUTPUT_EXCEL)

    # --- STAGE 4: Finalization ---
    if backfill and not backfill_left:
        state["backfill_done"] = True
        print("[*] LinkedIn month backfill complete - from now on only the last 24h.")
    save_json(STATE_FILE, state)
    save_json(SEEN_JOBS_FILE, seen_jobs)
    save_json(EVALUATED_FILE, evaluated)

    try:
        from notifier import notify_new_jobs
        notify_new_jobs(notify_this_run, borderline_this_run)
    except Exception as exc:
        print(f"[!] Notification step failed: {exc}")
    export_to_excel(confirmed_new_jobs, failed_companies, OUTPUT_EXCEL)

    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)

    print(f"\n" + "=" * 60)
    print(f"[✓] CRAWL COMPLETED SUCCESSFULLY!")
    print(f"[*] Newly Discovered Jobs: {len(confirmed_new_jobs)}")
    print(f"[*] Results saved to: {OUTPUT_EXCEL}")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\n[!] Unexpected exit: {exc}")
        print("Progress checkpoint and Excel are preserved.")