import asyncio
import imaplib
import email
import json
import random
import re
import string
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional
from email.header import decode_header

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright, Browser, Page
from pydantic import BaseModel

app = FastAPI(title="Mass Registrar")

jobs: Dict[str, dict] = {}

class RegisterRequest(BaseModel):
    url: str
    email_selector: str = "input[type='email'], input[name='email'], input[name='username']"
    password_selector: str = "input[type='password'], input[name='password']"
    submit_selector: str = "button[type='submit'], input[type='submit'], button:has-text('Sign up'), button:has-text('Register')"
    otp_selector: str = "input[name='otp'], input[name='code'], input[placeholder*='code'], input[placeholder*='OTP']"
    otp_submit_selector: str = "button[type='submit'], button:has-text('Verify'), button:has-text('Continue')"
    count: int = 5
    concurrency: int = 1
    proxy: Optional[str] = None
    captcha_key: Optional[str] = None
    base_email: Optional[str] = None
    imap_password: Optional[str] = None
    manual_otp: Optional[str] = None

def random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(random.choice(chars) for _ in range(length))

def generate_plus_email(base_email: str) -> str:
    if not base_email or "@" not in base_email:
        raise Exception("Base email required")
    local, domain = base_email.split("@", 1)
    tag = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"{local}+{tag}@{domain}"

def get_imap_server(email_addr: str) -> str:
    domain = email_addr.split("@")[-1].lower()
    if "gmail" in domain:
        return "imap.gmail.com"
    if "icloud" in domain or "me.com" in domain or "mac.com" in domain:
        return "imap.mail.me.com"
    if "outlook" in domain or "hotmail" in domain or "live.com" in domain:
        return "outlook.office365.com"
    return "imap.mail.me.com"

async def get_temp_email(base_email: Optional[str] = None) -> tuple[str, str, str]:
    if base_email:
        email_addr = generate_plus_email(base_email)
        return email_addr, email_addr.split("@")[0], email_addr
    await asyncio.sleep(random.uniform(0.5, 1.5))
    domains = ["grabmail.io", "mixozia.com", "linqmail.com"]
    domain = random.choice(domains)
    login = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
    email_addr = f"{login}@{domain}"
    return email_addr, login, email_addr

def extract_otp_from_text(text: str) -> Optional[str]:
    codes = re.findall(r"\b(\d{4,8})\b", text)
    if codes:
        return codes[0]
    m = re.search(r"(?:code|otp|verification|pin|codice)[:\s]*(\d{4,8})", text, re.I)
    if m:
        return m.group(1)
    return None

def safe_decode_payload(part) -> str:
    try:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        if isinstance(payload, bytes):
            return payload.decode(errors="ignore")
        if isinstance(payload, str):
            return payload
        return str(payload)
    except Exception:
        return ""

def safe_decode_header(header_value) -> str:
    try:
        if not header_value:
            return ""
        decoded = decode_header(header_value)
        parts = []
        for t, enc in decoded:
            if isinstance(t, bytes):
                parts.append(t.decode(enc or "utf-8", errors="ignore"))
            else:
                parts.append(str(t))
        return "".join(parts)
    except Exception:
        return str(header_value)

async def wait_for_otp_imap(base_email: str, imap_password: str, target_email: str, job_id: str, index: int, timeout: int = 90) -> Optional[str]:
    jobs[job_id]["log"].append(f"[{index}] OTP wait IMAP started for {target_email}")
    print(f"Starting OTP IMAP wait for {target_email}", flush=True)
    imap_server = get_imap_server(base_email)
    jobs[job_id]["log"].append(f"[{index}] Using IMAP server: {imap_server}")
    start = time.time()
    poll_count = 0
    while time.time() - start < timeout:
        poll_count += 1
        try:
            mail = imaplib.IMAP4_SSL(imap_server)
            mail.login(base_email, imap_password)
            mail.select("inbox")
            status, data = mail.search(None, "UNSEEN")
            if status != "OK" or not data[0]:
                status, data = mail.search(None, "ALL")
            if status == "OK" and data[0]:
                msg_ids = data[0].split()
                for mid in msg_ids[-8:]:
                    try:
                        status, msg_data = mail.fetch(mid, "(RFC822)")
                        if status != "OK":
                            continue
                        raw = msg_data[0][1]
                        msg = email.message_from_bytes(raw)
                        to_header = safe_decode_header(msg.get("To", ""))
                        delivered = safe_decode_header(msg.get("Delivered-To", ""))
                        if target_email.lower() not in (to_header + delivered).lower():
                            continue
                        body = ""
                        if msg.is_multipart():
                            for part in msg.walk():
                                ctype = part.get_content_type()
                                if ctype in ("text/plain", "text/html"):
                                    body += safe_decode_payload(part)
                        else:
                            body = safe_decode_payload(msg)
                        subject = safe_decode_header(msg.get("Subject", ""))
                        full_text = subject + " " + body
                        otp = extract_otp_from_text(full_text)
                        if otp:
                            mail.logout()
                            jobs[job_id]["log"].append(f"[{index}] Found OTP via IMAP: {otp}")
                            return otp
                    except Exception as inner:
                        jobs[job_id]["log"].append(f"[{index}] Message parse error: {inner}")
                        continue
            mail.logout()
            jobs[job_id]["log"].append(f"[{index}] IMAP poll {poll_count}: no matching message yet")
        except Exception as e:
            jobs[job_id]["log"].append(f"[{index}] IMAP poll error: {e}")
            print(f"IMAP error: {e}", flush=True)
        await asyncio.sleep(4)
    jobs[job_id]["log"].append(f"[{index}] IMAP OTP wait timed out")
    return None

