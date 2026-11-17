"""
LinkedIn job source (public, logged-out job search - no LinkedIn account is used).

Runs a few keyword searches for jobs posted in Israel in the last ~day, then
fetches each job's public description so it can go through the same filters
and AI check as the company career pages.

Config (config.json -> "linkedin"):
  enabled: true/false
  keywords: ["full stack developer", "ai engineer", ...]
  posted_within_hours: 24
  max_pages_per_keyword: 3   (~10 jobs per page)
"""

import re
import time
import random

import requests
from bs4 import BeautifulSoup

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,he;q=0.8",
}
JOB_ID_RE = re.compile(r"(?:jobPosting:|-)(\d{8,})")


def _get(url, params=None, attempts=3):
    for i in range(attempts):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=15)
            if r.status_code == 200:
                return r.text
            if r.status_code == 429:
                time.sleep(5 * (i + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(2)
    return None


def search_job_ids(keyword, location, hours, max_pages):
    found = {}
    start = 0
    for _page in range(max_pages):
        html = _get(SEARCH_URL, {
            "keywords": keyword,
            "location": location,
            "f_TPR": f"r{int(hours) * 3600}",
            "start": start,
        })
        if not html:
            break
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select("[data-entity-urn]")
        new_on_page = 0
        for card in cards:
            m = JOB_ID_RE.search(card.get("data-entity-urn", ""))
            if not m:
                continue
            job_id = m.group(1)
            title_el = card.select_one(".base-search-card__title")
            comp_el = card.select_one(".base-search-card__subtitle")
            loc_el = card.select_one(".job-search-card__location")
            time_el = card.select_one("time[datetime]")
            if job_id not in found:
                new_on_page += 1
                found[job_id] = {
                    "title": title_el.get_text(" ", strip=True) if title_el else "",
                    "company": comp_el.get_text(" ", strip=True) if comp_el else "",
                    "location": loc_el.get_text(" ", strip=True) if loc_el else "",
                    "posted": time_el["datetime"] if time_el else "",
                }
        if new_on_page == 0 or not cards:
            break
        start += len(cards)  # the endpoint returns ~10 per call, not 25
        time.sleep(random.uniform(1.5, 3.0))
    return found


def fetch_job_detail(job_id):
    html = _get(DETAIL_URL.format(job_id=job_id))
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ", strip=True).lower()
    closed = "no longer accepting applications" in page_text or bool(soup.select_one(".closed-job"))
    desc_el = soup.select_one("div.show-more-less-html__markup")
    criteria = [e.get_text(" ", strip=True) for e in soup.select("span.description__job-criteria-text")]
    title_el = soup.select_one("h2.top-card-layout__title")
    comp_el = soup.select_one("a.topcard__org-name-link") or soup.select_one("span.topcard__flavor")
    loc_el = soup.select_one("span.topcard__flavor--bullet")
    desc = desc_el.get_text("\n", strip=True) if desc_el else ""
    if criteria:
        desc = f"Seniority level: {criteria[0]}\n{desc}"
    return {
        "title": title_el.get_text(" ", strip=True) if title_el else "",
        "company": comp_el.get_text(" ", strip=True) if comp_el else "",
        "location": loc_el.get_text(" ", strip=True) if loc_el else "",
        "description": desc,
        "closed": closed,
    }


def collect_linkedin_candidates(cfg, is_title_candidate, already_seen, backfill=False):
    """
    Returns a list of candidate dicts in the crawler's format.
    `is_title_candidate(title, url)` is the crawler's title filter.
    `already_seen(company, title, url)` returns True if this job was handled before.
    """
    li_cfg = cfg.get("linkedin") or {}
    if not li_cfg.get("enabled", False):
        return []

    keywords = li_cfg.get("keywords") or ["full stack developer"]
    location = cfg.get("location", "Israel")
    hours = li_cfg.get("posted_within_hours", 24)
    max_pages = li_cfg.get("max_pages_per_keyword", 3)
    if backfill:  # first runs: look back a month so nothing older is missed
        hours = li_cfg.get("backfill_hours", 720)
        max_pages = li_cfg.get("backfill_max_pages", 10)

    print(f"\n=== LinkedIn: searching {len(keywords)} keyword(s), last {hours}h, {location} ===")
    listing = {}
    for kw in keywords:
        ids = search_job_ids(kw, location, hours, max_pages)
        print(f"  -> '{kw}': {len(ids)} job(s)")
        listing.update(ids)
        time.sleep(random.uniform(1.5, 3.0))

    candidates = []
    for job_id, meta in listing.items():
        url = f"https://www.linkedin.com/jobs/view/{job_id}/"
        if not is_title_candidate(meta["title"], url):
            continue
        if already_seen(meta["company"], meta["title"], url):
            continue
        detail = fetch_job_detail(job_id)
        time.sleep(random.uniform(1.0, 2.0))
        if not detail or len(detail["description"]) < 150:
            continue
        if detail.get("closed"):
            print(f"  [X] CLOSED on LinkedIn: {meta['company']} - {meta['title']}")
            continue
        candidates.append({
            "company": meta["company"] or detail["company"],
            "title": meta["title"] or detail["title"],
            "url": url,
            "context_snippet": f"{meta['title']} - {meta['location']}",
            "requirements_text": detail["description"][:6000],
            "page_location": detail["location"] or meta["location"],
            "referral_contact": "LinkedIn",
            "referral_type": "LinkedIn",
            "source": "linkedin",
            "posted": meta.get("posted", ""),
        })
    print(f"  -> LinkedIn: {len(candidates)} new candidate(s) after title filter")
    return candidates
