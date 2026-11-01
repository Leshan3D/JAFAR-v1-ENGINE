"""
TELEGRAM ALERT DELIVERY
Sends messages via the Bot API and enforces per-key cooldowns so the
service can't spam the same alert twice inside a window.

State lives in $DATA_DIR/alert_state.json (on Railway: the mounted volume).
No token configured → send() is a no-op that logs, so the pipeline runs
fine locally without Telegram.
"""

import json
import os
import time

import requests

DATA_DIR   = os.environ.get("DATA_DIR") or os.getcwd()
STATE_FILE = os.path.join(DATA_DIR, "alert_state.json")
API        = "https://api.telegram.org/bot{token}"

_state = None


def _load():
    global _state
    if _state is None:
        try:
            with open(STATE_FILE) as f:
                _state = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _state = {}
    return _state


def _save():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(_state, f, indent=2)
    except OSError as e:
        print(f"  [alerts] WARN — cannot persist state: {e}")


def token():
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def redact(text):
    """Strip the bot token from anything headed to a log.

    requests exceptions embed the full request URL (which contains the token),
    so every error-print path must run through here.
    """
    tok = token()
    return str(text).replace(tok, "<redacted>") if tok else str(text)


def adopt_chat(chat_id):
    """First /start wins when TELEGRAM_CHAT_ID isn't set explicitly."""
    st = _load()
    if str(st.get("adopted_chat_id")) != str(chat_id):
        st["adopted_chat_id"] = str(chat_id)
        _save()
        print(f"  [alerts] adopted chat id {chat_id}")


def recipients():
    env_ids = [c.strip() for c in
               os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    if env_ids:
        return env_ids
    adopted = str(_load().get("adopted_chat_id") or "")
    return [adopted] if adopted else []


def send(text):
    """Best-effort delivery. Returns True on success, False otherwise."""
    tok = token()
    if not tok:
        print(f"  [alerts] no TELEGRAM_BOT_TOKEN — would have sent:\n{text}")
        return False
    ids = recipients()
    if not ids:
        print("  [alerts] no chat id yet (nobody /start-ed the bot) — "
              f"dropped: {text.splitlines()[0]}")
        return False
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    ok = False
    for chat_id in ids:
        try:
            r = requests.post(
                API.format(token=tok) + "/sendMessage",
                json={"chat_id": chat_id, "text": text,
                      "disable_web_page_preview": True},
                timeout=15,
            )
            if r.ok and r.json().get("ok"):
                ok = True
            else:
                print(f"  [alerts] telegram rejected for {chat_id}: "
                      f"{redact(r.text[:200])}")
        except requests.RequestException as e:
            print(f"  [alerts] telegram send failed: {redact(e)}")
    return ok


def throttled(key, cooldown_s):
    """
    True if this alert key is allowed to fire now (and stamps it).
    False while still inside the cooldown window.
    """
    st = _load()
    now = time.time()
    last = st.get("cooldowns", {}).get(key, 0)
    if now - float(last) < cooldown_s:
        return False
    st.setdefault("cooldowns", {})[key] = now
    _save()
    return True

def alert(key, text, cooldown_s=900):
    st = _load()
    last = st.get('alerts', {}).get(str(key), 0)
    now = int(time.time())
    if now - last < cooldown_s:
        return False  # suppressed
    # send
    sent = send(text)
    if sent:
        st.setdefault('alerts', {})[str(key)] = now
        _save()
    return sent
