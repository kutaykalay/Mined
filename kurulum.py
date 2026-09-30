"""İkinci beyin kurulumu. Bu klasörde bir kez çalıştır: `python kurulum.py` (Windows'ta `py -3 kurulum.py`).

Yaptıkları:
  1. Adını sorar, .claude/ayarlar.json'a yazar (hook'lar bu dosya olmadan hiçbir şey yapmaz).
  2. Hook komutlarını işletim sistemine göre ayarlar (Windows: `py -3`, diğerleri: `python3`).
  3. Hafıza dosyalarındaki {{KULLANICI}} ve {{TARIH}} yer tutucularını doldurur.
  4. Git yedeğini ayarlar: otomatik commit her zaman açık, GitHub'a otomatik push isteğe bağlı.
  5. İsteğe bağlı: bütün projelerde çalışan global hook'ları ve "ikinci beyin köprüsü"nü
     ~/.claude/settings.json ve ~/.claude/CLAUDE.md'ye ekler (önce yedek alır).

Tekrar çalıştırmak güvenlidir. Soru sormadan kurmak için: --ad "Adın" --evet
"""
import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

VAULT = Path(__file__).resolve().parent
SETTINGS = VAULT / ".claude" / "ayarlar.json"
PLACEHOLDER_FILES = ["CLAUDE.md", "80-Hafiza/Kurallar.md", "80-Hafiza/Cekirdek.md",
                     "80-Hafiza/Konular.md", "20-Bilgi/log.md"]
HOME_CLAUDE = Path.home() / ".claude"
PROJECT_EVENTS = [("SessionStart", "oturum-basi", 15), ("UserPromptSubmit", "hatirlat", 10),
                  ("PreCompact", "sikistirma", 15), ("SessionEnd", "oturum-sonu", 15)]
BRIDGE_MARK = "## İkinci beyin köprüsü"


def ask(question, default, assume_yes):
    if assume_yes:
        return default
    suffix = " [E/h] " if default else " [e/H] "
    answer = input(question + suffix).strip().lower()
    return default if not answer else answer.startswith("e")


def python_launcher():
    if os.name == "nt":
        return "py -3" if shutil.which("py") else "python"
    return "python3"


def fill_placeholders(name):
    today = dt.date.today().isoformat()
    for rel in PLACEHOLDER_FILES:
        path = VAULT / rel
        if path.exists():
            text = path.read_text(encoding="utf-8")
            path.write_text(text.replace("{{KULLANICI}}", name).replace("{{TARIH}}", today), encoding="utf-8")


def set_launcher(launcher):
    for rel in (".claude/settings.json", "CLAUDE.md"):
        path = VAULT / rel
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("py -3 ", launcher + " "), encoding="utf-8")


def git(*args):
    return subprocess.run(["git", *args], cwd=VAULT, capture_output=True, text=True)


def setup_git(assume_yes):
    if not shutil.which("git"):
        print("! git bulunamadı: otomatik yedek kapalı kalır. Kurduktan sonra `git init` yeterli.")
        return False
    if not (VAULT / ".git").exists():
        git("init", "-q")
        print("✓ git deposu oluşturuldu (yerel yedek: her oturum başında ve her derlemede commit).")
    remote = git("remote", "get-url", "origin").stdout.strip()
    if not remote:
        print("  GitHub'a da yedeklemek istersen PRIVATE bir repo aç ve `git remote add origin <url>` yap.")
        return True  # push() skips on its own while there is no upstream
    print(f"\n  Bu klasörün git remote'u: {remote}")
    print("  Notların kişiseldir. Bu repo PRIVATE değilse ya da şablonun kendi reposuysa push'u AÇMA.")
    return ask("  Notlar her oturumda bu remote'a otomatik push'lansın mı?", False, assume_yes)


def add_global_hooks(launcher):
    path = HOME_CLAUDE / "settings.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if path.exists():
        shutil.copy2(path, path.with_name("settings.json.yedek"))
    script = (VAULT / ".claude" / "hooks" / "proje.py").as_posix()
    hooks = data.setdefault("hooks", {})
    added = 0
    for event, command, timeout in PROJECT_EVENTS:
        groups = hooks.setdefault(event, [])
        if any("proje.py" in h.get("command", "") for g in groups for h in g.get("hooks", [])):
            continue
        groups.append({"hooks": [{"type": "command", "command": f'{launcher} "{script}" {command}',
                                  "timeout": timeout}]})
        added += 1
    HOME_CLAUDE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"✓ ~/.claude/settings.json: {added} global hook eklendi (yedek: settings.json.yedek).")