async def wait_for_otp(email: str, _, job_id: str, index: int, timeout: int = 90, base_email: Optional[str] = None, imap_password: Optional[str] = None, manual_otp: Optional[str] = None) -> Optional[str]:
    if manual_otp and manual_otp.strip():
        jobs[job_id]["log"].append(f"[{index}] Using manual OTP: {manual_otp}")
        return manual_otp.strip()
    if base_email and imap_password:
        return await wait_for_otp_imap(base_email, imap_password, email, job_id, index, timeout)
    jobs[job_id]["log"].append(f"[{index}] OTP wait started for {email}")
    print(f"Starting OTP wait for {email}", flush=True)
    async with httpx.AsyncClient(timeout=15) as client:
        start = time.time()
        poll_count = 0
        while time.time() - start < timeout:
            poll_count += 1
            try:
                r = await client.get(f"https://grabmail.io/api/v1/mailbox?address={email}")
                jobs[job_id]["log"].append(f"[{index}] OTP poll {poll_count}: status {r.status_code}")
                if r.status_code == 200:
                    data = r.json()
                    msgs = data.get("messages", [])
                    if isinstance(msgs, list) and msgs:
                        text = ""
                        for m in msgs:
                            text += str(m.get("text", "")) + str(m.get("preview", "")) + str(m.get("subject", ""))
                        otp = extract_otp_from_text(text)
                        if otp:
                            return otp
                    else:
                        jobs[job_id]["log"].append(f"[{index}] No messages yet")
            except Exception as e:
                jobs[job_id]["log"].append(f"[{index}] OTP poll error: {e}")
            await asyncio.sleep(5)
    return None

async def fill_otp(page, otp: str):
    try:
        await page.fill("input[name='otp'], input[name='code'], input[placeholder*='code'], input[placeholder*='OTP']", otp, timeout=3000)
        return
    except:
        pass
    try:
        boxes = await page.query_selector_all("input[maxlength='1'], input[inputmode='numeric']")
        if len(boxes) >= len(otp):
            for i, digit in enumerate(otp):
                await boxes[i].fill(digit)
                await page.wait_for_timeout(150)
            return
    except:
        pass
    try:
        first = await page.query_selector("input")
        if first:
            await first.focus()
            await page.keyboard.type(otp, delay=80)
    except:
        pass

