import os
import asyncio
import time
import requests
import sys
from datetime import datetime
from fastapi import FastAPI
from playwright.async_api import async_playwright
from motor.motor_asyncio import AsyncIOMotorClient

# --- 🔥 ENV CONFIGURATION (HARDCODED FOR STABILITY) 🔥 ---
# آپ نے جو ٹوکن اور آئی ڈیز دی ہیں، وہ یہاں سیٹ کر دی ہیں
RAILWAY_TOKEN = "7a4ef37f-1830-4c06-a802-d1dff8922ee2"
RAILWAY_SERVICE_ID = "f3ebcb8d-70db-41a8-81dc-d7c14e550cbe" 
RAILWAY_ENV_ID = "f323efa2-e1bd-4c3e-9790-080ef9481b92"

# MongoDB Config
MONGO_URI = "mongodb://mongo:AEvrikOWlrmJCQrDTQgfGtqLlwhwLuAA@crossover.proxy.rlwy.net:29609"
DB_NAME = "number_manager"
COL_PENDING = "phone_numbers"
COL_SUCCESS = "success_numbers"
COL_FAILED = "failed_numbers"

# Global URL
BASE_URL = "https://id5.cloud.huawei.com"

# --- APP SETUP ---
app = FastAPI()

CAPTURE_DIR = "./captures"
if not os.path.exists(CAPTURE_DIR): os.makedirs(CAPTURE_DIR)

try:
    from captcha_solver import solve_captcha
except ImportError:
    async def solve_captcha(page, session_id, logger=print): 
        print("❌ Captcha Solver Module NOT Found!", flush=True)
        return False

SETTINGS = {"country": "Russia"} 

# --- LOGGING HELPER ---
def log_msg(message, level="INFO"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] [{level}] {message}", flush=True)

# --- DATABASE HELPERS ---
async def get_next_number_from_db():
    try:
        client = AsyncIOMotorClient(MONGO_URI)
        db = client[DB_NAME]
        col = db[COL_PENDING]
        doc = await col.find_one({}) 
        return doc
    except Exception as e:
        log_msg(f"DB Error: {e}", "ERROR")
        return None

async def move_number_to_collection(phone, status):
    try:
        client = AsyncIOMotorClient(MONGO_URI)
        db = client[DB_NAME]
        
        target_col_name = COL_SUCCESS if status == "success" else COL_FAILED
        target_col = db[target_col_name]
        source_col = db[COL_PENDING]
        
        await target_col.insert_one({
            "phone": phone,
            "status": status,
            "timestamp": datetime.now()
        })
        await source_col.delete_one({"phone": phone})
        log_msg(f"📦 Moved {phone} to {target_col_name}", "DB")
    except Exception as e:
        log_msg(f"DB Move Error: {e}", "ERROR")

# --- RAILWAY REDEPLOY (ACTUAL API CALL) ---
def trigger_redeploy():
    log_msg("🔄 Sending Redeploy Signal to Railway API...", "SYSTEM")
    
    url = "https://backboard.railway.app/graphql/v2"
    headers = {
        "Authorization": f"Bearer {RAILWAY_TOKEN}",
        "Content-Type": "application/json"
    }
    
    # Correct GraphQL Mutation
    query = """
    mutation serviceInstanceRedeploy($serviceId: String!, $environmentId: String!) {
        serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId)
    }
    """
    
    variables = {
        "serviceId": RAILWAY_SERVICE_ID,
        "environmentId": RAILWAY_ENV_ID
    }
    
    try:
        response = requests.post(url, json={"query": query, "variables": variables}, headers=headers)
        log_msg(f"API Response: {response.text}", "DEBUG")
        
        if response.status_code == 200 and "errors" not in response.json():
            log_msg("✅ Redeploy Triggered Successfully! Shutting down...", "SYSTEM")
            time.sleep(2) # Give logs time to flush
            os._exit(0)
        else:
            log_msg(f"❌ API Redeploy Failed: {response.text}", "ERROR")
            # Fallback: Just crash, maybe auto-restart helps
            os._exit(1)
            
    except Exception as e:
        log_msg(f"❌ Redeploy Request Error: {e}", "FATAL")
        os._exit(1)

# --- CLICK LOGIC ---
async def click_element(page, finder, name):
    try:
        el = finder()
        if await el.count() > 0:
            try: await el.first.scroll_into_view_if_needed()
            except: pass
            
            box = await el.first.bounding_box()
            if box:
                cx = box['x'] + box['width'] / 2
                cy = box['y'] + box['height'] / 2
                log_msg(f"🖱️ Tapping {name}...", "ACTION")
                await page.touchscreen.tap(cx, cy)
                return True
        else:
            log_msg(f"❌ Element NOT found: {name}", "DEBUG")
        return False
    except Exception as e: 
        log_msg(f"❌ Click Error {name}: {e}", "ERROR")
        return False

