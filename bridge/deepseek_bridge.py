# -*- coding: utf-8 -*-
"""
DeepSeek Bridge for OpenCode — локальный HTTP-мост между плагином OpenCode
и веб-чатом chat.deepseek.com. Работает на http://127.0.0.1:8777 (порт и имя
профиля свои, чтобы не конфликтовать с мостом надстройки Word на 8765).
Браузер по умолчанию НЕВИДИМЫЙ (headless); для ручного входа открывается
видимое окно (эндпоинт /open_login), для автоматического — /login.

ВАЖНО: в чат DeepSeek отправляются обычные текстовые запросы человека.
Никаких «ты ИИ-агент», системных промптов ассистента и служебных обёрток здесь нет.

API:
  GET  /health           -> {"ok": true}
  GET  /status           -> {"running", "logged_in", "thinking", "search", "busy"}
  POST /start            -> {"headless": bool} запускает браузер
  POST /login            -> {"email", "password"} автоматический вход в аккаунт
                            ( {"confirm": true} — только проверить, что вход уже выполнен )
  POST /open_login       -> открывает ВИДИМОЕ окно для ручного входа
  POST /confirm_login    -> проверяет вход и возвращает браузер в headless
  POST /chat             -> {"prompt", "images": [пути], "thinking": bool, "search": bool,
                             "new_chat": bool, "timeout": сек} -> {"ok", "text"}
  POST /shutdown
"""

import asyncio
import base64
import json
import logging
import os
import random
import re
import sys
import time
import urllib.parse
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional

APP_NAME = "OpenCodeDeepSeekBridge"
PORT = 8777
BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = Path(os.environ.get("LOCALAPPDATA", str(BASE_DIR))) / APP_NAME / "profile"
CONFIG_PATH = BASE_DIR / "config.json"
LOG_PATH = BASE_DIR / "bridge.log"

# pythonw.exe (запуск без консоли) не имеет stdout/stderr — подменяем на devnull,
# иначе logging и uvicorn падают при попытке вывода
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(APP_NAME)

DEFAULT_CONFIG = {
    "base_url": "https://chat.deepseek.com/",
    "headless": True,
    "timeout_chat_sec": 1500,
    "locale": "ru",
}

# Headless-Chromium шлёт User-Agent с «HeadlessChrome» — защита CloudFront DeepSeek
# на такой запрос отвечает 403 и чат не открывается. Подменяем на обычный Chrome,
# иначе весь мост в невидимом режиме нерабочий.
UA_CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_PATH.exists():
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except Exception as e:
        log.warning("config.json не прочитан: %s", e)
    return cfg

CFG = load_config()
PROFILE_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title=APP_NAME)

_pw = None
_ctx = None
_page = None
_current_headless: Optional[bool] = None
_browser_lock = asyncio.Lock()
BUSY = False  # идёт ли сейчас обработка /chat

class _State:
    thinking: Optional[bool] = None
    search: Optional[bool] = None

STATE = _State()

# ---------------------------------------------------------------------------
# Запуск/остановка браузера
# ---------------------------------------------------------------------------

