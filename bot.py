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
ADMIN_USER_IDS = {
    int(x) for x in os.environ.get("ADMIN_USER_IDS", "").split(",") if x.strip()
}

AZURE_TRANSLATOR_KEY = os.environ["AZURE_TRANSLATOR_KEY"]
AZURE_TRANSLATOR_REGION = os.environ["AZURE_TRANSLATOR_REGION"]
AZURE_TRANSLATOR_ENDPOINT = "https://api.cognitive.microsofttranslator.com"

DODO_API_KEY = os.environ["DODO_PAYMENTS_API_KEY"]
DODO_WEBHOOK_SECRET = os.environ["DODO_PAYMENTS_WEBHOOK_SECRET"]
DODO_ENVIRONMENT = os.environ.get("DODO_PAYMENTS_ENVIRONMENT", "live_mode")
DODO_PRODUCT_ID = os.environ["DODO_PRODUCT_ID"]
DODO_RETURN_URL = os.environ.get("DODO_RETURN_URL", "https://discord.com/channels/@me")

PORT = int(os.environ.get("PORT", 8080))
STATE_FILE = Path(os.environ.get("STATE_FILE_PATH", "subscription_state.json"))

dodo = AsyncDodoPayments(
    bearer_token=DODO_API_KEY,
    webhook_key=DODO_WEBHOOK_SECRET,
    environment=DODO_ENVIRONMENT,
)

# ---------------------------------------------------------------------------
# Subscription state (file-backed so a restart doesn't lose it; note this
# resets on a fresh Railway deploy unless you attach a persistent Volume —
# the hourly reconcile loop below re-syncs from Dodo either way)
# ---------------------------------------------------------------------------
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"active": False, "subscription_id": None, "customer_id": None, "subscribed_by": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state))


state = load_state()

# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


async def translate_text(text: str, target: str) -> str:
    # Calling Azure's REST API directly rather than going through
    # deep-translator's MicrosoftTranslator wrapper: that wrapper sends
    # source="auto" through literally as from=auto, which Azure rejects
    # (error 400035, "source language is not valid"). Azure's own
    # convention for auto-detection is to omit the `from` param entirely,
    # so we build the request ourselves to do that correctly.
    url = f"{AZURE_TRANSLATOR_ENDPOINT}/translate"
    params = {"api-version": "3.0", "to": target}
    headers = {
        "Ocp-Apim-Subscription-Key": AZURE_TRANSLATOR_KEY,
        "Ocp-Apim-Subscription-Region": AZURE_TRANSLATOR_REGION,
        "Content-Type": "application/json",
    }
    body = [{"text": text}]

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, params=params, headers=headers, json=body) as resp:
                data = await resp.json()
                if resp.status != 200:
                    raise RuntimeError(f"Azure Translator returned {resp.status}: {data}")
                return data[0]["translations"][0]["text"]
    except Exception as e:
        log.warning(f"Translation error ({target}): {e}")
        return text


async def mirror_message(message: discord.Message, webhook_url: str, target_lang: str):
    content = message.content
    if content:
        content = await translate_text(content, target_lang)

    files = []
    for attachment in message.attachments:
        try:
            files.append(await attachment.to_file())
        except Exception as e:
            log.warning(f"Could not fetch attachment: {e}")

    if message.stickers:
        # We don't fetch sticker images here, just preserve the name so the
        # message isn't silently dropped when it's sticker-only.
        sticker_note = "[sticker: " + ", ".join(s.name for s in message.stickers) + "]"
        content = f"{content}\n{sticker_note}".strip() if content else sticker_note

    if not content and not files:
        # Nothing to send (e.g. a poll-closed system message, or some other
        # message type with no content/attachments). Webhooks 400 on a
        # truly empty send, so skip it instead of letting that crash
        # on_message.
        log.info(f"Skipping mirror of message {message.id}: nothing to send")
        return

    async with aiohttp.ClientSession() as session:
        hook = discord.Webhook.from_url(webhook_url, session=session)
        try:
            await hook.send(
                content=content or None,
                username=message.author.display_name,
                avatar_url=message.author.display_avatar.url,
                files=files,
            )
        except discord.HTTPException as e:
            log.warning(f"Failed to mirror message {message.id} ({target_lang}): {e}")


@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (id: {bot.user.id})")
    guild_obj = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild_obj)
    synced = await bot.tree.sync(guild=guild_obj)
    log.info(f"Synced {len(synced)} slash command(s) to guild {GUILD_ID}")
    await adopt_existing_subscription_if_any()
    if not reconcile_subscription.is_running():
        reconcile_subscription.start()


