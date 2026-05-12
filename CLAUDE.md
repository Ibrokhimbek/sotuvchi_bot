# Jaloliddin — Linko-POS sotuvchi bot

Telegram boti. Instagram reklama → link → Telegram orqali mijozlar keladi.
"Jaloliddin" nomli AI sotuvchi Linko-POS dasturini chakana savdo nuqtalariga sotadi.

## Asosiy talablar
- Bot juda **tabiiy** bo'lishi kerak — odam kabi, hatto kichik imloviy "xato"lar bilan.
- Hech qachon o'zini bot/AI deb tan olmaydi. U Jaloliddin — 27 yoshli, 3 yillik
  tajribali sotuv menejeri.
- Matn, ovoz, dumaloq video (video note), oddiy video, rasm — barchasiga javob beradi.
- Mahsulot, narx, funksiya nomlari, telefon raqamlari **xatosiz** yoziladi.

## Arxitektura
- **aiogram 3** polling rejimida (webhook emas — sodda boshlash uchun)
- **google-genai** (`gemini-2.5-flash` default) — multimodal, async
- **SQLite + aiosqlite** — suhbat tarixi
- Bilim bazasi: `pos/*.json` to'g'ridan-to'g'ri system_instruction'ga yuklanadi
  (~200KB, embedding/vector kerakmas). Keyinroq context caching qo'shiladi.

## Modul javobgarliklari
- `src/config.py` — `.env` o'qiydi (`bot_token`, `gemini_key`, ...). Keylar lowercase.
- `src/knowledge/loader.py` — `pos/` papkadagi JSON fayllarni bitta matnga aylantiradi.
- `src/ai/prompts.py` — Jaloliddin shaxsiyati (PERSONA) va `build_system_prompt`.
- `src/ai/gemini.py` — `GeminiAgent.reply(history, user_text, media)`.
- `src/bot/humanize.py` — typing delay, apostrof tashlash, xabarni 2 qismga bo'lish.
- `src/bot/handlers.py` — text/voice/video_note/video/photo handlerlari.
- `src/storage/db.py` — users + messages jadvallari, suhbat tarixi.
- `src/main.py` — kompozitsion ildiz, polling.

## Ishga tushirish
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m src.main
```

`.env` da kerakli kalitlar: `bot_token`, `gemini_key`. `.env.example`'ga qarang.

## Xavfsizlik
- `.env` `.gitignore`'da. Hech qachon git'ga tushmasligi kerak.
- `bot_token` va `gemini_key` real qiymatlar bilan — agar tasodifan tushib qolsa,
  darhol Telegram BotFather va Google AI Studio'dan rotate qilish kerak.

## Nima qilinmagan (kelajakda)
- Gemini Context Caching (token tejash uchun)
- Lead'lar uchun alohida jadval va CRM eksporti
- Operatorga uzatish (handoff) mantiqi
- Webhook rejimi (production uchun)
- PostgreSQL'ga migratsiya (hozircha SQLite yetadi)
- Voice javob (TTS — masalan ElevenLabs uzb)
- Rate limiting / anti-spam
