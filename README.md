<img src="assets/logo.svg" width="96" align="left"> 

# DistillCat 🐱

Sends a library of prewritten prompts (HTML frontend, CSS design, JS backend, Python, logic, bug fixing) to **chat.deepseek.com** one by one, and saves the **question, thinking process and answer** into one `.jsonl` dataset.

## Setup
```powershell
python -m pip install -r requirements.txt
```
No Playwright browser download is needed: DistillCat uses your installed Chrome (or Edge with `--browser msedge`).

## Usage
Recommended: let DistillCat open your installed Chrome itself (own profile, log in once):
```powershell
python -m distillcat --browser chrome login
python -m distillcat --browser chrome diagnose     # test one tiny prompt first
python -m distillcat --browser chrome run --limit 3
```
Use the same `--browser` value every time: Chrome, Edge and bundled Chromium each keep their own login.

Attach to a browser you started with `launch` (uses debug port 9333; change with `--port`, use the same
value for `launch` and `--attach`):
```powershell
python -m distillcat launch                # log in to DeepSeek in the window that opens, leave it open
python -m distillcat --attach diagnose
python -m distillcat --attach run --limit 3
```
## Custom mode (only your own prompts)
```powershell
python -m distillcat add                    # opens my_prompts.txt in Notepad -> add prompts, save, close
python -m distillcat custom --dry-run       # check what will run (sends nothing)
python -m distillcat custom                 # run ONLY your prompts -> distillcat_dataset.jsonl
```
Everything else works the same, e.g. `custom --attach --limit 5 --delay 20 --no-thinking --out mine.jsonl`.
If `my_prompts.txt` has no prompts yet, `custom` opens it for you instead of failing.

**Prompt file formats** (auto-detected, any editor/encoding):
- **One prompt per line** (write `\n` for a line break). Lines starting with `#` are ignored.
- **Multi-line prompts**: separate them with a line containing only `---`.
  ```
  Fix this code:
  def f(x): return x+
  ---
  Write a poem about cats.
  ```
- `.jsonl` with `{"category": "...", "prompt": "..."}` per line (use `--file prompts.jsonl`).

**Several samples of the same prompt:** list it multiple times, or use `--repeat 3` (each sample is saved
separately). Runs are resumable: finished prompts are skipped.

Add one prompt from the command line: `python -m distillcat add "Write a Rust hello world"`.
Built-ins + yours together: `python -m distillcat run --custom my_prompts.txt`.
Browser flags (`--attach`, `--browser`, `--port`) work before or after the command.
Other options:
```powershell
python -m distillcat list                  # show categories
python -m distillcat --attach run --categories python logic --limit 5
python -m distillcat --attach run --custom my_prompts.txt --no-thinking
```
> Attach mode opens its own tab in that browser and leaves it running afterwards.
> Chrome 136+ refuses remote debugging on your *default* profile, so `launch` uses a dedicated
> profile folder (`browser_profile`). Your login there is remembered, but your normal bookmarks and
> extensions aren't. This is a Chrome security restriction that DistillCat doesn't work around.
- Runs are **resumable**: prompt ids already in the output file are skipped.
- Each prompt uses a **fresh chat**, so samples don't contaminate each other.
- `--custom` accepts `.txt` (one prompt per line) or `.jsonl` (`{"category":..., "prompt":...}`).
- Add your own built-in prompts in `distillcat/prompts.py`.

## Output format (one JSON object per line)
```json
{"id":"python-003","category":"python","question":"...","thinking":"...","output":"...",
 "source":"chat.deepseek.com","deepthink":true,"timestamp":"2026-10-30T16:00:00+00:00"}
```

## If you hit a rate limit
DistillCat detects DeepSeek's "too frequent / server busy / usage limit" messages, waits
(`--limit-wait`, default 5 min, doubling each time, `--limit-retries` default 3), retries the same prompt,
and if it's still limited it **stops and keeps everything saved**. Just re-run the same command later
(e.g. a few hours, or the next day) and it resumes where it stopped.
To avoid limits: use a bigger `--delay` (e.g. `--delay 30`), smaller batches (`--limit 10`), and
`--no-thinking` (shorter answers). Don't use extra accounts or proxies to get around a limit.
For high volume, use the official API (paid, with proper rate limits) instead.

## If extraction breaks
DeepSeek's web UI changes. CSS selectors live in `distillcat/selectors.json`
(`input`, `think_container`, `response`, ...). Update them using the browser's DevTools.

## Notes
- You log in yourself; DistillCat never bypasses login, captchas or rate limits (it pauses and waits for you).
- Keep delays reasonable. Check DeepSeek's Terms of Use for automated access and for how
  you may use outputs. The official API (`deepseek-reasoner`, returns `reasoning_content`) is
  the more reliable and sanctioned route for bulk collection.
