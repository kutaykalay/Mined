"""Global hooks for the user's project folders (registered in ~/.claude/settings.json).

oturum-basi : in a project with the ECC save-session command, put the latest session file's topic
              and "Exact Next Step" into context (full load is still /ecc-resume-session).
oturum-sonu,
sikistirma  : launch `sigorta` detached. The vault has its own hooks, so these skip it.
sigorta     : safety net for a forgotten /ecc-save-session. If the session had real work after the
              last saved session file, Haiku writes `<date>-<id>-otomatik-session.tmp` in the same
              format. Never touches the vault; the vault note stays save-session's job.
hatirlat    : UserPromptSubmit, everywhere. Matches the prompt against page names in
              20-Bilgi/index.md and injects only the matching names and paths (no LLM).
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import arka_plan as ap  # noqa: E402  (shared helpers: run_claude, masking, transcript parsing)

VAULT = ap.VAULT
STATE = ap.STATE
PROJECT_STATE = STATE / "proje-oturumlar"
TERMS_CACHE = STATE / "hatirlat_terimler.json"
SESSION_DIR = Path(".claude") / "session-data"
MIN_PROMPTS = 2          # fewer user messages than this since the last save: nothing to rescue
NEXT_STEP_LIMIT = 800
MAX_MATCHES = 5


# ---------- project detection ----------

def in_vault(path):
    try:
        path.resolve().relative_to(VAULT)
        return True
    except ValueError:
        return False


def project_root(cwd):
    """Nearest folder (cwd or a parent) that has the ECC save-session command, else None."""
    for folder in [cwd, *cwd.parents]:
        commands = folder / ".claude" / "commands"
        if commands.is_dir() and any(commands.glob("*save-session.md")):
            return folder
    return None


def session_files(root):
    folder = root / SESSION_DIR
    return sorted(folder.glob("*-session.tmp"), key=lambda p: p.stat().st_mtime) if folder.is_dir() else []


def section(text, heading):
    match = re.search(rf"^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    return match.group(1).strip().strip("-").strip() if match else ""


def field(text, name):
    match = re.search(rf"^\*\*{re.escape(name)}:\*\*\s*(.+)$", text, re.M)
    return match.group(1).strip() if match else ""


# ---------- oturum-basi ----------

def session_start(payload, cwd):
    if payload.get("source") == "compact" or in_vault(cwd):
        return
    root = project_root(cwd)
    if not root:
        return
    files = session_files(root)
    if not files:
        return
    latest = files[-1]
    text = latest.read_text(encoding="utf-8", errors="replace")
    step = section(text, "Exact Next Step")
    if len(step) > NEXT_STEP_LIMIT:
        step = step[:NEXT_STEP_LIMIT].rstrip() + " [...]"
    updated = field(text, "Last Updated") or f"{dt.datetime.fromtimestamp(latest.stat().st_mtime):%Y-%m-%d %H:%M}"
    lines = [f"[Proje: {root.name}] Son oturum dosyası: {SESSION_DIR.as_posix()}/{latest.name} (güncelleme: {updated})"]
    if "otomatik-session" in latest.name:
        lines.append("Not: bu dosyayı save-session değil otomatik sigorta yazdı; doğrulanmamış özet.")
    if field(text, "Topic"):
        lines.append(f"Konu: {field(text, 'Topic')}")
    if step:
        lines.append(f"Sıradaki adım: {step}")
    lines.append("Tam bağlam için kullanıcı /ecc-resume-session çalıştırabilir; iş devam ediyorsa bunu önerebilirsin.")
    emit("SessionStart", "\n".join(lines))


# ---------- oturum-sonu / sikistirma ----------

def launch_insurance(payload, cwd, reason):
    if in_vault(cwd):
        return
    root = project_root(cwd)
    session, transcript = payload.get("session_id"), payload.get("transcript_path")
    if not (root and session and transcript):
        return
    spawn([sys.executable, str(Path(__file__).resolve()), "sigorta", "--oturum", session,
           "--transkript", transcript, "--klasor", str(root), "--neden", reason])


def spawn(command):
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
               "stderr": subprocess.DEVNULL, "cwd": str(VAULT), "close_fds": True}
    if os.name == "nt":
        base = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(command, creationflags=base | subprocess.CREATE_BREAKAWAY_FROM_JOB, **options)
            return
        except OSError:
            subprocess.Popen(command, creationflags=base, **options)
    else:
        subprocess.Popen(command, start_new_session=True, **options)


# ---------- sigorta (Haiku) ----------

INSURANCE_PROMPT = """You write a session handoff file for {kullanici}'s coding project, because they closed the
session without running /ecc-save-session. Below is the conversation (user and assistant text, plus
"[araç: ...]" lines naming files the assistant edited). If an earlier automatic file of this same
session is given, merge it: keep what is still true, update what changed.

