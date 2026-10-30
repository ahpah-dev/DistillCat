"""DistillCat command line interface."""
import argparse
import hashlib
import json
import os
import re
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


def _read_text(p):
    """Read a text file saved by any editor (UTF-8, UTF-8 with BOM, UTF-16, ANSI)."""
    raw = Path(p).read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252")


def _is_block_mode(text):
    return any(l.strip() == "---" for l in text.splitlines())


def _parse_txt(text):
    """Two formats, auto-detected:
      * one prompt per line (write \\n for a line break; lines starting with # are ignored)
      * multi-line prompts separated by a line containing only ---
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    prompts = []
    if _is_block_mode(text):
        for block in re.split(r"(?m)^[ \t]*---[ \t]*$", text):
            lines = block.strip("\n").split("\n")
            if all(not l.strip() or l.lstrip().startswith("#") for l in lines):
                continue  # empty block or a comment-only block (e.g. the header)
            prompts.append(block.strip())
    else:
        for line in text.split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                prompts.append(line.replace("\\n", "\n"))
    return [("custom", p) for p in prompts]


def _load_custom(path, must_exist=True):
    """Custom prompts from .txt (see _parse_txt) or .jsonl ({"category","prompt"} per line)."""
    p = Path(path)
    if not p.exists():
        if must_exist:
            sys.exit(f"Custom prompt file not found: {p}\nCreate it with:  python -m distillcat add")
        return []
    text = _read_text(p)
    if p.suffix == ".jsonl":
        pairs = []
        for n, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                pairs.append((d.get("category", "custom"), d["prompt"]))
            except (json.JSONDecodeError, KeyError, AttributeError):
                sys.exit(f"{p}, line {n}: expected JSON like {{\"category\": \"x\", \"prompt\": \"...\"}}")
    else:
        pairs = _parse_txt(text)
    seen = {}
    out = []
    for cat, prompt in pairs:
        pid = _custom_id(prompt)
        seen[pid] = seen.get(pid, 0) + 1
        # The same prompt listed N times = N separate samples (ids: x, x-2, x-3, ...)
        out.append((pid if seen[pid] == 1 else f"{pid}-{seen[pid]}", cat, prompt))
    return out


TEMPLATE = """# DistillCat - your own prompts. Lines starting with # are ignored.
# Format 1: ONE PROMPT PER LINE (write \\n inside a line for a line break).
# Format 2: multi-line prompts, separated by a line containing only three dashes.
# Delete these comment lines or leave them. Then add your prompts below:

