import time
import os
import random
import pandas as pd
from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException, TimeoutException, StaleElementReferenceException
from selenium import webdriver
from bs4 import BeautifulSoup
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import datetime
import json
from selenium.webdriver.chrome.options import Options
from rapidfuzz import process, fuzz
from difflib import get_close_matches
from google.cloud import bigquery
from google.oauth2 import service_account
from zoneinfo import ZoneInfo

# ─────────────────────────────────────────────
# SELENIUM OPTIONS
# ─────────────────────────────────────────────
options = Options()
options.add_argument("--headless=new")
options.add_argument("--disable-gpu")
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
options.add_argument("--disable-blink-features=AutomationControlled")
options.add_argument("--remote-debugging-port=9222")
options.add_argument("--window-size=1920,1080")

# ─────────────────────────────────────────────
# PHASE 1 — COLLECT ALL JOB URLS
# ─────────────────────────────────────────────
driver = webdriver.Chrome(options=options)

base_url = "https://jobs.ubs.com/TGnewUI/Search/home/HomeWithPreLoad?partnerid=25008&siteid=5049&PageType=searchResults&SearchType=linkquery&LinkID=15241#keyWordSearch=&locationSearch=&domaine%20d'activit%C3%A9=Audit_or_Compliance_or_Conseil%20client%C3%A8le%20%2F%20Relationship%20management_or_Finance_or_Financial%20Advisors%20(US)_or_Fund%20services_or_Gestion%20et%20d%C3%A9veloppement%20produits_or_Gestion%20portefeuille%20et%20fonds_or_Investment%20Banking_or_Quantitative%20analysis_or_Risk_or_Sales_or_Sales%20Trading_or_Trading"
driver.get(base_url)
time.sleep(random.uniform(4, 7))

job_urls = []
seen = set()
MAX_CLICKS = 1  # 3 clicks = 150 additional cards

for click_count in range(MAX_CLICKS):
    # Scroll to bottom to make "Suivant" button visible
    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(random.uniform(2, 3))

    job_cards = driver.find_elements(By.CSS_SELECTOR, "a[href*='/TGnewUI/Search/home/HomeWithPreLoad?partnerid=25008&siteid=5049&PageType=jobdetails']")

    # Fallback
    if not job_cards:
        job_cards = driver.find_elements(By.CSS_SELECTOR, "a.jobProperty.jobtitle")

    for card in job_cards:
        href = card.get_attribute("href")
        if href and href not in seen:
            seen.add(href)
            job_urls.append(href)

    try:
        suivant_btn = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable((By.ID, "showMoreJobs"))
        )
        driver.execute_script("arguments[0].scrollIntoView(true);", suivant_btn)
        time.sleep(0.5)
        driver.execute_script("arguments[0].click();", suivant_btn)
        print(f"✅ Clicked 'Suivant' ({click_count + 1}/{MAX_CLICKS})")
        time.sleep(random.uniform(3, 5))
    except TimeoutException:
        print("ℹ️ No more 'Suivant' button — all jobs loaded.")
        break

# Scroll to top and collect all job links from the fully loaded page
driver.execute_script("window.scrollTo(0, 0);")
time.sleep(2)


driver.quit()
job_urls = set(job_urls)
print(f"Collected {len(job_urls)} job URLs")

#------------------------CHECK DUPLICATES URL DANS BIGQUERY--------------------------------------------------

# Load JSON from GitHub secret
key_json = json.loads(os.environ["BIGQUERY"])

# Create credentials from dict
credentials = service_account.Credentials.from_service_account_info(key_json)

# Initialize BigQuery client
client = bigquery.Client(
    credentials=credentials,
    project=key_json["project_id"]
)


# Query existing URLs from your BigQuery table
query = """
    SELECT url
    FROM `databasealfred.alfredFinance.ubs`
    WHERE url IS NOT NULL
"""
query_job = client.query(query)

# Convert results to a set for fast lookup
existing_urls = {row.url for row in query_job}

print(f"Loaded {len(existing_urls)} URLs from BigQuery")

