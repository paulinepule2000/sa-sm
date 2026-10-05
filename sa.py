import csv
import html as htmllib
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from urllib.parse import urlparse

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ── Config ───────────────────────────────────────────────────────────────────
# The Saudi LinkedIn scraper pushes this tracker CSV to the repo after every job.
DEFAULT_CSV_URL = "https://raw.githubusercontent.com/projectfetcher/salinkedin/main/processed_jobs_saudi_arabia.csv"
CSV_SOURCE    = os.environ.get("CSV_SOURCE", "").strip() or DEFAULT_CSV_URL
CSV_TOKEN     = (os.environ.get("CSV_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")).strip()
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "").strip().rstrip("/")

# What has already gone to Facebook. Committed to the repo so it survives between runs.
STATE_FILE = "fb_posted_saudi_arabia.csv"
STATE_COLS = ["Job ID", "FB Post ID", "Site Path", "Timestamp"]

FB_PAGE_ID     = os.environ.get("FB_PAGE_ID", "").strip()
FB_PAGE_TOKEN  = os.environ.get("FB_PAGE_ACCESS_TOKEN", "").strip()
FB_API_VERSION = "v21.0"
FB_POST_DELAY_S      = 45
FB_MAX_POSTS_PER_RUN = int(os.environ.get("FB_MAX_POSTS_PER_RUN", "15") or 15)
FB_MAX_AGE_DAYS      = int(os.environ.get("FB_MAX_AGE_DAYS", "7") or 7)
SNIPPET_CHARS        = 220
# 1 = only post jobs that have BOTH a short description and a location
FB_REQUIRE_DETAILS   = os.environ.get("FB_REQUIRE_DETAILS", "1").strip() != "0"
UA = {"User-Agent": "Mozilla/5.0 (compatible; MimusJobsFBPoster/1.0)"}
HASHTAGS             = "#SaudiArabia #KSA #Jobs #Hiring #وظائف"

# Commit + push the state file right after every post (GitHub Actions only),
# so a cancelled/timed-out run can never cause double-posting.
GIT_PUSH_STATE = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fb_poster")


# ── CSV loading (always from GitHub) ─────────────────────────────────────────
def _to_raw_github_url(url: str) -> str:
    """Accepts a github.com/.../blob/... link and converts it to the raw file URL."""
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/blob/(.+)", url)
    return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}" if m else url


def _read_source() -> str:
    url = _to_raw_github_url(CSV_SOURCE)
    if not url.lower().startswith("http"):
        raise ValueError(f"CSV_SOURCE must be a URL, got: {CSV_SOURCE!r}")

    attempts = []
    if CSV_TOKEN:
        attempts.append({"Authorization": f"token {CSV_TOKEN}"})
    attempts.append({})  # public repo / token rejected → try anonymously

    last_err = None
    for headers in attempts:
        try:
            r = requests.get(url, headers={**headers, "Cache-Control": "no-cache"}, timeout=30)
            r.raise_for_status()
            r.encoding = "utf-8"
            log.info(f"CSV fetched from GitHub ({len(r.text)} chars)")
            return r.text.lstrip("\ufeff")
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Could not download CSV from {url}: {last_err}")


def load_rows() -> list:
    text = _read_source()
    first_line = text.split("\n", 1)[0]
    delim = "\t" if first_line.count("\t") > first_line.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    rows = []
    for raw in reader:
        rows.append({(k or "").strip(): (v or "").strip() for k, v in raw.items()})
    return rows


# ── State (what has already gone to Facebook) ────────────────────────────────
def load_state() -> tuple:
    ids, paths = set(), set()
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                ids.add(r.get("Job ID", ""))
                paths.add(r.get("Site Path", ""))
    return ids, paths