async def smart_action(page, finder, verifier, step_name, wait_after=5):
    log_msg(f"🔍 Searching: {step_name}...", "STEP")
    
    for attempt in range(1, 4):
        if step_name != "Register_Text":
            if verifier and await verifier().count() > 0:
                log_msg(f"✅ {step_name} Already Completed.", "INFO")
                return True

        clicked = await click_element(page, finder, f"{step_name} (Try {attempt})")
        
        if clicked:
            log_msg(f"⏳ Waiting {wait_after}s for reaction...", "WAIT")
            await asyncio.sleep(wait_after)
            
            if verifier and await verifier().count() > 0:
                log_msg(f"✅ {step_name} Verified!", "SUCCESS")
                return True
            elif await finder().count() > 0:
                log_msg(f"⚠️ {step_name} clicked but still visible. Retrying...", "WARN")
                continue
            else:
                log_msg(f"⏳ Elements disappeared. Loading wait (5s)...", "WAIT")
                await asyncio.sleep(5)
                if verifier and await verifier().count() > 0:
                    log_msg(f"✅ {step_name} Verified (After Load)!", "SUCCESS")
                    return True
                else:
                    log_msg(f"⚠️ Page stuck or loading...", "WARN")
        else:
            log_msg(f"❌ {step_name} Not Found (Attempt {attempt})", "WARN")
            await asyncio.sleep(2)

    return False

# --- MAIN WORKER ---
async def master_loop():
    log_msg("🟢 Auto-Bot Started (v3.0 API Redeploy Mode)", "INIT")
    
    # 1. Fetch Number
    db_doc = await get_next_number_from_db()
    if not db_doc:
        log_msg("ℹ️ No 'pending' numbers found. Sleeping 5 mins...", "IDLE")
        await asyncio.sleep(300) 
        trigger_redeploy() 
        return

    current_number = db_doc['phone']
    log_msg(f"🔵 PROCESSING NUMBER: {current_number}", "START")

    # 2. Run Session
    try:
        # Pass BASE_URL explicitly if needed, but it's global now
        res = await run_session(current_number, SETTINGS["country"])
        
        # 3. Move Number
        if res == "success":
            log_msg("🎉 SESSION SUCCESS! Moving to DB...", "RESULT")
            await move_number_to_collection(current_number, "success")
        else:
            log_msg("❌ SESSION FAILED. Moving to DB...", "RESULT")
            await move_number_to_collection(current_number, "failed")
            
    except Exception as e:
        log_msg(f"🔥 CRITICAL CRASH: {e}", "FATAL")
        await move_number_to_collection(current_number, "failed")

    # 4. RESTART VIA API
    log_msg("🔄 Job Done. Calling Railway API...", "SYSTEM")
    await asyncio.sleep(2)
    trigger_redeploy()