# Filter job_urls
job_urls = [url for url in job_urls if url not in existing_urls]

print(f"✅ Remaining job URLs to scrape: {len(job_urls)}")


#------------------------ FIN CHECK DUPLICATES URL DANS BIGQUERY--------------------------------------------------


# ─────────────────────────────────────────────
# PHASE 2 — SCRAPE EACH JOB PAGE
# ─────────────────────────────────────────────
options2 = Options()
options2.add_argument("--headless=new")
options2.add_argument("--disable-gpu")
options2.add_argument("--no-sandbox")
options2.add_argument("--disable-dev-shm-usage")
options2.add_argument("--window-size=1920,1080")

driver = webdriver.Chrome(options=options2)

job_data = []

for job_url in job_urls:
    try:
        driver.get(job_url)
        time.sleep(random.uniform(3, 6))

        # ── Title ──────────────────────────────────────────────────────────
        try:
            title = WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "h1.answer.jobtitleInJobDetails"))
            ).text.strip()
        except TimeoutException:
            title = ""

        # ── Location ───────────────────────────────────────────────────────
        location = ""
        for city_label in ["City", "Ville", "Stadt", "ciudad", "cidade"]:
            try:
                location = WebDriverWait(driver, 5).until(
                    EC.presence_of_element_located((By.XPATH, f"//p[contains(@class,'question') and contains(text(),'{city_label}')]/following-sibling::p[contains(@class,'answer')]"))
                ).text.strip()
                if location:
                    break
            except TimeoutException:
                continue


        # ── Division / Business Area ───────────────────────────────────────
        division = ""
        try:
            elements = WebDriverWait(driver, 10).until(
                lambda d: d.find_elements(By.XPATH, "//p[contains(@class,'position3InJobDetails') and contains(@class,'answer')]")
            )
            division = elements[2].text.strip() if len(elements) > 2 else ""
        except TimeoutException:
            division = ""

        # ── Experience Level ───────────────────────────────────────────────
        experienceLevel = ""

        # ── Description ────────────────────────────────────────────────────
        description = ""
        try:
            sections = driver.find_elements(By.CSS_SELECTOR, "p.answer.section2LeftfieldsInJobDetails.jobDetailTextArea")
            labels = driver.find_elements(By.CSS_SELECTOR, "p.question.section2LeftfieldsInJobDetails.jobDetailTextArea")
    
            lines = []
            for label, section in zip(labels, sections):
                label_text = label.text.strip()
                # Get innerHTML to preserve <br> as line breaks
                html = section.get_attribute("innerHTML")
                soup = BeautifulSoup(html, "html.parser")
                # Replace <br> with newlines
                for br in soup.find_all("br"):
                    br.replace_with("\n")
                content = soup.get_text().strip()
                if label_text:
                    lines.append(f"{label_text}\n{content}")
                else:
                    lines.append(content)
    
            description = "\n\n".join(lines)
        except NoSuchElementException:
            description = ""

        # ── Timestamps ─────────────────────────────────────────────────────
        
        paris_now = datetime.datetime.now(ZoneInfo("Europe/Paris"))
        scrappedDateTime    = paris_now.isoformat()
        scrappedDate        = paris_now.strftime("%Y-%m-%d")
        scrappedHour        = paris_now.strftime("%H")
        scrappedMinutes     = paris_now.strftime("%M")

        # ── Activity ───────────────────────────────────────
        activity = ""
        try:
            elements = WebDriverWait(driver, 10).until(
                lambda d: d.find_elements(By.XPATH, "//p[contains(@class,'position3InJobDetails') and contains(@class,'answer')]")
            )
            activity = elements[1].text.strip() if len(elements) > 2 else ""
        except TimeoutException:
            activity = ""
        


        print(f"  → {title} | {location} | {division}")

        job_data.append({
            "title":                title,
            "location":             location,
            "scrappedDateTime":     scrappedDateTime,
            "description":          description,
            "division":             division,
            "experienceLevel":      experienceLevel,
            "url":                  job_url,
            "source":               "UBS Careers",
            "scrappedDate":         scrappedDate,
            "scrappedHour":         scrappedHour,
            "scrappedMinutes":      scrappedMinutes,
            "scrappedDateTimeText": scrappedDateTime,
            "activity" : activity
        })

    except Exception as e:
        print(f"⚠️ Error scraping {job_url}: {e}")
        continue

