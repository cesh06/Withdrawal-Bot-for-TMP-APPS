import asyncio
import os
import sys
import time
from datetime import datetime
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

TMP_LOGIN_URL = "https://tmpjob.net/login"
NETWORK_LATENCY_MS = 300  # fire early so the request lands at 09:00:00

# ─────────────────────────────────────────────
# Retry helper
# ─────────────────────────────────────────────
async def with_retry(fn, retries=3, delay=2, label="operation"):
    for attempt in range(1, retries + 1):
        try:
            return await fn()
        except Exception as e:
            print(f"⚠️  [{label}] Attempt {attempt}/{retries} failed: {e}")
            if attempt < retries:
                print(f"🔄  [{label}] Retrying in {delay}s...")
                await asyncio.sleep(delay)
    raise RuntimeError(f"[{label}] All {retries} attempts exhausted.")

# ─────────────────────────────────────────────
# Master gate: ENABLE_BOT + RUN_DATE
# ─────────────────────────────────────────────
def check_master_gate():
    print("📋 [Gate] Checking ENABLE_BOT and RUN_DATE...")

    # 1. Kill switch
    enable = os.getenv("ENABLE_BOT", "TRUE").strip().upper()
    if enable != "TRUE":
        print(f"🛑 [Gate] ENABLE_BOT='{enable}' — bot is OFF. Exiting.")
        sys.exit(0)
    print(f"✅ [Gate] ENABLE_BOT=TRUE — bot is ON.")

    # 2. Date check – skip if this is a manual run
    manual_run = os.getenv("MANUAL_RUN", "FALSE").strip().upper() == "TRUE"
    if manual_run:
        print("⚡ [Gate] Manual run detected — skipping date check.")
        return

    run_date_str = os.getenv("RUN_DATE", "").strip()
    today_str = datetime.now().strftime("%Y-%m-%d")
    if not run_date_str:
        print("🛑 [Gate] RUN_DATE variable is not set. Exiting.")
        sys.exit(0)
    if run_date_str != today_str:
        print(f"📅 [Gate] Today is {today_str}, but RUN_DATE='{run_date_str}'. Not our day. Exiting.")
        sys.exit(0)
    print(f"✅ [Gate] Date matched: {today_str} == RUN_DATE. Proceeding.")

# ─────────────────────────────────────────────
# Load accounts from SECRET_<phone> variables
# ─────────────────────────────────────────────
def load_accounts_from_env():
    print("📋 [Config] Loading account configurations...")
    configs = []
    for key, value in os.environ.items():
        if not key.startswith("SECRET_"):
            continue
        parts = value.split(":")
        if len(parts) != 3:
            print(f"⚠️  Skipping {key}: invalid format (expected phone:login:pin).")
            continue
        phone, login, pin = (p.strip() for p in parts)

        amount = os.getenv(f"AMOUNT_{phone}", "").strip()
        if not amount:
            print(f"⚠️  [Config] AMOUNT_{phone} not set — using default 160000 RWF.")
            amount = "160000"

        configs.append({
            "phone": phone,
            "login": login,
            "pin":   pin,
            "amount": amount,
            "label": phone[-4:],
        })
        print(f"✔️  [Config] Account ...{phone[-4:]} loaded (withdraw: {int(amount):,} RWF)")

    if not configs:
        print("🛑 [Config] No SECRET_* variables found. Exiting.")
        sys.exit(0)
    return configs

# ─────────────────────────────────────────────
# Safe click (for preparation steps)
# ─────────────────────────────────────────────
async def safe_click(page_or_frame, selectors, timeout=8000):
    if isinstance(selectors, str):
        selectors = [selectors]
    for selector in selectors:
        try:
            await page_or_frame.wait_for_selector(selector, state="visible", timeout=timeout)
            await page_or_frame.click(selector, force=True)
            return True
        except PlaywrightTimeout:
            continue
    return False

# ─────────────────────────────────────────────
# Ultra‑fast click for fire time – no wait
# ─────────────────────────────────────────────
async def direct_click(frame, selector):
    try:
        await frame.click(selector, force=True, timeout=100)
        return True
    except Exception:
        return False

# ─────────────────────────────────────────────
# Parallel frame searcher (faster than sequential)
# ─────────────────────────────────────────────
async def find_active_frame(page, selector, timeout=5000, index=0):
    candidates = [page] + list(page.frames)
    # Use asyncio.gather with return_exceptions to check all at once
    results = await asyncio.gather(
        *[frame.wait_for_selector(selector, state="visible", timeout=timeout)
          for frame in candidates],
        return_exceptions=True
    )
    for frame, result in zip(candidates, results):
        if not isinstance(result, Exception):
            return frame
    print(f"⚠️  [Acc {index}] '{selector}' not found in any frame.")
    return None

