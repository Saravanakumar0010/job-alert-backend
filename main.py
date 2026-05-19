"""
LinkedIn + Naukri Job Alert App — FastAPI Backend
Fixed: Naukri scraper updated, WhatsApp notifications improved
"""

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from datetime import datetime
import requests
import hashlib
import time
import random
import re
import os
from bs4 import BeautifulSoup
from twilio.rest import Client

app = FastAPI(title="Job Alert API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# ── Database ───────────────────────────────────────────────────────────────────

def get_conn():
    if DATABASE_URL:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        return conn, True
    else:
        import sqlite3
        conn = sqlite3.connect("jobapp.db")
        return conn, False


def init_db():
    conn, is_pg = get_conn()
    c = conn.cursor()
    if is_pg:
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            name TEXT, email TEXT UNIQUE, password TEXT, created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            designation TEXT DEFAULT 'Python Developer',
            location TEXT DEFAULT 'India',
            salary_min INTEGER DEFAULT 0,
            salary_max INTEGER DEFAULT 50,
            experience_min INTEGER DEFAULT 0,
            experience_max INTEGER DEFAULT 5,
            notify_time TEXT DEFAULT '00:00',
            whatsapp_number TEXT DEFAULT '',
            twilio_sid TEXT DEFAULT '',
            twilio_token TEXT DEFAULT '',
            twilio_from TEXT DEFAULT 'whatsapp:+14155238886',
            FOREIGN KEY (user_id) REFERENCES users(id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            title TEXT, company TEXT, location TEXT,
            salary TEXT, experience TEXT,
            url TEXT, source TEXT, posted TEXT,
            found_at TEXT, sent INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id))""")
    else:
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, email TEXT UNIQUE, password TEXT, created_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            designation TEXT DEFAULT 'Python Developer',
            location TEXT DEFAULT 'India',
            salary_min INTEGER DEFAULT 0,
            salary_max INTEGER DEFAULT 50,
            experience_min INTEGER DEFAULT 0,
            experience_max INTEGER DEFAULT 5,
            notify_time TEXT DEFAULT '00:00',
            whatsapp_number TEXT DEFAULT '',
            twilio_sid TEXT DEFAULT '',
            twilio_token TEXT DEFAULT '',
            twilio_from TEXT DEFAULT 'whatsapp:+14155238886',
            FOREIGN KEY (user_id) REFERENCES users(id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            user_id INTEGER,
            title TEXT, company TEXT, location TEXT,
            salary TEXT, experience TEXT,
            url TEXT, source TEXT, posted TEXT,
            found_at TEXT, sent INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id))""")
    conn.commit()
    conn.close()

init_db()

# ── Helpers ────────────────────────────────────────────────────────────────────

def hash_password(p):
    return hashlib.sha256(p.encode()).hexdigest()


def get_user(email):
    conn, is_pg = get_conn()
    c = conn.cursor()
    q = "SELECT * FROM users WHERE email=%s" if is_pg else "SELECT * FROM users WHERE email=?"
    c.execute(q, (email,))
    row = c.fetchone()
    conn.close()
    return row


def get_settings(user_id):
    conn, is_pg = get_conn()
    c = conn.cursor()
    q = "SELECT * FROM user_settings WHERE user_id=%s" if is_pg else "SELECT * FROM user_settings WHERE user_id=?"
    c.execute(q, (user_id,))
    row = c.fetchone()
    conn.close()
    return row


def get_seen_ids(user_id):
    conn, is_pg = get_conn()
    c = conn.cursor()
    q = "SELECT id FROM jobs WHERE user_id=%s" if is_pg else "SELECT id FROM jobs WHERE user_id=?"
    c.execute(q, (user_id,))
    rows = c.fetchall()
    conn.close()
    return {r[0] for r in rows}