driver.quit()

df_jobs = pd.DataFrame(job_data)
new_data = df_jobs
print(f"\n📦 Scraped {len(new_data)} jobs")

import re
import numpy as np

def extract_experience_level(title):
    if pd.isna(title):
        return ""
    
    title = title.lower()

    patterns = [
        (r'\bsummer\s+analyst\b|\bsummer\s+analyste\b', "Summer Analyst"),
        (r'\bsummer\s+associate\b|\bsummer\s+associé\b', "Summer Associate"),
        (r'\bvice\s+president\b|\bsvp\b|\bvp\b|\bprincipal\b', "Vice President"),
        (r'\bassistant\s+vice\s+president\b|\bsavp\b|\bavp\b', "Assistant Vice President"),
        (r'\bsenior\s+manager\b', "Senior Manager"),
        (r'\bproduct\s+manager\b|\bpm\b|\bmanager\b', "Manager"),
        (r'\bmanager\b', "Manager"),
        (r'\bengineer\b|\bengineering\b', "Engineer"),
        (r'\badministrative\s+assistant\b|\bexecutive\s+assistant\b|\badmin\b', "Assistant"),
        (r'\bassociate\b|\bassocié\b', "Associate"),
        (r'\banalyst\b|\banalyste\b|\banalist\b', "Analyst"),
        (r'\bchief\b|\bhead\b', "C-Level"),
        (r'\bV.I.E\b|\bVIE\b|\bvolontariat international\b|\bV I E\b|', "VIE"),
        (r'\btrader\b', "Trader"),
        (r'\bexpert\b', "Expert"),
        (r'\blead\b', "Lead")
    ]

    for pattern, label in patterns:
        if re.search(pattern, title):
            return label

    return "Associate or not specified" 

# Apply to dataframe
new_data["experienceLevel"] = new_data["title"].apply(extract_experience_level)

allowed_divisions_raw = [
    "Audit",
    "Compliance",
    "Conseil clientèle / Relationship management",
    "Finance",
    "Financial Advisors (US)",
    "Fund services",
    "Gestion et développement produits",
    "Gestion portefeuille et fonds",
    "Investment Banking",
    "Quantitative analysis",
    "Risk",
    "Sales",
    "Sales Trading",
    "Trading",
]

new_data = new_data[new_data["activity"].isin(allowed_divisions_raw)].reset_index(drop=True)
new_data = new_data.drop(columns=['activity'])

from rapidfuzz import process, fuzz
from difflib import get_close_matches

# -------------------------------
# 1. Base mapping (same as before)
# -------------------------------

