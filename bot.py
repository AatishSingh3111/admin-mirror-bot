import os
import json
import asyncio
import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
from aiohttp import web
from deep_translator import GoogleTranslator
from dodopayments import AsyncDodoPayments

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
SOURCE_CHANNEL_ID = int(os.environ["SOURCE_CHANNEL_ID"])
MIRROR_WEBHOOK_URL = os.environ["MIRROR_WEBHOOK_URL"]
SPANISH_MIRROR_WEBHOOK_URL = os.environ.get("SPANISH_MIRROR_WEBHOOK_URL")

GUILD_ID = int(os.environ["GUILD_ID"])

DODO_API_KEY = os.environ["DODO_PAYMENTS_API_KEY"]
DODO_WEBHOOK_SECRET = os.environ["DODO_PAYMENTS_WEBHOOK_SECRET"]
DODO_ENVIRONMENT = os.environ.get("DODO_PAYMENTS_ENVIRONMENT", "live_mode")
DODO_PRODUCT_ID = os.environ["DODO_PRODUCT_ID"]
DODO_RETURN_URL = os.environ.get("DODO_RETURN_URL", "https://discord.com/channels/@me")

PORT = int(os.environ.get("PORT", 8080))
STATE_FILE = Path("subscription_state.json")

dodo = AsyncDodoPayments(
    bearer_token=DODO_API_KEY,
    webhook_key=DODO_WEBHOOK_SECRET,
    environment=DODO_ENVIRONMENT,
)

# ---------------------------------------------------------------------------
# Subscription state — per-user now, keyed by Discord user ID (as a string,
# since JSON object keys are always strings). File-backed so a restart
# doesn't lose it; note this resets on a fresh Railway deploy unless you
# attach a persistent Volume — the hourly reconcile loop below re-syncs
# every known subscriber from Dodo either way.
# ---------------------------------------------------------------------------
DEFAULT_USER_ENTRY = {"active": False, "subscription_id": None, "customer_id": None}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            loaded = json.loads(STATE_FILE.read_text())
            if "users" in loaded:
                return loaded
        except Exception:
            pass
    return {"users": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state))


def get_user_state(user_id: str) -> dict:
    return state["users"].get(user_id, dict(DEFAULT_USER_ENTRY))


def get_or_create_user_entry(user_id: str) -> dict:
    return state["users"].setdefault(user_id, dict(DEFAULT_USER_ENTRY))


state = load_state()

# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


def translate_text(text: str, target: str) -> str:
    try:
        result = GoogleTranslator(source="auto", target=target).translate(text)
        result = result if result else text
        if result == text and not text.isascii():
            result = GoogleTranslator(source="zh-TW", target=target).translate(text)
            result = result if result else text
        return result
    except Exception as e:
        log.warning(f"Translation error ({target}): {e}")
        return text


async def mirror_message(message: discord.Message, webhook_url: str, target_lang: str):
    content = message.content
    if content:
        content = translate_text(content, target_lang)

    files = []
    for attachment in message.attachments:
        try:
            files.append(await attachment.to_file())
        except Exception as e:
            log.warning(f"Could not fetch attachment: {e}")

    async with aiohttp.ClientSession() as session:
        hook = discord.Webhook.from_url(webhook_url, session=session)
        await hook.send(
            content=content or None,
            username=message.author.display_name,
            avatar_url=message.author.display_avatar.url,
            files=files,
        )


@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (id: {bot.user.id})")
    guild_obj = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild_obj)
    synced = await bot.tree.sync(guild=guild_obj)
    log.info(f"Synced {len(synced)} slash command(s) to guild {GUILD_ID}")
    if not reconcile_subscriptions.is_running():
        reconcile_subscriptions.start()


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.channel.id != SOURCE_CHANNEL_ID:
        return

    author_state = get_user_state(str(message.author.id))
    if author_state.get("active"):
        await mirror_message(message, MIRROR_WEBHOOK_URL, "en")
        if SPANISH_MIRROR_WEBHOOK_URL:
            await mirror_message(message, SPANISH_MIRROR_WEBHOOK_URL, "es")
    # else: this author has no active subscription -> their messages
    # intentionally are not mirrored

    await bot.process_commands(message)


