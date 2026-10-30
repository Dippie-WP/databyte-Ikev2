"""Standalone polling service for the VPN bot.

Runs Application.run_polling() in the main thread (not inside gunicorn).
The webhook was deleted via deleteWebhook API so this is the only way
updates reach the bot now.
"""
import logging
import sys

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot-polling")

# Ensure we can import bot from the same dir
sys.path.insert(0, "/opt/vpn-portal")
import bot

application = bot.build_application()


def main():
    log.info("bot-polling: starting Application.run_polling() (main thread)")
    # run_polling is blocking; creates its own event loop internally
    application.run_polling(
        poll_interval=1.0,
        timeout=30,
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
