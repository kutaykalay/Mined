"""Background worker for the vault, launched detached by the SessionEnd/PreCompact hooks.

ozetle  : Haiku (no tools) summarizes the new part of a session transcript; this script
          writes the daily log entry and rewrites the Son-Oturum card itself.
derle   : Sonnet (no tools) extracts claims with verbatim quotes from queued daily entries; this
          script drops claims whose quotes are not in the entries (logged to durum/reddedilen.md);
          Sonnet (Read/Write/Edit/Glob/Grep only, no Bash) files only the verified claims
          into 20-Bilgi / 30-Kaynaklar pages, index, log, Konular and Cekirdek.
yedek   : at every session start (beyin.py): mask secrets, commit and push the vault.

Both run through `claude -p --safe-mode`, so they use the user's own subscription and
load no CLAUDE.md, hooks or skills (which also prevents hook recursion).
Failures are appended to .claude/durum/hatirlatma.txt and shown at the next session start.
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

VAULT = Path(__file__).resolve().parents[2]
STATE = VAULT / ".claude" / "durum"
SESSIONS = STATE / "oturumlar"
PENDING = STATE / "bekleyen"     # summaries that could not get the lock; the next worker runs them
QUEUE = STATE / "derlenecek.md"
LOG = STATE / "arka_plan.log"
REMINDER = STATE / "hatirlatma.txt"
LOCK = STATE / "kilit"
LAST_COMPILE = STATE / "son_derleme.txt"
SETTINGS = VAULT / ".claude" / "ayarlar.json"   # written by kurulum.py; hooks stay idle without it

HAIKU_MODEL = "haiku"
SONNET_MODEL = "sonnet"
TRANSCRIPT_CHAR_LIMIT = 80000
COMPILE_MIN_ENTRIES = 3          # compile once this many entries are queued...
COMPILE_MAX_AGE_HOURS = 20       # ...or when the last compile is older than this
AUTO_COMMIT = True               # snapshot the vault with git after each compile

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def load_settings():
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


CONFIG = load_settings()
USER = CONFIG.get("kullanici") or "Kullanıcı"
AUTO_PUSH = CONFIG.get("otomatik_push", True)  # kurulum.py asks; off keeps backups local-only
USER_LABEL = USER.replace("i", "İ").upper()  # speaker label in transcripts sent to the models


def personal(prompt):
    return prompt.replace("{kullanici}", USER)


# ---------- helpers ----------

def log(message):
    STATE.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {message}\n")


def remind(message):
    with REMINDER.open("a", encoding="utf-8") as f:
        f.write(message.strip() + "\n")


def claude_exe():
    found = shutil.which("claude")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
    return str(fallback) if fallback.exists() else None


def run_claude(prompt, model, cwd, tools, timeout):
    exe = claude_exe()
    if not exe:
        raise RuntimeError("claude komutu bulunamadı")
    command = [exe, "-p", "--model", model, "--safe-mode", "--no-session-persistence",
               "--output-format", "text", "--tools", ",".join(tools) if tools else ""]
    if tools:
        command += ["--allowedTools", ",".join(tools), "--permission-mode", "acceptEdits"]
    env = dict(os.environ, BEYIN_ARKA_PLAN="1")
    result = subprocess.run(command, input=prompt, text=True, encoding="utf-8", errors="replace",
                            capture_output=True, cwd=cwd, env=env, timeout=timeout,
                            creationflags=NO_WINDOW)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()[-300:]  # -p often prints errors to stdout
        raise RuntimeError(f"claude çıkış kodu {result.returncode}: {detail[:300]}")
    return result.stdout.strip()


class LockTimeout(RuntimeError):
    pass


class Lock:
    """Cross-process lock via exclusive file creation; a stale lock (>30 min) is taken over."""

    def __enter__(self):
        STATE.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + 1800  # a summary + compile can take ~20 min
        while True:
            try:
                fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - LOCK.stat().st_mtime > 1800:  # summary 300 + extract 300 + file 900 s
                        LOCK.unlink(missing_ok=True)
                        continue
                except OSError:
                    continue
                if time.time() > deadline:
                    raise LockTimeout("kilit alınamadı")
                time.sleep(3)

    def __exit__(self, *exc):
        LOCK.unlink(missing_ok=True)


def local_time(timestamp):
    try:
        return dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone()
    except (AttributeError, ValueError):
        return None


# ---------- secret masking ----------
# Safety net under the "never write secrets" rule: the transcript is masked before Haiku sees it,
# and changed .md files are masked before every git commit (the repo is pushed to GitHub).

SECRET_PATTERNS = [
    ("özel anahtar", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("API anahtarı", re.compile(r"\b(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}"
                                r"|github_pat_[A-Za-z0-9_]{40,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}"
                                r"|xox[abprs]-[A-Za-z0-9-]{10,})")),
    ("bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("şifre", re.compile(r"(?i)\b(?:şifre(?:m|si)?|parola(?:m|sı)?|password|passwd|pwd)\s*[:=]\s*(?![`'\x22*/\[])\S{4,}")),
    ("IBAN", re.compile(r"\bTR\d{2}(?:\s?\d{4}){5}\s?\d{2}\b")),
    ("kart no", re.compile(r"\b\d{4}(?:[ -]?\d{4}){3}\b")),
    ("TC kimlik no", re.compile(r"\b[1-9]\d{10}\b")),
]


def _luhn_ok(number):
    digits = [int(c) for c in number if c.isdigit()][::-1]
    total = sum(d if i % 2 == 0 else (d * 2 - 9 if d > 4 else d * 2) for i, d in enumerate(digits))
    return total % 10 == 0


def _tc_ok(number):
    d = [int(c) for c in number]
    return (sum(d[0:9:2]) * 7 - sum(d[1:8:2])) % 10 == d[9] and sum(d[:10]) % 10 == d[10]


CHECKS = {"kart no": _luhn_ok, "TC kimlik no": _tc_ok}


def mask_secrets(text):
    """Return (masked_text, found_kinds). Card and TC numbers are masked only if their checksum holds."""
    found = []
    for kind, pattern in SECRET_PATTERNS:
        check = CHECKS.get(kind)

        def replace(match, kind=kind, check=check):
            if check and not check(match.group(0)):
                return match.group(0)
            found.append(kind)
            return f"[GİZLİ: {kind}]"

        text = pattern.sub(replace, text)
    return text, sorted(set(found))


def mask_changed_notes(git):
    """Mask secrets in changed/new .md files before they are committed."""
    status = subprocess.run([git, "status", "--porcelain", "-z", "-uall", "--no-renames"], cwd=VAULT, capture_output=True,
                            text=True, encoding="utf-8", creationflags=NO_WINDOW).stdout
    for item in filter(None, status.split("\0")):
        path = VAULT / item[3:]
        if item[:2].strip() == "D" or path.suffix != ".md" or not path.is_file():
            continue
        original = path.read_text(encoding="utf-8", errors="replace")
        masked, kinds = mask_secrets(original)
        if kinds:
            path.write_text(masked, encoding="utf-8")
            log(f"gizli bilgi maskelendi: {item[3:]} ({', '.join(kinds)})")
            remind(f"{item[3:]} içinde gizli bilgi bulundu ve maskelendi ({', '.join(kinds)}). Kullanıcıya söyle.")


def strip_wrappers(text):
    """Drop <system-reminder>...</system-reminder> style blocks injected by the harness."""
    out, rest = [], text
    while "<system-reminder>" in rest:
        before, _, after = rest.partition("<system-reminder>")
        out.append(before)
        _, _, rest = after.partition("</system-reminder>")
    out.append(rest)
    return "".join(out).strip()


# ---------- transcript ----------

def read_new_turns(transcript_path, start_line):
    """Return (turns, user_prompt_count, first_time, total_lines) for lines after start_line."""
    turns, prompts, first_time, total = [], 0, None, 0
    with open(transcript_path, encoding="utf-8", errors="replace") as f:
        for number, line in enumerate(f):
            total = number + 1
            if number < start_line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            kind = entry.get("type")
            if kind not in ("user", "assistant") or entry.get("isMeta") or entry.get("isSidechain"):
                continue
            content = (entry.get("message") or {}).get("content")
            if isinstance(content, str):
                texts = [content]
            elif isinstance(content, list):
                texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            else:
                texts = []
            text = strip_wrappers("\n".join(texts))
            if not text or text.startswith("<"):  # slash-command wrappers, local command output
                continue
            when = local_time(entry.get("timestamp"))
            if kind == "user":
                prompts += 1
                first_time = first_time or when
                stamp = f"[{when:%H:%M}] " if when else ""
                turns.append(f"{stamp}{USER_LABEL}: {text}")
            else:
                turns.append(f"CLAUDE: {text}")
    return turns, prompts, first_time, total


def clip(turns):
    text = "\n\n".join(turns)
    if len(text) <= TRANSCRIPT_CHAR_LIMIT:
        return text
    head = text[:10000]
    tail = text[-(TRANSCRIPT_CHAR_LIMIT - 10000):]
    return head + "\n\n[... ortası kırpıldı ...]\n\n" + tail


# ---------- ozetle (Haiku) ----------

SUMMARY_PROMPT = """You are the note-taker for {kullanici}'s personal second brain (an Obsidian vault).
Below is a conversation between {kullanici} and Claude. Summarize it for the daily log.

