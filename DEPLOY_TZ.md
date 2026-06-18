# Texnik Topshiriq (TZ) — Nozimaxon Telegram Bot Deploy

**Buyurtmachi:** Linko
**Ijrochi:** Proxima (CI/CD / DevOps)
**Sana:** 2026-06-18
**Maqsad:** Nozimaxon AI sotuvchi botini lokal muhitdan productionga ko'chirish va
`https://nozimaxonbot.linko.uz` domeniga ulash.

> Hozir bot lokalda **Cloudflare Tunnel** orqali vaqtincha publik qilingan. Production'da
> tunnel kerak emas — bot to'g'ri server + reverse proxy + haqiqiy TLS sertifikat bilan
> `nozimaxonbot.linko.uz` da ishlashi kerak.

---

## 1. Loyiha haqida qisqacha

- **Til/Runtime:** Python **3.12**
- **Framework:** aiogram 3 (Telegram bot), aiohttp (webhook HTTP server)
- **AI:** Google Gemini (`google-genai` SDK)
- **Ma'lumotlar bazasi:** SQLite (fayl: `data/bot.db`)
- **Media fayllar:** `data/media/` (voice/video/photo — AmoCRM panel shu yerdan o'qiydi)
- **Tashqi integratsiyalar:** Telegram Bot API, Google Gemini API, AmoCRM (REST + Chats/amojo API), ixtiyoriy Google Sheets webhook
- **Ishga tushirish buyrug'i (production, webhook rejimi):**
  ```bash
  python -m src.main --webhook
  ```
- **Repozitoriy tarkibi (deploy uchun zarur):** `src/`, `pos/` (bilim bazasi), `assets/`, `requirements.txt`, `scripts/`
- **`.gitignore` da:** `.env`, `data/` — ya'ni secret'lar va ma'lumotlar git'da YO'Q.

---

## 2. Server talablari

| Resurs | Minimal | Tavsiya |
|--------|---------|---------|
| CPU | 1 vCPU | 2 vCPU |
| RAM | 512 MB | 1 GB |
| Disk | 5 GB SSD | 10 GB SSD (media o'sib boradi) |
| OS | Ubuntu 22.04 / 24.04 LTS (yoki Docker host) | |

- **Internet:** statik publik IP (yoki Cloudflare orqali).
- **NTP/vaqt sinxronizatsiyasi MAJBURIY** — AmoCRM Chats API HMAC imzosi `Date` sarlavhasiga
  bog'liq. Server soati noto'g'ri bo'lsa, imzo rad etiladi. `systemd-timesyncd`/`chrony` yoqilgan bo'lsin.

---

## 3. Domen va TLS (`nozimaxonbot.linko.uz`)

### 3.1 DNS
- `nozimaxonbot.linko.uz` uchun **A-record** → server publik IP.
- Yoki Cloudflare proxy ishlatilsa — `linko.uz` zonasida record yaratib, proxy (orange cloud) yoqiladi.

### 3.2 TLS sertifikat (MAJBURIY)
- Telegram webhook **faqat HTTPS** (port 443/88/8443) va **ishonchli CA** sertifikatini qabul qiladi.
  Self-signed YARAMAYDI (yoki alohida cert upload kerak — tavsiya etilmaydi).
- **Tavsiya:** Let's Encrypt (Caddy avtomatik) yoki Cloudflare Origin/Edge sertifikat.

### 3.3 Reverse proxy
TLS terminatsiya reverse proxy'da bo'ladi, ichkariga bot `127.0.0.1:8080` ga uzatiladi.

**MUHIM nuanslar:**
- Reverse proxy `POST` **body'ni o'zgartirmasligi** kerak (AmoCRM HMAC imzosi xom body bayt-ma-bayt
  bo'yicha tekshiriladi — har qanday qayta-kodlash/siqish imzoni buzadi).
- Quyidagi sarlavhalar **o'zgarmay** o'tishi shart:
  - `X-Telegram-Bot-Api-Secret-Token` (Telegram → bizning webhook secret tekshiruvi)
  - `X-Signature` (AmoCRM → HMAC imzo)
  - `Content-Type`, `Date`, `Content-MD5`
- Cloudflare proxy ishlatilsa: **Rocket Loader / Auto Minify / body transformatsiyalarni O'CHIRING**.

---

## 4. Publik endpointlar (proxy → bot :8080)

| Metod | Yo'l | Vazifa | Tashqi chaqiruvchi |
|-------|------|--------|--------------------|
| POST | `/telegram/webhook` | Telegram update'lari | Telegram serverlari |
| POST | `/amocrm/chats/{scope_id}` | Operator AmoCRM'da yozsa | AmoCRM (amojo) |
| GET | `/media/{filename}` | Voice/video/photo fayllarni AmoCRM uchun tarqatish | AmoCRM panel |
| GET | `/healthz` | Health check (`"ok"` qaytaradi) | Monitoring/orchestrator |

Hammasi `nozimaxonbot.linko.uz` ostida bo'ladi (masalan `https://nozimaxonbot.linko.uz/healthz`).

---

## 5. Tarmoq — chiquvchi (egress) ulanishlar

Bot quyidagi hostlarga **chiqa olishi** kerak (firewall'da ruxsat):
- `api.telegram.org` (Telegram Bot API)
- `generativelanguage.googleapis.com` (Gemini)
- `amojo.amocrm.ru` va `<subdomain>.amocrm.ru` (AmoCRM)
- `script.google.com` / `script.googleusercontent.com` (Google Sheets webhook — agar yoqilgan bo'lsa)

---

## 6. Muhit o'zgaruvchilari (`.env`)

`.env` git'da yo'q — **CI/CD secret store** orqali beriladi (kalitlar **kichik harf**).
`X` = majburiy, `S` = maxfiy (secret sifatida saqlansin).

| Kalit | X/S | Qiymat / izoh |
|-------|-----|---------------|
| `bot_token` | X, S | BotFather'dan |
| `gemini_key` | X, S | Google AI Studio'dan |
| `gemini_model` | | default `gemini-2.5-flash` |
| `db_path` | | default `data/bot.db` (volume ichida) |
| `log_level` | | `INFO` |
| `enable_gemini_cache` | | `true` |
| `webhook_url` | **X** | **`https://nozimaxonbot.linko.uz`** ← asosiy o'zgarish |
| `webhook_path` | | `/telegram/webhook` |
| `webhook_host` | | `0.0.0.0` |
| `webhook_port` | | `8080` |
| `webhook_secret` | S | tasodifiy uzun token (yangi generatsiya qiling) |
| `operator_chat_id` | | handoff signal yuboriladigan Telegram guruh/user ID |
| `throttle_seconds` | | `0.3` |
| `delayed_greeting_seconds` | | `40` |
| `followup_enabled` | | `true` |
| `followup_seconds` | | `7200` |
| `followup_max_attempts` | | `2` |
| `gsheets_webhook_url` | | Apps Script URL (ixtiyoriy) |
| `amocrm_subdomain` | | masalan `linkouz` |
| `amocrm_token` | S | AmoCRM long-lived token |
| `amocrm_pipeline_name` | | `POS New` |
| `amocrm_account_id` | | AmoCRM account id |
| `amocrm_chat_channel_id` | S | AmoCRM support'dan |
| `amocrm_chat_channel_secret` | S | HMAC secret |
| `amocrm_chat_scope_id` | X | connect handshake natijasi |
| `amocrm_chat_client_uuid` | | integratsiya UUID |
| `amocrm_manager_amojo_id` | | bo'sh bo'lsa startda avto-aniqlanadi |
| `amocrm_pipeline_id` | | `9857042` |
| `amocrm_telegram_stage_id` | | `86024566` |
| `amocrm_chat_default_stage_id` | | `78392566` |
| `amocrm_telegram_webhook_url` | | ixtiyoriy update-forward URL |

> Joriy ishlaydigan qiymatlarni Buyurtmachi alohida (xavfsiz kanal orqali) beradi.
> Bu faylga real secret YOZILMAYDI.

---

## 7. Doimiy ma'lumot (persistent volume) — KRITIK

`data/` katalogi **redeploy/restart'dan keyin saqlanishi SHART**:
- `data/bot.db` — foydalanuvchilar, suhbatlar, leadlar (yo'qolsa butun tarix yo'qoladi)
- `data/media/` — AmoCRM panel ko'rsatadigan media fayllar

➡️ Docker/K8s'da `data/` ni **named volume** yoki **persistent disk**ka montaj qiling.
Konteyner ichidagi ephemeral diskda QOLDIRMANG.

**Backup:** `data/bot.db` uchun kunlik backup (litefs/cp/`sqlite3 .backup`). Media — haftalik.

---

## 8. Build va ishga tushirish

### 8.1 Bog'liqliklar
```
aiogram>=3.13,<4
google-genai>=0.8
python-dotenv>=1.0
aiosqlite>=0.20
httpx>=0.27
```
> `aiohttp` to'g'ridan-to'g'ri ishlatiladi, lekin `aiogram` bog'liqligi sifatida o'rnatiladi.
> Aniqlik uchun `requirements.txt` ga `aiohttp>=3.10` ni alohida qo'shish tavsiya etiladi.

### 8.2 Dockerfile (tavsiya — namuna)
```dockerfile
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kod va statik resurslar
COPY src/ ./src/
COPY pos/ ./pos/
COPY assets/ ./assets/
COPY scripts/ ./scripts/

# Ma'lumotlar volume sifatida montaj qilinadi
RUN mkdir -p data/media && useradd -m app && chown -R app:app /app
USER app

EXPOSE 8080
CMD ["python", "-m", "src.main", "--webhook"]
```

### 8.3 docker-compose
Tayyor fayllar repo'da: **`Dockerfile`**, **`docker-compose.yml`**, **`.dockerignore`**.

- **Port:** tashqariga **`7040`** chiqariladi → konteyner ichidagi `8080` (webhook_port):
  `ports: ["7040:8080"]`.
- Reverse proxy (`nozimaxonbot.linko.uz`, TLS) host'ning **7040** portiga yo'naltiradi.
- `data/` → `bot-data` named volume (SQLite + media saqlanadi).

```bash
docker compose up -d --build     # ishga tushirish
docker compose logs -f           # loglar
docker compose down              # to'xtatish
```

### 8.4 Reverse proxy — Caddy (eng sodda, avto-TLS)
```
nozimaxonbot.linko.uz {
    reverse_proxy 127.0.0.1:8080
}
```

### 8.4b Reverse proxy — nginx (muqobil)
```nginx
server {
    listen 443 ssl http2;
    server_name nozimaxonbot.linko.uz;

    ssl_certificate     /etc/letsencrypt/live/nozimaxonbot.linko.uz/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/nozimaxonbot.linko.uz/privkey.pem;

    client_max_body_size 60m;     # media uchun

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_request_buffering off;   # body'ni o'zgartirmaslik
    }
}
```

---

## 9. CI/CD pipeline (Proxima zimmasida)

Kutilayotgan bosqichlar:
1. **Build:** Docker image (yorliq: git SHA + `latest`), registry'ga push.
2. **Secrets:** `.env` qiymatlari CI/CD secret store'dan inject qilinadi (loglarga TUSHMASIN).
3. **Deploy:** yangi image bilan konteynerni almashtirish, `data` volume saqlangan holda.
4. **Migratsiya:** alohida qadam YO'Q — bot startda SQLite sxemasini avtomatik yaratadi/yangilaydi
   (`Storage.init()` idempotent migratsiya).
5. **Health gate:** `/healthz` `200 "ok"` qaytarmaguncha deploy "muvaffaqiyatli" deb belgilanmasin.
6. **Restart policy:** `unless-stopped` / `always`. Crash bo'lsa avtomatik ko'tarilsin.

### ⚠️ Eng muhim cheklov — FAQAT BITTA INSTANS
Bot **horizontal scale qilinmaydi**. Sabablari:
- Telegram bitta bot uchun bitta webhook URL'ga ruxsat beradi.
- Pacing (debounce), kechiktirilgan salomlashish, follow-up, AmoCRM xabar tartibi (per-user lock +
  monoton timestamp) — barchasi **process xotirasida** saqlanadi.

➡️ **`replicas: 1`** bo'lishi shart. Ko'p instans = dublikat javoblar, buzilgan tartib.
Deploy strategiyasi: **recreate** (avval eskisini to'xtatib, keyin yangisini ko'tarish), `data` volume umumiy.

---

## 10. Deploy'dan keyingi sozlash (bir martalik)

1. **Telegram webhook** — qo'shimcha amal SHART EMAS: bot startda `webhook_url` asosida
   `https://nozimaxonbot.linko.uz/telegram/webhook` ni o'zi o'rnatadi (`set_webhook`).
   Tekshirish: `https://api.telegram.org/bot<token>/getWebhookInfo` → URL to'g'ri va `last_error` yo'q.

2. **AmoCRM Chats callback URL** — AmoCRM tomonida kanal callback manzilini yangi domenga yangilash kerak:
   `https://nozimaxonbot.linko.uz/amocrm/chats/{scope_id}`.
   (Avval Cloudflare Tunnel URL'i ko'rsatilgan bo'lsa — uni shu yangi URL'ga almashtiring.
   Kerak bo'lsa AmoCRM support orqali.)

3. **Media URL'lari** — avtomatik: `data/media` fayllar `webhook_url` asosida
   `https://nozimaxonbot.linko.uz/media/...` bo'lib beriladi. Qo'shimcha sozlash kerak emas.

4. **`amocrm_chat_scope_id`** allaqachon `.env` da bo'lsa — `scripts/amocrm_chat_connect.py` ni
   QAYTA ishga tushirish SHART EMAS (connect bir martalik handshake).

---

## 11. Qabul qilish (acceptance) checklisti

- [ ] `https://nozimaxonbot.linko.uz/healthz` → `200 "ok"`, TLS sertifikat haqiqiy (ishonchli CA).
- [ ] `getWebhookInfo` da URL `nozimaxonbot.linko.uz`, `pending_update_count` o'smayapti, `last_error_message` bo'sh.
- [ ] Telegram'da botga yozilganda Nozimaxon javob beradi (text + ovozli xabar).
- [ ] AmoCRM panelda suhbat ko'rinadi, operator yozsa Telegram'ga yetib boradi.
- [ ] AmoCRM'da ovozli xabar/rasm ochiladi (`/media/...` URL ishlayapti).
- [ ] Konteyner restart qilingach suhbat tarixi (`data/bot.db`) saqlangan.
- [ ] Faqat **1** instans ishlayapti.
- [ ] Server vaqti NTP bilan sinxron.
- [ ] `.env` secret'lari log'larga tushmaydi, repo'ga commit qilinmagan.

---

## 12. Monitoring, log, rollback

- **Loglar:** stdout (`INFO`), CI/CD log agregatorga yo'naltirilsin. `WARNING`/`ERROR` ga alert.
- **Monitoring:** `/healthz` ni 30s interval bilan tekshirish; Telegram `getWebhookInfo`
  `last_error_date` ni davriy kuzatish foydali.
- **Rollback:** oldingi image tegiga qaytarish + `data` volume o'zgarmaydi (sxema orqaga mos).
- **Restart resilience (ma'lum cheklov):** xotiradagi navbatlar (pacing/greeting/follow-up)
  restartda yo'qoladi — bu maqbul, suhbat tarixi DB'da saqlanadi, bot keyingi xabarda davom etadi.

---

## 13. Xavfsizlik

- `.env` va `data/` hech qachon image ichiga "pishirilmaydi"/commit qilinmaydi.
- `webhook_secret` — Telegram so'rovlarini tasdiqlaydi; uzun tasodifiy qiymat bo'lsin.
- `/media/{filename}` publik — fayl nomlari SHA256-hash (taxmin qilib bo'lmaydi), lekin
  imkon bo'lsa faqat AmoCRM IP'laridan kirishni cheklash (WAF/Cloudflare rule) tavsiya etiladi.
- Konteyner **root bo'lmagan** foydalanuvchi ostida ishlasin (Dockerfile'da `USER app`).
- Publik kirish faqat `nozimaxonbot.linko.uz` (443) orqali. Host'ning **7040** porti reverse
  proxy ortida turishi kerak — firewall'da 7040 ni faqat proxy/ichki tarmoq uchun oching,
  to'g'ridan-to'g'ri ochiq internetga qoldirmang.

---

## 14. Xulosa — lokaldan production'ga asosiy farqlar

| | Lokal (hozir) | Production (kerak) |
|---|---|---|
| Publik kirish | Cloudflare Tunnel | Server + reverse proxy + TLS |
| Domen | tunnel URL | `nozimaxonbot.linko.uz` |
| Ishga tushirish | qo'lda terminal | konteyner + restart policy |
| `webhook_url` | tunnel manzili | `https://nozimaxonbot.linko.uz` |
| Ma'lumot | lokal `data/` | persistent volume + backup |
| Instans | 1 (noutbuk) | 1 (server, scale YO'Q) |
