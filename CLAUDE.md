# Nozimaxon — Linko-POS sotuvchi bot

Telegram boti. Instagram reklama → link → Telegram orqali mijozlar keladi.
"Nozimaxon" nomli AI sotuv menejeri Linko-POS dasturini chakana savdo nuqtalariga
sotadi.

## Asosiy talablar
- Bot juda **tabiiy** bo'lishi kerak — odam kabi, kichik imloviy "xato"lar bilan.
- Hech qachon o'zini bot/AI deb tan olmaydi. U Nozimaxon — 26 yoshli ayol, 3 yillik
  tajribali sotuv menejeri.
- Uzun matnlarni **2-5 ta qisqa xabarga uzib** yuboradi (Telegram chat uslubida).
- Matn, ovoz, dumaloq video, oddiy video, rasm — barchasiga javob beradi.
- Mahsulot, narx, funksiya nomlari, telefon raqamlari **xatosiz** yoziladi.

## Arxitektura
- **aiogram 3** polling rejimida
- **google-genai** (`gemini-2.5-flash` default) — multimodal, async, **function calling**,
  **context caching** (system prompt 217KB cache'da turadi, har so'rovda yuborilmaydi)
- **SQLite + aiosqlite** — users, messages, leads jadvallari
- **ElevenLabs TTS** (ixtiyoriy) — Nozimaxon ba'zan voice javob beradi
- **ThrottleMiddleware** — bitta foydalanuvchidan minimal interval (default 1.2s)

## Function calling
Gemini ikkita tool ishlatadi:
- `save_lead(contact_name, phone, business_type, store_size, notes)` — mijoz biznes
  ma'lumoti yoki telefon bersa, model bu funksiyani chaqiradi, `leads` jadvaliga yoziladi
- `request_operator(reason)` — mijoz tirik odam so'rasa, `operator_chat_id` ga signal
  yuboriladi, user `handed_off=1` qilib belgilanadi (bot endi javob bermaydi)

## Modul javobgarliklari
- `src/config.py` — `.env` o'qiydi (lowercase keylar)
- `src/knowledge/loader.py` — `pos/*.json` → bitta matn
- `src/ai/prompts.py` — Nozimaxon PERSONA + `MESSAGE_SEPARATOR = "~~~"`
- `src/ai/gemini.py` — `GeminiAgent.reply(...)` function calling + caching
- `src/bot/humanize.py` — typing delay, apostrof tashlash, `~~~` bo'yicha ajratish
- `src/bot/handlers.py` — text/voice/video_note/video/photo/start
- `src/bot/throttle.py` — rate limit middleware
- `src/bot/handoff.py` — operator chat'iga signal
- `src/bot/tts.py` — ElevenLabs TTS (optional)
- `src/storage/db.py` — users, messages, leads jadvallari
- `src/main.py` — kompozitsion ildiz

## .env qatnashchilari (.env.example ga qarang)
- `bot_token` — BotFatherdan (majburiy)
- `gemini_key` — Google AI Studio'dan (majburiy)
- `gemini_model` — default `gemini-2.5-flash`
- `enable_gemini_cache` — true/false (default true)
- `operator_chat_id` — handoff signal yuboriladigan guruh/user (bo'sh qoldirilsa, faqat DB belgilanadi)
- `throttle_seconds` — anti-spam minimal interval, default 1.2
- `elevenlabs_key` — TTS uchun (bo'sh qoldirilsa, voice javob o'chirilgan)
- `voice_probability` — TTS bilan javob berish ehtimoli (default 0.18)

## Ishga tushirish
```bash
.venv/bin/python -m src.main
```

## Xavfsizlik
- `.env` `.gitignore`'da. Hech qachon git'ga tushmasligi kerak.
- Operator chat ID — bot guruhga admin sifatida qo'shilgan bo'lishi kerak.

## Hozir qilinmagan
- PostgreSQL migratsiyasi (hozircha SQLite yetadi)
- CRM export (CSV/Bitrix24/AmoCRM)
- Webhook rejimi (production)
- Per-user lead status tracking workflow (qualified → contacted → converted)
