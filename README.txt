QUIZ BOT

External dependency detected from imports: aiogram
requirements.txt pins aiogram==3.22.0.

Install:
  py -m pip install -r requirements.txt

PowerShell:
  $env:BOT_TOKEN="YOUR_BOT_TOKEN"
  $env:ADMIN_IDS="YOUR_TELEGRAM_ID"
  py bot.py

Optional environment variables:
  PACKS_DIR=packs
  STATE_FILE=state.json

The bot creates the packs directory automatically.
