#!/usr/bin/env python3
"""
Claude Player - lets Claude watch your Android phone screen and play a
Pixel-Flow-style puzzle game by tapping through ADB.

Loop:  screencap (adb) -> Claude vision -> structured tap decision -> adb tap
Live dashboard (screenshot + Claude's reasoning) at http://localhost:8765
"""
import argparse
import base64
import hashlib
import io
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anthropic
from PIL import Image, ImageDraw

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
SEND_WIDTH = 540  # screenshots are downscaled to this width before sending

SYSTEM_PROMPT = """You are playing a mobile puzzle game (Pixel Flow style) by looking at screenshots and tapping.

GAME RULES (as understood):
- The big framed picture is made of colored pixel blocks. Colored "pig" shooters travel along the
  conveyor belt around the picture and shoot blocks of THEIR OWN color on the nearest edge.
- Each pig has a number = its ammo (blocks it can destroy). Pigs at the bottom of the screen are
  waiting in a grid. Tap a pig that is NOT covered by another pig (front/bottom-most of its column,
  fully visible) to send it onto the belt.
- The belt holds a limited number of pigs (shown as e.g. 5/5 and the empty slots above the pig grid).
  If the slots are full you must wait: pigs on the belt are working.
- STRATEGY: look at the outer edge of the picture, which is what pigs hit first. Pick pigs whose
  color matches the blocks currently exposed on the outer layers. Prefer pigs with enough ammo to
  clear a whole color region. Don't waste slots on colors that have no exposed blocks. Never fill
  the slots with colors that cannot shoot anything yet.
- The level is won when the whole picture is cleared. Keys/special blocks are a bonus.

SCREEN HANDLING:
- Main menu / level map with a big yellow "Play" button: tap Play.
- Win / "Level complete" / "Continue" / "Next" / "Tap to continue" screens: tap the continue button.
- Fail / out of space screen: tap the free retry/try again/close option, NEVER pay.
- Popups: close them with an X or "No thanks"/"Close". Never watch ads on purpose.
- NEVER tap anything that costs coins or real money: the red/green/orange "+" buttons, the
  power-up buttons at the bottom of the game screen, "No Ads", shop, gift/bonus offers.

OUTPUT: always answer by calling the `act` tool. Coordinates are in pixels of the image you are shown
(origin top-left). Tap the CENTER of the target. If nothing useful can be done right now (pigs on the
belt are still shooting, animation playing), use action "wait"."""

TOOL = {
    "name": "act",
    "description": "Report what you see and choose the next action.",
    "input_schema": {
        "type": "object",
        "properties": {
            "screen_state": {
                "type": "string",
                "enum": ["menu", "gameplay", "popup", "win", "fail", "loading", "unknown"],
            },
            "observation": {
                "type": "string",
                "description": "Short: belt slots free?, exposed picture colors, which pigs are tappable.",
            },
            "reasoning": {"type": "string", "description": "Why this action, 1-3 sentences."},
            "action": {"type": "string", "enum": ["tap", "wait"]},
            "x": {"type": "integer", "description": "Tap x in image pixels (tap only)"},
            "y": {"type": "integer", "description": "Tap y in image pixels (tap only)"},
        },
        "required": ["screen_state", "observation", "reasoning", "action"],
    },
}

STATE = {"frame": b"", "log": [], "status": "starting"}
LOCK = threading.Lock()


# ---------------------------------------------------------------- ADB helpers
class Phone:
    def __init__(self, serial=None):
        self.base = ["adb"] + (["-s", serial] if serial else [])

    def screenshot(self) -> Image.Image:
        raw = subprocess.run(self.base + ["exec-out", "screencap", "-p"],
                             capture_output=True, check=True).stdout
        return Image.open(io.BytesIO(raw)).convert("RGB")

    def tap(self, x, y):
        subprocess.run(self.base + ["shell", "input", "tap", str(int(x)), str(int(y))], check=True)