BASE_MAPPING = {

    # ================= INVESTMENT BANKING =================
    "investment banking": "Investment Banking (M&A / Advisory)",
    "m&a": "Investment Banking (M&A / Advisory)",
    "mergers and acquisitions": "Investment Banking (M&A / Advisory)",
    "corporate finance": "Investment Banking (M&A / Advisory)",
    "ecm": "Investment Banking (M&A / Advisory)",
    "dcm": "Investment Banking (M&A / Advisory)",
    "capital markets origination": "Investment Banking (M&A / Advisory)",
    "corporate & investment banking": "Investment Banking (M&A / Advisory)",
    "banque de financement et d'investissement": "Investment Banking (M&A / Advisory)",
    "investment banking (m&a / advisory)": "Investment Banking (M&A / Advisory)",

    # ================= MARKETS =================
    "markets": "Markets (Sales & Trading)",
    "sales and trading": "Markets (Sales & Trading)",
    "trading": "Markets (Sales & Trading)",
    "sales": "Markets (Sales & Trading)",
    "structuring": "Markets (Sales & Trading)",
    "derivatives": "Markets (Sales & Trading)",
    "fixed income": "Markets (Sales & Trading)",
    "equities": "Markets (Sales & Trading)",
    "fx": "Markets (Sales & Trading)",
    "global markets": "Markets (Sales & Trading)",
    "global banking & markets": "Markets (Sales & Trading)",
    "sales development": "Markets (Sales & Trading)",
    "developpement commercial": "Markets (Sales & Trading)",
    "desarrollo comercial": "Markets (Sales & Trading)",
    "customer relationship management": "Markets (Sales & Trading)",
    "markets (sales & trading)": "Markets (Sales & Trading)",

    # ================= ASSET & WEALTH =================
    "asset & wealth management": "Asset & Wealth Management",
    "asset management": "Asset & Wealth Management",
    "wealth management": "Asset & Wealth Management",
    "gestion d'actifs": "Asset & Wealth Management",
    "gestion de patrimoine": "Asset & Wealth Management",
    "portfolio management": "Asset & Wealth Management",
    "investment management": "Asset & Wealth Management",
    "private wealth": "Asset & Wealth Management",
    "private banking": "Asset & Wealth Management",
    "banque privée": "Asset & Wealth Management",
    "retail banking": "Asset & Wealth Management",
    "retail & online banking": "Asset & Wealth Management",
    "réseau d'agences et banque en ligne": "Asset & Wealth Management",
    "asset & wealth management": "Asset & Wealth Management",

    # ================= PRIVATE EQUITY =================
    "private equity": "Private Equity & Alternatives",
    "alternatives": "Private Equity & Alternatives",
    "growth equity": "Private Equity & Alternatives",
    "venture capital": "Private Equity & Alternatives",
    "buyout": "Private Equity & Alternatives",
    "lbo": "Private Equity & Alternatives",
    "multi-asset investing (bxma)": "Private Equity & Alternatives",
    "private equity & alternatives": "Private Equity & Alternatives",

    # ================= CREDIT =================
    "credit": "Credit & Lending",
    "lending": "Credit & Lending",
    "leveraged finance": "Credit & Lending",
    "structured finance": "Credit & Lending",
    "corporate treasury": "Credit & Lending",
    "blackstone credit & insurance": "Credit & Lending",
    "credit & lending": "Credit & Lending",

    # ================= RESEARCH =================
    "research": "Research & Strategy",
    "equity research": "Research & Strategy",
    "credit research": "Research & Strategy",
    "macro research": "Research & Strategy",
    "global investment research division": "Research & Strategy",
    "research & strategy": "Research & Strategy",

    # ================= RISK =================
    "risk": "Risk Management",
    "risk management": "Risk Management",
    "market risk": "Risk Management",
    "credit risk": "Risk Management",
    "operational risk": "Risk Management",
    "enterprise risk": "Risk Management",
    "risque": "Risk Management",
    "riesgos": "Risk Management",
    "risks": "Risk Management",
    "risques": "Risk Management",
    "risk division": "Risk Management",
    "risk management": "Risk Management",

    # ================= COMPLIANCE =================
    "compliance": "Compliance & Financial Crime",
    "financial crime": "Compliance & Financial Crime",
    "aml": "Compliance & Financial Crime",
    "kyc": "Compliance & Financial Crime",
    "regulatory compliance": "Compliance & Financial Crime",
    "compliance division": "Compliance & Financial Crime",
    "conformite": "Compliance & Financial Crime",
    "legal and compliance": "Compliance & Financial Crime",
    "conflicts resolution group": "Compliance & Financial Crime",
    "compliance & financial crime": "Compliance & Financial Crime",

    # ================= FINANCE =================
    "finance": "Finance (Accounting / Controlling / Tax)",
    "accounting": "Finance (Accounting / Controlling / Tax)",
    "controlling": "Finance (Accounting / Controlling / Tax)",
    "fp&a": "Finance (Accounting / Controlling / Tax)",
    "financial planning": "Finance (Accounting / Controlling / Tax)",
    "management control": "Finance (Accounting / Controlling / Tax)",
    "finance accounts and management control": "Finance (Accounting / Controlling / Tax)",
    "finance comptabilite et controle de gestion": "Finance (Accounting / Controlling / Tax)",
    "contabilidad financiera y control de gestión": "Finance (Accounting / Controlling / Tax)",
    "controllers": "Finance (Accounting / Controlling / Tax)",
    "tax": "Finance (Accounting / Controlling / Tax)",
    "financial and technical expertise": "Finance (Accounting / Controlling / Tax)",
    "expertise financiere et technique": "Finance (Accounting / Controlling / Tax)",
    "expertise financiero y técnico": "Finance (Accounting / Controlling / Tax)",
    "financieel en technisch experts": "Finance (Accounting / Controlling / Tax)",
    "finance (accounting / controlling / tax)": "Finance (Accounting / Controlling / Tax)",

    # ================= OPERATIONS =================
    "operations": "Operations (Back/Middle Office)",
    "middle office": "Operations (Back/Middle Office)",
    "back office": "Operations (Back/Middle Office)",
    "operation processing": "Operations (Back/Middle Office)",
    "trade support": "Operations (Back/Middle Office)",
    "settlement": "Operations (Back/Middle Office)",
    "clearing": "Operations (Back/Middle Office)",
    "reconciliation": "Operations (Back/Middle Office)",
    "traitement des operations": "Operations (Back/Middle Office)",
    "gestión de operaciones": "Operations (Back/Middle Office)",
    "gestion des opérations bancaires": "Operations (Back/Middle Office)",
    "operations division": "Operations (Back/Middle Office)",
    "banking operations processing": "Operations (Back/Middle Office)",
    "portfolio operations": "Operations (Back/Middle Office)",
    "platform solutions": "Operations (Back/Middle Office)",
    "operations (back/middle office)": "Operations (Back/Middle Office)",

    # ================= AUDIT =================
    "audit": "Audit & Internal Control",
    "internal control": "Audit & Internal Control",
    "audit / control / quality": "Audit & Internal Control",
    "audit / contrôle / qualité": "Audit & Internal Control",
    "internal audit": "Audit & Internal Control",
    "permanent control": "Audit & Internal Control",
    "controle permanent": "Audit & Internal Control",
    "ics": "Audit & Internal Control",
    "audit & internal control": "Audit & Internal Control",

    # ================= TECHNOLOGY =================
    "technology": "Technology (IT / Engineering)",
    "it": "Technology (IT / Engineering)",
    "data": "Technology (IT / Engineering)",
    "data science": "Technology (IT / Engineering)",
    "engineering": "Technology (IT / Engineering)",
    "software": "Technology (IT / Engineering)",
    "information technology": "Technology (IT / Engineering)",
    "engineering division": "Technology (IT / Engineering)",
    "infrastructure": "Technology (IT / Engineering)",
    "technology (it / engineering)": "Technology (IT / Engineering)",

    # ================= CORPORATE =================
    "corporate functions": "Corporate Functions",
    "corporate affairs": "Corporate Functions",
    "communications": "Corporate Functions",
    "marketing": "Corporate Functions",
    "global corporate services": "Corporate Functions",
    "security or facilities management": "Corporate Functions",
    "procurement": "Corporate Functions",
    "purchasing / procurement": "Corporate Functions",
    "human resources": "Corporate Functions",
    "human capital management": "Corporate Functions",
    "human capital management division": "Corporate Functions",
    "strategic partners": "Corporate Functions",
    "corporate functions": "Corporate Functions",

    # ================= STRATEGY =================
    "executive": "Executive / Strategy / Management",
    "management": "Executive / Strategy / Management",
    "strategy": "Executive / Strategy / Management",
    "consulting": "Executive / Strategy / Management",
    "executive office division": "Executive / Strategy / Management",
    "pilotage": "Executive / Strategy / Management",
    "steering": "Executive / Strategy / Management",
    "executive / strategy / management": "Executive / Strategy / Management",

    # ================= REAL ESTATE =================
    "real estate": "Real Estate",
    "real assets": "Real Estate",

    # ================= OTHER =================
    "temporary": "Other / Temporary",
    "temporary status": "Other / Temporary",
    "statuts temporaires": "Other / Temporary",
    "miscellaneous": "Other / Temporary",
    "other": "Other / Temporary",
    "division": "Other / Temporary",
    "other / temporary": "Other / Temporary",

    # ================= ASSET & WEALTH =================
    "global wealth management": "Asset & Wealth Management",        # UBS
    "personal & corporate banking": "Asset & Wealth Management",   # UBS

    # ================= INVESTMENT BANKING =================
    "investment bank": "Investment Banking (M&A / Advisory)",      # UBS

    # ================= CORPORATE =================
    "group functions": "Corporate Functions",                       # UBS

    # ================= ASSET & WEALTH =================
    "asset management": "Asset & Wealth Management",               # already exists, UBS confirms
}

