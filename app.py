import asyncio
import json
import random
import re
import string
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

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

def random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    return ''.join(random.choice(chars) for _ in range(length))

async def get_temp_email() -> tuple[str, str, str]:
    await asyncio.sleep(random.uniform(2.0, 4.0))
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get("https://api.mail.tm/domains")
        r.raise_for_status()
        domains = r.json().get("hydra:member", [])
        if not domains:
            raise Exception("No domains from mail.tm")
        domain = random.choice(domains)["domain"]
        login = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
        email = f"{login}@{domain}"
        password = random_password(12)
        create = await client.post("https://api.mail.tm/accounts", json={"address": email, "password": password})
        create.raise_for_status()
        token_r = await client.post("https://api.mail.tm/token", json={"address": email, "password": password})
        token_r.raise_for_status()
        token = token_r.json()["token"]
        return email, login, token

async def wait_for_otp(email: str, token: str, timeout: int = 180) -> Optional[str]:
    async with httpx.AsyncClient(timeout=20) as client:
        headers = {"Authorization": f"Bearer {token}"}
        start = time.time()
        while time.time() - start < timeout:
            r = await client.get("https://api.mail.tm/messages", headers=headers)
            r.raise_for_status()
            msgs = r.json().get("hydra:member", [])
            if msgs:
                mid = msgs[0]["id"]
                mr = await client.get(f"https://api.mail.tm/messages/{mid}", headers=headers)
                mr.raise_for_status()
                body = mr.json()
                text = body.get("text", "") + body.get("intro", "") + body.get("subject", "")
                print(f"OTP search text preview: {text[:200]}", flush=True)
                codes = re.findall(r"\b(\d{4,8})\b", text)
                if codes:
                    return codes[0]
                m = re.search(r"(?:code|otp|verification|pin|codice)[:\s]*(\d{4,8})", text, re.I)
                if m:
                    return m.group(1)
            await asyncio.sleep(5)
    return None

async def fill_otp(page, otp: str):
    # Try single field first
    try:
        await page.fill("input[name='otp'], input[name='code'], input[placeholder*='code'], input[placeholder*='OTP']", otp, timeout=3000)
        return
    except:
        pass
    # Multi-box OTP (common pattern: several single-digit inputs)
    try:
        boxes = await page.query_selector_all("input[maxlength='1'], input[inputmode='numeric']")
        if len(boxes) >= len(otp):
            for i, digit in enumerate(otp):
                await boxes[i].fill(digit)
                await page.wait_for_timeout(100)
            return
    except:
        pass
    # Fallback: type into first visible input
    try:
        first = await page.query_selector("input")
        if first:
            await first.focus()
            await page.keyboard.type(otp, delay=50)
    except:
        pass

