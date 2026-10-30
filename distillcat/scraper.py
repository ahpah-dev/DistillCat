"""Browser driver: sends prompts to chat.deepseek.com and extracts thinking + answer.

The user logs in manually in a persistent browser profile. Nothing here bypasses
login, captchas or rate limits - if DeepSeek shows a challenge, the tool pauses
and waits for the human to solve it.
"""
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

SELECTORS_PATH = Path(__file__).with_name("selectors.json")

# Extracts the last assistant turn. Converts rendered HTML back to Markdown so
# code fences, lists, headings and tables survive in the dataset.
EXTRACT_JS = r"""
(sel) => {
  const esc = (s) => s;
  function md(node) {
    if (node.nodeType === 3) return node.textContent;
    if (node.nodeType !== 1) return '';
    const tag = node.tagName.toLowerCase();
    if (node.matches && node.matches(sel.code_block)) {
      const pre = node.querySelector('pre');
      const lang = (node.querySelector('[class*="banner"] span, .md-code-block-banner span') || {}).innerText || '';
      const code = pre ? pre.innerText : node.innerText;
      return '\n```' + lang.trim().toLowerCase() + '\n' + code.replace(/\n$/, '') + '\n```\n';
    }
    const kids = () => Array.from(node.childNodes).map(md).join('');
    switch (tag) {
      case 'pre': return '\n```\n' + node.innerText.replace(/\n$/, '') + '\n```\n';
      case 'code': return '`' + node.innerText + '`';
      case 'strong': case 'b': return '**' + kids() + '**';
      case 'em': case 'i': return '*' + kids() + '*';
      case 'h1': return '\n# ' + kids() + '\n';
      case 'h2': return '\n## ' + kids() + '\n';
      case 'h3': return '\n### ' + kids() + '\n';
      case 'h4': case 'h5': case 'h6': return '\n#### ' + kids() + '\n';
      case 'p': return '\n' + kids() + '\n';
      case 'br': return '\n';
      case 'hr': return '\n---\n';
      case 'a': return '[' + kids() + '](' + (node.getAttribute('href') || '') + ')';
      case 'blockquote': return '\n' + kids().trim().split('\n').map(l => '> ' + l).join('\n') + '\n';
      case 'ul': return '\n' + Array.from(node.children).map(li => '- ' + md(li).trim().replace(/\n/g, '\n  ')).join('\n') + '\n';
      case 'ol': return '\n' + Array.from(node.children).map((li, i) => (i + 1) + '. ' + md(li).trim().replace(/\n/g, '\n   ')).join('\n') + '\n';
      case 'table': {
        const rows = Array.from(node.querySelectorAll('tr')).map(tr =>
          '| ' + Array.from(tr.children).map(c => c.innerText.trim().replace(/\|/g, '\\|').replace(/\n/g, ' ')).join(' | ') + ' |');
        if (rows.length > 1) {
          const n = (rows[0].match(/\|/g).length - 1);
          rows.splice(1, 0, '|' + ' --- |'.repeat(n));
        }
        return '\n' + rows.join('\n') + '\n';
      }
      case 'button': case 'svg': case 'style': case 'script': return '';
      default: return kids();
    }
  }
  const clean = (s) => s.replace(/\n{3,}/g, '\n\n').trim();

  const thinkEls = Array.from(document.querySelectorAll(sel.think_container));
  const respEls = Array.from(document.querySelectorAll(sel.response))
    .filter(e => !e.closest(sel.think_container) && !e.matches(sel.think_container));
  const thinkLast = thinkEls[thinkEls.length - 1];
  const respLast = respEls[respEls.length - 1];
  return {
    thinking: thinkLast ? clean(md(thinkLast)) : '',
    output: respLast ? clean(md(respLast)) : '',
  };
}
"""

CHALLENGE_HINTS = re.compile(r"verify you are human|captcha|just a moment|unusual activity", re.I)


def load_selectors():
    return json.loads(SELECTORS_PATH.read_text(encoding="utf-8"))


