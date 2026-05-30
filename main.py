import re
import json
import time
import random
import os
import requests
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
import warnings
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
from groq import Groq

# ─────────────────────────────────────────────
# App Setup
# ─────────────────────────────────────────────
app = FastAPI(title="Company Research API")
app.mount("/static", StaticFiles(directory="static"), name="static")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY environment variable not set")
RESULTS_FILE = "results.json"

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

RELEVANT_KEYWORDS = [
    "about", "about-us", "about_us", "company", "who-we-are", "our-story",
    "contact", "contact-us", "reach-us", "get-in-touch",
    "services", "solutions", "what-we-do", "offerings", "products",
    "team", "overview", "mission",
]

BOILERPLATE_TAGS = [
    "script", "style", "nav", "footer", "header",
    "noscript", "iframe", "svg", "form", "aside",
]

BOILERPLATE_CLASSES = [
    "cookie", "gdpr", "banner", "popup", "modal",
    "newsletter", "subscribe", "social", "share",
    "breadcrumb", "pagination", "sidebar",
]

MAX_PAGES     = 4
MAX_CHARS     = 6000
REQUEST_DELAY = 1.0

# ─────────────────────────────────────────────
# Scraping Utilities
# ─────────────────────────────────────────────
def safe_get(url, timeout=10):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        return resp if resp.status_code == 200 else None
    except Exception:
        return None

def normalize_url(base, href):
    if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    abs_url = urljoin(base, href)
    base_domain = urlparse(base).netloc
    link_domain = urlparse(abs_url).netloc
    if base_domain not in link_domain and link_domain not in base_domain:
        return None
    return abs_url.split("#")[0].rstrip("/")

def fuzzy_score(url):
    path = urlparse(url).path.lower()
    score = 0
    for kw in RELEVANT_KEYWORDS:
        if kw in path:
            score += 20
    for bad in ["blog", "news", "press", "privacy", "terms", "cookie", "career", "jobs", "login", "signup"]:
        if bad in path:
            score -= 15
    return max(score, 0)

def get_links_from_sitemap(base_url):
    links = []
    for path in ["/sitemap.xml", "/sitemap_index.xml", "/sitemap/sitemap.xml"]:
        resp = safe_get(base_url.rstrip("/") + path)
        if resp:
            soup = BeautifulSoup(resp.text, "lxml")
            for loc in soup.find_all("loc"):
                links.append(loc.get_text(strip=True))
            if links:
                break
    return links

def get_links_from_homepage(base_url, soup):
    links = []
    for tag in soup.find_all("a", href=True):
        url = normalize_url(base_url, tag["href"])
        if url:
            links.append(url)
    return list(set(links))

def get_links_from_guessing(base_url):
    return [
        base_url.rstrip("/") + "/" + kw
        for kw in ["about", "about-us", "contact", "contact-us", "services", "solutions"]
    ]

def select_best_links(all_links, base_url, max_pages=MAX_PAGES):
    seen, scored = set(), []
    for link in all_links:
        clean = link.split("?")[0].rstrip("/")
        if clean in seen:
            continue
        seen.add(clean)
        s = fuzzy_score(clean)
        if s > 0:
            scored.append((s, clean))
    scored.sort(reverse=True)
    top = [base_url]
    for _, url in scored[:max_pages - 1]:
        if url != base_url:
            top.append(url)
    return top[:max_pages]

def clean_html(html_text):
    soup = BeautifulSoup(html_text, "lxml")
    for tag in soup(BOILERPLATE_TAGS):
        tag.decompose()
    to_remove = []
    for el in soup.find_all(True):
        if el is None:
            continue
        try:
            classes  = " ".join(el.get("class") or [])
            el_id    = el.get("id") or ""
            combined = (classes + " " + el_id).lower()
            if any(kw in combined for kw in BOILERPLATE_CLASSES):
                to_remove.append(el)
        except Exception:
            continue
    for el in to_remove:
        try:
            el.decompose()
        except Exception:
            pass
    text = soup.get_text(separator=" ", strip=True)
    return re.sub(r"\s{2,}", " ", text)

