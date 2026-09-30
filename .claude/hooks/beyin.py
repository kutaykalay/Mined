"""Second-brain hooks for this vault. Fast, stdlib only; heavy work goes to arka_plan.py.

oturum-basi (SessionStart): injects rules, last-session card, active threads,
    knowledge index and the tail of today's/yesterday's daily note, each capped,
    plus any warnings left by the background worker.
    It also starts arka_plan.py yedek in the background (commit + push the vault).
oturum-sonu (SessionEnd) / sikistirma (PreCompact): launch arka_plan.py detached,
    so Haiku summarizes the session (and Sonnet compiles when due) after Claude exits.
"""
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

VAULT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
MEMORY = VAULT / "80-Hafiza"
STATE = VAULT / ".claude" / "durum"
REMINDER = STATE / "hatirlatma.txt"
QUEUE = STATE / "derlenecek.md"
WORKER = Path(__file__).resolve().parent / "arka_plan.py"
SETTINGS = VAULT / ".claude" / "ayarlar.json"

TOTAL_LIMIT = 16000
SIZE_WARNINGS = {"Son-Oturum.md": 4000, "Konular.md": 8000, "Kurallar.md": 6000}
DAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]


def read_lines(path):
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def cap(text, limit, name):
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n[not: {name} {limit} karakterde kırpıldı, dosyanın tamamını oku]"


def last_session():
    kept, active = [], False
    for line in read_lines(MEMORY / "Son-Oturum.md"):
        if line.startswith("## Oturum:"):
            active = True
        elif active and line.startswith("## "):
            break
        if active and not line.startswith("<!-- oturum:"):
            kept.append(line)
    return "\n".join(kept[:50])


def active_threads():
    picked, active = [], False
    for line in read_lines(MEMORY / "Konular.md"):
        if line.startswith("## "):
            active = line.startswith("## Aktif")
            continue
        if active and (line.startswith("### ") or line.startswith("**Durum:**")):
            picked.append(line)
    return "\n".join(picked[:24])


def rules():
    lines = read_lines(MEMORY / "Kurallar.md")
    if lines and lines[0] == "---":  # drop frontmatter
        end = next((i for i, l in enumerate(lines[1:], 1) if l == "---"), 0)
        lines = lines[end + 1:]
    return "\n".join(lines[:60]).strip()


def daily_tail():
    today = dt.date.today()
    for day in (today, today - dt.timedelta(days=1)):
        path = VAULT / "50-Gunler" / f"{day.isoformat()}.md"
        if path.exists():
            return day.isoformat(), "\n".join(read_lines(path)[-25:])
    return None, ""


def size_warnings():
    out = []
    for name, limit in SIZE_WARNINGS.items():
        try:
            size = len((MEMORY / name).read_text(encoding="utf-8"))
        except OSError:
            continue
        if size > limit:
            out.append(f"{name} {size} karakter (sınır {limit})")
    return out


def session_start(payload):
    sections = []
    warnings = []
    if REMINDER.exists():
        warnings += [l for l in REMINDER.read_text(encoding="utf-8").splitlines() if l.strip()][-5:]
        REMINDER.unlink(missing_ok=True)
    big = size_warnings()
    if big:
        warnings.append("Hafıza dosyaları şişti: " + ", ".join(big) + ". Uygun bir anda beyin-bakim skill'ini öner.")
    if warnings:
        sections.append("[Hafıza uyarıları]\n" + "\n".join(f"- {w}" for w in warnings))

    now = dt.datetime.now()
    pending = QUEUE.read_text(encoding="utf-8").count("\n## ") if QUEUE.exists() else 0
    sections.append(f"[Şu an] {now:%Y-%m-%d %H:%M} ({DAYS[now.weekday()]})"
                    + (f" · bilgi sayfalarına henüz işlenmemiş {pending} günlük giriş var" if pending else ""))
    for title, text, limit in (
        ("Kurallar", rules(), 4000),
        ("Son Oturum", last_session(), 4000),
        ("Aktif Konular", active_threads(), 2000),
        ("Bilgi İndeksi (20-Bilgi/index.md)", "\n".join(read_lines(VAULT / "20-Bilgi" / "index.md")[:150]), 6000),
    ):
        if text.strip():
            sections.append(f"[Hafıza: {title}]\n{cap(text, limit, title)}")
    day, tail = daily_tail()
    if tail.strip():
        sections.append(f"[Hafıza: Günlük {day} (son 25 satır)]\n{cap(tail, 2500, 'günlük')}")
    sections.append("[Hafıza] Oturum özetleri arka planda otomatik yazılır; CLAUDE.md'deki canlı görevleri uygula.")

    context = "\n\n".join(sections)
    if len(context) > TOTAL_LIMIT:
        context = context[:TOTAL_LIMIT] + "\n[not: oturum bağlamı 16.000 karakterde kırpıldı, beyin-bakim çalıştır]"
    # ASCII-escaped JSON survives any console code page on Windows.
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}))


def launch_worker(payload, reason):
    session, transcript = payload.get("session_id"), payload.get("transcript_path")
    if not session or not transcript:
        return
    spawn([sys.executable, str(WORKER), "ozetle", "--oturum", session,
           "--transkript", transcript, "--neden", reason])


def spawn(command):
    """Run the worker detached so it never delays or dies with the Claude session."""
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
               "stderr": subprocess.DEVNULL, "cwd": str(VAULT), "close_fds": True}
    if os.name == "nt":
        base = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:  # outlive Claude Code even if it runs hooks inside a job object
            subprocess.Popen(command, creationflags=base | subprocess.CREATE_BREAKAWAY_FROM_JOB, **options)
            return
        except OSError:
            subprocess.Popen(command, creationflags=base, **options)
    else:
        subprocess.Popen(command, start_new_session=True, **options)


def main():
    if os.environ.get("BEYIN_ARKA_PLAN"):  # never recurse from the background worker
        return
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except ValueError:
        payload = {}
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if not SETTINGS.exists():  # fresh template clone: stay idle until kurulum.py has run
        if command == "oturum-basi":
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
                  "[Beyin] Kurulum yapılmamış; hafıza hook'ları kapalı. Kullanıcıya bu klasörde "
                  "`python kurulum.py` çalıştırmasını söyle (README'deki Kurulum bölümü)."}}))
        return
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        if command == "oturum-basi":
            session_start(payload)
            spawn([sys.executable, str(WORKER), "yedek"])
        elif command == "oturum-sonu":
            launch_worker(payload, "son")
        elif command == "sikistirma":
            launch_worker(payload, "sikistirma")
    except Exception as exc:  # a hook must never break the session
        print(f"beyin hook hatası: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