async def adopt_existing_subscription_if_any():
    # Startup safety net. If local state has no subscription_id on record —
    # e.g. this is a fresh Volume, or a redeploy happened without one ever
    # being attached — the reconcile loop below has nothing to check
    # against, since it only re-verifies a subscription_id it already
    # knows. This asks Dodo directly whether an active subscription for
    # this product already exists and adopts it, so a paying user is never
    # left stranded waiting for the next billing cycle's webhook.
    if state.get("subscription_id"):
        return

    active_subs = []
    try:
        async for sub in dodo.subscriptions.list():
            sub_dict = sub.model_dump() if hasattr(sub, "model_dump") else dict(sub)
            if sub_dict.get("status") != "active":
                continue
            # If the SDK exposes product_id on the subscription, scope the
            # adoption to this bot's product. If it doesn't, don't block on
            # a field we're not sure exists.
            product_id = sub_dict.get("product_id")
            if product_id and product_id != DODO_PRODUCT_ID:
                continue
            active_subs.append(sub_dict)
    except Exception as e:
        log.warning(f"Startup subscription lookup failed: {e}")
        return

    if not active_subs:
        log.info("No active Dodo subscription found on startup; nothing to adopt")
        return

    if len(active_subs) > 1:
        active_subs.sort(key=lambda s: s.get("created_at") or "", reverse=True)
        log.warning(
            f"Found {len(active_subs)} active Dodo subscriptions on startup; "
            f"adopting the most recent one and ignoring the rest"
        )

    sub_dict = active_subs[0]
    sub_id = sub_dict.get("subscription_id") or sub_dict.get("id")
    customer = sub_dict.get("customer") or {}
    metadata = sub_dict.get("metadata") or {}

    state["active"] = True
    if sub_id:
        state["subscription_id"] = sub_id
    if customer.get("customer_id"):
        state["customer_id"] = customer["customer_id"]
    if metadata.get("discord_user_id"):
        state["subscribed_by"] = metadata["discord_user_id"]
    save_state(state)
    log.info(f"Adopted existing active subscription on startup: {sub_id}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.channel.id != SOURCE_CHANNEL_ID:
        return

    if state.get("active"):
        try:
            await mirror_message(message, MIRROR_WEBHOOK_URL, "en")
        except Exception as e:
            log.error(f"Failed to mirror message {message.id} (en): {e}")

        if SPANISH_MIRROR_WEBHOOK_URL:
            try:
                await mirror_message(message, SPANISH_MIRROR_WEBHOOK_URL, "es")
            except Exception as e:
                log.error(f"Failed to mirror message {message.id} (es): {e}")
    # else: subscription inactive -> intentionally does not mirror anything

    await bot.process_commands(message)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------
def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.id in ADMIN_USER_IDS:
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


@bot.tree.command(name="subscribe", description="Get a payment link to activate translation")
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
            f"automatically within a few seconds:\n{url}",
            ephemeral=True,
        )
    except Exception as e:
        log.error(f"Checkout session creation failed: {e}")
        await interaction.followup.send(
            "Something went wrong creating the payment link. Check the bot logs.",
            ephemeral=True,
        )


@bot.tree.command(name="subscription_status", description="Check whether translation is currently active")
async def subscription_status(interaction: discord.Interaction):
    active = state.get("active", False)
    msg = (
        "✅ Translation is **active**."
        if active
        else "❌ Translation is **inactive** — run `/subscribe` to activate it."
    )
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="cancel_subscription", description="Cancel the translation subscription")
@app_commands.describe(when="Cancel right away, or let it run until the period you've already paid for ends")
@app_commands.choices(when=[
    app_commands.Choice(name="At end of current billing period (recommended)", value="next_billing_date"),
    app_commands.Choice(name="Immediately", value="now"),
])
async def cancel_subscription(interaction: discord.Interaction, when: app_commands.Choice[str] = None):
    sub_id = state.get("subscription_id")
    if not sub_id:
        await interaction.response.send_message(
            "There's no subscription on record to cancel.", ephemeral=True
        )
        return

    subscribed_by = state.get("subscribed_by")
    is_subscriber = subscribed_by is not None and str(interaction.user.id) == subscribed_by
    if not is_subscriber and not is_admin(interaction):
        await interaction.response.send_message(
            "Only the person who subscribed, or a server admin, can cancel this.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    mode = when.value if when else "next_billing_date"

    try:
        if mode == "now":
            # Cancels the mandate immediately; no further charges can occur.
            # The webhook (subscription.cancelled) will flip state["active"]
            # to False once Dodo confirms it.
            await dodo.subscriptions.update(sub_id, status="cancelled")
            msg = (
                "Subscription cancelled immediately. Translation will switch "
                "off shortly once Dodo confirms the cancellation."
            )
        else:
            # Keeps the subscription (and translation) active until the
            # period already paid for ends, then it auto-cancels and the
            # subscription.cancelled webhook fires at that point.
            await dodo.subscriptions.update(sub_id, cancel_at_next_billing_date=True)
            msg = (
                "Subscription is scheduled to cancel at the end of the "
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
# Reconciliation: self-heals if a webhook was ever missed
# ---------------------------------------------------------------------------
@tasks.loop(hours=1)
async def reconcile_subscription():
    sub_id = state.get("subscription_id")
    if not sub_id:
        return
    try:
        sub = await dodo.subscriptions.retrieve(sub_id)
        status = getattr(sub, "status", None)
        active = status == "active"
        if active != state.get("active"):
            state["active"] = active
            save_state(state)
            log.info(f"Reconciled subscription status from Dodo: active={active}")
    except Exception as e:
        log.warning(f"Subscription reconcile failed: {e}")


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

    if event_type in ACTIVATING_EVENTS:
        state["active"] = True
        sub_id = data.get("subscription_id") or data.get("id")
        customer = data.get("customer") or {}
        metadata = data.get("metadata") or {}
        if sub_id:
            state["subscription_id"] = sub_id
        if customer.get("customer_id"):
            state["customer_id"] = customer["customer_id"]
        if metadata.get("discord_user_id"):
            # Whoever's checkout activated this subscription is the one
            # allowed to cancel it later (admins can still override).
            state["subscribed_by"] = metadata["discord_user_id"]
        save_state(state)
        log.info("Translation ACTIVATED")
    elif event_type in DEACTIVATING_EVENTS:
        state["active"] = False
        save_state(state)
        log.info("Translation DEACTIVATED")

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