def save_job(user_id, job, sent=False):
    conn, is_pg = get_conn()
    c = conn.cursor()
    if is_pg:
        c.execute("""INSERT INTO jobs
            (id,user_id,title,company,location,salary,experience,url,source,posted,found_at,sent)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO NOTHING""",
            (job["id"], user_id, job["title"], job["company"], job["location"],
             job.get("salary", "Not mentioned"), job.get("experience", "Not mentioned"),
             job["url"], job["source"], job["posted"],
             datetime.now().isoformat(), 1 if sent else 0))
    else:
        c.execute("""INSERT OR IGNORE INTO jobs
            (id,user_id,title,company,location,salary,experience,url,source,posted,found_at,sent)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job["id"], user_id, job["title"], job["company"], job["location"],
             job.get("salary", "Not mentioned"), job.get("experience", "Not mentioned"),
             job["url"], job["source"], job["posted"],
             datetime.now().isoformat(), 1 if sent else 0))
    conn.commit()
    conn.close()

# ── LinkedIn Scraper ───────────────────────────────────────────────────────────

LINKEDIN_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def scrape_linkedin(designation, location):
    jobs = []
    for start in [0, 25, 50]:
        url = (
            "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?"
            f"keywords={requests.utils.quote(designation)}"
            f"&location={requests.utils.quote(location)}"
            f"&f_TPR=r86400&sortBy=DD&start={start}"
        )
        try:
            r = requests.get(url, headers=LINKEDIN_HEADERS, timeout=15)
            if r.status_code != 200:
                print(f"[LinkedIn] Page {start} status: {r.status_code}")
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            for card in soup.find_all("li"):
                try:
                    t = card.find("h3", class_="base-search-card__title")
                    if not t:
                        continue
                    title = t.get_text(strip=True)
                    co = card.find("h4", class_="base-search-card__subtitle") or \
                         card.find("a", class_="hidden-nested-link")
                    company = co.get_text(strip=True) if co else "See link"
                    le = card.find("span", class_="job-search-card__location")
                    loc = le.get_text(strip=True) if le else location
                    ae = card.find("a", class_="base-card__full-link")
                    href = ae["href"].split("?")[0] if ae else ""
                    jid = "li_" + (href.split("-")[-1] if href else hashlib.md5(title.encode()).hexdigest()[:8])
                    te = card.find("time")
                    posted = te["datetime"] if te and te.get("datetime") else "recently"
                    if title:
                        jobs.append({
                            "id": jid, "title": title, "company": company,
                            "location": loc, "salary": "Not mentioned",
                            "experience": "Not mentioned", "url": href,
                            "source": "LinkedIn", "posted": posted
                        })
                except:
                    continue
            time.sleep(random.uniform(1, 2))
        except Exception as e:
            print(f"[LinkedIn] Error: {e}")
            continue
    print(f"[LinkedIn] Found {len(jobs)} jobs")
    return jobs

# ── Naukri Scraper (Fixed) ─────────────────────────────────────────────────────

def scrape_naukri(designation, location):
    jobs = []

    # ── Method 1: Naukri API v3 (updated headers) ──────────────────────────────
    try:
        api_url = (
            f"https://www.naukri.com/jobapi/v3/search"
            f"?noOfResults=20&urlType=search_by_key_loc"
            f"&searchType=adv&keyword={requests.utils.quote(designation)}"
            f"&location={requests.utils.quote(location)}"
            f"&jobAge=1&sort=1&pageNo=1"
        )
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "appid": "109",
            "systemid": "Naukri",
            "gid": "LOCATION,INDUSTRY,EDUCATION,FAREA_ROLE",
            "Referer": "https://www.naukri.com/",
            "x-http-method-override": "GET",
            "Origin": "https://www.naukri.com",
        }
        r = requests.get(api_url, headers=headers, timeout=20)
        print(f"[Naukri API] Status: {r.status_code}")

        if r.status_code == 200:
            data = r.json()
            job_list = data.get("jobDetails", [])
            print(f"[Naukri API] Raw jobs: {len(job_list)}")
            for job in job_list:
                try:
                    title = job.get("title", "")
                    company = job.get("companyName", "See link")
                    placeholders = job.get("placeholders", [])
                    loc = placeholders[0].get("label", location) if placeholders else location
                    salary = placeholders[1].get("label", "Not mentioned") if len(placeholders) > 1 else "Not mentioned"
                    experience = placeholders[0].get("label", "Not mentioned") if placeholders else "Not mentioned"
                    job_id = str(job.get("jobId", ""))
                    link = job.get("jdURL", f"https://www.naukri.com/job-listings-{job_id}")
                    if not link.startswith("http"):
                        link = "https://www.naukri.com" + link
                    jid = "nk_" + hashlib.md5(job_id.encode()).hexdigest()[:10]
                    if title and job_id:
                        jobs.append({
                            "id": jid, "title": title, "company": company,
                            "location": loc, "salary": salary,
                            "experience": experience, "url": link,
                            "source": "Naukri", "posted": "recently"
                        })
                except:
                    continue
    except Exception as e:
        print(f"[Naukri API] Error: {e}")

    # ── Method 2: Naukri HTML scraping fallback ────────────────────────────────
    if not jobs:
        print("[Naukri] API returned 0 jobs, trying HTML fallback...")
        try:
            slug_d = designation.lower().replace(" ", "-")
            slug_l = location.lower().replace(" ", "-")
            url = f"https://www.naukri.com/{slug_d}-jobs-in-{slug_l}?jobAge=1"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Referer": "https://www.naukri.com/",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            }
            r2 = requests.get(url, headers=headers, timeout=20)
            print(f"[Naukri HTML] Status: {r2.status_code}")

            if r2.status_code == 200:
                soup = BeautifulSoup(r2.text, "html.parser")
                # Try multiple selectors as Naukri changes classes often
                cards = (
                    soup.find_all("article", class_=re.compile("jobTuple|job-tuple")) or
                    soup.find_all("div", class_=re.compile("srp-jobtuple-wrapper|jobTupleHeader")) or
                    soup.find_all("div", class_=re.compile("job-container|list")) or
                    soup.select("article.jobTuple") or
                    soup.select("[data-job-id]")
                )
                print(f"[Naukri HTML] Cards found: {len(cards)}")
                for card in cards:
                    try:
                        te = (card.find("a", class_=re.compile("title|jobTitle")) or
                              card.find("a", attrs={"title": True}))
                        if not te:
                            continue
                        title = te.get_text(strip=True)
                        co = card.find(class_=re.compile("companyInfo|company-name|subTitle|comp-name"))
                        company = co.get_text(strip=True) if co else "See link"
                        le = card.find(class_=re.compile("location|loc|locWdth"))
                        loc = le.get_text(strip=True) if le else location
                        se = card.find(class_=re.compile("salary|sal|salaryText"))
                        salary = se.get_text(strip=True) if se else "Not mentioned"
                        ee = card.find(class_=re.compile("experience|exp|expwdth"))
                        experience = ee.get_text(strip=True) if ee else "Not mentioned"
                        link = te.get("href", "")
                        if not link.startswith("http"):
                            link = "https://www.naukri.com" + link
                        jid = "nk_" + hashlib.md5(link.encode()).hexdigest()[:10]
                        if title:
                            jobs.append({
                                "id": jid, "title": title, "company": company,
                                "location": loc, "salary": salary,
                                "experience": experience, "url": link,
                                "source": "Naukri", "posted": "recently"
                            })
                    except:
                        continue
        except Exception as e:
            print(f"[Naukri HTML] Error: {e}")

    print(f"[Naukri] Total jobs found: {len(jobs)}")
    return jobs

# ── Filter ─────────────────────────────────────────────────────────────────────

def parse_salary(s):
    try:
        nums = [float(n) for n in re.findall(r'\d+\.?\d*', s.replace(',', ''))]
        if nums:
            avg = sum(nums) / len(nums)
            return avg / 100000 if avg > 1000 else avg
    except:
        pass
    return None


def parse_exp(s):
    try:
        if "fresher" in s.lower():
            return 0.0
        nums = [float(n) for n in re.findall(r'\d+\.?\d*', s)]
        if nums:
            return sum(nums) / len(nums)
    except:
        pass
    return None


def filter_jobs(jobs, designation, sal_min, sal_max, exp_min, exp_max):
    keywords = designation.lower().split() + [designation.lower()]
    result = []
    for job in jobs:
        title_lower = job["title"].lower()
        if not any(kw in title_lower for kw in keywords):
            continue
        if job["salary"] != "Not mentioned":
            sal = parse_salary(job["salary"])
            if sal and (sal < sal_min or sal > sal_max):
                continue
        if job["experience"] != "Not mentioned":
            exp = parse_exp(job["experience"])
            if exp is not None and (exp < exp_min or exp > exp_max):
                continue
        result.append(job)
    return result

# ── WhatsApp ───────────────────────────────────────────────────────────────────

def send_whatsapp(job, settings):
    # Read from settings row: [user_id, designation, location, sal_min, sal_max,
    #                           exp_min, exp_max, notify_time, whatsapp, sid, token, from]
    whatsapp = settings[8]
    sid      = settings[9]  or os.environ.get("TWILIO_SID", "")
    token    = settings[10] or os.environ.get("TWILIO_TOKEN", "")
    from_    = settings[11] or os.environ.get("TWILIO_FROM", "whatsapp:+14155238886")

    # Validate
    if not whatsapp:
        raise Exception("WhatsApp number is empty — go to Settings and save your number!")
    if not sid or not token:
        raise Exception("Twilio SID or Token is empty — go to Settings and save your Twilio credentials!")

    # Make sure WhatsApp number has whatsapp: prefix
    if not whatsapp.startswith("whatsapp:"):
        whatsapp = "whatsapp:" + whatsapp

    # Make sure from_ has whatsapp: prefix
    if not from_.startswith("whatsapp:"):
        from_ = "whatsapp:" + from_

    client = Client(sid, token)
    icon = "💼" if job["source"] == "LinkedIn" else "🔍"
    msg = (
        f"{icon} *New Job Alert — {job['source']}*\n\n"
        f"*Role:* {job['title']}\n"
        f"*Company:* {job['company']}\n"
        f"*Location:* {job['location']}\n"
        f"*Salary:* {job['salary']}\n"
        f"*Experience:* {job['experience']}\n"
        f"*Posted:* {job['posted']}\n"
        f"*Link:* {job['url']}"
    )
    client.messages.create(body=msg, from_=from_, to=whatsapp)
    print(f"[WhatsApp] Sent: {job['title']} at {job['company']}")

# ── Background task ────────────────────────────────────────────────────────────

scrape_status = {}


def run_scrape(user_id):
    scrape_status[user_id] = {
        "running": True,
        "last_run": datetime.now().isoformat(),
        "result": "Running..."
    }
    whatsapp_errors = []

    try:
        s = get_settings(user_id)
        if not s:
            scrape_status[user_id]["result"] = "❌ No settings found — go to Settings and save!"
            return

        designation = s[1] or "Python Developer"
        location    = s[2] or "India"
        sal_min     = s[3] or 0
        sal_max     = s[4] or 50
        exp_min     = s[5] or 0
        exp_max     = s[6] or 10

        print(f"[Search] User {user_id} | {designation} | {location}")

        seen = get_seen_ids(user_id)

        li_jobs = scrape_linkedin(designation, location)
        nk_jobs = scrape_naukri(designation, location)
        all_jobs = li_jobs + nk_jobs

        matched  = filter_jobs(all_jobs, designation, sal_min, sal_max, exp_min, exp_max)
        new_jobs = [j for j in matched if j["id"] not in seen]

        print(f"[Search] LinkedIn:{len(li_jobs)} Naukri:{len(nk_jobs)} Matched:{len(matched)} New:{len(new_jobs)}")

        sent = 0
        for job in new_jobs:
            try:
                send_whatsapp(job, s)
                sent += 1
                time.sleep(random.uniform(1, 2))
            except Exception as e:
                err = str(e)
                print(f"[WhatsApp] Error: {err}")
                whatsapp_errors.append(err)
            save_job(user_id, job, sent=(sent > 0))

        result = (
            f"✅ LinkedIn: {len(li_jobs)} | "
            f"Naukri: {len(nk_jobs)} | "
            f"Matched: {len(matched)} | "
            f"New: {len(new_jobs)} | "
            f"Sent: {sent}"
        )
        if whatsapp_errors:
            result += f" | ⚠️ WhatsApp error: {whatsapp_errors[0][:100]}"

        scrape_status[user_id]["result"] = result

    except Exception as e:
        print(f"[Error] {e}")
        scrape_status[user_id]["result"] = f"❌ Error: {str(e)}"
    finally:
        scrape_status[user_id]["running"] = False

# ── Models ─────────────────────────────────────────────────────────────────────

class RegisterModel(BaseModel):
    name: str
    email: str
    password: str


class LoginModel(BaseModel):
    email: str
    password: str


class SettingsModel(BaseModel):
    designation: str
    location: str
    salary_min: int
    salary_max: int
    experience_min: int
    experience_max: int
    notify_time: str
    whatsapp_number: str
    twilio_sid: str
    twilio_token: str
    twilio_from: str

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"message": "Job Alert API is running!", "version": "1.0.0"}


@app.post("/register")
def register(data: RegisterModel):
    if get_user(data.email):
        raise HTTPException(400, "Email already registered")
    conn, is_pg = get_conn()
    c = conn.cursor()
    if is_pg:
        c.execute(
            "INSERT INTO users (name,email,password,created_at) VALUES (%s,%s,%s,%s) RETURNING id",
            (data.name, data.email, hash_password(data.password), datetime.now().isoformat())
        )
        uid = c.fetchone()[0]
        c.execute("INSERT INTO user_settings (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (uid,))
    else:
        c.execute(
            "INSERT INTO users (name,email,password,created_at) VALUES (?,?,?,?)",
            (data.name, data.email, hash_password(data.password), datetime.now().isoformat())
        )
        uid = c.lastrowid
        c.execute("INSERT INTO user_settings (user_id) VALUES (?)", (uid,))
    conn.commit()
    conn.close()
    return {"message": "Registered!", "user_id": uid, "name": data.name}


@app.post("/login")
def login(data: LoginModel):
    user = get_user(data.email)
    if not user or user[3] != hash_password(data.password):
        raise HTTPException(401, "Invalid email or password")
    return {"message": "Login successful!", "user_id": user[0], "name": user[1]}


@app.get("/settings/{user_id}")
def get_user_settings(user_id: int):
    s = get_settings(user_id)
    if not s:
        raise HTTPException(404, "Settings not found")
    return {
        "designation":    s[1],
        "location":       s[2],
        "salary_min":     s[3],
        "salary_max":     s[4],
        "experience_min": s[5],
        "experience_max": s[6],
        "notify_time":    s[7],
        "whatsapp_number": s[8],
        "twilio_sid":     s[9],
        "twilio_token":   s[10],
        "twilio_from":    s[11],
    }


@app.post("/settings/{user_id}")
def update_settings(user_id: int, data: SettingsModel):
    conn, is_pg = get_conn()
    c = conn.cursor()
    if is_pg:
        c.execute("""INSERT INTO user_settings
            (user_id,designation,location,salary_min,salary_max,
             experience_min,experience_max,notify_time,whatsapp_number,
             twilio_sid,twilio_token,twilio_from)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (user_id) DO UPDATE SET
                designation=EXCLUDED.designation,
                location=EXCLUDED.location,
                salary_min=EXCLUDED.salary_min,
                salary_max=EXCLUDED.salary_max,
                experience_min=EXCLUDED.experience_min,
                experience_max=EXCLUDED.experience_max,
                notify_time=EXCLUDED.notify_time,
                whatsapp_number=EXCLUDED.whatsapp_number,
                twilio_sid=EXCLUDED.twilio_sid,
                twilio_token=EXCLUDED.twilio_token,
                twilio_from=EXCLUDED.twilio_from""",
            (user_id, data.designation, data.location, data.salary_min, data.salary_max,
             data.experience_min, data.experience_max, data.notify_time, data.whatsapp_number,
             data.twilio_sid, data.twilio_token, data.twilio_from))
    else:
        c.execute("""INSERT OR REPLACE INTO user_settings
            (user_id,designation,location,salary_min,salary_max,
             experience_min,experience_max,notify_time,whatsapp_number,
             twilio_sid,twilio_token,twilio_from)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user_id, data.designation, data.location, data.salary_min, data.salary_max,
             data.experience_min, data.experience_max, data.notify_time, data.whatsapp_number,
             data.twilio_sid, data.twilio_token, data.twilio_from))
    conn.commit()
    conn.close()
    return {"message": "Settings saved!"}


