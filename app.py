import asyncio
import json
import random
import re
import string
import time
from typing import Dict, List, Optional

import httpx
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse
from playwright.async_api import async_playwright, Browser
from pydantic import BaseModel

app = FastAPI(title="Mass Registrar")

jobs: Dict[str, dict] = {}

class RegisterRequest(BaseModel):
    url: str
    email_selector: str = "input[type='email'], input[name='email']"
    password_selector: str = "input[type='password']"
    submit_selector: str = "button[type='submit']"
    otp_selector: str = "input[name='otp'], input[placeholder*='code']"
    otp_submit_selector: str = "button[type='submit']"
    count: int = 1
    concurrency: int = 1
    proxy: Optional[str] = None
    manual_otp: Optional[str] = None

def random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(random.choice(chars) for _ in range(length))

async def create_mail_tm() -> tuple[str, str, str]:
    async with httpx.AsyncClient(timeout=20, verify=False) as client:
        domains_r = await client.get("https://api.mail.tm/domains")
        domains = domains_r.json().get("hydra:member", [])
        if not domains:
            raise Exception("no domains")
        domain = random.choice(domains)["domain"]
        local = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
        address = f"{local}@{domain}"
        password = random_password(12)
        acc = await client.post("https://api.mail.tm/accounts", json={"address": address, "password": password})
        if acc.status_code not in (200, 201):
            raise Exception(f"account create {acc.status_code}")
        token_r = await client.post("https://api.mail.tm/token", json={"address": address, "password": password})
        token = token_r.json().get("token")
        if not token:
            raise Exception("no token")
        return address, password, token

def extract_otp_from_text(text: str) -> Optional[str]:
    codes = re.findall(r"\b(\d{4,8})\b", text)
    if codes:
        return codes[0]
    m = re.search(r"(?:code|otp|verification|pin|codice)[:\s]*(\d{4,8})", text, re.I)
    if m:
        return m.group(1)
    return None

async def wait_for_otp_mailtm(token: str, job_id: str, index: int, timeout: int = 90) -> Optional[str]:
    jobs[job_id]["log"].append(f"[{index}] OTP wait mail.tm started")
    headers = {"Authorization": f"Bearer {token}"}
    start = time.time()
    poll_count = 0
    async with httpx.AsyncClient(timeout=15, verify=False) as client:
        while time.time() - start < timeout:
            poll_count += 1
            try:
                r = await client.get("https://api.mail.tm/messages", headers=headers)
                jobs[job_id]["log"].append(f"[{index}] mail.tm poll {poll_count}: {r.status_code}")
                if r.status_code == 200:
                    msgs = r.json().get("hydra:member", [])
                    if msgs:
                        latest = msgs[0]
                        msg_id = latest["id"]
                        full = await client.get(f"https://api.mail.tm/messages/{msg_id}", headers=headers)
                        if full.status_code == 200:
                            data = full.json()
                            text = str(data.get("text", "")) + str(data.get("intro", "")) + str(data.get("subject", ""))
                            otp = extract_otp_from_text(text)
                            if otp:
                                jobs[job_id]["log"].append(f"[{index}] Found OTP: {otp}")
                                return otp
                    else:
                        jobs[job_id]["log"].append(f"[{index}] No messages yet")
            except Exception as e:
                jobs[job_id]["log"].append(f"[{index}] poll error: {e}")
            await asyncio.sleep(4)
    jobs[job_id]["log"].append(f"[{index}] OTP wait timed out")
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

async def register_one(browser: Browser, req: RegisterRequest, job_id: str, index: int) -> dict:
    jobs[job_id]["log"].append(f"[{index}] Entering register_one")
    try:
        address, mail_pass, token = await create_mail_tm()
        password = random_password()
        result = {"index": index, "email": address, "password": password, "status": "started", "otp": None, "error": None, "inbox": f"https://mail.tm (login {address})"}
        jobs[job_id]["results"].append(result)
        jobs[job_id]["log"].append(f"[{index}] Generated {address}")
        jobs[job_id]["log"].append(f"[{index}] Inbox: https://mail.tm (user: {address} / pass: {mail_pass})")
    except Exception as e:
        jobs[job_id]["log"].append(f"[{index}] Failed to generate email: {e}")
        return {"index": index, "status": "error", "error": str(e)}

    context = None
    try:
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(req.url, wait_until="networkidle", timeout=60000)
        jobs[job_id]["log"].append(f"[{index}] Loaded page")

        await page.fill(req.email_selector, address, timeout=10000)
        try:
            await page.fill(req.password_selector, password, timeout=5000)
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
        except Exception as e:
            jobs[job_id]["log"].append(f"[{index}] Checkbox JS: {e}")

        try:
            await page.click(req.submit_selector, timeout=8000)
        except:
            await page.click(req.submit_selector, force=True, timeout=5000)
        jobs[job_id]["log"].append(f"[{index}] Submitted form")

        otp = None
        if req.manual_otp and req.manual_otp.strip():
            otp = req.manual_otp.strip()
            jobs[job_id]["log"].append(f"[{index}] Using manual OTP: {otp}")
        else:
            otp = await wait_for_otp_mailtm(token, job_id, index)

        if not otp:
            result["status"] = "no_otp"
            result["error"] = "OTP not received - check inbox and use Manual OTP"
            jobs[job_id]["log"].append(f"[{index}] No OTP - check https://mail.tm with {address}")
            return result

        result["otp"] = otp
        jobs[job_id]["log"].append(f"[{index}] Got OTP: {otp}")

        await fill_otp(page, otp)
        jobs[job_id]["log"].append(f"[{index}] Filled OTP boxes")

        try:
            await page.click("button[type='submit'], button:has-text('Verify'), button:has-text('Continue'), button:has-text('Conferma')", timeout=4000)
        except:
            try:
                await page.keyboard.press("Enter")
            except:
                pass
        jobs[job_id]["log"].append(f"[{index}] OTP submitted")

        result["status"] = "otp_submitted"
        return result
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        jobs[job_id]["log"].append(f"[{index}] Error: {e}")
        return result
    finally:
        if context:
            await context.close()