async def register_one(browser: Browser, req: RegisterRequest, job_id: str, index: int, proxy: Optional[str] = None) -> dict:
    print(f"[{index}] Entering register_one", flush=True)
    jobs[job_id]["log"].append(f"[{index}] Entering register_one")
    try:
        email_addr, login, _ = await get_temp_email(req.base_email)
        password = random_password()
        result = {"index": index, "email": email_addr, "password": password, "status": "started", "otp": None, "error": None}
        jobs[job_id]["results"].append(result)
        jobs[job_id]["log"].append(f"[{index}] Generated {email_addr}")
        print(f"[{index}] Generated {email_addr}", flush=True)
    except Exception as e:
        msg = f"[{index}] Failed to generate email: {e}"
        jobs[job_id]["log"].append(msg)
        print(msg, flush=True)
        return {"index": index, "status": "error", "error": str(e)}

    context = None
    try:
        context_options = {}
        if proxy:
            context_options["proxy"] = {"server": proxy}
        context = await browser.new_context(**context_options)
        page = await context.new_page()
        await page.goto(req.url, wait_until="networkidle", timeout=60000)
        jobs[job_id]["log"].append(f"[{index}] Loaded page")
        print(f"[{index}] Loaded page", flush=True)

        await page.fill(req.email_selector, email_addr, timeout=10000)
        try:
            await page.fill(req.password_selector, password, timeout=5000)
        except:
            pass

        try:
            await page.click("label:has-text('terms'), label:has-text('termini'), label:has-text('agree'), label:has-text('accetto')", timeout=3000)
        except:
            pass
        try:
            await page.evaluate("""() => {
                const cb = document.querySelector('input[name="agreeToTerms"], input[type="checkbox"]');
                if (cb) {
                    cb.checked = true;
                    cb.dispatchEvent(new Event('change', { bubbles: true }));
                    cb.dispatchEvent(new Event('input', { bubbles: true }));
                }
            }""")
            jobs[job_id]["log"].append(f"[{index}] Forced checkbox checked")
            print(f"[{index}] Forced checkbox checked", flush=True)
        except Exception as e:
            jobs[job_id]["log"].append(f"[{index}] Checkbox JS: {e}")

        try:
            await page.click(req.submit_selector, timeout=8000)
        except:
            await page.click(req.submit_selector, force=True, timeout=5000)
        jobs[job_id]["log"].append(f"[{index}] Submitted form")
        print(f"[{index}] Submitted form", flush=True)

        jobs[job_id]["log"].append(f"[{index}] Starting OTP poll")
        print(f"[{index}] Starting OTP poll immediately", flush=True)

        otp = await wait_for_otp(email_addr, email_addr, job_id, index, timeout=90, base_email=req.base_email, imap_password=req.imap_password, manual_otp=req.manual_otp)
        if not otp:
            result["status"] = "no_otp"
            result["error"] = "OTP not received in time"
            jobs[job_id]["log"].append(f"[{index}] No OTP")
            print(f"[{index}] No OTP", flush=True)
            return result
        result["otp"] = otp
        jobs[job_id]["log"].append(f"[{index}] Got OTP: {otp}")
        print(f"[{index}] Got OTP: {otp}", flush=True)

        await fill_otp(page, otp)
        jobs[job_id]["log"].append(f"[{index}] Filled OTP boxes")
        print(f"[{index}] Filled OTP boxes", flush=True)

        try:
            await page.click("button[type='submit'], button:has-text('Verify'), button:has-text('Continue'), button:has-text('Conferma')", timeout=4000)
        except:
            try:
                await page.keyboard.press("Enter")
            except:
                pass
        jobs[job_id]["log"].append(f"[{index}] OTP submitted")
        print(f"[{index}] OTP submitted", flush=True)

        result["status"] = "otp_submitted"
        return result
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        jobs[job_id]["log"].append(f"[{index}] Error: {e}")
        print(f"[{index}] Error: {e}", flush=True)
        return result
    finally:
        if context:
            await context.close()

async def run_batch(job_id: str, req: RegisterRequest):
    jobs[job_id]["status"] = "running"
    jobs[job_id]["log"].append("Entering run_batch")
    print("Entering run_batch", flush=True)
    try:
        async with async_playwright() as p:
            jobs[job_id]["log"].append("Playwright context entered, launching chromium...")
            print("Launching chromium...", flush=True)
            browser = await p.chromium.launch(headless=True)
            jobs[job_id]["log"].append("Chromium launched successfully")
            print("Chromium launched", flush=True)
            sem = asyncio.Semaphore(1)
            tasks = []

            async def wrapped(i):
                async with sem:
                    jobs[job_id]["log"].append(f"Starting account {i}")
                    print(f"Starting account {i}", flush=True)
                    return await register_one(browser, req, job_id, i, req.proxy)

            for i in range(req.count):
                tasks.append(asyncio.create_task(wrapped(i)))

            await asyncio.gather(*tasks, return_exceptions=True)
            await browser.close()
            jobs[job_id]["log"].append("Browser closed")
            print("Browser closed", flush=True)
    except Exception as e:
        msg = f"run_batch exception: {type(e).__name__}: {e}"
        jobs[job_id]["log"].append(msg)
        print(msg, flush=True)
        jobs[job_id]["status"] = "error"
        return
    jobs[job_id]["status"] = "done"
    jobs[job_id]["log"].append("Batch finished")
    print("Batch finished", flush=True)