def add_bridge():
    path = HOME_CLAUDE / "CLAUDE.md"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if BRIDGE_MARK in text:
        print("✓ ~/.claude/CLAUDE.md: köprü zaten var.")
        return
    vault = VAULT.as_posix()
    block = f"""
{BRIDGE_MARK}

İkinci beynim `{vault}` klasöründeki Obsidian vault'u. O klasörde çalışıyorsan bu bölümü yok say;
oranın kendi CLAUDE.md'si geçerli.

- **Gerektiğinde oku.** Kişisel bağlam (kim olduğum, tercihlerim, hedeflerim) lazımsa
  `{vault}/80-Hafiza/Cekirdek.md` ve `{vault}/80-Hafiza/Kurallar.md` dosyalarını oku. Kurallar bağlayıcıdır.
- **Sadece kalıcı olanı yaz.** Önemli bir proje kararı, dönüm noktası veya ders olduğunda ya da
  "beyne yaz" / "not al" dediğimde bunu `{vault}/10-Projeler/<proje adı>.md` sayfasına ekle
  (önce oku, sonuna ekle; yoksa `Sablonlar/Not.md`'den oluştur ve `20-Bilgi/index.md`'ye bir satır,
  `20-Bilgi/log.md`'ye bir satır ekle). Türkçe yaz, tek satırla onayla.
- **Kopyalama.** Kod, log ve sırlar proje reposunda kalır; vault'a girmez.
"""
    if path.exists():
        shutil.copy2(path, path.with_name("CLAUDE.md.yedek"))
    path.write_text(text.rstrip() + "\n" + block if text else block.lstrip(), encoding="utf-8")
    print("✓ ~/.claude/CLAUDE.md: ikinci beyin köprüsü eklendi.")


def main():
    if sys.version_info < (3, 9):
        sys.exit("Python 3.9 veya üstü gerekli.")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to a legacy code page
    parser = argparse.ArgumentParser(description="İkinci beyin kurulumu")
    parser.add_argument("--ad", help="Claude'un sana hitap edeceği ad")
    parser.add_argument("--evet", action="store_true", help="soruları varsayılanla geç")
    parser.add_argument("--global", dest="global_", action=argparse.BooleanOptionalAction, default=None,
                        help="global proje hook'ları ve köprü (varsayılan: sor)")
    args = parser.parse_args()

    current = {}
    if SETTINGS.exists():
        current = json.loads(SETTINGS.read_text(encoding="utf-8"))
        print(f"Kurulum daha önce yapılmış ({current.get('kullanici')}); ayarlar güncellenecek.")

    name = args.ad or current.get("kullanici") or ("" if args.evet else input("Adın ne? ").strip())
    if not name:
        sys.exit("Ad gerekli (--ad ile de verebilirsin).")

    if not shutil.which("claude"):
        print("! `claude` komutu bulunamadı. Claude Code'u kur: https://claude.com/claude-code")
    launcher = python_launcher()
    set_launcher(launcher)
    fill_placeholders(name)
    push = setup_git(args.evet)

    SETTINGS.write_text(json.dumps({"kullanici": name, "otomatik_push": push}, indent=2,
                                   ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"✓ .claude/ayarlar.json yazıldı (ad: {name}, otomatik push: {'açık' if push else 'kapalı'}).")

    want_global = args.global_
    if want_global is None:
        want_global = ask("\nBaşka proje klasörlerinde de beyni kullanmak için global hook ve köprü eklensin mi?\n"
                          "  (~/.claude/settings.json ve ~/.claude/CLAUDE.md değişir, önce yedek alınır)",
                          False, args.evet)
    if want_global:
        add_global_hooks(launcher)
        add_bridge()

    print(f"\nHazır. Bu klasörde `claude` yaz ve konuşmaya başla.\n"
          f"Obsidian'da bu klasörü vault olarak açıp notları okuyabilirsin.")


if __name__ == "__main__":
    main()
