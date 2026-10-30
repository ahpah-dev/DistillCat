"""DistillCat command line interface."""
import argparse
import hashlib
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .dataset import append_record, load_done_ids
from .prompts import PROMPTS, all_prompts

BANNER = r"""
   /\_/\    DistillCat
  ( o.o )   prompt -> think -> answer -> .jsonl
   > ^ <
"""


DEFAULT_CUSTOM = "my_prompts.txt"


def _custom_id(prompt):
    """Stable id from the prompt text, so editing/reordering the file never mixes up resume."""
    return "custom-" + hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:8]


def _load_custom(path):
    """Custom prompts: .jsonl ({"category","prompt"}) or .txt (one per line;
    use \\n inside a line for multi-line prompts)."""
    p = Path(path)
    if not p.exists():
        sys.exit(f"Custom prompt file not found: {p}  (create it with: python -m distillcat add)")
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or (p.suffix != '.jsonl' and line.startswith('#')):
            continue
        if p.suffix == ".jsonl":
            d = json.loads(line)
            cat, prompt = d.get("category", "custom"), d["prompt"]
        else:
            cat, prompt = "custom", line.replace("\\n", "\n")
        out.append((_custom_id(prompt), cat, prompt))
    return out


TEMPLATE = """# DistillCat - your own prompts. ONE PROMPT PER LINE. Lines starting with # are ignored.
# For a line break inside a prompt, write \\n
# Example:
# Write a Python function that checks if a number is prime, with tests.
"""


def cmd_add(args):
    """Add your own prompts. With text: append it. Without text: open the file in Notepad."""
    path = Path(args.file)
    if not path.exists():
        path.write_text(TEMPLATE, encoding="utf-8")
    if args.text:
        prompt = " ".join(args.text).strip().replace("\r", "").replace("\n", "\\n")
        with path.open("a", encoding="utf-8") as f:
            f.write(prompt + "\n")
        print(f"Added to {path}.")
    else:
        print(f"Opening {path} - type one prompt per line, save the file, close it.")
        try:
            os.startfile(path.resolve())
        except AttributeError:  # not Windows
            print(f"Edit this file in any editor: {path.resolve()}")
    print("Run your prompts with:  python -m distillcat --browser chrome run --custom-only")

def cmd_list(args):
    for cat, items in PROMPTS.items():
        print(f"{cat:15s} {len(items)} prompts")
    print(f"{'TOTAL':15s} {sum(len(v) for v in PROMPTS.values())} prompts")


def _session_kwargs(args):
    return {
        "cdp_url": f"http://localhost:{args.port}" if args.attach else None,
        "channel": {"chromium": None}.get(args.browser, args.browser),
    }