async def run_batch(job_id: str, req: RegisterRequest):
    jobs[job_id]["status"] = "running"
    jobs[job_id]["log"].append("Entering run_batch")
    try:
        async with async_playwright() as p:
            jobs[job_id]["log"].append("Launching chromium...")
            browser = await p.chromium.launch(headless=True)
            jobs[job_id]["log"].append("Chromium launched")
            sem = asyncio.Semaphore(1)
            tasks = []

            async def wrapped(i):
                async with sem:
                    jobs[job_id]["log"].append(f"Starting account {i}")
                    return await register_one(browser, req, job_id, i)

            for i in range(req.count):
                tasks.append(asyncio.create_task(wrapped(i)))

            await asyncio.gather(*tasks, return_exceptions=True)
            await browser.close()
            jobs[job_id]["log"].append("Browser closed")
    except Exception as e:
        jobs[job_id]["log"].append(f"run_batch exception: {e}")
        jobs[job_id]["status"] = "error"
        return
    jobs[job_id]["status"] = "done"
    jobs[job_id]["log"].append("Batch finished")

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
    return """<!DOCTYPE html><html><head><title>Mass Registrar</title><style>body{font-family:monospace;background:#111;color:#0f0;padding:20px;}input,button{background:#222;color:#0f0;border:1px solid #0f0;padding:8px;margin:4px;width:100%;max-width:600px;}button{width:auto;cursor:pointer;}#log{height:350px;overflow:auto;border:1px solid #0f0;padding:10px;background:#000;}.success{color:#0f0;}.error{color:#f00;}</style></head><body><h1>Mass Registrar (mail.tm)</h1><p>Genera email temporanea su mail.tm. Controlla inbox su https://mail.tm dal telefono se serve. Incolla OTP nel campo Manual se auto non lo prende.</p><form id="form"><label>Target Registration URL</label><br><input name="url" value="https://www.bbdbuyeu.com/m/register?inviteCode=oTitSv" required><br><label>Manual OTP (optional)</label><br><input name="manual_otp" placeholder="incolla codice se non arriva auto"><br><label>Email field selector</label><br><input name="email_selector" value="input[type='email'], input[name='email']"><br><label>Password field selector</label><br><input name="password_selector" value="input[type='password']"><br><label>Submit button selector</label><br><input name="submit_selector" value="button[type='submit']"><br><label>OTP field selector</label><br><input name="otp_selector" value="input[name='otp'], input[placeholder*='code'], input[placeholder*='OTP']"><br><label>OTP submit selector</label><br><input name="otp_submit_selector" value="button[type='submit']"><br><label>Count</label><br><input name="count" type="number" value="1"><br><label>Concurrency</label><br><input name="concurrency" type="number" value="1"><br><button type="submit">Start Batch</button></form><div id="status"></div><div id="log"></div><script>const form=document.getElementById('form');const logEl=document.getElementById('log');const statusEl=document.getElementById('status');let currentJob=null;form.onsubmit=async(e)=>{e.preventDefault();const fd=new FormData(form);const res=await fetch('/start',{method:'POST',body:fd});const data=await res.json();currentJob=data.job_id;statusEl.textContent='Job started: '+currentJob;poll();};async function poll(){if(!currentJob)return;const res=await fetch('/status/'+currentJob);const data=await res.json();logEl.innerHTML=data.log.slice(-60).join('<br>')+'<br>';const results=data.results||[];let summary=results.map(r=>`<div class="${r.status==='otp_submitted'?'success':'error'}">[${r.index}] ${r.email} : ${r.password} | ${r.status} ${r.otp||''} ${r.error||''}</div>`).join('');logEl.innerHTML+=summary;logEl.scrollTop=logEl.scrollHeight;if(data.status!=='done'){setTimeout(poll,2000);}else{statusEl.textContent='Done.';}}</script></body></html>"""