# -------------------------------
# 2. Precompute keys
# -------------------------------
KNOWN_DIVISIONS = list(BASE_MAPPING.keys())

# -------------------------------
# 3. Fuzzy-enhanced mapper
# -------------------------------
def map_division_fuzzy(value: str, threshold: int = 85) -> str:
    if not value:
        return "Other / Temporary"

    v = str(value).strip().lower()

    # 1. Exact match
    if v in BASE_MAPPING:
        return BASE_MAPPING[v]

    # 2. Fuzzy match
    match, score, _ = process.extractOne(
        v,
        KNOWN_DIVISIONS,
        scorer=fuzz.token_sort_ratio
    )

    if score >= threshold:
        return BASE_MAPPING[match]

    # 3. Fallback
    return "Other / Temporary"


# -------------------------------
# 4. Apply to DataFrame
# -------------------------------
new_data["division"] = new_data["division"].apply(map_division_fuzzy)

# -------------------------------
# 1. BASE CITY MAPPING
# -------------------------------
BASE_CITY_MAPPING = {
    # USA
    "albany": "Albany",
    "atlanta": "Atlanta",
    "boston": "Boston",
    "chicago": "Chicago",
    "dallas": "Dallas",
    "detroit": "Detroit",
    "houston": "Houston",
    "los angeles": "Los Angeles",
    "minneapolis": "Minneapolis",
    "new york": "New York",
    "new york city": "New York",
    "new york & jersey city": "New York",
    "new york, new jersey": "New York",
    "new york, chicago": "New York",
    "new york 601 lex": "New York",
    "morristown": "Morristown",
    "philadelphia": "Philadelphia",
    "pittsburgh": "Pittsburgh",
    "norwalk": "Norwalk",
    "west palm beach": "West Palm Beach",
    "richardson": "Richardson",
    "richardson, dallas": "Richardson",

    # Canada
    "calgary": "Calgary",
    "montreal": "Montreal",
    "montéal": "Montreal",
    "monteal": "Montreal",
    "chesterbrook": "Chesterbrook",
    "toronto": "Toronto",

    # Europe
    "amsterdam": "Amsterdam",
    "berlin": "Berlin",
    "frankfurt": "Frankfurt",
    "frankfurt am main": "Frankfurt",
    "frankfurt omniturm": "Frankfurt",
    "birmingham": "Birmingham",
    "brussels": "Brussels",
    "bruxelles": "Brussels",
    "budapest": "Budapest",
    "geneva": "Geneva",
    "genève": "Geneva",
    "genève (lancy)": "Geneva",
    "glasgow": "Glasgow",
    "hannover": "Hanover",
    "lisbon": "Lisbon",
    "lisboa": "Lisbon",
    "lisboa or porto": "Lisbon",
    "lisboa/porto": "Lisbon",
    "porto": "Porto",
    "porto / lisbon": "Porto",
    "porto or lisbon": "Porto",
    "madrid": "Madrid",
    "milano": "Milan",
    "milan": "Milan",
    "munich": "Munich",
    "krakow": "Krakow",
    "kraków": "Krakow",
    "warsaw": "Warsaw",
    "warszawa": "Warsaw",
    "rome": "Rome",
    "roma": "Rome",
    "stockholm": "Stockholm",
    "zurich": "Zurich",
    "zürich": "Zurich",
    "pantin": "Pantin",
    "nanterre": "Nanterre",
    "la defense": "Paris",
    "montreuil (idf)": "Paris",
    "ile de france": "Paris",
    "paris": "Paris",

    # Middle East
    "doha": "Doha",
    "dubai": "Dubai",
    "riyadh": "Riyadh",
    "tel aviv": "Tel Aviv",

    # Asia
    "auckland": "Auckland",
    "bangalore": "Bangalore",
    "bengaluru": "Bangalore",
    "bengalutru": "Bangalore",
    "beijing": "Beijing",
    "chennai": "Chennai",
    "ho chi minh": "Ho Chi Minh City",
    "hong kong": "Hong Kong",
    "hyderabad": "Hyderabad",
    "mumbai": "Mumbai",
    "mumbai/ bangalore": "Mumbai",
    "minato-ku": "Tokyo",
    "seoul": "Seoul",
    "shanghai": "Shanghai",
    "singapore": "Singapore",
    "singapour": "Singapore",
    "taipei": "Taipei",
    "tokyo": "Tokyo",
    "xinyi district": "Taipei",

    # Latin America
    "bogota": "Bogota",
    "buenos aires": "Buenos Aires",
    "ciudad de mexico": "Mexico City",
    "metro manila": "Metro Manila",
    "sao paulo": "Sao Paulo",
    "são paulo": "Sao Paulo",
    "region metropolitana de santiago": "Santiago",

    # Oceania
    "sydney": "Sydney",
    "auckland": "Auckland",

    # Other / Remote
    "remote - deutschlandweit": "Remote",
    "ireland": "Dublin",
    "dublin": "Dublin",
    "galway": "Dublin",
    "dublin or galway": "Dublin",
    "galway/dublin": "Dublin",
    "jersey city": "Jersey City",
    "berkeley square house london": "London",
}