# ─────────────────────────────────────────────
# Wait until 09:00:00 CAT with latency offset
# ─────────────────────────────────────────────
async def wait_until_sharp_9am():
    now = datetime.now()
    target = now.replace(hour=9, minute=0, second=0, microsecond=0)
    offset = (target - now).total_seconds() - (NETWORK_LATENCY_MS / 1000)

    if offset > 0:
        print(f"\n⏱️  [{now.strftime('%H:%M:%S')}] Waiting {offset:.3f}s "
              f"(firing {NETWORK_LATENCY_MS}ms early)...")
        if offset > 0.5:
            await asyncio.sleep(offset - 0.5)
        fire_at = time.perf_counter() + (target.timestamp() - time.time()) - (NETWORK_LATENCY_MS / 1000)
        while time.perf_counter() < fire_at:
            await asyncio.sleep(0)
        print(f"🎯 [{datetime.now().strftime('%H:%M:%S.%f')}] Fire threshold reached!")
    else:
        print(f"\n⚠️  [{now.strftime('%H:%M:%S')}] Already past 09:00 — firing immediately.")

# ─────────────────────────────────────────────
# Smart balance scraper (fix: get sibling of label)
# ─────────────────────────────────────────────
async def scrape_available_balance(form_frame, index):
    try:
        label = await form_frame.wait_for_selector("text=Amafaranga asigaye", timeout=3000)
        # Get the parent element that holds the balance value
        parent = await label.locator("xpath=..")
        # Find the element containing digits inside that parent
        balance_elem = await parent.locator("text=/\\d{3,}/").first
        if balance_elem:
            raw = await balance_elem.inner_text()
            numeric = "".join(c for c in raw if c.isdigit())
            if numeric:
                print(f"💳 [Acc {index}] Live balance: {int(numeric):,} RWF")
                return numeric
    except Exception:
        pass

    # Fallback selectors
    for sel in [".balance", "[class*='balance']", "text=/\\d{3,}/"]:
        try:
            el = await form_frame.wait_for_selector(sel, timeout=2000)
            raw = await el.inner_text()
            numeric = "".join(c for c in raw if c.isdigit())
            if numeric:
                print(f"💳 [Acc {index}] Live balance: {int(numeric):,} RWF")
                return numeric
        except Exception:
            continue
    print(f"⚠️  [Acc {index}] Could not scrape live balance — using configured amount.")
    return None

def resolve_withdrawal_amount(live, configured, index):
    if live is None:
        print(f"📌 [Acc {index}] Fallback: withdrawing {int(configured):,} RWF")
        return configured
    live_int = int(live)
    configured_int = int(configured)
    if live_int < configured_int:
        print(f"⚡ [Acc {index}] Balance ({live_int:,}) < configured ({configured_int:,}). Withdrawing full balance.")
        return str(live_int)
    print(f"✅ [Acc {index}] Balance OK ({live_int:,} ≥ {configured_int:,}). Using configured amount.")
    return configured