Rules:
- Use exactly the Markdown format below, headings in English as shown. Content in Turkish; code,
  paths, commands and names stay as they are.
- Only state what is explicit in the conversation. "What WORKED" needs evidence seen in the
  conversation (test output, a run); otherwise put it under "What Has NOT Been Tried Yet" or
  "Blockers". Never invent file states, errors or results.
- Never include passwords, API keys, tokens, ID or card numbers.
- If the conversation has no real project work (only greetings, a quick question, or nothing
  worth resuming), reply with the single word ONEMSIZ and nothing else.

Format:
# Session: YYYY-MM-DD

**Started:** ...
**Last Updated:** ...
**Project:** {project}
**Topic:** ...
**Kaynak:** otomatik sigorta (save-session yapılmadı; transkriptten Haiku yazdı, doğrulanmamış)

---

## What We Are Building
## What WORKED (with evidence)
## What Did NOT Work (and why)
## What Has NOT Been Tried Yet
## Current State of Files
## Decisions Made
## Blockers & Open Questions
## Exact Next Step

(Separate sections with "---". Write "Yok." for an empty section.)
"""

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def read_turns(transcript, start_line):
    """Return (turns, total_lines); each turn is (time, is_user, text)."""
    turns, total = [], 0
    with open(transcript, encoding="utf-8", errors="replace") as f:
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
            when = ap.local_time(entry.get("timestamp"))
            content = (entry.get("message") or {}).get("content")
            parts = []
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "text":
                        parts.append(c.get("text", ""))
                    elif c.get("type") == "tool_use" and c.get("name") in EDIT_TOOLS:
                        target = (c.get("input") or {}).get("file_path") or (c.get("input") or {}).get("notebook_path")
                        if target:
                            parts.append(f"[araç: {c['name']} {target}]")
            text = ap.strip_wrappers("\n".join(parts))
            if not text or text.startswith("<"):
                continue
            turns.append((when, kind == "user", text))
    return turns, total


def insurance(session, transcript, root, reason):
    PROJECT_STATE.mkdir(parents=True, exist_ok=True)
    state_file = PROJECT_STATE / f"{session}.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    turns, total = read_turns(transcript, state.get("satir", 0))
    tag = f"sigorta {root.name}/{session[:8]} ({reason})"

    saved = [p for p in session_files(root) if "otomatik-session" not in p.name]
    if saved:  # work before the last real save is already covered
        since = dt.datetime.fromtimestamp(saved[-1].stat().st_mtime).astimezone()
        turns = [t for t in turns if t[0] is None or t[0] > since]
    prompts = sum(1 for t in turns if t[1])
    state["satir"] = total
    if prompts < MIN_PROMPTS:
        ap.log(f"{tag}: save sonrası iş yok ({prompts} mesaj), atlandı")
        state_file.write_text(json.dumps(state), encoding="utf-8")
        return

    lines = []
    for when, is_user, text in turns:
        stamp = f"[{when:%H:%M}] " if when and is_user else ""
        lines.append(f"{stamp}{ap.USER_LABEL if is_user else 'CLAUDE'}: {text}")
    conversation, kinds = ap.mask_secrets(ap.clip(lines))
    if kinds:
        ap.log(f"{tag}: transkriptte gizli bilgi maskelendi ({', '.join(kinds)})")

    folder = root / SESSION_DIR
    folder.mkdir(parents=True, exist_ok=True)
    existing = next(iter(folder.glob(f"*-{session[:8]}-otomatik-session.tmp")), None)
    earlier = ""
    if existing:
        earlier = "\n\nEarlier automatic file of this session:\n" + existing.read_text(encoding="utf-8", errors="replace")
    prompt = ap.personal(INSURANCE_PROMPT).replace("{project}", root.name) + earlier + "\n\nConversation:\n" + conversation
    result = ap.run_claude(prompt, ap.HAIKU_MODEL, str(STATE), [], 300).strip()
    if result.startswith("```"):
        result = result.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    if not result or result.upper().startswith("ONEMSIZ"):
        ap.log(f"{tag}: önemsiz, yazılmadı")
    else:
        result, _ = ap.mask_secrets(result)
        target = existing or folder / f"{dt.date.today().isoformat()}-{session[:8]}-otomatik-session.tmp"
        ensure_excluded(root)
        target.write_text(result + "\n", encoding="utf-8")
        ap.log(f"{tag}: {prompts} mesaj → {target.name}")
    state_file.write_text(json.dumps(state), encoding="utf-8")


def ensure_excluded(root):
    """Personal tooling never enters a project repo: keep .claude/ in .git/info/exclude."""
    git_dir = root / ".git"
    if not git_dir.is_dir():
        return
    probe = subprocess.run(["git", "check-ignore", "-q", ".claude/session-data/x"], cwd=root,
                           capture_output=True, creationflags=ap.NO_WINDOW)
    if probe.returncode != 0:
        exclude = git_dir / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as f:
            f.write("\n.claude/\n")


# ---------- hatirlat ----------

def fold(text):
    return text.replace("İ", "i").replace("I", "ı").lower()


def page_terms():
    """[(term, link, relative path)] from index.md, cached until index.md changes."""
    index = VAULT / "20-Bilgi" / "index.md"
    try:
        stamp = index.stat().st_mtime
    except OSError:
        return []
    try:
        cache = json.loads(TERMS_CACHE.read_text(encoding="utf-8"))
        if cache.get("stamp") == stamp:
            return cache["terms"]
    except (OSError, ValueError, KeyError):
        pass
    terms = []
    for line in index.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\|\s*\[\[([^\]|\\]+)(?:\\?\|([^\]]+))?\]\]", line)
        if not match:
            continue
        target, alias = match.group(1).strip(), (match.group(2) or "").strip()
        stem = Path(target).name
        path = next((p for p in VAULT.glob(f"[123]0-*/**/{stem}.md")), None)
        rel = path.relative_to(VAULT).as_posix() if path else None
        names = {stem, alias} if alias else {stem}
        if path:  # frontmatter aliases
            head = path.read_text(encoding="utf-8", errors="replace")[:600]
            found = re.search(r"^aliases:\s*\[(.*?)\]", head, re.M)
            if found:
                names |= {a.strip().strip("'\"") for a in found.group(1).split(",") if a.strip()}
        link = alias or stem
        terms += [(n, link, rel) for n in names if len(n) >= 3]
    STATE.mkdir(parents=True, exist_ok=True)
    TERMS_CACHE.write_text(json.dumps({"stamp": stamp, "terms": terms}, ensure_ascii=False), encoding="utf-8")
    return terms


def recall(payload):
    prompt = fold(payload.get("prompt") or "")
    if len(prompt) < 3:
        return
    hits = {}
    for term, link, rel in page_terms():
        t = re.escape(fold(term))
        # short names must stand alone ("git" ≠ "gitti"); longer ones may take a Turkish suffix
        pattern = rf"(?<!\w){t}(?!\w)" if len(term) < 5 else rf"(?<!\w){t}"
        if re.search(pattern, prompt) and link not in hits:
            hits[link] = rel
    if not hits:
        return
    items = [f"[[{link}]] ({VAULT.name}/{rel})" if rel else f"[[{link}]]" for link, rel in list(hits.items())[:MAX_MATCHES]]
    emit("UserPromptSubmit", "[Beyin] Bu mesajla ilgili olabilecek notlar: " + ", ".join(items)
         + ". Konuyla gerçekten ilgiliyse cevaplamadan önce oku.")


def emit(event, context):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}))


# ---------- main ----------

def main():
    if os.environ.get("BEYIN_ARKA_PLAN") or not ap.SETTINGS.exists():  # workers, or vault not set up
        return
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "sigorta":
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("komut")
        parser.add_argument("--oturum", required=True)
        parser.add_argument("--transkript", required=True)
        parser.add_argument("--klasor", required=True)
        parser.add_argument("--neden", default="son")
        args = parser.parse_args()
        try:
            insurance(args.oturum, args.transkript, Path(args.klasor), args.neden)
        except Exception as exc:
            ap.log(f"HATA sigorta {Path(args.klasor).name}: {exc}")
            ap.remind(f"Proje oturum sigortası başarısız oldu ({Path(args.klasor).name}, "
                      f"{dt.datetime.now():%Y-%m-%d %H:%M}): {str(exc)[:200]}. Kullanıcıya kısaca söyle.")
        return
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except ValueError:
        payload = {}
    cwd = Path(payload.get("cwd") or os.getcwd())
    try:
        if command == "oturum-basi":
            session_start(payload, cwd)
        elif command == "oturum-sonu":
            launch_insurance(payload, cwd, "son")
        elif command == "sikistirma":
            launch_insurance(payload, cwd, "sıkıştırma")
        elif command == "hatirlat":
            recall(payload)
    except Exception as exc:  # a hook must never break the session
        ap.log(f"HATA proje {command}: {exc}")


if __name__ == "__main__":
    main()