def extract_contacts(text):
    emails = list(set(re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", text)))
    emails = [e for e in emails if not re.search(r"\.(png|jpg|gif|svg|css|js)$", e)]
    phones = list(set(re.findall(r"(?:\+?\d[\s\-.]?){7,15}\d", text)))
    phones = [p.strip() for p in phones if 9 <= len(re.sub(r"\D", "", p)) <= 15]
    return emails[:5], phones[:3]

def chunk_text(text, max_chars=MAX_CHARS):
    if len(text) <= max_chars:
        return text
    keep_front = int(max_chars * 0.7)
    keep_back  = max_chars - keep_front
    return text[:keep_front] + " ... " + text[-keep_back:]

# ─────────────────────────────────────────────
# Core Scraper
# ─────────────────────────────────────────────
def scrape_company(base_url):
    homepage_resp = safe_get(base_url)
    if not homepage_resp:
        alt = base_url.replace("https://", "https://www.").replace("http://", "http://www.")
        homepage_resp = safe_get(alt)
        if homepage_resp:
            base_url = alt

    if not homepage_resp:
        return {"combined_text": "", "emails": [], "phones": [], "scraped_urls": []}

    homepage_soup = BeautifulSoup(homepage_resp.text, "lxml")
    all_links = []
    all_links.extend(get_links_from_sitemap(base_url))
    all_links.extend(get_links_from_homepage(base_url, homepage_soup))
    all_links.extend(get_links_from_guessing(base_url))

    pages_to_scrape = select_best_links(all_links, base_url)

    all_text_parts, scraped_urls = [], []
    for url in pages_to_scrape:
        time.sleep(REQUEST_DELAY + random.uniform(0, 0.5))
        resp = safe_get(url)
        if not resp:
            continue
        cleaned = clean_html(resp.text)
        if len(cleaned) > 100:
            all_text_parts.append(f"[PAGE: {url}]\n{cleaned}")
            scraped_urls.append(url)

    combined_text = "\n\n".join(all_text_parts)
    emails, phones = extract_contacts(combined_text)
    return {"combined_text": combined_text, "emails": emails, "phones": phones, "scraped_urls": scraped_urls}

# ─────────────────────────────────────────────
# AI Analysis
# ─────────────────────────────────────────────
def analyze_with_ai(base_url, scraped_data):
    client     = Groq(api_key=GROQ_API_KEY)
    text_chunk = chunk_text(scraped_data["combined_text"])
    email_hint = scraped_data["emails"]
    phone_hint = scraped_data["phones"]

    if not text_chunk.strip():
        return build_empty_result(base_url)

    prompt = f"""You are a business analyst AI. Analyze the following scraped website content and extract structured data.

Company website: {base_url}

Pre-extracted contacts (from regex — use as hints, verify in text):
  Emails found: {email_hint}
  Phones found: {phone_hint}

Scraped content:
---
{text_chunk}
---

Return ONLY a valid JSON object. No explanation, no markdown, no extra text.

Rules:
- NEVER invent or fabricate contact details, addresses, or services not present in the text.
- If a field cannot be determined, return "" or [].
- For "mail", only include emails found verbatim in the text. Return as array.
- For "mobile_number", only include numbers found verbatim. Single string.
- For "outreach_opener", write a specific cold outreach sentence based on actual services.
- For "probable_pain_point", be specific based on their industry.

{{
  "website_name": "<short brand name>",
  "company_name": "<full company name>",
  "address": "<full address or ''>",
  "mobile_number": "<phone number or ''>",
  "mail": ["<email1>", "<email2>"],
  "core_service": "<main service in 1 sentence>",
  "target_customer": "<who they sell to>",
  "probable_pain_point": "<specific customer pain point>",
  "outreach_opener": "<personalized cold outreach opener>"
}}"""

    try:
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=600,
            temperature=0.2,
        )
        raw = response.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"```\s*$", "", raw, flags=re.MULTILINE)

        result = json.loads(raw)

        if isinstance(result.get("mail"), str):
            result["mail"] = [result["mail"]] if result["mail"] else []
        if not result.get("mail") and email_hint:
            result["mail"] = email_hint
        if not result.get("mobile_number") and phone_hint:
            result["mobile_number"] = phone_hint[0]

        return result

    except Exception as e:
        print(f"AI error: {e}")
        return build_empty_result(base_url)

def build_empty_result(url):
    return {
        "website_name": urlparse(url).netloc,
        "company_name": "",
        "address": "",
        "mobile_number": "",
        "mail": [],
        "core_service": "",
        "target_customer": "",
        "probable_pain_point": "",
        "outreach_opener": "",
    }

# ─────────────────────────────────────────────
# Results Storage
# ─────────────────────────────────────────────
def load_results():
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE, "r") as f:
            return json.load(f)
    return []

def save_results(results):
    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2)

# ─────────────────────────────────────────────
# API Models
# ─────────────────────────────────────────────
class EnrichRequest(BaseModel):
    url: str
    website_name: str = ""

# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse("static/index.html")

@app.post("/enrich")
async def enrich(req: EnrichRequest):
    url = req.url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        scraped = scrape_company(url)
        print(f"Scraped text length: {len(scraped['combined_text'])} chars")  # add this
        result  = analyze_with_ai(url, scraped)
        # Override website_name if user provided one
        if req.website_name.strip():
            result["website_name"] = req.website_name.strip()

        # Save to results store
        results = load_results()
        # Update if URL already exists, else append
        existing = next((i for i, r in enumerate(results)
                         if r.get("website_name") == result["website_name"]
                         or urlparse(url).netloc in str(r.get("company_name", ""))), None)
        if existing is not None:
            results[existing] = result
        else:
            results.append(result)
        save_results(results)

        return result

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/results")
async def get_results():
    return load_results()
