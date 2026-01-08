import os
import glob
import asyncio
import random
import time
import shutil
import requests
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from playwright.async_api import async_playwright
from motor.motor_asyncio import AsyncIOMotorClient

# --- 🔥 ENV CONFIGURATION 🔥 ---
RAILWAY_TOKEN = os.environ.get("RAILWAY_TOKEN", "YOUR_RAILWAY_TOKEN_HERE")
RAILWAY_PROJECT_ID = os.environ.get("RAILWAY_PROJECT_ID", "YOUR_PROJECT_ID_HERE")
RAILWAY_SERVICE_ID = os.environ.get("RAILWAY_SERVICE_ID", "YOUR_SERVICE_ID_HERE") 

# MongoDB Config
MONGO_URI = "mongodb://mongo:AEvrikOWlrmJCQrDTQgfGtqLlwhwLuAA@crossover.proxy.rlwy.net:29609"
DB_NAME = "number_manager"
COL_PENDING = "phone_numbers"      # Main List
COL_SUCCESS = "success_numbers"    # Moved Here
COL_FAILED = "failed_numbers"      # Moved Here

# --- APP SETUP ---
app = FastAPI()
CAPTURE_DIR = "./captures"
if not os.path.exists(CAPTURE_DIR): os.makedirs(CAPTURE_DIR)
app.mount("/captures", StaticFiles(directory=CAPTURE_DIR), name="captures")

try:
    from captcha_solver import solve_captcha
except ImportError:
    async def solve_captcha(page, session_id, logger=print): return False

SETTINGS = {"country": "Russia"} 

# --- DATABASE HELPERS (MOVE LOGIC) ---
async def get_next_number_from_db():
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]
    col = db[COL_PENDING]
    # Get one number (FIFO)
    doc = await col.find_one({}) 
    return doc

async def move_number_to_collection(phone, status):
    """Moves number from Pending -> Success/Failed Collection"""
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]
    
    target_col_name = COL_SUCCESS if status == "success" else COL_FAILED
    target_col = db[target_col_name]
    source_col = db[COL_PENDING]
    
    # 1. Insert into new collection
    await target_col.insert_one({
        "phone": phone,
        "status": status,
        "timestamp": datetime.now()
    })
    
    # 2. Delete from main list
    await source_col.delete_one({"phone": phone})
    print(f"📦 Moved {phone} to {target_col_name}")

# --- RAILWAY REDEPLOY ---
def trigger_redeploy():
    print("🔄 Triggering Railway Redeploy for New IP...")
    # Clean exit lets Railway restart the service automatically if configured properly.
    # For forcing a full rebuild/redeploy to rotate IP, use API.
    # Assuming simple restart works for IP rotation on Railway:
    os._exit(0) 

# --- LOGGING ---
def log_msg(message, level="step"):
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}")

# --- VISUALS ---
async def capture_step(page, step_name, wait_time=0, force=False):
    if wait_time > 0: await asyncio.sleep(wait_time)
    timestamp = datetime.now().strftime("%H%M%S")
    filename = f"{CAPTURE_DIR}/{timestamp}_{step_name}.jpg"
    try: await page.screenshot(path=filename)
    except: pass

async def show_red_dot(page, x, y):
    try:
        await page.evaluate(f"""
            var dot = document.createElement('div');
            dot.style.position = 'absolute'; 
            dot.style.left = '{x-15}px'; dot.style.top = '{y-15}px';
            dot.style.width = '30px'; dot.style.height = '30px'; 
            dot.style.background = 'rgba(255, 0, 0, 0.7)'; 
            dot.style.borderRadius = '50%'; dot.style.zIndex = '999999'; 
            dot.style.pointerEvents = 'none'; dot.style.border = '3px solid white'; 
            document.body.appendChild(dot);
            setTimeout(() => {{ dot.remove(); }}, 1500);
        """)
    except: pass

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
                log_msg(f"🖱️ Tapping {name}...")
                await show_red_dot(page, cx, cy)
                await asyncio.sleep(0.3)
                await page.touchscreen.tap(cx, cy)
                return True
        return False
    except: return False

async def smart_action(page, finder, verifier, step_name, wait_after=5):
    log_msg(f"🔍 Action: {step_name}...")
    await capture_step(page, f"Pre_{step_name}")

    for attempt in range(1, 4):
        if step_name != "Register_Text":
            if verifier and await verifier().count() > 0:
                log_msg(f"✅ {step_name} Already Done.")
                return True

        clicked = await click_element(page, finder, f"{step_name} (Try {attempt})")
        
        if clicked:
            await capture_step(page, f"Click_{step_name}_{attempt}")
            log_msg(f"⏳ Waiting {wait_after}s...")
            await asyncio.sleep(wait_after)
            
            await capture_step(page, f"Post_{step_name}_{attempt}")

            if verifier and await verifier().count() > 0:
                log_msg(f"✅ {step_name} Success!")
                return True
            elif await finder().count() > 0:
                log_msg(f"⚠️ {step_name} click failed. Retrying...")
                continue
            else:
                log_msg(f"⏳ Loading... Waiting 5s...")
                await asyncio.sleep(5)
                if verifier and await verifier().count() > 0:
                    log_msg(f"✅ {step_name} Success (After Load)!")
                    return True
                else:
                    log_msg(f"⚠️ Stuck / Loading...")
                    await capture_step(page, f"Stuck_{step_name}")
        else:
            log_msg(f"❌ {step_name} Not Found (Attempt {attempt})")
            await asyncio.sleep(2)

    return False