async def register_one(browser: Browser, req: RegisterRequest, job_id: str, index: int, proxy: Optional[str] = None) -> dict:
    print(f"[{index}] Entering register_one", flush=True)
    jobs[job_id]["log"].append(f"[{index}] Entering register_one")
    try:
        email, login, token = await get_temp_email()
        password = random_password()
        result = {"index": index, "email": email, "password": password, "status": "started", "otp": None, "error": None}
        jobs[job_id]["results"].append(result)
        jobs[job_id]["log"].append(f"[{index}] Generated {email}")
        print(f"[{index}] Generated {email}", flush=True)
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

        await page.fill(req.email_selector, email, timeout=10000)
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
            await page.wait_for_timeout(1500)
        except Exception as e:
            jobs[job_id]["log"].append(f"[{index}] Checkbox JS: {e}")

        try:
            await page.click(req.submit_selector, timeout=8000)
        except:
            await page.click(req.submit_selector, force=True, timeout=5000)
        jobs[job_id]["log"].append(f"[{index}] Submitted form")
        print(f"[{index}] Submitted form", flush=True)
        await page.wait_for_timeout(5000)

        content = await page.content()
        if "error" in content.lower() or "invalid" in content.lower() or "already" in content.lower():
            jobs[job_id]["log"].append(f"[{index}] Possible form error on page")
            print(f"[{index}] Possible form error on page", flush=True)

        otp = await wait_for_otp(email, token, timeout=180)
        if not otp:
            result["status"] = "no_otp"
            result["error"] = "OTP not received in time"
            return result
        result["otp"] = otp
        jobs[job_id]["log"].append(f"[{index}] Got OTP: {otp}")
        print(f"[{index}] Got OTP: {otp}", flush=True)

        await fill_otp(page, otp)
        jobs[job_id]["log"].append(f"[{index}] Filled OTP boxes")
        print(f"[{index}] Filled OTP boxes", flush=True)

        # Try to submit OTP (may auto-submit on last digit)
        try:
            await page.click("button[type='submit'], button:has-text('Verify'), button:has-text('Continue'), button:has-text('Conferma')", timeout=5000)
        except:
            pass
        jobs[job_id]["log"].append(f"[{index}] OTP submitted or auto-submitted")
        await page.wait_for_timeout(3000)

        result["status"] = "success"
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
    count: int = Form(2),
    concurrency: int = Form(1),
    proxy: str = Form(None),
):
    job_id = f"job_{int(time.time())}"
    req = RegisterRequest(
        url=url,
        email_selector=email_selector,
        password_selector=password_selector,
        submit_selector=submit_selector,
        otp_selector=otp_selector,
        otp_submit_selector=otp_submit_selector,
        count=min(count, 5),
        concurrency=1,
        proxy=proxy if proxy else None,
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
    return """<!DOCTYPE html><html><head><title>Mass Account Registrar</title><style>body{font-family:monospace;background:#111;color:#0f0;padding:20px;}input,button{background:#222;color:#0f0;border:1px solid #0f0;padding:8px;margin:4px;width:100%;max-width:600px;}button{width:auto;cursor:pointer;}#log{height:300px;overflow:auto;border:1px solid #0f0;padding:10px;background:#000;}.success{color:#0f0;}.error{color:#f00;}</style></head><body><h1>Mass TempMail Registrar</h1><p>Registers accounts on target site using mail.tm temporary emails. Pulls OTP automatically.</p><form id="form"><label>Target Registration URL</label><br><input name="url" placeholder="https://example.com/register" required><br><label>Email field selector</label><br><input name="email_selector" value="input[type='email'], input[name='email']"><br><label>Password field selector</label><br><input name="password_selector" value="input[type='password']"><br><label>Submit button selector</label><br><input name="submit_selector" value="button[type='submit']"><br><label>OTP field selector</label><br><input name="otp_selector" value="input[name='otp'], input[placeholder*='code'], input[placeholder*='OTP']"><br><label>OTP submit selector</label><br><input name="otp_submit_selector" value="button[type='submit']"><br><label>Count</label><br><input name="count" type="number" value="2"><br><label>Concurrency</label><br><input name="concurrency" type="number" value="1"><br><label>Proxy (optional)</label><br><input name="proxy" placeholder=""><br><button type="submit">Start Batch</button></form><div id="status"></div><div id="log"></div><script>const form=document.getElementById('form');const logEl=document.getElementById('log');const statusEl=document.getElementById('status');let currentJob=null;form.onsubmit=async(e)=>{e.preventDefault();const fd=new FormData(form);const res=await fetch('/start',{method:'POST',body:fd});const data=await res.json();currentJob=data.job_id;statusEl.textContent='Job started: '+currentJob;poll();};async function poll(){if(!currentJob)return;const res=await fetch('/status/'+currentJob);const data=await res.json();logEl.innerHTML=data.log.slice(-50).join('<br>')+'<br>';const results=data.results||[];let summary=results.map(r=>`<div class="${r.status==='success'?'success':'error'}">[${r.index}] ${r.email} : ${r.password} | ${r.status} ${r.otp||''} ${r.error||''}</div>`).join('');logEl.innerHTML+=summary;logEl.scrollTop=logEl.scrollHeight;if(data.status!=='done'){setTimeout(poll,2000);}else{statusEl.textContent='Done.';}}</script></body></html>"""
