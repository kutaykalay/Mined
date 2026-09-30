---
name: gecmis-aktar
description: Import old chat history exports (ChatGPT or Claude conversations.json, Gemini Takeout MyActivity.json) into this vault as knowledge pages and daily-log entries. Use when the user says "geçmişimi aktar", "eski sohbetlerimi yükle", "chatgpt geçmişi", "takeout".
---

# Geçmiş aktarımı

Reply in Turkish. Private data is involved, so the consent gate below is mandatory.

## Consent gate (do not skip)

1. Explain the data flow: the export is read locally; the chosen conversations are summarized by
   you in this session (the user's own Claude subscription) and written only into this vault.
   Nothing else is uploaded anywhere.
2. Say that private or sensitive conversations are included unless filtered. Offer a date range
   and a case-insensitive exclude-keyword list (a matching conversation is skipped entirely).
3. Ask for the export path as text. Don't guess it and don't open the file yet. Then ask for
   permission to run a **read-only preview**.
4. Preview: write a small Python script to `.claude/durum/` that parses the file and prints only
   aggregates: conversation count, date range, number skipped by each filter, and the top titles.
   Print no message bodies.
5. Show the preview, restate the filters, and require an explicit "evet" before writing anything.

## Formats

- ChatGPT: `conversations.json` in the export zip; each conversation has a `mapping` tree of
  nodes with `message.author.role` and `message.content.parts`. Skip system and tool messages.
- Claude: `conversations.json`; each item has `chat_messages` with `sender` and `text`.
- Gemini: `My Activity/Gemini Apps/MyActivity.json`.
- Above 50 MB, default to the last 12 months unless the user sets a narrower range.

## Import

Process month by month, so each batch fits in context:

1. Use the script to dump one month's filtered conversations into a temporary text file under
   `.claude/durum/`, and read it.
2. Apply the vault's normal memory protocol: durable knowledge goes into `20-Bilgi/` (merged
   into existing pages, never duplicated); lasting facts about the user go into
   `80-Hafiza/Cekirdek.md`; and one summary per month goes to
   `50-Gunler/ice-aktarim-YYYY-MM.md` (a short entry per conversation worth keeping; skip
   trivial ones).
3. Update `20-Bilgi/index.md`, add one line per month to `20-Bilgi/log.md`, and delete the
   temporary dump.
4. After each month, report progress in one line. Stop if the user asks.

Never write the raw export, or a full transcript, into the vault.