@app.post("/search/{user_id}")
def trigger_search(user_id: int, background_tasks: BackgroundTasks):
    if scrape_status.get(user_id, {}).get("running"):
        return {"message": "Already running..."}
    background_tasks.add_task(run_scrape, user_id)
    return {"message": "Search started!"}


@app.get("/status/{user_id}")
def get_status(user_id: int):
    return scrape_status.get(user_id, {
        "running": False,
        "last_run": None,
        "result": "Not run yet"
    })


@app.get("/jobs/{user_id}")
def get_jobs(user_id: int, source: str = "all", limit: int = 50):
    conn, is_pg = get_conn()
    c = conn.cursor()
    if is_pg:
        if source == "all":
            c.execute(
                "SELECT id,title,company,location,salary,experience,url,source,posted,found_at "
                "FROM jobs WHERE user_id=%s ORDER BY found_at DESC LIMIT %s",
                (user_id, limit)
            )
        else:
            c.execute(
                "SELECT id,title,company,location,salary,experience,url,source,posted,found_at "
                "FROM jobs WHERE user_id=%s AND source=%s ORDER BY found_at DESC LIMIT %s",
                (user_id, source, limit)
            )
    else:
        if source == "all":
            c.execute(
                "SELECT id,title,company,location,salary,experience,url,source,posted,found_at "
                "FROM jobs WHERE user_id=? ORDER BY found_at DESC LIMIT ?",
                (user_id, limit)
            )
        else:
            c.execute(
                "SELECT id,title,company,location,salary,experience,url,source,posted,found_at "
                "FROM jobs WHERE user_id=? AND source=? ORDER BY found_at DESC LIMIT ?",
                (user_id, source, limit)
            )
    rows = c.fetchall()
    conn.close()
    return [
        {"id": r[0], "title": r[1], "company": r[2], "location": r[3],
         "salary": r[4], "experience": r[5], "url": r[6],
         "source": r[7], "posted": r[8], "found_at": r[9]}
        for r in rows
    ]


@app.get("/history/{user_id}")
def get_history(user_id: int):
    conn, is_pg = get_conn()
    c = conn.cursor()
    if is_pg:
        c.execute(
            "SELECT id,title,company,location,salary,experience,url,source,posted,found_at "
            "FROM jobs WHERE user_id=%s AND sent=1 ORDER BY found_at DESC",
            (user_id,)
        )
    else:
        c.execute(
            "SELECT id,title,company,location,salary,experience,url,source,posted,found_at "
            "FROM jobs WHERE user_id=? AND sent=1 ORDER BY found_at DESC",
            (user_id,)
        )
    rows = c.fetchall()
    conn.close()
    return [
        {"id": r[0], "title": r[1], "company": r[2], "location": r[3],
         "salary": r[4], "experience": r[5], "url": r[6],
         "source": r[7], "posted": r[8], "found_at": r[9]}
        for r in rows
    ]


@app.delete("/history/{user_id}")
def clear_history(user_id: int):
    conn, is_pg = get_conn()
    c = conn.cursor()
    if is_pg:
        c.execute("DELETE FROM jobs WHERE user_id=%s", (user_id,))
    else:
        c.execute("DELETE FROM jobs WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    return {"message": "History cleared!"}