# ---------------------------------------------------------------------------
# Slash commands (all act on the calling user's own subscription)
# ---------------------------------------------------------------------------
@bot.tree.command(name="subscribe", description="Get a payment link to activate translation for your messages")
async def subscribe(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        session = await dodo.checkout_sessions.create(
            product_cart=[{"product_id": DODO_PRODUCT_ID, "quantity": 1}],
            return_url=DODO_RETURN_URL,
            metadata={
                "discord_user_id": str(interaction.user.id),
                "discord_guild_id": str(interaction.guild_id),
            },
        )
        url = getattr(session, "checkout_url", None) or getattr(session, "url", None)
        await interaction.followup.send(
            f"Here's your payment link — once it's paid, translation switches on "
            f"for your messages automatically within a few seconds:\n{url}",
            ephemeral=True,
        )
    except Exception as e:
        log.error(f"Checkout session creation failed: {e}")
        await interaction.followup.send(
            "Something went wrong creating the payment link. Check the bot logs.",
            ephemeral=True,
        )


@bot.tree.command(name="subscription_status", description="Check whether translation is active for your messages")
async def subscription_status(interaction: discord.Interaction):
    author_state = get_user_state(str(interaction.user.id))
    active = author_state.get("active", False)
    msg = (
        "✅ Translation is **active** for your messages."
        if active
        else "❌ Translation is **inactive** for your messages — run `/subscribe` to activate it."
    )
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="cancel_subscription", description="Cancel your translation subscription")
@app_commands.describe(when="Cancel right away, or let it run until the period you've already paid for ends")
@app_commands.choices(when=[
    app_commands.Choice(name="At end of current billing period (recommended)", value="next_billing_date"),
    app_commands.Choice(name="Immediately", value="now"),
])
async def cancel_subscription(interaction: discord.Interaction, when: app_commands.Choice[str] = None):
    user_id = str(interaction.user.id)
    author_state = get_user_state(user_id)
    sub_id = author_state.get("subscription_id")
    if not sub_id:
        await interaction.response.send_message(
            "You don't have a subscription on record to cancel.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    mode = when.value if when else "next_billing_date"

    try:
        if mode == "now":
            # Cancels the mandate immediately; no further charges can occur.
            # The webhook (subscription.cancelled) will flip this user's
            # active flag to False once Dodo confirms it.
            await dodo.subscriptions.update(sub_id, status="cancelled")
            msg = (
                "Your subscription is cancelled immediately. Translation for "
                "your messages will switch off shortly once Dodo confirms it."
            )
        else:
            # Keeps this user's translation active until the period already
            # paid for ends, then it auto-cancels and the subscription.cancelled
            # webhook fires at that point.
            await dodo.subscriptions.update(sub_id, cancel_at_next_billing_date=True)
            msg = (
                "Your subscription is scheduled to cancel at the end of the "
                "current billing period. Translation stays active until then."
            )
        await interaction.followup.send(msg, ephemeral=True)
    except Exception as e:
        log.error(f"Subscription cancellation failed: {e}")
        await interaction.followup.send(
            "Something went wrong cancelling the subscription. Check the bot logs.",
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Reconciliation: self-heals if a webhook was ever missed, per subscriber
# ---------------------------------------------------------------------------
@tasks.loop(hours=1)
async def reconcile_subscriptions():
    for user_id, entry in list(state["users"].items()):
        sub_id = entry.get("subscription_id")
        if not sub_id:
            continue
        try:
            sub = await dodo.subscriptions.retrieve(sub_id)
            status = getattr(sub, "status", None)
            active = status == "active"
            if active != entry.get("active"):
                entry["active"] = active
                log.info(f"Reconciled subscription status from Dodo for user {user_id}: active={active}")
        except Exception as e:
            log.warning(f"Subscription reconcile failed for user {user_id}: {e}")
    save_state(state)


# ---------------------------------------------------------------------------
# Webhook server (runs in the same process/event loop as the bot)
# ---------------------------------------------------------------------------
# NOTE: field names inside `data` (e.g. subscription_id vs id) are based on
# Dodo's published examples. Use Dodo Dashboard -> Webhooks -> "send test
# event" and check the Railway logs after a real event to confirm the exact
# shape, then adjust the lookups below if needed.
ACTIVATING_EVENTS = {"subscription.active", "subscription.renewed"}
DEACTIVATING_EVENTS = {
    "subscription.cancelled",
    "subscription.expired",
    "subscription.failed",
    "subscription.on_hold",
}


async def handle_dodo_webhook(request: web.Request) -> web.Response:
    body = await request.read()
    headers = {
        "webhook-id": request.headers.get("webhook-id", ""),
        "webhook-signature": request.headers.get("webhook-signature", ""),
        "webhook-timestamp": request.headers.get("webhook-timestamp", ""),
    }

    try:
        # NOTE: unwrap() is synchronous (pure HMAC verification + JSON parsing,
        # no network I/O) even though it hangs off the async client, so it must
        # NOT be awaited. Awaiting it throws "object <EventType> can't be used
        # in 'await' expression", which looked like a signature failure but
        # wasn't.
        event = dodo.webhooks.unwrap(body, headers=headers)
    except Exception as e:
        log.warning(f"Webhook signature verification failed: {e}")
        return web.json_response({"error": "invalid signature"}, status=401)

    event_dict = event.model_dump() if hasattr(event, "model_dump") else dict(event)
    event_type = event_dict.get("type")
    data = event_dict.get("data") or {}
    log.info(f"Received Dodo webhook: {event_type}")

    # Per-user routing: the discord_user_id was set as checkout metadata in
    # /subscribe, and Dodo carries metadata through to the subscription and
    # every subsequent webhook event for it.
    metadata = data.get("metadata") or {}
    discord_user_id = metadata.get("discord_user_id")

    if not discord_user_id:
        log.warning(f"Webhook {event_type} had no discord_user_id in metadata; ignoring")
        return web.json_response({"received": True})

    entry = get_or_create_user_entry(discord_user_id)
    sub_id = data.get("subscription_id") or data.get("id")
    customer = data.get("customer") or {}

    if event_type in ACTIVATING_EVENTS:
        entry["active"] = True
        if sub_id:
            entry["subscription_id"] = sub_id
        if customer.get("customer_id"):
            entry["customer_id"] = customer["customer_id"]
        save_state(state)
        log.info(f"Translation ACTIVATED for user {discord_user_id}")
    elif event_type in DEACTIVATING_EVENTS:
        entry["active"] = False
        save_state(state)
        log.info(f"Translation DEACTIVATED for user {discord_user_id}")

    return web.json_response({"received": True})


async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def start_webserver():
    app = web.Application()
    app.router.add_post("/webhooks/dodo", handle_dodo_webhook)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"Webhook server listening on 0.0.0.0:{PORT}")


async def main():
    async with bot:
        await start_webserver()
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