# ─────────────────────────────────────────────
# Prepare a single account – log in, navigate,
# fill amount + PIN, and park at submit button
# ─────────────────────────────────────────────
async def prepare_account_for_withdrawal(browser, acc, index):
    print(f"\n🚀 [Acc {index}] Staging ...{acc['label']}...")

    context = await browser.new_context(
        viewport={"width": 412, "height": 915},
        is_mobile=True,
        user_agent=(
            "Mozilla/5.0 (Linux; Android 13; Pixel 7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/116.0.0.0 Mobile Safari/537.36"
        ),
    )
    page = await context.new_page()

    try:
        # Step 1 – load login
        print(f"🌐 [Acc {index}] Loading {TMP_LOGIN_URL}...")
        await with_retry(
            lambda: page.goto(TMP_LOGIN_URL, wait_until="domcontentloaded", timeout=60000),
            retries=3, delay=3, label=f"Acc{index}/goto"
        )

        # Step 2 – fill credentials
        print(f"✍️  [Acc {index}] Finding login inputs...")
        main = page
        try:
            await page.wait_for_selector("input", timeout=8000)
        except Exception:
            print(f"📦 [Acc {index}] No direct input — switching to iframe...")
            iframe_el = await page.wait_for_selector("iframe", timeout=12000)
            main = await iframe_el.content_frame()

        await main.locator("input").nth(0).fill(acc["phone"])
        await main.locator("input").nth(1).fill(acc["login"])
        await safe_click(main, ["button", ".login-button", "button:has-text('Injira')"])
        print(f"✅ [Acc {index}] Credentials submitted.")

        # Step 3 – wait for dashboard
        print(f"⏳ [Acc {index}] Waiting 6 s for dashboard...")
        await asyncio.sleep(6)

        # Step 4 – dismiss popup
        print(f"🔍 [Acc {index}] Scanning for popup...")
        for frame in [page] + list(page.frames):
            closed = await safe_click(
                frame,
                ["text=Gufunga", "button:has-text('Gufunga')", "button:has-text('X')",
                 ".close", "[aria-label='Close']"],
                timeout=2000,
            )
            if closed:
                print(f"🔒 [Acc {index}] Popup dismissed.")
                break
        else:
            print(f"ℹ️  [Acc {index}] No popup detected.")

        # Step 5 – navigate to 'Uwanjye'
        print(f"🧭 [Acc {index}] Clicking 'Uwanjye'...")
        uwanjye_frame = await find_active_frame(page, "text=Uwanjye", timeout=10000, index=index)
        if not uwanjye_frame:
            raise Exception("'Uwanjye' tab not found.")
        await safe_click(uwanjye_frame, ["text=Uwanjye", ".bottom-nav > a:nth-child(5)"], timeout=8000)
        await asyncio.sleep(4)
        print(f"✅ [Acc {index}] 'Uwanjye' loaded.")

        # Step 6 – click 'Kuramo'
        print(f"💰 [Acc {index}] Clicking 'Kuramo'...")
        kuramo_frame = await find_active_frame(page, "text=Kuramo", timeout=12000, index=index)
        if not kuramo_frame:
            raise Exception("'Kuramo' button not found.")
        await safe_click(kuramo_frame, ["text=Kuramo", "button:has-text('Kuramo')"], timeout=8000)
        print(f"✅ [Acc {index}] Withdrawal screen loading...")

        # Step 7 – wait for 100% loader
        print(f"⏳ [Acc {index}] Waiting for 100% loader...")
        try:
            await page.wait_for_selector("text=100%", timeout=20000)
            print(f"💯 [Acc {index}] Loader done. Settling 1.5 s...")
            await asyncio.sleep(1.5)
        except Exception:
            print(f"⚠️  [Acc {index}] 100% text not seen — proceeding.")

        # Step 8 – resolve form frame
        print(f"🔎 [Acc {index}] Locating form frame...")
        form_frame = await find_active_frame(page, "text=Amafaranga asigaye", timeout=8000, index=index)
        if not form_frame:
            form_frame = await find_active_frame(page, "input[type='password']", timeout=8000, index=index)
        if not form_frame:
            print(f"⚠️  [Acc {index}] Form frame not isolated — using kuramo_frame.")
            form_frame = kuramo_frame

        # Step 9 – resolve amount
        live_balance = await scrape_available_balance(form_frame, index)
        final_amount = resolve_withdrawal_amount(live_balance, acc["amount"], index)
        formatted = f"{int(final_amount):,}"

        # Step 10 – select amount grid
        print(f"🔘 [Acc {index}] Selecting amount: {formatted} RWF...")
        grid_selectors = [
            f"text={formatted}",
            f"button:has-text('{formatted}')",
            f"text={final_amount}",
            f"button:has-text('{final_amount}')",
        ]
        grid_frame = await find_active_frame(page, f"text={formatted}", timeout=4000, index=index)
        if not grid_frame:
            grid_frame = await find_active_frame(page, f"text={final_amount}", timeout=2000, index=index)

        if grid_frame:
            clicked = await safe_click(grid_frame, grid_selectors, timeout=4000)
            if clicked:
                print(f"✅ [Acc {index}] Grid amount selected.")
            else:
                print(f"⚠️  [Acc {index}] Grid click failed.")
        else:
            print(f"⚠️  [Acc {index}] Grid not found — fallback to text input...")
            try:
                await form_frame.locator("input[type='text']").fill(final_amount)
                print(f"✅ [Acc {index}] Amount entered via text input.")
            except Exception as fe:
                print(f"⚠️  [Acc {index}] Text input also failed: {fe}")

        # Step 11 – enter PIN
        print(f"🔑 [Acc {index}] Entering PIN...")
        pin_frame = await find_active_frame(page, "input[type='password']", timeout=8000, index=index)
        if not pin_frame:
            raise Exception("PIN field not found.")
        await pin_frame.locator("input[type='password']").fill(acc["pin"])
        print(f"✅ [Acc {index}] PIN entered.")

        # Step 12 – pre‑resolve submit frame
        print(f"🔎 [Acc {index}] Pre‑resolving 'Saba kubikuza' frame...")
        submit_frame = await find_active_frame(page, "text=Saba kubikuza", timeout=8000, index=index)
        if not submit_frame:
            print(f"⚠️  [Acc {index}] Submit frame not found — will use pin_frame.")
            submit_frame = pin_frame

        print(f"📌 [Acc {index}] FULLY STAGED — waiting for 09:00:00.")
        return submit_frame, context, page

    except Exception as e:
        print(f"❌ [Acc {index}] Fatal preparation error: {e}")
        await page.screenshot(path=f"error_{acc['label']}.png")
        await context.close()
        return None, None, None

