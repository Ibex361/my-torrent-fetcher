"""
generate_session.py

RUN THIS ONCE, LOCALLY (not in GitHub Actions), to log into your Telegram
account and produce a "session string". This string acts like a saved
login and lets the GitHub Action send files as you, without needing your
password every time.

You will need:
  - Your phone number
  - A one-time login code Telegram sends to your app when you run this
  - api_id and api_hash from https://my.telegram.org (see instructions)

Run with:  python generate_session.py

It will print a long string at the end. Copy that entire string — you'll
paste it into a GitHub secret called TG_SESSION. Treat it like a password:
anyone with this string can log into your Telegram account.
"""

from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = input("Enter your api_id (from my.telegram.org): ").strip()
api_hash = input("Enter your api_hash (from my.telegram.org): ").strip()

with TelegramClient(StringSession(), int(api_id), api_hash) as client:
    session_string = client.session.save()
    print("\n\n=== COPY EVERYTHING BELOW THIS LINE ===")
    print(session_string)
    print("=== COPY EVERYTHING ABOVE THIS LINE ===\n")
    print("Save this as a GitHub secret named TG_SESSION.")