class DeepSeekSession:
    def __init__(self, profile_dir, thinking=True, search=False, headless=False,
                 settle_seconds=8.0, max_wait_seconds=600.0, log=print,
                 cdp_url=None, channel=None):
        self.cdp_url = cdp_url      # attach to an already-running browser
        self.channel = channel      # "chrome" / "msedge" to use installed browser
        self.sel = load_selectors()
        self.profile_dir = str(profile_dir)
        self.thinking = thinking
        self.search = search
        self.headless = headless
        self.settle = settle_seconds
        self.max_wait = max_wait_seconds
        self.log = log
        self._pw = None
        self.ctx = None
        self.page = None

    # ---- lifecycle -------------------------------------------------------
    def __enter__(self):
        self._pw = sync_playwright().start()
        if self.cdp_url:
            try:
                self.browser = self._pw.chromium.connect_over_cdp(self.cdp_url)
            except Exception as e:
                self._pw.stop()
                raise RuntimeError(
                    f"Could not attach to a browser at {self.cdp_url}. Newer Chrome versions may refuse "
                    f"remote-debugging connections. Close that window and run without --attach "
                    f"(e.g. 'python -m distillcat --browser chrome diagnose'), which lets "
                    f"DistillCat open your installed Chrome itself. ({e})")
            self.ctx = self.browser.contexts[0] if self.browser.contexts else self.browser.new_context()
            self.page = self.ctx.new_page()  # own tab; your other tabs are untouched
        else:
            self.ctx = self._pw.chromium.launch_persistent_context(
                self.profile_dir, headless=self.headless, channel=self.channel,
                viewport={"width": 1280, "height": 900},
            )
            self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        return self

    def __exit__(self, *exc):
        try:
            if self.cdp_url:
                self.page.close()   # leave the user's browser running
            else:
                self.ctx.close()
        finally:
            self._pw.stop()

    # ---- helpers ---------------------------------------------------------
    def _wait_for_human_if_needed(self):
        """If we're on a login page / challenge, let the human handle it."""
        while True:
            try:
                body = self.page.inner_text("body", timeout=3000)[:2000]
            except Exception:
                body = ""
            on_login = "/sign_in" in self.page.url or "/login" in self.page.url
            has_input = self.page.locator(self.sel["input"]).count() > 0
            if has_input and not on_login and not CHALLENGE_HINTS.search(body):
                return
            self.log("  [!] Login or verification needed. Complete it in the browser window; "
                     "waiting...")
            time.sleep(4)

    def open_chat(self):
        self.page.goto(self.sel["url"], wait_until="domcontentloaded")
        self._wait_for_human_if_needed()
        self._set_toggle(self.sel["deepthink_button_text"], self.thinking)
        self._set_toggle(self.sel["search_button_text"], self.search)

    def _set_toggle(self, text, want_on):
        btn = self.page.locator(f'div[role="button"]:has-text("{text}")').first
        try:
            if btn.count() == 0:
                return
            cls = btn.get_attribute("class") or ""
            is_on = re.search(self.sel["selected_class_regex"], cls, re.I) is not None
            if is_on != want_on:
                btn.click()
                time.sleep(0.4)
        except Exception as e:  # toggle is best-effort
            self.log(f"  [warn] could not set '{text}' toggle: {e}")

    def login_only(self):
        self.page.goto(self.sel["url"], wait_until="domcontentloaded")
        self._wait_for_human_if_needed()
        self.log("Logged in. Session saved in the browser profile.")

    # ---- main operation --------------------------------------------------
    def ask(self, prompt):
        """Send one prompt in a fresh chat; return dict(thinking, output)."""
        self.open_chat()
        box = self.page.locator(self.sel["input"]).first
        box.click()
        # fill() is instant and handles multi-line text (Enter would send).
        box.fill(prompt)
        time.sleep(0.3)
        self.page.keyboard.press("Enter")

        # Wait for response to start
        try:
            self.page.wait_for_selector(
                f'{self.sel["response"]}, {self.sel["think_container"]}', timeout=60000)
        except PWTimeout:
            raise RuntimeError("No response started within 60s (rate limit, challenge or selector changed?)")

        # Wait until the content stops changing for `settle` seconds.
        start = last_change = time.time()
        last = None
        while True:
            time.sleep(1.0)
            cur = self.page.evaluate(EXTRACT_JS, self.sel)
            key = (len(cur["thinking"]), len(cur["output"]))
            if key != last:
                last, last_change = key, time.time()
            idle = time.time() - last_change
            if cur["output"] and idle >= self.settle:
                return cur
            if time.time() - start > self.max_wait:
                self.log("  [warn] hit max wait; saving what we have")
                return cur