# ─────────────────────────────────────────────
# Main orchestrator
# ─────────────────────────────────────────────
async def main():
    print("=" * 60)
    print("⚡  Precision Withdrawal Bot")
    print(f"🕐  Current time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (CAT)")
    print("=" * 60)

    check_master_gate()
    account_configs = load_accounts_from_env()

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )

        print(f"\n⚙️  Preparing {len(account_configs)} account(s) in parallel...")
        results = await asyncio.gather(*[
            prepare_account_for_withdrawal(browser, cfg, idx + 1)
            for idx, cfg in enumerate(account_configs)
        ])

        # Build ready list with context
        ready = [(sf, ctx, pg) for sf, ctx, pg in results if sf is not None]

        if len(ready) < len(account_configs):
            failed = len(account_configs) - len(ready)
            print(f"\n🛑 {failed} account(s) failed. Aborting.")
            await browser.close()
            return

        print(f"\n✅ All {len(ready)} account(s) staged.")

        # Wait for 09:00:00 CAT
        await wait_until_sharp_9am()

        # ── Start Nibyo polling early ──
        print(f"\n⚡ [{datetime.now().strftime('%H:%M:%S.%f')}] Starting Nibyo polling...")

        async def confirm_nibyo(pg, acc_idx):
            deadline = time.time() + 5.0
            while time.time() < deadline:
                for frame in [pg] + list(pg.frames):
                    try:
                        await frame.wait_for_selector("text=Nibyo", state="visible", timeout=150)
                        await frame.click("text=Nibyo", force=True)
                        print(f"🔒 [{datetime.now().strftime('%H:%M:%S.%f')}] [Acc {acc_idx}] 'Nibyo' confirmed!")
                        return True
                    except Exception:
                        continue
                await asyncio.sleep(0.02)
            print(f"⚠️  [Acc {acc_idx}] 'Nibyo' not found within 5 s.")
            return False

        nibyo_tasks = [asyncio.create_task(confirm_nibyo(pg, idx + 1))
                       for idx, (_, _, pg) in enumerate(ready)]

        # ── Fire submit with direct click ──
        print(f"🔥 [{datetime.now().strftime('%H:%M:%S.%f')}] Dispatching 'Saba kubikuza'...")

        async def fire_submit(submit_frame, acc_idx):
            result = await direct_click(submit_frame, "text=Saba kubikuza")
            ts = datetime.now().strftime("%H:%M:%S.%f")
            if result:
                print(f"🚀 [{ts}] [Acc {acc_idx}] Submit fired!")
            else:
                print(f"⚠️  [{ts}] [Acc {acc_idx}] Direct click failed — fallback...")
                result = await safe_click(submit_frame,
                                         ["text=Saba kubikuza", "button:has-text('Saba kubikuza')"],
                                         timeout=1000)
                if result:
                    print(f"🚀 [{ts}] [Acc {acc_idx}] Submit fired (fallback).")
                else:
                    print(f"❌ [{ts}] [Acc {acc_idx}] Submit failed.")

        fire_tasks = [asyncio.create_task(fire_submit(sf, idx + 1))
                      for idx, (sf, _, _) in enumerate(ready)]

        # Wait for fires and then confirmations
        await asyncio.gather(*fire_tasks)
        await asyncio.gather(*nibyo_tasks)

        # ── Receipts ──
        print("\n📸 Capturing receipts...")
        await asyncio.sleep(4)
        for i, (_, _, pg) in enumerate(ready):
            filename = f"receipt_acc_{i+1}.png"
            await pg.screenshot(path=filename)
            print(f"💾 Saved: {filename}")

        # ── Clean up contexts ──
        for _, ctx, _ in ready:
            await ctx.close()
        await browser.close()
        print("\n🏁 Pipeline completed.")

if __name__ == "__main__":
    asyncio.run(main())