"""


def _open_in_editor(path):
    try:
        os.startfile(path.resolve())
    except AttributeError:  # not Windows
        print(f"Edit this file in any editor: {path.resolve()}")


def cmd_add(args):
    """Add your own prompts. With text: append it. Without text: open the file in Notepad."""
    path = Path(args.file)
    if not path.exists():
        path.write_text(TEMPLATE, encoding="utf-8")
    if args.text:
        prompt = " ".join(args.text).strip()
        existing = _read_text(path)
        with path.open("a", encoding="utf-8") as f:
            if not existing.endswith("\n"):
                f.write("\n")
            if _is_block_mode(existing):
                f.write("---\n" + prompt + "\n")
            else:
                f.write(prompt.replace("\r", "").replace("\n", "\\n") + "\n")
        print(f"Added to {path}.")
    else:
        print(f"Opening {path} - add your prompts, save, and close it.")
        _open_in_editor(path)
    print(f"Check them:  python -m distillcat custom --dry-run")
    print(f"Run them:    python -m distillcat custom")


def cmd_custom(args):
    """Run ONLY your own prompts (my_prompts.txt by default)."""
    path = Path(args.file)
    prompts = _load_custom(path, must_exist=False)
    if not prompts:
        if not path.exists():
            path.write_text(TEMPLATE, encoding="utf-8")
        print(f"No prompts found in {path.resolve()}")
        if getattr(args, "dry_run", False):
            return
        print("Opening it for you: add your prompts, SAVE, then run this command again.")
        _open_in_editor(path)
        return
    args.custom, args.custom_only = str(path), True
    cmd_run(args)

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
    items = [x for x in items if not (x[0] in seen or seen.add(x[0]))]  # drop duplicate ids
    if args.repeat > 1:  # ask every prompt N times -> N samples each
        items = [(pid if k == 1 else f"{pid}~{k}", cat, prompt)
                 for pid, cat, prompt in items for k in range(1, args.repeat + 1)]
    if args.categories:
        items = [x for x in items if x[1] in args.categories]
    done = load_done_ids(args.out)
    todo = [x for x in items if x[0] not in done]
    if args.shuffle:
        random.shuffle(todo)
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(items)} selected, {len(done)} already in {args.out}, {len(todo)} to run.")
    if args.dry_run:
        for pid, cat, prompt in todo:
            print(f"  {pid} [{cat}] {prompt[:90]!r}")
        print("Dry run only - nothing was sent. Remove --dry-run to run for real.")
        return
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


def _add_browser_args(p, sub=False):
    """Browser options. Added to the main parser AND every subcommand, so they work on
    either side of the command: `--attach run` or `run --attach`."""
    d = (lambda v: argparse.SUPPRESS) if sub else (lambda v: v)
    p.add_argument("--profile", default=d("browser_profile"), help="browser profile dir")
    p.add_argument("--attach", action="store_true", default=d(False),
                   help="attach to your already-running Chrome/Edge (start it with 'launch')")
    p.add_argument("--port", type=int, default=d(9333), help="remote debugging port (default 9333)")
    p.add_argument("--browser", default=d("chrome"), choices=["chrome", "msedge", "chromium"],
                   help="installed browser to use (default: chrome)")


def _add_run_args(r):
    r.add_argument("--out", default="distillcat_dataset.jsonl", help="output dataset (.jsonl)")
    r.add_argument("--limit", type=int, help="max prompts this run")
    r.add_argument("--repeat", type=int, default=1, help="ask each prompt N times (N samples each)")
    r.add_argument("--shuffle", action="store_true")
    r.add_argument("--dry-run", action="store_true", help="show what would run, send nothing")
    r.add_argument("--no-thinking", action="store_true", help="don't enable DeepThink")
    r.add_argument("--delay", type=float, default=8.0, help="seconds between prompts (+ jitter)")
    r.add_argument("--settle", type=float, default=8.0, help="idle seconds that mean 'finished'")
    r.add_argument("--max-wait", type=float, default=600.0, help="per-prompt timeout seconds")
    r.add_argument("--limit-wait", type=float, default=300.0,
                   help="seconds to wait after a rate limit (doubles each retry)")
    r.add_argument("--limit-retries", type=int, default=3,
                   help="retries after a rate limit before stopping the run")


def main(argv=None):
    print(BANNER)
    ap = argparse.ArgumentParser(
        prog="distillcat",
        epilog="Quick start:  login -> add -> custom      (see README.md)")
    _add_browser_args(ap)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def new(name, fn, help):
        p = sub.add_parser(name, help=help)
        _add_browser_args(p, sub=True)
        p.set_defaults(fn=fn)
        return p

    new("login", cmd_login, "open the browser to log in to DeepSeek once")
    new("add", cmd_add, "add your own prompts (no text = open my_prompts.txt in Notepad)")
    new("custom", cmd_custom, "run ONLY your own prompts from my_prompts.txt")
    new("run", cmd_run, "run the built-in prompts (optionally plus yours)")
    new("list", cmd_list, "show built-in prompt categories")
    new("diagnose", cmd_diagnose, "test one tiny prompt and dump selector matches/screenshot")
    new("launch", cmd_launch, "start your Chrome/Edge with a debug port for --attach")

    a = sub.choices["add"]
    a.add_argument("--file", default=DEFAULT_CUSTOM, help=f"default: {DEFAULT_CUSTOM}")
    a.add_argument("text", nargs="*", help="prompt text to append (optional)")

    c = sub.choices["custom"]
    c.add_argument("--file", default=DEFAULT_CUSTOM,
                   help=f"prompt file (.txt or .jsonl), default: {DEFAULT_CUSTOM}")
    _add_run_args(c)
    c.set_defaults(categories=None)

    r = sub.choices["run"]
    r.add_argument("--categories", nargs="*", help="only these categories (built-in or your own)")
    r.add_argument("--custom", help="also include prompts from this file (.txt or .jsonl)")
    r.add_argument("--custom-only", action="store_true",
                   help=f"only your prompts (same as the 'custom' command)")
    _add_run_args(r)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())