async def ensure_browser(headless: Optional[bool] = None):
    """Запускает браузер с нужным режимом видимости. Если режим поменялся — перезапускает."""
    global _pw, _ctx, _page, _current_headless
    want_headless = CFG["headless"] if headless is None else bool(headless)
    async with _browser_lock:
        if _ctx is not None and _current_headless == want_headless:
            _page = _ctx.pages[0] if _ctx.pages else _page
            return _page
        # пересоздание с другим режимом
        await _close_ctx_locked()
        from playwright.async_api import async_playwright
        if _pw is None:
            _pw = await async_playwright().start()
        log.info("Запуск Chromium (headless=%s, профиль=%s)", want_headless, PROFILE_DIR)
        launch_kw = dict(
            headless=want_headless,
            viewport={"width": 1360, "height": 900},
            locale=CFG.get("locale", "ru"),
            user_agent=UA_CHROME,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        try:
            _ctx = await _pw.chromium.launch_persistent_context(str(PROFILE_DIR), **launch_kw)
        except Exception as e:
            # Playwright-Chromium не скачан (cdn.playwright.dev часто недоступен) —
            # используем системный Edge/Chrome, он предустановлен на Windows 10/11.
            if "Executable doesn't exist" not in str(e):
                raise
            for channel in ("msedge", "chrome"):
                try:
                    log.warning("Playwright-Chromium не найден, пробую системный браузер: %s", channel)
                    _ctx = await _pw.chromium.launch_persistent_context(
                        str(PROFILE_DIR), channel=channel, **launch_kw)
                    break
                except Exception as e2:
                    log.warning("%s не запустился: %s", channel, e2)
            else:
                raise RuntimeError(
                    "Не найден ни Playwright-Chromium, ни системный Edge/Chrome. "
                    "Выполните: python -m playwright install chromium") from e
        _current_headless = want_headless
        if _ctx.pages:
            _page = _ctx.pages[0]
        else:
            _page = await _ctx.new_page()
        try:
            await _page.goto(CFG["base_url"], wait_until="domcontentloaded")
        except Exception as e:
            log.warning("goto при запуске: %s", e)
        return _page


async def _close_ctx_locked():
    global _ctx, _page, _current_headless
    try:
        if _ctx is not None:
            await _ctx.close()
    except Exception:
        pass
    _ctx, _page, _current_headless = None, None, None


async def close_browser():
    async with _browser_lock:
        await _close_ctx_locked()
    global _pw
    try:
        if _pw is not None:
            await _pw.stop()
    except Exception:
        pass
    _pw = None


# ---------------------------------------------------------------------------
# Вспомогательные операции над страницей
# ---------------------------------------------------------------------------

async def is_logged_in(page) -> bool:
    """Вход выполнен, если на странице есть доступное поле ввода чата."""
    try:
        await page.bring_to_front()
    except Exception:
        pass
    try:
        ta = page.locator("textarea").first
        await ta.wait_for(state="visible", timeout=6000)
        return True
    except Exception:
        return False


def page_url() -> str:
    return CFG["base_url"]


async def wait_idle(page, timeout_sec: float = 60):
    """Ждёт, пока DeepSeek не закончит предыдущую генерацию (если она была)."""
    start = time.time()
    while time.time() - start < timeout_sec:
        try:
            stopping = await page.evaluate(
                """() => {
                    const ta = document.querySelector('textarea');
                    if (!ta) return false;
                    // во время генерации поле заблокировано
                    return ta.disabled || ta.getAttribute('readonly') !== null;
                }"""
            )
            if not stopping:
                # доп. проверка: нет растущего ответа — считаем свободно
                return True
        except Exception:
            return True
        await page.wait_for_timeout(1000)
    return False


async def goto_chat(page, new_chat: bool):
    if new_chat:
        await page.goto(page_url(), wait_until="domcontentloaded")
    else:
        try:
            if not page.url.startswith("https://chat.deepseek.com"):
                await page.goto(page_url(), wait_until="domcontentloaded")
        except Exception:
            await page.goto(page_url(), wait_until="domcontentloaded")
    try:
        await page.locator("textarea").first.wait_for(state="visible", timeout=20000)
    except Exception:
        pass


# --- тумблеры «Глубокое мышление» и «Умный поиск» ---------------------------

async def _toggle_labels():
    return (["Глубокое мышление", "DeepThink", "Глубокое размышление"],
            ["Умный поиск", "Поиск", "Search"])


async def _find_toggle(page, labels):
    for label in labels:
        try:
            loc = page.locator("div.ds-toggle-button", has_text=label).first
            if await loc.count() == 0:
                continue
            cls = (await loc.get_attribute("class")) or ""
            return loc, "ds-toggle-button--selected" in cls
        except Exception:
            continue
    return None, None


async def set_thinking(page, enabled: bool):
    labels_think, _ = await _toggle_labels()
    loc, checked = await _find_toggle(page, labels_think)
    if loc is None:
        log.warning("Тумблер «Размышление» не найден")
        return False
    if checked == enabled:
        STATE.thinking = enabled
        return True
    try:
        await loc.click(timeout=5000)
        STATE.thinking = enabled
        log.info("Тумблер «Размышление» -> %s", enabled)
        return True
    except Exception as e:
        log.error("Клик по тумблеру размышления: %s", e)
        return False


async def set_search(page, enabled: bool):
    _, labels_search = await _toggle_labels()
    loc, checked = await _find_toggle(page, labels_search)
    if loc is None:
        log.warning("Тумблер «Поиск» не найден")
        return False
    if checked == enabled:
        STATE.search = enabled
        return True
    try:
        await loc.click(timeout=5000)
        STATE.search = enabled
        log.info("Тумблер «Поиск» -> %s", enabled)
        return True
    except Exception as e:
        log.error("Клик по тумблеру поиска: %s", e)
        return False


# --- изображения --------------------------------------------------------------

async def _attach_images(page, image_paths: List[str]):
    if not image_paths:
        return 0
    existing = [p for p in image_paths if os.path.exists(p)]
    if not existing:
        log.warning("Ни одного файла-картинки не найдено: %s", image_paths)
        return 0
    try:
        file_input = page.locator("input[type='file']").first
        if await file_input.count() == 0:
            for lb in ["Прикрепить", "Attach", "Upload"]:
                btn = page.locator(f"div[role='button'][aria-label*='{lb}'], button[aria-label*='{lb}']").first
                if await btn.count() > 0:
                    await btn.click()
                    break
            file_input = page.locator("input[type='file']").first
        await file_input.set_input_files(existing)
        await page.wait_for_timeout(1500)
        log.info("Прикреплено изображений: %d", len(existing))
        return len(existing)
    except Exception as e:
        log.error("Не удалось прикрепить изображения: %s", e)
        return 0


# --- отправка и ожидание ответа -------------------------------------------------

# Живой режим: мост читает settings.json при каждом запросе, так что
# переключение применяется сразу, без перезапуска. По умолчанию — включён
# (защита аккаунта от блокировки за «мгновенные» запросы).
HUMAN_SETTINGS_PATH = Path(os.environ.get("LOCALAPPDATA", str(BASE_DIR))) / APP_NAME / "settings.json"


def human_mode_enabled() -> bool:
    """Галочка «Живые запросы» из settings.json надстройки (по умолчанию включена)."""
    try:
        if HUMAN_SETTINGS_PATH.exists():
            data = json.loads(HUMAN_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "humanMode" in data:
                v = data["humanMode"]
                if isinstance(v, bool):
                    return v
                return str(v).strip().lower() in ("1", "true", "yes")
    except Exception as e:
        log.debug("humanMode не прочитан: %s", e)
    return True


# момент последней отправки — от него отсчитываем паузу между запросами
_last_send_at = 0.0


def next_delay(now, last, enabled=True):
    """Секунды ожидания перед следующей отправкой.

    Живой режим держит паузу не меньше 3 секунд от предыдущего запроса
    (DeepSeek просит не чаще) и иногда делает её длиннее — как человек,
    который дочитал ответ, прежде чем писать дальше. Чистая функция:
    её же гоняют тесты (test_agent_api.py).
    """
    if not enabled or last <= 0:
        return 0.0
    gap = 3.0 + random.random() * 3.5            # 3.0–6.5 с
    if random.random() < 0.15:                   # изредка — заметно дольше
        gap += random.random() * 6.0
    return max(0.0, last + gap - now)


async def _fill_human(page, ta, prompt: str) -> bool:
    """Живой ввод: начало печатается с человеческим темпом, остаток вставляется
    одним куском (как из буфера). False — ввод не удался, нужен обычный fill()."""
    if not prompt:
        return True

    # до первого перевода строки: Enter отправил бы сообщение раньше времени
    limit = random.randint(18, 40)
    cut = limit
    for i, ch in enumerate(prompt[:limit]):
        if ch in "\r\n":
            cut = i
            break
    if cut == 0:
        return False

    head, rest = prompt[:cut], prompt[cut:]
    try:
        await ta.fill("", timeout=5000)          # поле должно быть пустым
        await page.wait_for_timeout(int(150 + random.random() * 450))
        if head:
            await page.keyboard.type(head, delay=random.randint(35, 75))
        if rest:
            # вставка без нажатий клавиш — как Ctrl+V. Важно: идёт через
            # browser editing-pipeline (CDP Input.insertText), а не через JS-подстановку
            # в DOM, иначе React-состояние поля не обновится и в чат уйдёт
            # только напечатанная «голова» промпта.
            await page.keyboard.insert_text(rest)
        return True
    except Exception as e:
        log.warning("живой ввод не удался: %s", e)
        return False


async def _send_message(page, prompt: str):
    global _last_send_at
    human = human_mode_enabled()
    ta = page.locator("textarea").first
    await ta.wait_for(state="visible", timeout=20000)
    try:
        await ta.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    await ta.click(timeout=10000)

    if human:
        pause = next_delay(time.time(), _last_send_at, True)
        if pause > 0.1:
            log.info("Живой режим: пауза %.1f с перед вводом", pause)
            await asyncio.sleep(pause)
        if not await _fill_human(page, ta, prompt):
            await ta.fill(prompt, timeout=20000)
        # «прочитал перед отправкой» — короткая пауза перед Enter
        await asyncio.sleep(0.4 + random.random() * 1.0)
    else:
        await ta.fill(prompt, timeout=20000)

    val = await ta.input_value()
    norm = lambda s: s.replace("\r\n", "\n").replace("\r", "\n")
    # живой ввод обязан сложиться в точности в промпт; иначе переписываем поле целиком
    if len(val.strip()) < min(50, len(prompt)) or norm(val) != norm(prompt):
        # JS-подстановка не доходит до React-состоянию контролируемого поля,
        # поэтому кладём текст штатным fill() — он идёт через editing-pipeline браузера
        await ta.fill(prompt, timeout=20000)
        val = await ta.input_value()
        if len(val.strip()) < min(20, len(prompt)) or norm(val) != norm(prompt):
            raise RuntimeError("текст не встал в поле ввода DeepSeek (%d из %d)" % (len(val), len(prompt)))
    await ta.press("Enter")
    _last_send_at = time.time()
    log.info("Отправлено (живой режим: %s, в поле %d симв.)", "да" if human else "нет", len(val))
    await page.wait_for_timeout(1500)
    try:
        rest = await ta.input_value()
    except Exception:
        rest = ""
    if rest.strip():
        # сообщение не ушло — жмём кнопку отправки (последняя активная кнопка в зоне ввода)
        clicked = await page.evaluate(
            """() => {
                const ta = document.querySelector('textarea');
                let root = ta ? ta.closest('div[class*=\"ds-\"]')?.parentElement?.parentElement : null;
                if (!root) root = document;
                const btns = [...root.querySelectorAll('div[role=\"button\"], button')];
                for (let i = btns.length - 1; i >= 0; i--) {
                    const b = btns[i];
                    const r = b.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0 && r.top > 0) { b.click(); return true; }
                }
                return false;
            }"""
        )
        log.info("Enter не отправил — fallback кнопка: %s", clicked)
        await page.wait_for_timeout(1500)


async def _generating(page) -> bool:
    """Лучший эвристический детект идущей генерации."""
    try:
        return await page.evaluate(
            """() => {
                const ta = document.querySelector('textarea');
                if (ta && (ta.disabled || ta.getAttribute('readonly') !== null)) return true;
                // кнопка «стоп» вместо отправки
                const btns = [...document.querySelectorAll('div[role="button"]')];
                for (const b of btns) {
                    const aria = (b.getAttribute('aria-label') || '');
                    if (/стоп|stop|останов/i.test(aria)) return true;
                }
                return false;
            }"""
        )
    except Exception:
        return False


async def _last_answer_text(page) -> str:
    try:
        blocks = page.locator(".ds-markdown")
        n = await blocks.count()
        if n == 0:
            return ""
        return (await blocks.nth(n - 1).inner_text()).strip()
    except Exception:
        return ""


async def _wait_response(page, timeout_sec: float, min_wait_sec: float = 8.0):
    """
    Ждёт завершения генерации: текст последнего ответа стабилен 5 секунд
    и поле ввода снова активно. Возвращает финальный текст.
    """
    start = time.time()
    stable_since = None
    last_text = ""
    while time.time() - start < timeout_sec:
        await page.wait_for_timeout(1500)
        text = await _last_answer_text(page)
        gen = await _generating(page)
        if text and text == last_text and not gen and (time.time() - start) > min_wait_sec:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since >= 5.0:
                return text
        else:
            stable_since = None
        if text:
            last_text = text
    log.warning("Тайм-аут ожидания ответа (%.0f c), возвращаю то, что есть", timeout_sec)
    return last_text

# ---------------------------------------------------------------------------
# Схемы запросов
# ---------------------------------------------------------------------------

class StartReq(BaseModel):
    headless: Optional[bool] = None

class ChatReq(BaseModel):
    prompt: str
    images: List[str] = []
    thinking: bool = False
    search: bool = False
    new_chat: bool = True
    timeout: Optional[int] = None


# ---------------------------------------------------------------------------
# Эндпоинты
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/start")
async def start(req: StartReq):
    try:
        page = await ensure_browser(req.headless)
        logged = await is_logged_in(page)
        return {"ok": True, "logged_in": logged, "page": page.url}
    except Exception as e:
        log.exception("start failed")
        return {"ok": False, "error": str(e)}


@app.get("/status")
async def status():
    if _page is None:
        return {"running": False, "logged_in": False, "busy": BUSY}
    try:
        logged = await is_logged_in(_page)
        return {"running": True, "logged_in": logged, "page": _page.url,
                "thinking": STATE.thinking, "search": STATE.search, "busy": BUSY}
    except Exception as e:
        return {"running": True, "logged_in": False, "error": str(e), "busy": BUSY}


@app.post("/open_login")
async def open_login():
    """Открывает ВИДИМОЕ окно браузера для входа в аккаунт."""
    try:
        page = await ensure_browser(headless=False)
        await page.goto("https://chat.deepseek.com/sign_in", wait_until="domcontentloaded")
        log.info("Открыто видимое окно входа")
        return {"ok": True, "message": "Открыт браузер. Войдите в аккаунт DeepSeek, затем нажмите «Проверить вход»."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/confirm_login")
async def confirm_login():
    """Проверяет вход и возвращает браузер в невидимый режим."""
    try:
        page = await ensure_browser(headless=False)
        await page.goto(page_url(), wait_until="domcontentloaded")
        logged = await is_logged_in(page)
        # возвращаем headless
        await ensure_browser(headless=True)
        return {"ok": True, "logged_in": logged}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/chat")
async def chat(req: ChatReq):
    global BUSY
    if BUSY:
        return {"ok": False, "error": "busy", "message": "Предыдущий запрос ещё выполняется"}
    BUSY = True
    try:
        page = await ensure_browser()
        if not await is_logged_in(page):
            return {"ok": False, "error": "not_logged_in",
                    "message": "Требуется вход в DeepSeek: панель → «Вход в DeepSeek»."}
        await wait_idle(page, timeout_sec=90)
        await goto_chat(page, req.new_chat)
        await set_thinking(page, req.thinking)
        await set_search(page, req.search)
        await _attach_images(page, req.images)
        log.info("Отправка запроса (%d символов)...", len(req.prompt))
        await _send_message(page, req.prompt)
        text = await _wait_response(page, req.timeout or CFG["timeout_chat_sec"])
        if not text:
            return {"ok": False, "error": "empty_response"}
        log.info("Ответ получен (%d символов)", len(text))
        return {"ok": True, "text": text}
    except Exception as e:
        log.exception("chat failed")
        return {"ok": False, "error": str(e)}
    finally:
        BUSY = False


async def auto_login(page, email: str, password: str, timeout_sec: int = 60) -> dict:
    """Вход в аккаунт DeepSeek по email/паролю прямо в headless-браузере.

    Возвращает {"ok": bool, "logged_in": bool, "error"?: str}.
    """
    await page.goto("https://chat.deepseek.com/sign_in", wait_until="domcontentloaded")
    await page.wait_for_timeout(3000)

    # при необходимости включаем вход по email (на странице может быть только телефон)
    if await page.locator("input[type='password']").count() == 0:
        for label in ("Вход по email", "Email", "По email", "e-mail"):
            try:
                btn = page.get_by_text(label, exact=False).first
                if await btn.count() > 0:
                    await btn.click(timeout=3000)
                    await page.wait_for_timeout(1500)
                    break
            except Exception:
                continue

    email_input = page.locator("input[type='text']").first
    pwd_input = page.locator("input[type='password']").first
    if await email_input.count() == 0 or await pwd_input.count() == 0:
        return {"ok": False, "logged_in": False,
                "error": "форма входа не найдена (нет полей email/пароль)"}

    await email_input.fill(email)
    await pwd_input.fill(password)
    await page.wait_for_timeout(600)

    login_btn = page.locator("div.ds-button--primary", has_text="Войти").first
    if await login_btn.count() == 0:
        login_btn = page.locator("button[type='submit']").first
    if await login_btn.count() == 0:
        return {"ok": False, "logged_in": False, "error": "кнопка «Войти» не найдена"}
    await login_btn.click()

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        await page.wait_for_timeout(1000)
        try:
            ta = page.locator("textarea").first
            if await ta.count() > 0 and await ta.is_visible():
                log.info("Вход выполнен: %s", email)
                return {"ok": True, "logged_in": True}
        except Exception:
            pass
    # не вошли — собираем видимую ошибку со страницы (неверный пароль, код, captcha)
    err = ""
    try:
        err = (await page.evaluate(
            """() => [...document.querySelectorAll('div,span,p')]
                   .map(e => (e.innerText || '').trim())
                   .filter(s => s && s.length < 160 && /невер|ошиб|код|captcha|подтвер|невозможно/i.test(s))
                   .slice(0, 4).join(' | ')""")) or ""
    except Exception:
        pass
    log.warning("Автовход не удался: %s", err or "нет textarea чата")
    return {"ok": False, "logged_in": False,
            "error": ("автовход не удался: " + err) if err else "автовход не удался: чат не открылся"}


class LoginReq(BaseModel):
    email: str = ""
    password: str = ""
    confirm: bool = False
    visible: bool = False


@app.post("/login")
async def login(req: LoginReq):
    """Автовход по email/паролю; confirm=true — только проверить, что вход уже есть."""
    global BUSY
    if BUSY:
        return {"ok": False, "error": "busy", "message": "Предыдущий запрос ещё выполняется"}
    BUSY = True
    try:
        if req.confirm:
            page = await ensure_browser()
            return {"ok": True, "logged_in": await is_logged_in(page)}
        if not req.email or not req.password:
            return {"ok": False, "error": "email и password обязательны"}
        page = await ensure_browser(headless=not req.visible)
        res = await auto_login(page, req.email, req.password)
        if res.get("logged_in"):
            # возвращаем браузер в невидимый режим (сессия сохраняется в профиле)
            await ensure_browser(headless=True)
        return res
    except Exception as e:
        log.exception("login failed")
        return {"ok": False, "error": str(e)}
    finally:
        BUSY = False


@app.post("/shutdown")
async def shutdown():
    asyncio.get_event_loop().create_task(_shutdown_task())
    return {"ok": True}


async def _shutdown_task():
    await close_browser()
    os._exit(0)


def run():
    log.info("DeepSeek Bridge запускается на http://127.0.0.1:%s (headless=%s)", PORT, CFG["headless"])
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")

if __name__ == "__main__":
    run()