def _find_browser(name):
    candidates = {
        "chrome": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                   r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                   str(Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe")],
        "msedge": [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                   r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"],
    }
    for p in candidates.get(name, []):
        if Path(p).exists():
            return p
    return None


def cmd_launch(args):
    """Start your installed Chrome/Edge with a debugging port, then use --attach."""
    import subprocess
    name = args.browser if args.browser in ("chrome", "msedge") else "chrome"
    exe = _find_browser(name)
    if not exe:
        sys.exit(f"Could not find {name}. Start it yourself with: "
                 f"--remote-debugging-port={args.port} --user-data-dir=<folder>")
    profile = str(Path(args.profile).resolve())
    subprocess.Popen([exe, f"--remote-debugging-port={args.port}",
                      f"--user-data-dir={profile}", "https://chat.deepseek.com/"])
    print(f"Started {name}. Log in to DeepSeek in that window (once; it's remembered),")
    print(f"leave it open, then run:  python -m distillcat --attach run")


def cmd_diagnose(args):
    """Open DeepSeek, send one tiny prompt, and report which selectors match."""
    from .scraper import DeepSeekSession, EXTRACT_JS
    out = Path("diagnose")
    out.mkdir(exist_ok=True)
    with DeepSeekSession(args.profile, **_session_kwargs(args)) as s:
        try:
            s.open_chat()
            print("URL:", s.page.url)
            for k in ("input", "think_container", "response"):
                print(f"selector {k!r} ({s.sel[k]}): {s.page.locator(s.sel[k]).count()} match(es) before sending")
            res = s.ask("Reply with exactly: pong")
            print("thinking:", repr(res["thinking"][:200]))
            print("output:  ", repr(res["output"][:200]))
        except Exception as e:
            print("ERROR:", type(e).__name__, e)
        finally:
            for k in ("input", "think_container", "response"):
                print(f"selector {k!r}: {s.page.locator(s.sel[k]).count()} match(es) now")
            s.page.screenshot(path=str(out / "page.png"), full_page=True)
            (out / "page.html").write_text(s.page.content(), encoding="utf-8")
            print(f"Saved {out / 'page.png'} and {out / 'page.html'}")


def cmd_login(args):
    from .scraper import DeepSeekSession
    with DeepSeekSession(args.profile, **_session_kwargs(args)) as s:
        s.login_only()
        input("Press Enter to close...")


def cmd_run(args):
    from .scraper import DeepSeekSession, RateLimited
    if args.custom_only:
        items = _load_custom(args.custom or DEFAULT_CUSTOM)   # only your prompts
    else:
        items = list(all_prompts())
        if args.custom:
            items += _load_custom(args.custom)                # built-in + yours
    seen = set()
    items = [x for x in items if not (x[0] in seen or seen.add(x[0]))]  # drop duplicates
    if args.categories:
        items = [x for x in items if x[1] in args.categories]
    done = load_done_ids(args.out)
    todo = [x for x in items if x[0] not in done]
    if args.shuffle:
        random.shuffle(todo)
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(items)} selected, {len(done)} already in {args.out}, {len(todo)} to run.")
    if not todo:
        return

    ok = fail = 0
    stopped_by_limit = False
    with DeepSeekSession(args.profile, thinking=not args.no_thinking, search=False,
                         headless=False, settle_seconds=args.settle,
                         max_wait_seconds=args.max_wait, **_session_kwargs(args)) as s:
        for n, (pid, cat, prompt) in enumerate(todo, 1):
            print(f"[{n}/{len(todo)}] {pid}: {prompt[:70].replace(chr(10), ' ')}...")
            res = None
            wait = args.limit_wait
            for attempt in range(args.limit_retries + 1):
                try:
                    res = s.ask(prompt)
                    break
                except RateLimited:
                    if attempt == args.limit_retries:
                        stopped_by_limit = True
                        break
                    print(f"  rate limit hit. Waiting {wait / 60:.0f} min, then retrying "
                          f"({attempt + 1}/{args.limit_retries}). Ctrl+C to stop.")
                    time.sleep(wait)
                    wait *= 2
                except KeyboardInterrupt:
                    print("Interrupted. Progress is saved; re-run to resume.")
                    stopped_by_limit = True
                    break
                except Exception as e:
                    fail += 1
                    print(f"  failed: {e}")
                    break
            if stopped_by_limit:
                print("Stopping: still rate limited. Progress is saved; re-run the same command "
                      "later (e.g. in a few hours) and it will resume where it left off.")
                break
            if res is None:
                continue
            if not res["output"]:
                fail += 1
                print("  empty output, skipped")
                continue
            append_record(args.out, {
                "id": pid,
                "category": cat,
                "question": prompt,
                "thinking": res["thinking"],
                "output": res["output"],
                "source": "chat.deepseek.com",
                "deepthink": not args.no_thinking,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            ok += 1
            print(f"  saved (thinking {len(res['thinking'])} chars, output {len(res['output'])} chars)")
            time.sleep(args.delay + random.uniform(0, args.delay / 2))
    print(f"Done. {ok} saved, {fail} failed -> {args.out}")


def main(argv=None):
    print(BANNER)
    ap = argparse.ArgumentParser(prog="distillcat")
    ap.add_argument("--profile", default="browser_profile", help="browser profile dir")
    ap.add_argument("--attach", action="store_true",
                    help="attach to your already-running Chrome/Edge (start it with 'launch')")
    ap.add_argument("--port", type=int, default=9333, help="remote debugging port")
    ap.add_argument("--browser", default="chrome", choices=["chrome", "msedge", "chromium"],
                    help="installed browser to use (default: chrome)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("diagnose", help="test one tiny prompt and dump selector matches/screenshot"
                   ).set_defaults(fn=cmd_diagnose)
    sub.add_parser("launch", help="start your Chrome/Edge with a debug port for --attach"
                   ).set_defaults(fn=cmd_launch)
    sub.add_parser("list", help="show built-in prompt categories").set_defaults(fn=cmd_list)
    sub.add_parser("login", help="open browser to log in once").set_defaults(fn=cmd_login)
    a = sub.add_parser("add", help="add your own prompts (no text = open my_prompts.txt in Notepad)")
    a.add_argument("--file", default=DEFAULT_CUSTOM, help=f"default: {DEFAULT_CUSTOM}")
    a.add_argument("text", nargs="*", help="prompt text to append (optional)")
    a.set_defaults(fn=cmd_add)

    r = sub.add_parser("run", help="send prompts and collect the dataset")
    r.add_argument("--out", default="distillcat_dataset.jsonl")
    r.add_argument("--categories", nargs="*", help="only these categories (built-in or your own)")
    r.add_argument("--custom", help=f"your prompts file (.jsonl or .txt); added to the built-ins")
    r.add_argument("--custom-only", action="store_true",
                   help=f"run ONLY your prompts (from --custom, default {DEFAULT_CUSTOM}), no built-ins")
    r.add_argument("--limit", type=int, help="max prompts this run")
    r.add_argument("--shuffle", action="store_true")
    r.add_argument("--no-thinking", action="store_true", help="don't enable DeepThink")
    r.add_argument("--delay", type=float, default=8.0, help="seconds between prompts (+ jitter)")
    r.add_argument("--settle", type=float, default=8.0, help="idle seconds that mean 'finished'")
    r.add_argument("--max-wait", type=float, default=600.0, help="per-prompt timeout seconds")
    r.add_argument("--limit-wait", type=float, default=300.0,
                   help="seconds to wait after a rate limit (doubles each retry)")
    r.add_argument("--limit-retries", type=int, default=3,
                   help="retries after a rate limit before stopping the run")
    r.set_defaults(fn=cmd_run)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
