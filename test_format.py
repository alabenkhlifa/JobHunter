import argparse
import sqlite3
import sys

parser = argparse.ArgumentParser(description="Preview a formatted job notification")
parser.add_argument("--send", action="store_true", help="Actually send the preview to Telegram")
args = parser.parse_args()

conn = sqlite3.connect('data/jobs.db')
conn.row_factory = sqlite3.Row

# Get first job
row = conn.execute("SELECT * FROM jobs LIMIT 1").fetchone()
if not row:
    print("No jobs in database", file=sys.stderr)
    sys.exit(1)

job = dict(row)

# Import formatting function
sys.path.insert(0, '.')
from scraper import format_job_message, job_inline_keyboard, send_telegram
import os
from dotenv import load_dotenv

load_dotenv()

# Format and print
msg = format_job_message(job)
print("MESSAGE:")
print(msg)
print("\n" + "="*50 + "\n")

if args.send:
    # Send to Telegram only when explicitly requested
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    keyboard = job_inline_keyboard(job)
    print("Sending to Telegram with buttons...")
    success = send_telegram(token, chat_id, msg, reply_markup=keyboard)
    print(f"Result: {'✅ Sent' if success else '❌ Failed'}")
else:
    print("Preview only. Use --send to send this message to Telegram.")