def save_state(job_id: str, fb_id: str, site_path_: str):
    new_file = not os.path.exists(STATE_FILE)
    with open(STATE_FILE, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(STATE_COLS)
        w.writerow([job_id, fb_id, site_path_, datetime.now().isoformat()])


def _git(*args, timeout: int = 60):
    return subprocess.run(["git", *args], capture_output=True, text=True, timeout=timeout)


def push_state_now(message: str):
    """Commit and push the state file immediately. Never raises."""
    if not GIT_PUSH_STATE:
        return
    try:
        _git("config", "user.name",  "github-actions[bot]")
        _git("config", "user.email", "github-actions[bot]@users.noreply.github.com")
        _git("add", STATE_FILE)
        if _git("diff", "--cached", "--quiet").returncode == 0:
            return  # nothing changed
        _git("commit", "-m", f"{message} [skip ci]")
        for attempt in range(4):
            # Rebase first so pushes from the scraper workflow don't reject ours
            _git("pull", "--rebase", "--autostash")
            push = _git("push")
            if push.returncode == 0:
                log.info(f"📌 State pushed: {message}")
                return
            log.warning(f"State push attempt {attempt+1} failed: {push.stderr.strip()[:200]}")
            time.sleep(2 + attempt * 2)
    except Exception as e:
        log.warning(f"push_state_now error: {e}")


# ── Row filtering / text building ────────────────────────────────────────────
def job_link(row: dict) -> str:
    """Job Site URL from the CSV, or a ?p=<WP ID> fallback built from SITE_BASE_URL."""
    url = row.get("Job Site URL", "")
    if url.startswith("http"):
        return url
    wp_id = row.get("WP ID", "")
    if SITE_BASE_URL and wp_id:
        try:
            return f"{SITE_BASE_URL}/?p={int(float(wp_id))}"
        except ValueError:
            pass
    return ""


def site_path(url: str) -> str:
    """Path (+query) only — keeps the domain out of logs and the state file."""
    p = urlparse(url)
    path = p.path or url
    if p.query:
        path += "?" + p.query
    return path


def parse_ts(value: str):
    """Naive datetime, or None. Timezone info is stripped so comparisons never crash."""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def skip_reason(row: dict, cutoff: datetime):
    """Returns None if the row is eligible, otherwise a short reason string."""
    if row.get("Status", "").lower() != "posted":
        return "status_not_posted"
    if not row.get("Job Title"):
        return "no_title"
    if not job_link(row):
        return "no_site_url"
    ts = parse_ts(row.get("Timestamp", ""))
    if ts and ts < cutoff:
        return "too_old"
    return None


def clean_location(text: str) -> str:
    """The site stores locations like 'الرياض الرياض السعودية' — drop consecutive duplicate words."""
    out = []
    for tok in (text or "").split():
        if not out or out[-1] != tok:
            out.append(tok)
    return " ".join(out)


def short_description(text: str, limit: int = SNIPPET_CHARS) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(".,;:!?…")
    return cut + "…"


def build_message(row: dict, link: str) -> str:
    lines = [f"📢 {row['Job Title']}"]
    if row.get("Company Name"):
        lines.append(f"🏢 {row['Company Name']}")
    loc = clean_location(row.get("Location", ""))
    if loc:
        lines.append(f"📍 {loc}")
    snippet = short_description(row.get("Short Description", ""))
    if snippet:
        lines += ["", snippet]
    lines += ["", f"👉 Full details & how to apply: {link}", "", HASHTAGS]
    return "\n".join(lines)


# ── Fill missing description / location from the job page ────────────────────
def _strip_html(text: str) -> str:
    text = htmllib.unescape(text or "")
    text = re.sub(r"<(br|/p|/li|/div)[^>]*>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", htmllib.unescape(text)).strip()


def _find_jobposting(node):
    if isinstance(node, list):
        for n in node:
            found = _find_jobposting(n)
            if found:
                return found
    elif isinstance(node, dict):
        t = node.get("@type")
        if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
            return node
        for v in node.values():
            if isinstance(v, (list, dict)):
                found = _find_jobposting(v)
                if found:
                    return found
    return None


def _location_from_jobposting(jp: dict) -> str:
    parts = []
    locs = jp.get("jobLocation") or []
    for loc in (locs if isinstance(locs, list) else [locs]):
        addr = loc.get("address", {}) if isinstance(loc, dict) else {}
        if isinstance(addr, str):
            parts.append(addr)
            continue
        for key in ("addressLocality", "addressRegion", "addressCountry"):
            v = addr.get(key)
            if isinstance(v, dict):
                v = v.get("name")
            if v and v not in parts:
                parts.append(str(v))
    return ", ".join(parts)


def fetch_page_details(url: str) -> tuple:
    """Returns (description, location, final_url) from the job page; empty strings on failure."""
    try:
        r = requests.get(url, headers=UA, timeout=25, allow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        log.warning(f"Could not open job page for details: {e}")
        return "", "", url
    page, desc, loc = r.text, "", ""
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                         page, flags=re.S | re.I):
        try:
            jp = _find_jobposting(json.loads(m.group(1).strip()))
        except Exception:
            continue
        if jp:
            desc = _strip_html(jp.get("description", ""))
            loc = _location_from_jobposting(jp)
            break
    if not desc:
        for pat in (r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
                    r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']'):
            m = re.search(pat, page, flags=re.S | re.I)
            if m:
                desc = _strip_html(m.group(1))
                break
    return desc, loc, r.url


def enrich_row(row: dict, link: str) -> tuple:
    """Fills Short Description / Location from the page when the CSV lacks them. Returns (row, link)."""
    if row.get("Short Description") and row.get("Location"):
        return row, link
    desc, loc, final_url = fetch_page_details(link)
    row = dict(row)
    if not row.get("Short Description") and desc:
        row["Short Description"] = desc
    if not row.get("Location") and loc:
        row["Location"] = loc
    return row, final_url


# ── Facebook ─────────────────────────────────────────────────────────────────
def post_to_facebook(message: str, link: str) -> tuple:
    """Returns (fb_post_id | None, status) where status is 'ok' | 'retry' | 'rate_limit' | 'bad_token'."""
    endpoint = f"https://graph.facebook.com/{FB_API_VERSION}/{FB_PAGE_ID}/feed"
    payload = {"message": message, "link": link, "access_token": FB_PAGE_TOKEN}
    for attempt in range(3):
        try:
            r = requests.post(endpoint, data=payload, timeout=30)
            data = r.json() if r.content else {}
            if r.status_code == 200 and data.get("id"):
                return data["id"], "ok"
            err = data.get("error", {})
            code = err.get("code")
            log.error(f"Facebook error (attempt {attempt+1}) code={code} "
                      f"subcode={err.get('error_subcode')}: {err.get('message', r.text[:200])}")
            if code == 190:
                return None, "bad_token"
            if code in (4, 17, 32, 613):
                return None, "rate_limit"
        except Exception as e:
            log.error(f"Facebook request failed (attempt {attempt+1}): {e}")
        time.sleep(3 * 2 ** attempt)
    return None, "retry"


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    if not (FB_PAGE_ID and FB_PAGE_TOKEN):
        log.error("FB_PAGE_ID / FB_PAGE_ACCESS_TOKEN not set — nothing posted.")
        return 1

    try:
        rows = load_rows()
    except Exception as e:
        log.error(f"Could not read CSV from GitHub: {e}")
        return 1

    cutoff = datetime.now() - timedelta(days=FB_MAX_AGE_DAYS)
    done_ids, done_paths = load_state()
    log.info(f"State loaded: {len(done_ids)} job(s) already posted to Facebook ({STATE_FILE})")

    todo, seen_paths = [], set()
    skipped = Counter()
    for r in sorted(rows, key=lambda r: r.get("Timestamp", "")):
        reason = skip_reason(r, cutoff)
        if reason:
            skipped[reason] += 1
            continue
        link = job_link(r)
        p = site_path(link)
        if r.get("Job ID") in done_ids or p in done_paths:
            skipped["already_posted"] += 1
            continue
        if p in seen_paths:
            skipped["duplicate_in_csv"] += 1
            continue
        seen_paths.add(p)
        todo.append((r, link, p))

    log.info(f"CSV rows: {len(rows)} | new jobs to post: {len(todo)} | cap this run: {FB_MAX_POSTS_PER_RUN}")
    if skipped:
        log.info("Skipped rows by reason: " + ", ".join(f"{k}={v}" for k, v in skipped.most_common()))

    posted = 0
    for r, link, path in todo:
        if posted >= FB_MAX_POSTS_PER_RUN:
            log.info("Per-run cap reached — the rest will go out next run.")
            break
        r, link = enrich_row(r, link)
        path = site_path(link)
        if path in done_paths:
            log.info(f"Already posted (same page): {path}")
            continue
        if FB_REQUIRE_DETAILS and not (r.get("Short Description") and r.get("Location")):
            log.warning(f"Skipped '{r['Job Title']}': no description/location available ({path})")
            continue
        msg = build_message(r, link)

        fb_id, status = post_to_facebook(msg, link)
        if status == "ok":
            save_state(r.get("Job ID", ""), fb_id, path)
            done_paths.add(path)
            posted += 1
            log.info(f"✅ posted '{r['Job Title']}' → {fb_id}  ({path})")
            push_state_now(f"FB posted {r.get('Job ID', '')}")
            time.sleep(FB_POST_DELAY_S)
        elif status == "bad_token":
            log.error("Facebook token invalid/expired — generate a new Page access token.")
            return 1
        elif status == "rate_limit":
            log.warning("Facebook rate limit hit — stopping; will resume next run.")
            break
        else:
            log.warning(f"Skipped '{r['Job Title']}' this run (will retry next run).")

    log.info(f"Done. Posted {posted} job(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