@app.post("/start")
async def start_registration(
    url: str = Form(...),
    email_selector: str = Form("input[type='email'], input[name='email']"),
    password_selector: str = Form("input[type='password']"),
    submit_selector: str = Form("button[type='submit']"),
    otp_selector: str = Form("input[name='otp'], input[placeholder*='code']"),
    otp_submit_selector: str = Form("button[type='submit']"),
    count: int = Form(1),
    concurrency: int = Form(1),
    proxy: str = Form(None),
    base_email: str = Form(None),
    imap_password: str = Form(None),
    manual_otp: str = Form(None),
):
    job_id = f"job_{int(time.time())}"
    req = RegisterRequest(
        url=url,
        email_selector=email_selector,
        password_selector=password_selector,
        submit_selector=submit_selector,
        otp_selector=otp_selector,
        otp_submit_selector=otp_submit_selector,
        count=1,
        concurrency=1,
        proxy=proxy if proxy else None,
        base_email=base_email if base_email else None,
        imap_password=imap_password if imap_password else None,
        manual_otp=manual_otp if manual_otp else None,
    )
    jobs[job_id] = {
        "status": "queued",
        "results": [],
        "log": [f"Started batch of {req.count}"],
        "req": req.dict(),
    }
    asyncio.create_task(run_batch(job_id, req))
    return {"job_id": job_id}

@app.get("/status/{job_id}")
async def get_status(job_id: str):
    if job_id not in jobs:
        return JSONResponse({"error": "not found"}, 404)
    return jobs[job_id]

@app.get("/", response_class=HTMLResponse)
async def index():
    return """<!DOCTYPE html><html><head><title>Mass Account Registrar</title><style>body{font-family:monospace;background:#111;color:#0f0;padding:20px;}input,button{background:#222;color:#0f0;border:1px solid #0f0;padding:8px;margin:4px;width:100%;max-width:600px;}button{width:auto;cursor:pointer;}#log{height:300px;overflow:auto;border:1px solid #0f0;padding:10px;background:#000;}.success{color:#0f0;}.error{color:#f00;}</style></head><body><h1>Mass Registrar (iCloud)</h1><p>iCloud plus addressing + IMAP. Credentials pre-filled. Optional: paste OTP manually if auto fails.</p><form id="form"><label>Target Registration URL</label><br><input name="url" value="https://www.bbdbuyeu.com/m/register?inviteCode=oTitSv" required><br><label>Base iCloud</label><br><input name="base_email" value="jacopobalbi1@icloud.com" required><br><label>App Password</label><br><input name="imap_password" type="password" value="ibkt-uxvn-xgae-kasn" required><br><label>Manual OTP (optional)</label><br><input name="manual_otp" placeholder="paste code here if needed"><br><label>Email field selector</label><br><input name="email_selector" value="input[type='email'], input[name='email']"><br><label>Password field selector</label><br><input name="password_selector" value="input[type='password']"><br><label>Submit button selector</label><br><input name="submit_selector" value="button[type='submit']"><br><label>OTP field selector</label><br><input name="otp_selector" value="input[name='otp'], input[placeholder*='code'], input[placeholder*='OTP']"><br><label>OTP submit selector</label><br><input name="otp_submit_selector" value="button[type='submit']"><br><label>Count</label><br><input name="count" type="number" value="1"><br><label>Concurrency</label><br><input name="concurrency" type="number" value="1"><br><label>Proxy (optional)</label><br><input name="proxy" placeholder=""><br><button type="submit">Start Batch</button></form><div id="status"></div><div id="log"></div><script>const form=document.getElementById('form');const logEl=document.getElementById('log');const statusEl=document.getElementById('status');let currentJob=null;form.onsubmit=async(e)=>{e.preventDefault();const fd=new FormData(form);const res=await fetch('/start',{method:'POST',body:fd});const data=await res.json();currentJob=data.job_id;statusEl.textContent='Job started: '+currentJob;poll();};async function poll(){if(!currentJob)return;const res=await fetch('/status/'+currentJob);const data=await res.json();logEl.innerHTML=data.log.slice(-50).join('<br>')+'<br>';const results=data.results||[];let summary=results.map(r=>`<div class="${r.status==='success'?'success':'error'}">[${r.index}] ${r.email} : ${r.password} | ${r.status} ${r.otp||''} ${r.error||''}</div>`).join('');logEl.innerHTML+=summary;logEl.scrollTop=logEl.scrollHeight;if(data.status!=='done'){setTimeout(poll,2000);}else{statusEl.textContent='Done.';}}</script></body></html>"""
