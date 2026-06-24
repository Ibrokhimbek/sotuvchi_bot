# Jaloliddin — Linko POS sotuvchi bot

Instagram reklamasi orqali kelgan mijozlarga Telegramda Linko POS dasturini sotadigan AI sotuvchi bot. Gemini multimodal modeli orqali matn, ovoz va video xabarlarga tabiiy javob beradi.

## Texnologiyalar

- **aiogram 3** — Telegram bot framework
- **Google Gemini 2.5 Flash/Pro** — multimodal AI
- **SQLite** — suhbat tarixi
- **Python 3.11+**

## O'rnatish

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`.env` faylida `bot_token` va `gemini_key` to'ldirilgan bo'lishi kerak (`.env.example` ga qarang).

## Ishga tushirish

```bash
python -m src.main
```

## Loyiha tuzilmasi

```
src/
├── main.py              # entry point
├── config.py            # env va sozlamalar
├── bot/
│   ├── handlers.py      # Telegram message handlers
│   └── humanize.py      # tabiiy javob: typing delay, imloviy xatolar
├── ai/
│   ├── gemini.py        # Gemini client
│   └── prompts.py       # Jaloliddin shaxsiyati va system prompt
├── knowledge/
│   └── loader.py        # pos/*.json bilim bazasini yuklash
└── storage/
    └── db.py            # SQLite suhbat tarixi
pos/                     # Linko POS bilim bazasi (JSON)
data/                    # SQLite DB (gitignored)
```