# --- MAIN WORKER ---
async def master_loop():
    log_msg("🟢 Auto-Bot Started (MongoDB Move Mode).")
    
    # 1. Fetch Number
    db_doc = await get_next_number_from_db()
    if not db_doc:
        log_msg("ℹ️ No numbers left in Main DB. Sleeping...")
        await asyncio.sleep(300) 
        return # Or keep sleeping

    current_number = db_doc['phone']
    log_msg(f"🔵 Processing: {current_number}")

    # 2. Run Session
    try:
        res = await run_session(current_number, SETTINGS["country"])
        
        # 3. Move Number based on Result
        if res == "success":
            log_msg("🎉 Verified! Moving to Success DB...")
            await move_number_to_collection(current_number, "success")
        else:
            log_msg("❌ Failed. Moving to Failed DB...")
            await move_number_to_collection(current_number, "failed")
            
    except Exception as e:
        log_msg(f"🔥 Crash: {e}")
        await move_number_to_collection(current_number, "failed")

    # 4. RESTART FOR NEW IP
    log_msg("🔄 Restarting Container...")
    trigger_redeploy()

async def run_session(phone, country):
    try:
        async with async_playwright() as p:
            launch_args = {
                "headless": True, 
                "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox", "--ignore-certificate-errors", "--disable-web-security"]
            }
            # No proxy arg (Using Direct IP)

            log_msg("🚀 Launching Browser...")
            try: browser = await p.chromium.launch(**launch_args)
            except Exception as e: log_msg(f"❌ Launch Fail: {e}"); return "failed"

            pixel_5 = p.devices['Pixel 5'].copy()
            pixel_5['viewport'] = {'width': 412, 'height': 950}
            pixel_5['has_touch'] = True 
            
            context = await browser.new_context(**pixel_5, locale="en-US", ignore_https_errors=True)
            page = await context.new_page()

            # URL
            log_msg("🌐 Opening URL...")
            try:
                await page.goto(BASE_URL, timeout=90000)
                log_msg("⏳ Page Load Wait (5s)...")
                await asyncio.sleep(5) 
                await capture_step(page, "01_Loaded")
            except: return "failed"

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
            log_msg(f"🌍 Selecting {country}...")
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
            await capture_step(page, "04_Country_Typed")
            
            matches = page.get_by_text(country, exact=False)
            if await matches.count() > 0:
                await click_element(page, lambda: matches.first, f"Country: {country}")
                await asyncio.sleep(3) 
            else:
                log_msg("❌ Country Not Found"); await browser.close(); return "failed"

            # INPUT PHONE
            inp = page.locator("input[type='tel']").first
            if await inp.count() == 0: inp = page.locator("input").first
            
            if await inp.count() > 0:
                # CLEAN NUMBER
                clean_phone = phone
                if country == "Russia" and clean_phone.startswith("7"): clean_phone = clean_phone[1:]
                elif country == "Pakistan" and clean_phone.startswith("92"): clean_phone = clean_phone[2:]
                
                log_msg(f"🔢 Inputting: {clean_phone}")
                await inp.click()
                for c in clean_phone:
                    await page.keyboard.type(c); await asyncio.sleep(0.05)
                
                await show_red_dot(page, 350, 100)
                await page.touchscreen.tap(350, 100) 
                await capture_step(page, "05_Filled")
                
                # GET CODE
                get_code = page.locator(".get-code-btn").or_(page.get_by_text("Get code"))
                if await get_code.count() > 0:
                    await click_element(page, lambda: get_code.first, "Get Code Button")
                    
                    log_msg("⏳ Hard Wait: 10s for Captcha...")
                    await asyncio.sleep(5); await capture_step(page, "06_Wait_5s_Check")
                    await asyncio.sleep(5); await capture_step(page, "07_Wait_10s_Check")

                    # Error Popup
                    if await page.get_by_text("An unexpected problem", exact=False).count() > 0:
                        log_msg("⛔ FATAL: System Error")
                        await capture_step(page, "Error_Popup", force=True)
                        await browser.close(); return "failed"

                    # Captcha Logic
                    start_solve_time = time.time()
                    while True: # Run until solved or timeout
                        if time.time() - start_solve_time > 120: break

                        if await page.get_by_text("swap 2 tiles", exact=False).count() > 0:
                            log_msg("🧩 CAPTCHA FOUND!")
                            await capture_step(page, "08_Captcha_Found", force=True)
                            
                            session_id = f"sess_{int(time.time())}"
                            ai_success = await solve_captcha(page, session_id, logger=lambda m: log_msg(m, level="step"))
                            
                            if not ai_success:
                                log_msg("⚠️ Solver Failed")
                                await browser.close(); return "failed" 
                            
                            await asyncio.sleep(5)
                            
                            if await page.get_by_text("swap 2 tiles", exact=False).count() == 0:
                                log_msg("✅ CAPTCHA SOLVED!")
                                await capture_step(page, "Success_Solved", force=True)
                                await browser.close(); return "success"
                            else:
                                log_msg("🔁 Captcha still there...")
                                continue
                        
                        if await page.get_by_text("sent", exact=False).count() > 0:
                            log_msg("✅ CODE SENT (Direct)!")
                            await capture_step(page, "Success_Direct", force=True)
                            await browser.close(); return "success"
                        
                        log_msg("❌ No Captcha & No Success.")
                        await capture_step(page, "Error_Nothing", force=True)
                        await browser.close(); return "failed"

                else:
                    log_msg("❌ Get Code Missing"); return "failed"

            await browser.close(); return "failed"

    except Exception as e:
        log_msg(f"❌ Error: {str(e)}"); return "failed"
    except: return "failed"

# --- LIFECYCLE ---
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(master_loop())

@app.get("/")
def read_root(): return {"status": "Running", "mode": "MongoDB Auto-Pilot"}