async def run_session(phone, country):
    try:
        async with async_playwright() as p:
            launch_args = {
                "headless": True, 
                "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox", "--ignore-certificate-errors", "--disable-web-security"]
            }

            log_msg("🚀 Launching Browser...", "INIT")
            try: browser = await p.chromium.launch(**launch_args)
            except Exception as e: log_msg(f"❌ Launch Fail: {e}", "ERROR"); return "failed"

            pixel_5 = p.devices['Pixel 5'].copy()
            pixel_5['viewport'] = {'width': 412, 'height': 950}
            pixel_5['has_touch'] = True 
            
            context = await browser.new_context(**pixel_5, locale="en-US", ignore_https_errors=True)
            page = await context.new_page()

            # URL
            log_msg(f"🌐 Navigating to {BASE_URL}...", "NAV")
            try:
                await page.goto(BASE_URL, timeout=90000)
                log_msg("⏳ Page Load Wait (5s)...", "WAIT")
                await asyncio.sleep(5) 
            except Exception as e: 
                log_msg(f"❌ Timeout/Load Error: {e}", "ERROR")
                return "failed"

            # REGISTER
            if not await smart_action(
                page, 
                lambda: page.get_by_text("Register", exact=True), 
                lambda: page.get_by_text("Stay informed", exact=False), 
                "Register_Text",
                wait_after=5
            ): return "failed"

            # AGREE
            cb = page.get_by_text("Stay informed", exact=False)
            if await cb.count() > 0:
                await click_element(page, lambda: cb, "Stay Informed Checkbox")
                await asyncio.sleep(1)
            
            if not await smart_action(
                page,
                lambda: page.get_by_text("Agree", exact=False).last, 
                lambda: page.get_by_text("Date of birth", exact=False),
                "Agree_Last",
                wait_after=5
            ): return "failed"

            # DOB
            if not await smart_action(
                page,
                lambda: page.get_by_text("Next", exact=False).last, 
                lambda: page.get_by_text("Use phone number", exact=False),
                "DOB_Next_Text",
                wait_after=5
            ): return "failed"

            # PHONE TAB
            if not await smart_action(
                page,
                lambda: page.get_by_text("Use phone number", exact=False),
                lambda: page.get_by_text("Country/Region"), 
                "UsePhone_Text",
                wait_after=5
            ): return "failed"

            # COUNTRY
            log_msg(f"🌍 Selecting Country: {country}", "ACTION")
            if not await smart_action(
                page,
                lambda: page.get_by_text("Hong Kong", exact=False).or_(page.locator(".arrow-icon").first),
                lambda: page.get_by_placeholder("Search", exact=False),
                "Open_Country_List",
                wait_after=3
            ): return "failed"

            search = page.get_by_placeholder("Search", exact=False).first
            await search.click()
            await page.keyboard.type(country, delay=50)
            await asyncio.sleep(2)
            
            matches = page.get_by_text(country, exact=False)
            if await matches.count() > 0:
                await click_element(page, lambda: matches.first, f"Country: {country}")
                await asyncio.sleep(3) 
            else:
                log_msg("❌ Country Not Found in List", "ERROR")
                await browser.close(); return "failed"

            # INPUT PHONE
            inp = page.locator("input[type='tel']").first
            if await inp.count() == 0: inp = page.locator("input").first
            
            if await inp.count() > 0:
                clean_phone = phone
                if country == "Russia" and clean_phone.startswith("7"): clean_phone = clean_phone[1:]
                elif country == "Pakistan" and clean_phone.startswith("92"): clean_phone = clean_phone[2:]
                
                log_msg(f"🔢 Typing Number: {clean_phone}", "ACTION")
                await inp.click()
                for c in clean_phone:
                    await page.keyboard.type(c); await asyncio.sleep(0.05)
                
                # Close keyboard
                await page.touchscreen.tap(350, 100) 
                
                # GET CODE
                get_code = page.locator(".get-code-btn").or_(page.get_by_text("Get code"))
                if await get_code.count() > 0:
                    await click_element(page, lambda: get_code.first, "Get Code Button")
                    
                    log_msg("⏳ Hard Wait: 10s for Captcha...", "WAIT")
                    await asyncio.sleep(10)

                    # Error Popup
                    if await page.get_by_text("An unexpected problem", exact=False).count() > 0:
                        log_msg("⛔ FATAL: System Error (IP Block/Rate Limit)", "FATAL")
                        await browser.close(); return "failed"

                    # Captcha Logic
                    start_solve_time = time.time()
                    while True: 
                        if time.time() - start_solve_time > 120: 
                            log_msg("⏰ Captcha Loop Timeout", "TIMEOUT")
                            break

                        if await page.get_by_text("swap 2 tiles", exact=False).count() > 0:
                            log_msg("🧩 CAPTCHA DETECTED!", "CAPTCHA")
                            
                            session_id = f"sess_{int(time.time())}"
                            
                            log_msg("🧠 Calling Solver...", "AI")
                            ai_success = await solve_captcha(page, session_id, logger=lambda m: log_msg(m, "SOLVER"))
                            
                            if not ai_success:
                                log_msg("⚠️ Solver Returned False", "AI")
                                await browser.close(); return "failed" 
                            
                            log_msg("⏳ Verifying Solution (5s)...", "WAIT")
                            await asyncio.sleep(5)
                            
                            if await page.get_by_text("swap 2 tiles", exact=False).count() == 0:
                                log_msg("✅ CAPTCHA SOLVED!", "SUCCESS")
                                await browser.close(); return "success"
                            else:
                                log_msg("🔁 Captcha still visible. Retrying...", "RETRY")
                                continue
                        
                        if await page.get_by_text("sent", exact=False).count() > 0:
                            log_msg("✅ CODE SENT (Direct Success)!", "SUCCESS")
                            await browser.close(); return "success"
                        
                        log_msg("❌ No Captcha & No Success Message Found.", "ERROR")
                        await browser.close(); return "failed"

                else:
                    log_msg("❌ Get Code Button Missing", "ERROR")
                    return "failed"

            await browser.close(); return "failed"

    except Exception as e:
        log_msg(f"❌ Session Exception: {str(e)}", "ERROR")
        return "failed"
    except: return "failed"

# --- LIFECYCLE ---
@app.on_event("startup")
async def startup_event():
    log_msg("🚀 App Startup: Initiating Master Loop...", "INIT")
    asyncio.create_task(master_loop())

@app.get("/")
def read_root(): return {"status": "Running", "mode": "MongoDB Auto-Pilot (API Redeploy)"}