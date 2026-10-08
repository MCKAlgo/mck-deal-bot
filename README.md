🤖 DealBot — Flipkart + Amazon + Meesho 24/7 Deal Tracker
==========================================================
YOUR BOT: t.me/mck_deal_bot  (token already configured ✅)

WHAT IT DOES
------------
✅ Checks Flipkart, Amazon & Meesho continuously (round-robin every minute)
✅ Records every price into history (SQLite)
✅ Compares the same product across all 3 platforms
✅ Alerts you ONLY genuine best deals, with clear reasons:
   - 🏆 All-time lowest price ever tracked
   - 🔥 60%+ off MRP
   - 💰 Cheapest platform among Flipkart/Amazon/Meesho
   - 📉 Price dropped ₹500+ in last 7 days
✅ Daily best-buy list at 9:00 AM IST
✅ ZERO duplicate alerts (24h cooldown per product+reason)
✅ Group support: same alerts to your group with all users
✅ /broadcast: send YOUR message to all users at once

————————————————————————————————————————————
⚠️ IMPORTANT — HOW 24/7 WORKS (please read!)
————————————————————————————————————————————
A Telegram bot is a program that must RUN somewhere 24/7.
This chat demo workspace switches off background apps —
so for REAL 24/7 alerts, run the bot on ONE of these:

  🥇 EASIEST: YOUR ANDROID PHONE (Termux app, FREE, 5 min)
  🥈 Your Windows PC (must stay on)
  🥉 Free VPS (Oracle Cloud Always Free / ₹200 VPS)

After that, alerts come automatically day & night —
even when your phone is locked and you are sleeping.

————————————————————————————————————————————
OPTION A — ANDROID PHONE (RECOMMENDED, 5 minutes)
————————————————————————————————————————————
1. Install "Termux" from Play Store or F-Droid
2. Extract telegram-deal-bot.zip → you get "dealbot" folder.
   Put it inside your Phone's Download folder.
3. Open Termux and type ONE command:

      bash /sdcard/Download/dealbot/install_termux.sh

   (if that path fails: first run  termux-setup-storage
    and allow permission, then run the command again)
4. Wait for "✅ DealBot is RUNNING 24/7!"
5. Open Telegram → t.me/mck_deal_bot → send  /start
6. DONE — alerts start coming!

KEEP TERMSUX RUNNING: pull down Android notifications →
Termux → tap "ACQUIRE WAKE LOCK". Charge phone normally.
Bot auto-restarts via supervisor even after crash/reboot*
(*for reboot auto-start: install Termux:Boot app and add
 the nohup line to ~/.termux/boot/start-dealbot)

————————————————————————————————————————————
OPTION B — WINDOWS PC
————————————————————————————————————————————
1. Install Python 3.10+ from python.org (tick "Add to PATH")
2. Double-click  start_windows.bat
3. Keep the window open → send /start to the bot in Telegram

————————————————————————————————————————————
OPTION C — VPS (BEST UPTIME, for pros)
————————————————————————————————————————————
Oracle Cloud Always Free / any cheap VPS:
   sudo apt update && sudo apt install -y python3-pip unzip
   unzip telegram-deal-bot.zip && cd dealbot
   pip3 install -r requirements.txt
   chmod +x supervisor.sh start.sh
   nohup bash supervisor.sh > /dev/null 2>&1 &
   → send /start to the bot

————————————————————————————————————————————
👥 GROUP SETUP — SEND ALERTS TO ALL YOUR USERS
————————————————————————————————————————————
The SAME bot sends alerts to unlimited chats:

1. Create your Telegram group (e.g. "MCK Best Deals")
2. Add @mck_deal_bot as member (Admin is better)
3. In the group type:   /start@mck_deal_bot
4. ✅ Group registered — ALL group members now see
   every deal alert + daily buy list automatically!

TIP: In @BotFather → /setprivacy → select your bot →
Disable — so the bot reads all group messages reliably.

📣 /broadcast  (send YOUR message to ALL users):
   Private-chat the bot:  /broadcast Big sale tonight 8PM!
   → message goes to every registered chat instantly.

————————————————————————————————————————————
TELEGRAM COMMANDS
————————————————————————————————————————————
/start      — register this chat & activate alerts
/deals      — top 5 best deals RIGHT NOW
/status     — products tracked, price points, alerts sent
/mute 8     — pause alerts for 8 hours (1-72)
/unmute     — resume alerts
/broadcast  — your message → all chats (private chat only)
/id         — show chat ID
/help       — full help

————————————————————————————————————————————
ABOUT FLIPKART / MEESHO CONNECTIVITY
————————————————————————————————————————————
• FLIPKART: fully built-in (2 pages × 4 categories + JSON
  parser). Some datacenter IPs get empty "shell" pages —
  from your phone/home WiFi it works NORMALLY. The bot
  retries automatically every cycle, so no action needed.

• MEESHO: behind strong Cloudflare protection. From home/
  Indian IPs it usually works; the bot auto-retries every
  cycle — the moment it succeeds, Meesho deals flow in.

• Alerts get SMARTER over 24-48h as price history builds:
  Day 1: 60%-off alerts. Day 2+: all-time-lows, 7-day drops,
  cross-platform winners activate.

• CONFIG (config.json, hot-reloads in 1 min, no restart):
    check_interval_seconds : 60   (seconds between checks)
    digest_hour            : 9    (IST daily buy-list hour)
    max_alerts_per_cycle   : 8    (anti-spam cap)
    rules / platforms      : true/false each

• FILES: data/deals.db = price history, logs/bot.log = activity
• STOP:  pkill -f "python3 bot.py"
