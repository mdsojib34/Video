# Single Video Bot + Monetag Ad Gate

একটি Telegram bot-এর মধ্যে channel video import, ad gate, previous/new video navigation এবং admin controls।

## Setup

1. Bot-কে source channel-এ **Admin** করুন এবং `Post Messages`/channel posts দেখার permission দিন।
2. `.env.example` কপি করে `.env` বানান।
3. `BOT_TOKEN` বসান।
4. `PUBLIC_BASE_URL`-এ আপনার HTTPS domain দিন (যেমন `https://bot.example.com`)। Monetag ad page public HTTPS URL ছাড়া কাজ করবে না।
5. Install:

```bash
pip install -r requirements.txt
python bot.py
```

## User flow

`/start` → `🎬 ভিডিও দেখুন` → Monetag ad page → ad success callback → video send → `🎬 নতুন ভিডিও দেখুন` / `⬅️ আগের ভিডিও দেখুন`

## Admin

Owner `/admin` লিখে panel খুলবেন।

## Important security note

এই build-এ Monetag SDK Promise success-এর পর backend session complete করা হয়। Monetag-এর server-to-server signed postback/API verification credential পাওয়া গেলে `/api/ad-complete`-কে provider-side verification দিয়ে শক্ত করা উচিত। Client-side completion একা 100% anti-spoof নয়।