Rules:
- Write everything in correct, natural Turkish. Technical terms, code and proper names stay as they are.
- Only state what is explicitly in the conversation. Do not infer, embellish or guess mechanisms.
- Split into one entry per distinct topic. For each: the question/goal and the conclusion or decision.
  Keep facts that will be useful later (numbers, names, commands, recommendations). No filler.
- "baglantilar": 1-4 short Turkish page names for durable topics, people, tools or projects in the
  entry (e.g. "Python asyncio", "Obsidian"). Empty if nothing durable.
- If the conversation is trivial (greetings, a unit conversion, a test), set "kaydet": false.
- If {kullanici} said "bunu kaydetme" (don't save this) about something, leave that part out entirely.
- Never include passwords, API keys, tokens, ID or card numbers.
- Sensitive personal topics (health, money, relationships): one neutral line at most.
- "son_oturum": 2-5 lines for the next session. What was done, what is open, the next step.
- "duzeltmeler": corrections {kullanici} made about how Claude should behave, in {kullanici}'s own words
  (e.g. "daha kısa cevap ver"). Empty list if none.

Reply with JSON only, no code fences:
{"kaydet": true, "oturum_basligi": "...", "girisler": [{"saat": "HH:MM", "baslik": "...", "maddeler": ["..."], "baglantilar": ["..."]}], "son_oturum": "...", "duzeltmeler": []}

Conversation:
"""


def parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


PAGE_DIRS = ("10-Projeler", "20-Bilgi", "30-Kaynaklar", "80-Hafiza", "90-Arsiv")


def fold_name(text):
    return text.replace("İ", "i").replace("I", "ı").lower().strip()


def page_names():
    """{folded name: (link target, label or None)} from file names, H1 titles and frontmatter
    aliases, so daily-log links point only at pages that exist."""
    pages = [p for d in PAGE_DIRS for p in (VAULT / d).rglob("*.md")]
    stems = [p.stem for p in pages]
    names = {}
    for p in pages:
        target = p.stem if stems.count(p.stem) == 1 else p.relative_to(VAULT).with_suffix("").as_posix()
        head = p.read_text(encoding="utf-8", errors="replace")[:800]
        titles = re.findall(r"^# (.+)$", head, re.M)[:1]
        found = re.search(r"^aliases:\s*\[(.*?)\]", head, re.M)
        aliases = [a.strip().strip("'\"") for a in found.group(1).split(",")] if found else []
        for name in [p.stem, *titles, *aliases]:
            if name:
                names.setdefault(fold_name(name), (target, None if name == target else name))
    return names


def link_or_text(name, names):
    """[[page]] when the name matches an existing page, else plain text (no broken links)."""
    hit = names.get(fold_name(name))
    if not hit:
        return name
    target, label = hit
    return f"[[{target}|{label}]]" if label else f"[[{target}]]"


def daily_path(day):
    return VAULT / "50-Gunler" / f"{day.isoformat()}.md"


def append_daily(day, entries):
    path = daily_path(day)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(f"---\ntur: gun\ntarih: {day.isoformat()}\ntags: [gunluk]\n---\n\n# {day.isoformat()}\n",
                        encoding="utf-8")
    blocks, names = [], page_names()
    for e in entries:
        heading = " · ".join(p for p in (e.get("saat"), e.get("baslik") or "Oturum") if p)
        lines = [f"\n## {heading}"]
        lines += [f"- {m}" for m in e.get("maddeler", [])]
        links = [l for l in e.get("baglantilar", []) if l]
        if links:
            lines.append("\nBağlantılar: " + " · ".join(link_or_text(l, names) for l in links))
        blocks.append("\n".join(lines))
    text = "\n".join(blocks) + "\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(text)
    return text


CARD_MARK = re.compile(r"<!-- oturum: (\S+) -->")


def rewrite_last_session(title, body, when, session, wrote_before):
    """Put this session's card on top. Replace the top card only if it is this session's own,
    so a parallel session's card is moved to the history instead of being overwritten."""
    path = VAULT / "80-Hafiza" / "Son-Oturum.md"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    header_end = next((i for i, l in enumerate(lines) if l.startswith("## Oturum:")), len(lines))
    prev_start = next((i for i, l in enumerate(lines) if l.startswith("## Önceki Oturumlar")), len(lines))
    header = lines[:header_end] or ["---", "tur: hafiza", "tags: [hafiza]", "---", "", "# Son Oturum", ""]
    old_card = lines[header_end:prev_start]
    previous = [l for l in lines[prev_start + 1:] if l.strip() and l.strip() != "(henüz yok)"]
    owner = next((m.group(1) for l in old_card for m in [CARD_MARK.search(l)] if m), None)
    own_card = owner == session or (owner is None and wrote_before)  # unmarked: card from before marks
    if old_card and not own_card:
        gist = old_card[0].removeprefix("## Oturum:").strip()
        first = next((l.strip() for l in old_card[1:] if l.strip() and not CARD_MARK.search(l)), "")
        previous.insert(0, f"- {gist}: {first}"[:220])
    card = [f"## Oturum: {when:%Y-%m-%d %H:%M} · {title}", f"<!-- oturum: {session} -->", body.strip(), ""]
    path.write_text("\n".join(header + card + ["## Önceki Oturumlar"] + previous[:10]) + "\n", encoding="utf-8")


def summarize(session, transcript, reason):
    SESSIONS.mkdir(parents=True, exist_ok=True)
    state_file = SESSIONS / f"{session}.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    turns, prompts, first_time, total = read_new_turns(transcript, state.get("satir", 0))
    if prompts == 0:
        log(f"ozetle {session[:8]} ({reason}): yeni mesaj yok")
        return
    conversation, kinds = mask_secrets(clip(turns))
    if kinds:
        log(f"ozetle {session[:8]}: transkriptte gizli bilgi maskelendi ({', '.join(kinds)})")
    result = parse_json(run_claude(personal(SUMMARY_PROMPT) + conversation, HAIKU_MODEL, str(STATE), [], 300))
    state["satir"] = total
    if result.get("kaydet") and result.get("girisler"):
        day = (first_time or dt.datetime.now()).date()
        written = append_daily(day, result["girisler"])
        rewrite_last_session(result.get("oturum_basligi") or result["girisler"][0].get("baslik", "Oturum"),
                             result.get("son_oturum", ""), first_time or dt.datetime.now(),
                             session, wrote_before=state.get("kart_yazildi", False))
        state["kart_yazildi"] = True
        corrections = "".join(f"\n- DÜZELTME: {c}" for c in result.get("duzeltmeler", []))
        with QUEUE.open("a", encoding="utf-8") as f:
            f.write(f"\n### {day.isoformat()}{written}{corrections}\n")
        log(f"ozetle {session[:8]} ({reason}): {len(result['girisler'])} giriş, {prompts} mesaj")
    else:
        log(f"ozetle {session[:8]} ({reason}): önemsiz, kaydedilmedi")
    state_file.write_text(json.dumps(state), encoding="utf-8")


# ---------- derle (Sonnet) ----------

EXTRACT_PROMPT = """You are the first stage of the background compiler of {kullanici}'s second brain.
Below are new daily-log entries. Extract every durable claim worth keeping in the knowledge pages:
a reusable fact, decision, lesson, recommendation, project progress, a lasting fact about {kullanici},
or a "DÜZELTME" (a correction of how Claude should behave). Skip trivia.

For each claim give one or more quotes copied VERBATIM from the entries below (character for
character, 4 to 30 words each, from a single line). The quotes are checked by a script against
the entries; a claim whose quote does not appear exactly is thrown away. Do not quote headings
alone, do not fix typos inside a quote, do not join text from two lines into one quote.
The claim itself must say nothing that its quotes do not support.

- "iddia": the claim as one standalone Turkish sentence (names spelled out, no "bu", "o").
- "konu": the page it most likely belongs to (e.g. "Obsidian", "İkinci beyin kurulumu").
- "tarih": the YYYY-MM-DD of the "### " section the quote comes from.
- "tur": one of bilgi | kaynak | proje | konu | cekirdek | duzeltme.

Reply with JSON only, no code fences:
{"iddialar": [{"iddia": "...", "alintilar": ["..."], "konu": "...", "tarih": "YYYY-MM-DD", "tur": "bilgi"}]}

Entries:
"""

QUOTE_MIN_WORDS = 3


def normalize_quote(text):
    """Compare quotes loosely on formatting only: case, whitespace, markdown marks, quote styles."""
    text = text.translate(str.maketrans("‘’“”", "''\"\"")).casefold()
    text = re.sub(r"[*_`#\[\]|]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def verify_claims(claims, source):
    """Keep claims whose every quote appears verbatim (after normalize_quote) in the source."""
    haystack = normalize_quote(source)
    kept, rejected = [], []
    for claim in claims:
        quotes = [q for q in claim.get("alintilar") or [] if isinstance(q, str)]
        ok = bool(claim.get("iddia")) and bool(quotes) and all(
            len(q.split()) >= QUOTE_MIN_WORDS and normalize_quote(q) in haystack for q in quotes)
        (kept if ok else rejected).append(claim)
    return kept, rejected


def format_claims(claims):
    lines = []
    for c in claims:
        quotes = " / ".join(f"«{q}»" for q in c["alintilar"])
        lines.append(f"- [{c.get('tarih', '?')}] [{c.get('tur', 'bilgi')}] ({c.get('konu', '')}) "
                     f"{c['iddia']}\n  kaynak: {quotes}")
    return "\n".join(lines)


COMPILE_PROMPT = """You are the background compiler of {kullanici}'s second brain. The current directory is
the Obsidian vault. First read CLAUDE.md (conventions and folder map) and 20-Bilgi/index.md.
You are NOT in a chat: nobody will answer questions. Do the work, then reply with one line.

Below are claims extracted from new daily-log entries. Each one was checked against its verbatim
source quote. Write ONLY what these claims state: add no facts from your own knowledge, from
50-Gunler/ or from guesses about what a claim implies. Existing pages may be read for context,
placement and linking. Do not edit 50-Gunler/ or Son-Oturum.md. The "[tur]" and "(konu)" tags are
hints; claims tagged "duzeltme" are the DÜZELTME lines of rule 7. Keep each claim's meaning exact:
do not change its status or tense ("benimsendi" is not "uygulandı", "planlandı" is not "yapıldı") and
do not change who did what (a tool's user is not {kullanici} unless the claim says so).
File the claims into the vault:

1. For each durable topic (a reusable fact, decision, lesson, recommendation), update the matching
   page in 20-Bilgi/ or create one following Sablonlar/Not.md (fill the frontmatter with real dates).
   Check index.md and existing file names first; never create a duplicate. People, tools, sites
   and products go to 30-Kaynaklar/. Project work goes to 10-Projeler/<proje>/.
   Page names must match the [[links]] in the entries where sensible, so the links resolve.
2. Merge, don't append blindly: integrate new info into the right section, note the date for
   facts that may change, and mark contradictions with the older statement instead of silently
   dropping it. Never delete text {kullanici} wrote by hand.
3. Link related pages with [[wikilinks]]; every new page links to or from an existing one.
4. 20-Bilgi/index.md is the catalog of EVERY page in 10-Projeler/, 20-Bilgi/ and 30-Kaynaklar/:
   add or update one row per page you touched (use the full link path when the name is ambiguous).
   Append one line per change to 20-Bilgi/log.md:
   `- YYYY-MM-DD HH:MM · oluşturuldu|güncellendi · [[sayfa]] · neden`.
5. Update 80-Hafiza/Konular.md for multi-session matters (start / progress / close; edit in place).
   Each thread is a `### name` heading plus ONE `**Durum:**` line (current state + next step, under
   ~300 characters). Replace the line, never append history to it; history goes on the project page.
6. Add lasting, fundamental facts about {kullanici} (work, interests, preferences, goals) to
   80-Hafiza/Cekirdek.md.
7. For each "duzeltme" claim, add or update a rule in 80-Hafiza/Kurallar.md as
   `- **kural:** ... **neden:** ...`, unless an equivalent rule already exists.
8. Skip trivia. Everything you write is Turkish. Never write secrets.

Final reply: one Turkish line listing the pages created or updated.

Verified claims:
"""

REJECTED = STATE / "reddedilen.md"


def should_compile():
    if not QUEUE.exists() or not QUEUE.read_text(encoding="utf-8").strip():
        return False
    entries = QUEUE.read_text(encoding="utf-8").count("\n## ")
    try:
        age_hours = (time.time() - float(LAST_COMPILE.read_text(encoding="utf-8"))) / 3600
    except (OSError, ValueError):  # first run: start the 20 h clock now
        LAST_COMPILE.write_text(str(time.time()), encoding="utf-8")
        age_hours = 0
    return entries >= COMPILE_MIN_ENTRIES or age_hours >= COMPILE_MAX_AGE_HOURS


PROJECT_BACKUP = VAULT / ".claude" / "proje-oturumlari"


def copy_project_sessions():
    """ECC session files live in each project's excluded .claude/ (in no git). Keep a masked copy
    here so they reach the private vault repo; the project repos never see them."""
    for source in VAULT.parent.glob("*/.claude/session-data/*-session.tmp"):
        project = source.parents[2].name
        if project == VAULT.name:
            continue
        target = PROJECT_BACKUP / project / source.name
        try:
            if target.exists() and target.stat().st_mtime >= source.stat().st_mtime:
                continue
            masked, kinds = mask_secrets(source.read_text(encoding="utf-8", errors="replace"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(masked, encoding="utf-8")
            if kinds:
                log(f"proje oturumu yedeklenirken gizli bilgi maskelendi: {project}/{source.name} ({', '.join(kinds)})")
        except OSError as exc:
            log(f"proje oturumu yedeklenemedi: {project}/{source.name}: {exc}")


def git_snapshot(message):
    git = shutil.which("git")
    if not (AUTO_COMMIT and git and (VAULT / ".git").exists()):
        return
    run = lambda *a: subprocess.run([git, *a], cwd=VAULT, capture_output=True, text=True, creationflags=NO_WINDOW)
    copy_project_sessions()
    mask_changed_notes(git)
    run("add", "-A")
    result = run("commit", "-q", "-m", message)
    if result.returncode not in (0, 1):  # 1 = nothing to commit
        log(f"git commit başarısız: {result.stderr.strip()[:200]}")


def push():
    git = shutil.which("git")
    if not (AUTO_COMMIT and AUTO_PUSH and git and (VAULT / ".git").exists()):
        return
    run = lambda *a: subprocess.run([git, *a], cwd=VAULT, capture_output=True, text=True, creationflags=NO_WINDOW)
    ahead = run("rev-list", "--count", "@{u}..HEAD").stdout.strip()
    if ahead in ("", "0"):
        return
    result = run("push", "-q")
    if result.returncode != 0:
        raise RuntimeError(f"git push başarısız: {result.stderr.strip()[:200]}")
    log(f"yedek: {ahead} commit GitHub'a gönderildi")


def backup():
    """Session start: commit whatever changed in the vault, including pages written by other
    project sessions through the bridge, and push."""
    git_snapshot(f"beyin: vault yedeği (oturum başı) {dt.datetime.now():%Y-%m-%d %H:%M}")
    push()


BATCH = STATE / "derleniyor.md"


def park(session, transcript, reason):
    PENDING.mkdir(parents=True, exist_ok=True)
    data = {"oturum": session, "transkript": transcript, "neden": reason}
    (PENDING / f"{session}.json").write_text(json.dumps(data), encoding="utf-8")
    log(f"ozetle {session[:8]} ({reason}): kilit alınamadı, bekleyen/ altına alındı")


def run_pending():
    """Summarize sessions parked by a worker that timed out on the lock. Called while holding it."""
    if not PENDING.exists():
        return
    for item in sorted(PENDING.glob("*.json")):
        try:
            data = json.loads(item.read_text(encoding="utf-8"))
            summarize(data["oturum"], data["transkript"], data.get("neden", "son") + ", bekleyen")
        except Exception as exc:
            log(f"HATA bekleyen {item.stem[:8]}: {exc}")
        item.unlink(missing_ok=True)


def recover_batch():
    """A compile killed mid-run (shutdown, sleep) leaves its batch behind; put it back in the queue.
    Only called while holding the lock, so no other compile can be using the batch."""
    if not BATCH.exists():
        return
    pending = QUEUE.read_text(encoding="utf-8") if QUEUE.exists() else ""
    QUEUE.write_text(BATCH.read_text(encoding="utf-8") + pending, encoding="utf-8")
    BATCH.unlink()
    log("yarım kalan derleme kuyruğa geri alındı")


def compile_queue():
    if not QUEUE.exists():
        return
    batch = BATCH
    QUEUE.replace(batch)
    try:
        git_snapshot(f"beyin: vault yedeği (derleme öncesi) {dt.datetime.now():%Y-%m-%d %H:%M}")
        source = batch.read_text(encoding="utf-8")
        claims = parse_json(run_claude(personal(EXTRACT_PROMPT) + source, SONNET_MODEL, str(STATE), [], 300))
        kept, rejected = verify_claims(claims.get("iddialar") or [], source)
        if rejected:
            with REJECTED.open("a", encoding="utf-8") as f:
                f.write(f"\n### {dt.datetime.now():%Y-%m-%d %H:%M}\n"
                        + "\n".join(f"- {json.dumps(c, ensure_ascii=False)}" for c in rejected) + "\n")
        answer = "işlenecek doğrulanmış iddia yok"
        if kept:
            answer = run_claude(personal(COMPILE_PROMPT) + format_claims(kept), SONNET_MODEL, str(VAULT),
                                ["Read", "Write", "Edit", "Glob", "Grep"], 900)
        LAST_COMPILE.write_text(str(time.time()), encoding="utf-8")
        batch.unlink(missing_ok=True)
        git_snapshot(f"beyin: vault yedeği (derleme sonrası) {dt.datetime.now():%Y-%m-%d %H:%M}")
        log(f"derle: {len(kept)} iddia doğrulandı, {len(rejected)} reddedildi · "
            f"{answer.splitlines()[-1] if answer else 'tamam'}")
    except Exception:
        # put the batch back in front of anything queued meanwhile
        pending = QUEUE.read_text(encoding="utf-8") if QUEUE.exists() else ""
        QUEUE.write_text(batch.read_text(encoding="utf-8") + pending, encoding="utf-8")
        batch.unlink(missing_ok=True)
        raise
    push()  # outside the try: the batch is already filed, a failed push must not re-queue it


# ---------- entry ----------

def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="komut", required=True)
    s = sub.add_parser("ozetle")
    s.add_argument("--oturum", required=True)
    s.add_argument("--transkript", required=True)
    s.add_argument("--neden", default="son")
    d = sub.add_parser("derle")
    d.add_argument("--zorla", action="store_true")
    sub.add_parser("yedek")
    args = parser.parse_args()

    if not SETTINGS.exists():  # not set up yet (e.g. a fresh template clone): never touch notes or git
        return
    if args.komut == "yedek" and LOCK.exists():  # a summary/compile is running; it commits and pushes itself
        return
    try:
        with Lock():
            recover_batch()
            run_pending()
            if args.komut == "yedek":
                backup()
            elif args.komut == "ozetle":
                summarize(args.oturum, args.transkript, args.neden)
                if should_compile():
                    compile_queue()
            elif args.zorla or should_compile():
                compile_queue()
    except LockTimeout:
        if args.komut == "ozetle":  # don't lose the session: park it for the next worker
            park(args.oturum, args.transkript, args.neden)
        else:
            log(f"{args.komut}: kilit alınamadı, atlandı")
    except Exception as exc:
        log(f"HATA {args.komut}: {exc}")
        remind(f"Arka plan {args.komut} başarısız oldu ({dt.datetime.now():%Y-%m-%d %H:%M}): {str(exc)[:200]}. "
               "Ayrıntı: .claude/durum/arka_plan.log. Kullanıcıya kısaca söyle.")


if __name__ == "__main__":
    main()
