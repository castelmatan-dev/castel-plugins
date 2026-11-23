# החוזה מול n8n, וההגדרה

הסקיל לא יודע איך n8n עושה את העבודה. הוא יודע רק את החוזה: אילו כתובות יש, מה שולחים ומה חוזר. מי שמשנה את הוורקפלואים צריך לשמור על החוזה הזה, ואז הסקיל לא צריך להשתנות.

## הגדרה (פעם אחת, אצל המשתמש)

1. **יצירת מפתח:** המשתמש מריץ בטרמינל **שלו**, לא דרך Claude: `openssl rand -hex 32`. אם Claude מריץ את הפקודה, המפתח מופיע בפלט שלו.
2. **ב-n8n:** credential מסוג Header Auth, עם השם `X-KB-Key` והערך מסעיף 1. מחברים אותו לכל ארבעת ה-webhooks.
3. **קובץ הסודות:** `~/.config/castel-kb/.env.local`, בהרשאות `600`. המשתמש פותח אותו בעורך, ממלא לפי `assets/.env.example`, שומר, **וסוגר את הקובץ** לפני שהוא חוזר לשיחה. קובץ פתוח על המסך נחשף ל-Claude.
4. **בדיקה:** `kb.py check`. היא מדווחת אם הקובץ קיים, אם יש בו את שני השמות, ואם n8n עונה. היא לא מדפיסה את הערכים.
5. **ב-home-products:** `check_setup.py save ... --vector on`.

## הפקודות של kb.py

```bash
K=<תיקיית הסקיל>/scripts/kb.py
python3 $K check
python3 $K list   [--product-id samsung-tv]
python3 $K store  --product-id samsung-tv --doc-kind manual --drive-file-id <id> --title "..." --source-url <drive link> [--lang en]
python3 $K status --upload-id <id> [--wait 120]
python3 $K search --product-id samsung-tv --query "אין תמונה, יש קול" [--doc-kind manual] [--k 6]
```
- הפלט הוא JSON ב-stdout. שגיאות מגיעות ל-stderr, עם קוד יציאה שאינו 0.
- קודי יציאה: `0` הצלחה · `1` שגיאה · `2` לא מוגדר · `3` לא נמצא · `4` רץ כבר (409).

## ה-webhooks (מה n8n צריך לממש)

לכל הבקשות יש header בשם `X-KB-Key`. בלעדיו חוזר 401. הכתובות יחסיות ל-`KB_N8N_URL`.

| פעולה | בקשה | תשובה |
|---|---|---|
| store | `POST /kb-products/store` `{product_id, doc_kind, drive_file_id, source_title, source_url, lang?}` | `202 {upload_id, source_id, version_no}` · `409` רץ כבר · `413` מעל התקרה · `400` שדה חסר או `doc_kind` לא מותר |
| status | `GET /kb-products/status?upload_id=` | `{status: queued\|parsing\|chunking\|embedding\|verifying\|active\|failed, stage, pages?, chunks?, error?}` |
| search | `POST /kb-products/search` `{query, product_id?, doc_kind?, k}` | `{chunks: [{content, page, section, source_title, source_url, doc_kind, product_id, version_no, similarity}]}` |
| list | `GET /kb-products/list?product_id=` | `{sources: [{source_id, product_id, doc_kind, source_title, version_no, pages, chunks, status, ingested_at}]}` |

## מה קורה ב-n8n (לתיעוד, לא לסקיל)

- **מבנה:**
  - **הקליטה** מבוססת על הדפוס של "נגריית אלון": גרסאות, החלפה אטומית, אימות ודיווח שלבים.
  - **הפירוק** נעשה בעותק של LlamaParse שמופעל רק מתוך וורקפלואו אחר. **אין לו כתובת חיצונית**, והוא מחזיר טקסט לכל עמוד בנפרד.
  - **ההמרה** היא OpenAI `text-embedding-3-small`.
  - **האחסון** ב-Supabase, בפרויקט ייעודי, בטבלה `kb_products`, עם `match_kb_products(query_embedding, match_count, filter jsonb)`.
- **השדות בכל קטע:**
  - `collection` (`products`)
  - `source_id` (`<product_id>-<doc_kind>`)
  - `source_title`, `source_url`, `page`, `section`, `version_no`, `ingested_at`, `lang`, `product_id`, `doc_kind`
- **הגבלות:**
  - `doc_kind` חייב להיות אחד מ-`manual`, `warranty`, `safety`, `usage_summary`.
  - תקרה של 500 עמודים 🔶.
  - tier זול של LlamaParse.
- **שמירה על זמינות:** וורקפלואו מתוזמן שפונה למסד כדי שהפרויקט לא יושהה. 🔶 צריך לבדוק את תנאי התוכנית.
