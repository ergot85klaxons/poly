import asyncio
import json
import os
from pathlib import Path
from typing import Optional

import aiohttp
from dotenv import load_dotenv

load_dotenv()

# ---- настройки / файлы ----
NAMES: dict = {}                       # адрес -> ник (загрузим из names.json)
NAMES_FILE = Path("names.json")
STATE_FILE = Path("state.json")

TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
HANDLES = [h.strip() for h in os.getenv("HANDLES", "").split(",") if h.strip()]
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "20"))

# Анти-спам к Telegram
SEND_DELAY = 0.6       # пауза между сообщениями (сек)
TG_MAX_RETRIES = 3     # сколько раз ретраить при 429

# ---- Polymarket endpoints ----
GAMMA_SEARCH = "https://gamma-api.polymarket.com/public-search"
DATA_TRADES = "https://data-api.polymarket.com/trades"
CLOB_MARKET = "https://clob.polymarket.com/markets"


# --------- utils: файлы состояния / имён ----------
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_seen": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def load_names() -> dict:
    if NAMES_FILE.exists():
        try:
            data = json.loads(NAMES_FILE.read_text())
            # нормализуем ключи-адреса к нижнему регистру
            return {k.lower(): v for k, v in data.items()}
        except Exception as e:
            print("names.json parse error:", e)
    return {}


# --------- utils: Telegram ----------
async def tg_send(session: aiohttp.ClientSession, text: str, disable_web_page_preview: bool = True):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_web_page_preview,
    }

    last_err = None
    for _ in range(TG_MAX_RETRIES):
        async with session.post(url, data=payload) as r:
            data = await r.json()
            if data.get("ok"):
                # лёгкий троттлинг
                await asyncio.sleep(SEND_DELAY)
                return data

            # обработка 429 Too Many Requests
            if data.get("error_code") == 429:
                retry_after = int(
                    data.get("parameters", {}).get("retry_after", 1))
                await asyncio.sleep(retry_after + 1)
                last_err = data
                continue

            last_err = data
            break

    print("TG send error:", last_err)
    return last_err or {"ok": False}


# --------- utils: Polymarket ----------
def is_address(s: str) -> bool:
    return isinstance(s, str) and s.startswith("0x") and len(s) == 42


async def resolve_wallet(session: aiohttp.ClientSession, handle: str) -> Optional[str]:
    # если уже адрес кошелька — возвращаем как есть
    if is_address(handle):
        return handle
    # иначе ищем по нику
    params = {"q": handle}
    async with session.get(GAMMA_SEARCH, params=params, timeout=20) as r:
        r.raise_for_status()
        js = await r.json()
    for p in js.get("profiles", []):
        if (p.get("pseudonym") or "").lower() == handle.lower():
            return p.get("proxyWallet")
    return None


async def fetch_user_trades(session: aiohttp.ClientSession, wallet: str, limit: int = 50):
    params = {"user": wallet, "limit": limit}
    async with session.get(DATA_TRADES, params=params, timeout=30) as r:
        r.raise_for_status()
        return await r.json()


async def get_market_info(session: aiohttp.ClientSession, condition_id: str) -> dict:
    url = f"{CLOB_MARKET}/{condition_id}"
    try:
        async with session.get(url, timeout=20) as r:
            if r.status != 200:
                return {}
            return await r.json()
    except Exception:
        return {}


def price_to_cents(p) -> str:
    try:
        return f"{round(float(p)*100, 2)}¢"
    except Exception:
        return "?"


def build_event_link(slug: Optional[str]) -> Optional[str]:
    return f"https://polymarket.com/event/{slug}" if slug else None


# --------- основная логика ----------
async def process_handle(session: aiohttp.ClientSession, handle: str, state: dict):
    wallet = await resolve_wallet(session, handle)
    if not wallet:
        await tg_send(session, f"Wallet for profile not found <b>{handle}</b>.")
        return

    last_seen = state["last_seen"].get(wallet)
    # новые → старые
    trades = await fetch_user_trades(session, wallet, limit=50)

    # --- WARM START: новый кошелёк — запомнить самый свежий и ничего не слать
    if last_seen is None and trades:
        newest_tid = f"{trades[0].get('transactionHash')}-{trades[0].get('timestamp')}-{trades[0].get('conditionId')}"
        state["last_seen"][wallet] = newest_tid
        save_state(state)
        return

    # собрать только новые сделки после last_seen
    new_items = []
    for t in trades:
        tid = f"{t.get('transactionHash')}-{t.get('timestamp')}-{t.get('conditionId')}"
        if tid == last_seen:
            break
        new_items.append((tid, t))

    if not new_items:
        return

    for tid, t in reversed(new_items):
        side = (t.get("side") or "?").upper()
        price = price_to_cents(t.get("price"))
        size = t.get("size") or "?"
        condition_id = t.get("conditionId")
        market = await get_market_info(session, condition_id) if condition_id else {}
        question = market.get("question") or market.get(
            "title") or "(без названия)"
        link = build_event_link(market.get("slug"))

        # имя: names.json -> исходный handle -> сокращённый адрес
        addr_lower = wallet.lower()
        display_name = NAMES.get(addr_lower) or handle or (
            wallet[:6] + "…" + wallet[-4:])

        # индикатор сделки
        emoji = "🟢" if side == "BUY" else "🔴" if side == "SELL" else "⚪️"

        # кликабельная ссылка на событие (если есть)
        if link:
            event_line = f"<b>Событие:</b> <a href=\"{link}\">{question}</a>"
        else:
            event_line = f"<b>Событие:</b> {question}"

        # красивый формат
        text = (
            f"<b>{emoji} {display_name} — {side}</b>\n"
            f"{event_line}\n"
            f"<b>Цена:</b> {price} · <b>Размер:</b> {size}"
        )

        await tg_send(session, text)
        state["last_seen"][wallet] = tid

    save_state(state)


async def main():
    if not TG_TOKEN or not TG_CHAT_ID or not HANDLES:
        raise SystemExit(
            "Заполни TELEGRAM_TOKEN, TELEGRAM_CHAT_ID и HANDLES в .env")
    state = load_state()
    global NAMES
    NAMES = load_names()
    async with aiohttp.ClientSession() as session:
        await tg_send(session, "Wallet tracker active ✅.")
        while True:
            try:
                tasks = [process_handle(session, h, state) for h in HANDLES]
                await asyncio.gather(*tasks)
            except Exception as e:
                print("Loop error:", e)
            await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