# ---------------------------------------------------------------- dashboard
HTML = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Claude Player</title>
<body style="margin:0;background:#111;color:#eee;font-family:system-ui;display:flex;gap:16px;padding:12px">
<img id=f style="height:96vh;border-radius:12px">
<div style="flex:1;overflow:auto;height:96vh"><h2 id=s></h2><div id=l></div></div>
<script>
async function t(){try{
 const j=await (await fetch('/state.json')).json();
 document.getElementById('s').textContent=j.status;
 document.getElementById('l').innerHTML=j.log.slice().reverse().map(e=>
  `<p><b>#${e.n} ${e.screen_state} -> ${e.action} ${e.x??''},${e.y??''}</b><br>
   <i>${e.observation}</i><br>${e.reasoning}</p>`).join('');
 document.getElementById('f').src='/frame.jpg?'+Date.now();
}catch(e){} setTimeout(t,700)} t();
</script></body>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.startswith("/frame.jpg"):
            with LOCK:
                body, ctype = STATE["frame"], "image/jpeg"
        elif self.path.startswith("/state.json"):
            with LOCK:
                body = json.dumps({"status": STATE["status"], "log": STATE["log"][-30:]}).encode()
            ctype = "application/json"
        else:
            body, ctype = HTML.encode(), "text/html"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body)


def publish(img: Image.Image, tap=None, status=None, entry=None):
    img = img.copy()
    if tap:
        d = ImageDraw.Draw(img)
        r = 18
        d.ellipse([tap[0] - r, tap[1] - r, tap[0] + r, tap[1] + r], outline="red", width=4)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    with LOCK:
        STATE["frame"] = buf.getvalue()
        if status:
            STATE["status"] = status
        if entry:
            STATE["log"].append(entry)


# ---------------------------------------------------------------- brain
def ask_claude(client, img, history, stuck):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()
    note = ""
    if history:
        note += "Your last actions (oldest first):\n" + "\n".join(history[-6:]) + "\n"
    if stuck:
        note += ("WARNING: the screen did not change after your last tap. That tap probably missed, "
                 "hit a non-tappable pig, or the belt is full. Try something different or wait.\n")
    note += f"Image size: {img.width}x{img.height}. Decide the next action."
    resp = client.messages.create(
        model=MODEL,
        max_tokens=800,
        system=SYSTEM_PROMPT,
        tools=[TOOL],
        tool_choice={"type": "tool", "name": "act"},
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
            {"type": "text", "text": note},
        ]}],
    )
    for block in resp.content:
        if block.type == "tool_use":
            return block.input
    return {"screen_state": "unknown", "observation": "", "reasoning": "no tool call", "action": "wait"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", help="adb device serial (if several devices)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--delay", type=float, default=1.0, help="seconds to wait after each tap")
    ap.add_argument("--max-steps", type=int, default=0, help="0 = unlimited")
    ap.add_argument("--dry-run", action="store_true", help="think but never tap")
    args = ap.parse_args()

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    phone = Phone(args.serial)
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"Dashboard: http://localhost:{args.port}   model: {MODEL}")

    history, last_hash, n = [], None, 0
    while args.max_steps == 0 or n < args.max_steps:
        n += 1
        full = phone.screenshot()
        scale = full.width / SEND_WIDTH
        small = full.resize((SEND_WIDTH, round(full.height / scale)))
        h = hashlib.md5(small.resize((54, 120)).tobytes()).hexdigest()
        stuck = h == last_hash
        last_hash = h

        publish(small, status="Claude is thinking...")
        try:
            d = ask_claude(client, small, history, stuck)
        except Exception as e:  # network / rate limit
            publish(small, status=f"API error: {e}")
            time.sleep(3)
            continue

        entry = {"n": n, **{k: d.get(k) for k in
                 ("screen_state", "observation", "reasoning", "action", "x", "y")}}
        tap = None
        if d["action"] == "tap" and d.get("x") is not None and d.get("y") is not None:
            x, y = int(d["x"]), int(d["y"])
            frac_y = y / small.height
            # Safety rail: top bar (hearts/coins/shop "+") and bottom power-ups/nav cost money.
            in_menu = d["screen_state"] in ("menu", "win", "fail", "popup")
            blocked = frac_y < 0.09 or (frac_y > 0.90 and not in_menu) or (frac_y > 0.93)
            if blocked:
                entry["reasoning"] += "  [BLOCKED by safety rail]"
                d["action"] = "wait"
            else:
                tap = (x, y)
                if not args.dry_run:
                    phone.tap(x * scale, y * scale)
        history.append(f"#{n} {d['screen_state']}: {d['action']} {d.get('x','')},{d.get('y','')} - {d['reasoning'][:90]}")
        publish(small, tap=tap, status=f"Step {n}: {d['action']}", entry=entry)
        print(f"#{n} [{d['screen_state']}] {d['action']} {tap or ''} | {d['reasoning']}")
        time.sleep(args.delay if tap else max(args.delay, 1.5))


if __name__ == "__main__":
    main()
