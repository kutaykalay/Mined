---
name: beyin-bakim
description: Health check and cleanup for this second-brain vault - duplicate or orphan pages, broken wikilinks, index/log drift, oversized memory files, stale threads. Use when the user says "bakım yap", "beyni kontrol et", "notları toparla", or when the session-start context shows a memory warning.
---

# Beyin bakımı

Reply to the user in Turkish. Work in two phases: **report, then fix**. Never delete a note's
content; merge or move to `90-Arsiv/` instead.

## 1. Survey (read-only)

Check each item, gathering facts with Glob/Grep/Read:

1. **Index drift:** every page in `10-Projeler/`, `20-Bilgi/` and `30-Kaynaklar/` (except
   `index.md`, `log.md`) has a row in `20-Bilgi/index.md`, and every row points to an existing page.
0. **Background worker:** read the tail of `.claude/durum/arka_plan.log` for failures, and check
   whether `.claude/durum/derlenecek.md` has entries waiting to be compiled.
2. **Duplicates:** pages covering the same topic (similar names, overlapping aliases or content).
3. **Broken links:** `[[...]]` targets that match no file name or alias in the vault.
4. **Orphans:** pages in `20-Bilgi/` and `30-Kaynaklar/` with no incoming and no outgoing links.
5. **Inbox:** notes in `00-Gelen/` older than 7 days that were never filed.
6. **Memory hygiene** (character counts):
   - `80-Hafiza/Son-Oturum.md` > 4000: the "Önceki Oturumlar" list is too long.
   - `80-Hafiza/Konular.md` > 8000, or threads marked Aktif with no update for 30+ days.
   - `80-Hafiza/Kurallar.md` > 6000, or rules that conflict or repeat.
7. **Contradictions:** statements in `20-Bilgi/` that conflict with a newer daily log or page.
8. **Secrets:** anything that looks like a password, API key, token or card number (report the
   file and line only, never print the value).

## 2. Report

Give a short Turkish summary: what is healthy, what was found (counts plus the top examples), and
the proposed fixes as a numbered list. Ask once for approval, unless the user already said to fix.

## 3. Fix (after approval)

- Duplicates: merge into the richer page, turn the other into an alias or move it to
  `90-Arsiv/`, and repoint links.
- Broken links: fix the spelling, or create a stub page only if the topic is clearly worth one.
- Orphans: link them from the closest related page.
- Inbox: file each note into the right folder, or move it to `90-Arsiv/`.
- Oversized memory files: move old sections **verbatim** to `80-Hafiza/Arsiv/<dosya>-YYYY-MM.md`
  and leave the recent part in place. Move stale Aktif threads to Kapanan with a one-line reason.
- Secrets: ask the user before redacting.
- Log every change as one line in `20-Bilgi/log.md` (`· bakım ·`) and update `index.md`.

Finish with one line: what changed, and anything left for the user to decide.