# -------------------------------
# 2. SELF-MAPPING
# -------------------------------
CITY_CATEGORIES = set(BASE_CITY_MAPPING.values())
BASE_CITY_MAPPING.update({city.lower(): city for city in CITY_CATEGORIES})

# -------------------------------
# 3. FUZZY MATCHING
# -------------------------------
KNOWN_LOCATIONS = list(BASE_CITY_MAPPING.keys())

def map_location(value: str, cutoff: float = 0.8) -> str:
    if not value:
        return "Other / Unknown"

    v = str(value).strip().lower()

    # Exact match
    if v in BASE_CITY_MAPPING:
        return BASE_CITY_MAPPING[v]

    # Fuzzy match
    matches = get_close_matches(v, KNOWN_LOCATIONS, n=1, cutoff=cutoff)
    if matches:
        return BASE_CITY_MAPPING[matches[0]]

    return value

# -------------------------------
# 4. APPLY TO DATAFRAME
# -------------------------------
new_data["location"] = new_data["location"].apply(map_location)

#---------UPLOAD TO BIGQUERY-------------------------------------------------------------------------------------------------------------

# Load JSON from GitHub secret
key_json = json.loads(os.environ["BIGQUERY"])

# Create credentials from dict
credentials = service_account.Credentials.from_service_account_info(key_json)

# Initialize BigQuery client
client = bigquery.Client(
    credentials=credentials,
    project=key_json["project_id"]
)


table_id = "databasealfred.alfredFinance.ubs"

# CONFIG WITHOUT PYARROW
job_config = bigquery.LoadJobConfig(
    write_disposition="WRITE_APPEND",
    source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
)

# Convert DataFrame → list of dict rows (JSON compatible)
rows = new_data.to_dict(orient="records")

# Upload
job = client.load_table_from_json(
    rows,
    table_id,
    job_config=job_config
)

job.result()

