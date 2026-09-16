import os
import json
import re
import html
import asyncio
import logging
import signal
from collections import OrderedDict
from io import BytesIO
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
# Shared HTTP session
#
# Both Azure Translator calls and webhook sends used to open a brand new
# aiohttp.ClientSession (-> new TCP connection + TLS handshake) for every
# single call. Under a burst of messages that churn adds real latency and,
# at high enough volume, risks exhausting connections. One shared, pooled
# session fixes that — and gives every request an explicit timeout so a
# single hung call can't stall the mirror queue indefinitely.
# ---------------------------------------------------------------------------
http_session: aiohttp.ClientSession | None = None  # created in main() before the bot connects


def get_session() -> aiohttp.ClientSession:
    assert http_session is not None, "http_session used before main() initialized it"
    return http_session


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


# Discord syntax that shouldn't be run through translation: user/role/channel
# mentions, custom emoji, and URLs. Matched against the HTML-escaped text
# (so e.g. <@123> becomes &lt;@123&gt; before matching) and wrapped in
# <span class="notranslate">, which Azure guarantees to leave untouched when
# textType=html. This is what keeps mentions and emoji from being corrupted
# by translation, and stops the translator from trying to "translate" a URL.
_PROTECT_RE = re.compile(
    r"&lt;@!?\d+&gt;"       # user mentions
    r"|&lt;@&amp;\d+&gt;"   # role mentions
    r"|&lt;#\d+&gt;"        # channel mentions
    r"|&lt;a?:\w+:\d+&gt;"  # custom/animated emoji
    r"|https?://\S+"        # URLs
)
_SPAN_RE = re.compile(r'<span class="notranslate">(.*?)</span>', re.DOTALL)

TRANSLATE_MAX_ATTEMPTS = 3     # total tries (including the first) before falling back to untranslated text
TRANSLATE_RETRY_BACKOFF = 1.5  # base seconds between retries, scaled by attempt number


async def translate_text(text: str, target: str) -> str:
    # Escape the whole message first so nothing in it is misread as HTML,
    # then wrap Discord-specific tokens in notranslate spans so Azure passes
    # them through unchanged while translating the surrounding text.
    escaped = html.escape(text, quote=False)
    protected = _PROTECT_RE.sub(lambda m: f'<span class="notranslate">{m.group(0)}</span>', escaped)

    url = f"{AZURE_TRANSLATOR_ENDPOINT}/translate"
    params = {"api-version": "3.0", "to": target, "textType": "html"}
    headers = {
        "Ocp-Apim-Subscription-Key": AZURE_TRANSLATOR_KEY,
        "Ocp-Apim-Subscription-Region": AZURE_TRANSLATOR_REGION,
        "Content-Type": "application/json",
    }
    body = [{"text": protected}]

    session = get_session()
    last_error: object = None

    for attempt in range(1, TRANSLATE_MAX_ATTEMPTS + 1):
        try:
            async with session.post(url, params=params, headers=headers, json=body) as resp:
                # Azure itself can get rate-limited or briefly flaky under
                # load — both are transient, so retry instead of immediately
                # falling back to untranslated text. Capped so one slow
                # request can't stall the queue worker for long.
                if resp.status == 429 or resp.status >= 500:
                    last_error = f"HTTP {resp.status}"
                    if attempt < TRANSLATE_MAX_ATTEMPTS:
                        retry_after = resp.headers.get("Retry-After")
                        delay = float(retry_after) if retry_after else TRANSLATE_RETRY_BACKOFF * attempt
                        await asyncio.sleep(min(delay, 10.0))
                        continue
                    break

                data = await resp.json()
                if resp.status != 200:
                    raise RuntimeError(f"Azure Translator returned {resp.status}: {data}")

                translated_html = data[0]["translations"][0]["text"]
                # Strip the notranslate wrapper tags, then unescape HTML
                # entities back to plain characters for posting to Discord.
                plain = _SPAN_RE.sub(lambda m: m.group(1), translated_html)
                return html.unescape(plain)
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            last_error = e
            if attempt < TRANSLATE_MAX_ATTEMPTS:
                await asyncio.sleep(TRANSLATE_RETRY_BACKOFF * attempt)
                continue
            break
        except Exception as e:
            last_error = e
            break

    log.warning(f"Translation error ({target}) after {TRANSLATE_MAX_ATTEMPTS} attempt(s): {last_error}")
    return text


async def build_reply_line(message: discord.Message, target_lang: str) -> str | None:
    # Discord replies carry no indication of what/whom they're replying to
    # in message.content — that context lives in message.reference — so
    # without this the mirror just showed the reply text floating with no
    # link back to the message it was answering, unlike the native Discord
    # UI which shows a small quoted preview above it.
    ref = message.reference
    if ref is None:
        return None

    resolved = ref.resolved
    if resolved is None and ref.message_id:
        try:
            resolved = await message.channel.fetch_message(ref.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            resolved = None

    if resolved is None or isinstance(resolved, discord.DeletedReferencedMessage):
        return "↪️ *Replying to a message that's no longer available*"

    ref_author = resolved.author.display_name
    ref_content = resolved.content or ("[attachment]" if resolved.attachments else "")
    if not ref_content:
        return f"↪️ Replying to **{ref_author}**"

    translated_ref = await translate_text(ref_content, target_lang)
    snippet = translated_ref if len(translated_ref) <= 100 else translated_ref[:100] + "…"
    return f"↪️ Replying to **{ref_author}**: {snippet}"


async def fetch_message_assets(message: discord.Message) -> list[dict]:
    # Downloaded once per source message (not once per target language), so
    # mirroring to English *and* Spanish doesn't pull every attachment off
    # Discord's CDN twice — that duplicate traffic was unnecessary load on
    # every image/video-heavy burst.
    snapshots = getattr(message, "message_snapshots", None) or []
    all_attachments = list(message.attachments) + [
        a for snapshot in snapshots for a in (getattr(snapshot, "attachments", None) or [])
    ]
    assets = []
    for attachment in all_attachments:
        try:
            data = await attachment.read()
            assets.append({
                "filename": attachment.filename,
                "data": data,
                "spoiler": attachment.is_spoiler(),
            })
        except Exception as e:
            log.warning(f"Could not fetch attachment {attachment.filename}: {e}")
    return assets


def build_files_from_assets(assets: list[dict]) -> list[discord.File]:
    # Builds a fresh discord.File per send from already-downloaded bytes —
    # discord.py consumes/closes a File object once it's sent, so the same
    # File can't be reused across the EN and ES jobs, but the expensive
    # part (the network fetch) only happens once.
    files = []
    for asset in assets:
        try:
            files.append(discord.File(
                BytesIO(asset["data"]),
                filename=asset["filename"],
                spoiler=asset.get("spoiler", False),
            ))
        except Exception as e:
            log.warning(f"Could not prepare file {asset.get('filename')}: {e}")
    return files


async def build_mirror_content(message: discord.Message, target_lang: str, assets: list[dict]):
    # Shared by both the initial send and edit paths, so a forwarded message
    # or an edited message get the exact same treatment either way.
    parts = []

    reply_line = await build_reply_line(message, target_lang)
    if reply_line:
        parts.append(reply_line)

    content = message.content
    if content:
        parts.append(await translate_text(content, target_lang))

    # Forwarded messages: Discord stores the forwarded content as a
    # "snapshot" (message.message_snapshots) rather than putting it in
    # message.content — a pure forward has empty message.content, so
    # without this it looked like an empty message and got skipped
    # entirely (the "Skipping mirror of message ...: nothing to send" log
    # line), which is why forwards weren't showing up in the mirror at all.
    snapshots = getattr(message, "message_snapshots", None) or []
    for snapshot in snapshots:
        snapshot_content = getattr(snapshot, "content", "") or ""
        if snapshot_content:
            translated_snapshot = await translate_text(snapshot_content, target_lang)
            parts.append(f"↪️ **Forwarded:**\n{translated_snapshot}")

    content_out = "\n\n".join(part for part in parts if part)

    all_stickers = list(message.stickers) + [
        s for snapshot in snapshots for s in (getattr(snapshot, "stickers", None) or [])
    ]
    if all_stickers:
        # We don't fetch sticker images here, just preserve the name so the
        # message isn't silently dropped when it's sticker-only.
        sticker_note = "[sticker: " + ", ".join(s.name for s in all_stickers) + "]"
        content_out = f"{content_out}\n{sticker_note}".strip() if content_out else sticker_note

    files = build_files_from_assets(assets)
    return content_out, files


# In-memory map of (source_message_id, target_lang) -> mirrored message id,
# so an edit to an already-mirrored message can find and edit its mirrored
# copy instead of either duplicating it or being silently ignored. This
# resets on a restart — an edit to a message mirrored in a previous process
# lifetime just falls back to sending a fresh copy (see mirror_message_edit)
# rather than failing. Bounded so long uptimes don't grow this forever.
_MIRROR_ID_CACHE_MAX = 5000
mirrored_message_ids: "OrderedDict[tuple[int, str], int]" = OrderedDict()


def _remember_mirrored(source_id: int, target_lang: str, mirrored_id: int):
    key = (source_id, target_lang)
    mirrored_message_ids[key] = mirrored_id
    mirrored_message_ids.move_to_end(key)
    while len(mirrored_message_ids) > _MIRROR_ID_CACHE_MAX:
        mirrored_message_ids.popitem(last=False)


async def mirror_message(message: discord.Message, webhook_url: str, target_lang: str, assets: list[dict]):
    # NOTE: this lets discord.HTTPException propagate so the queue worker
    # below can tell a genuine 429/5xx apart from other failures and retry
    # it instead of silently dropping the message.
    content, files = await build_mirror_content(message, target_lang, assets)

    if not content and not files:
        # Nothing to send (e.g. a poll-closed system message, or some other
        # message type with no content/attachments). Webhooks 400 on a
        # truly empty send, so skip it instead of letting that crash
        # on_message.
        log.info(f"Skipping mirror of message {message.id}: nothing to send")
        return

    hook = discord.Webhook.from_url(webhook_url, session=get_session())
    sent = await hook.send(
        content=content or None,
        username=message.author.display_name,
        avatar_url=message.author.display_avatar.url,
        files=files,
        wait=True,  # need the sent message back so edits can find it later
    )
    _remember_mirrored(message.id, target_lang, sent.id)


async def mirror_message_edit(message: discord.Message, webhook_url: str, target_lang: str, assets: list[dict]):
    mirrored_id = mirrored_message_ids.get((message.id, target_lang))
    if mirrored_id is None:
        # We have no record of mirroring the original (never mirrored it,
        # it was skipped as empty, or the bot restarted since). Rather than
        # drop the edit, mirror the edited version fresh.
        await mirror_message(message, webhook_url, target_lang, assets)
        return

    content, files = await build_mirror_content(message, target_lang, assets)
    if not content and not files:
        return

    hook = discord.Webhook.from_url(webhook_url, session=get_session())
    await hook.edit_message(
        mirrored_id,
        content=content or None,
        attachments=files,
    )


# ---------------------------------------------------------------------------
# Mirror queue
#
# on_message never calls the webhooks directly — that's what let a burst of
# source-channel messages trip Discord's per-webhook rate limit in the first
# place. Instead:
#
#  - Attachments are downloaded once per source message (fetch_message_assets),
#    not once per target language.
#  - Each target language gets its OWN queue and its OWN worker, so English
#    and Spanish mirroring proceed in parallel instead of taking turns on a
#    single shared "one message per second" budget.
#  - Each worker only sends to its own webhook, MIRROR_MIN_INTERVAL apart,
#    which stays well under Discord's per-webhook rate limit even with both
#    workers running at once.
#  - A genuine 429/5xx re-queues the job with backoff (up to
#    MIRROR_MAX_ATTEMPTS) instead of dropping the message. A backlog past
#    _QUEUE_WARN_THRESHOLD gets logged so a stuck webhook / bad translator
#    key shows up immediately instead of silently piling up.
# ---------------------------------------------------------------------------
MIRROR_MIN_INTERVAL = 1.0      # seconds to wait between consecutive sends on one worker
MIRROR_MAX_ATTEMPTS = 5        # give up on a job (and log it) after this many tries
MIRROR_RETRY_BACKOFF = 5.0     # base seconds to wait before retrying a rate-limited/failed job
_QUEUE_WARN_THRESHOLD = 30     # log a warning if a queue backs up past this many pending jobs

mirror_queues: dict[str, asyncio.Queue] = {}
_mirror_worker_tasks: list[asyncio.Task] = []


def _mirror_targets() -> list[tuple[str, str]]:
    targets = [("en", MIRROR_WEBHOOK_URL)]
    if SPANISH_MIRROR_WEBHOOK_URL:
        targets.append(("es", SPANISH_MIRROR_WEBHOOK_URL))
    return targets


async def _enqueue_job(action: str, message: discord.Message, webhook_url: str, target_lang: str,
                        assets: list[dict], attempt: int = 1):
    queue = mirror_queues.setdefault(target_lang, asyncio.Queue())
    await queue.put((action, message, webhook_url, target_lang, assets, attempt))
    size = queue.qsize()
    if size >= _QUEUE_WARN_THRESHOLD:
        log.warning(f"Mirror queue for '{target_lang}' backing up: {size} pending job(s)")


async def enqueue_mirror(message: discord.Message):
    assets = await fetch_message_assets(message)
    for lang, webhook_url in _mirror_targets():
        await _enqueue_job("send", message, webhook_url, lang, assets)


async def enqueue_mirror_edit(message: discord.Message):
    assets = await fetch_message_assets(message)
    for lang, webhook_url in _mirror_targets():
        await _enqueue_job("edit", message, webhook_url, lang, assets)


async def mirror_worker(target_lang: str, queue: asyncio.Queue):
    log.info(f"Mirror worker started for target='{target_lang}'")
    while True:
        action, message, webhook_url, lang, assets, attempt = await queue.get()
        try:
            if action == "edit":
                await mirror_message_edit(message, webhook_url, lang, assets)
            else:
                await mirror_message(message, webhook_url, lang, assets)
        except discord.HTTPException as e:
            if e.status in (429, 500, 502, 503, 504) and attempt < MIRROR_MAX_ATTEMPTS:
                delay = MIRROR_RETRY_BACKOFF * attempt
                log.warning(
                    f"HTTP {e.status} mirroring message {message.id} ({lang}, {action}), "
                    f"attempt {attempt}/{MIRROR_MAX_ATTEMPTS} — retrying in {delay:.0f}s"
                )
                await asyncio.sleep(delay)
                await _enqueue_job(action, message, webhook_url, lang, assets, attempt + 1)
            else:
                log.error(
                    f"Giving up mirroring message {message.id} ({lang}, {action}) "
                    f"after {attempt} attempt(s): {e}"
                )
        except Exception as e:
            log.error(f"Failed to mirror message {message.id} ({lang}, {action}): {e}")
        finally:
            queue.task_done()
            await asyncio.sleep(MIRROR_MIN_INTERVAL)


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

    if not _mirror_worker_tasks:
        for lang, _ in _mirror_targets():
            queue = mirror_queues.setdefault(lang, asyncio.Queue())
            _mirror_worker_tasks.append(asyncio.create_task(mirror_worker(lang, queue)))


@bot.event
async def on_disconnect():
    # discord.py auto-reconnects on its own; this is just visibility so a
    # peak-hour gateway hiccup shows up in the logs instead of looking like
    # silence.
    log.warning("Disconnected from Discord gateway (auto-reconnecting)")


@bot.event
async def on_resumed():
    log.info("Discord gateway session resumed")


@bot.event
async def on_error(event_method: str, *args, **kwargs):
    # Default discord.py behavior just prints a traceback; logging it
    # properly means it actually shows up in Railway's log stream instead of
    # possibly getting lost, and — critically — this keeps the bot itself
    # running instead of an unhandled error in one event handler taking
    # down the process.
    log.exception(f"Unhandled exception in event handler '{event_method}'")


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
        await enqueue_mirror(message)
    # else: subscription inactive -> intentionally does not mirror anything

    await bot.process_commands(message)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    # Previously there was no edit handler at all, so editing a message
    # after it had already been mirrored left the mirrored copies showing
    # the stale, pre-edit text forever.
    if after.author.bot:
        return
    if after.channel.id != SOURCE_CHANNEL_ID:
        return
    if not state.get("active"):
        return
    if before.content == after.content:
        # Discord also fires this event for edits that don't touch content
        # (e.g. a link unfurling into an embed a moment after posting) —
        # skip those so we're not re-translating and re-editing for no
        # visible change.
        return
    await enqueue_mirror_edit(after)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------
def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.id in ADMIN_USER_IDS:
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Without this, an unhandled error in any slash command (e.g. a Dodo API
    # hiccup) just leaves the user's interaction hanging with "the
    # application did not respond" and dies silently in the logs.
    cmd_name = interaction.command.name if interaction.command else "?"
    log.error(f"Slash command error in /{cmd_name}: {error}")
    message = "Something went wrong running that command. Please try again in a moment."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


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
    global http_session
    # limit / limit_per_host bound how many concurrent connections we open —
    # generous enough for two mirror workers + Azure calls + attachment
    # fetches running at once, without letting a runaway burst open
    # unlimited sockets. The timeout means a hung request to Azure or
    # Discord gets abandoned (and retried/logged) instead of hanging a
    # worker forever.
    connector = aiohttp.TCPConnector(limit=20, limit_per_host=10, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=15, connect=5)
    http_session = aiohttp.ClientSession(connector=connector, timeout=timeout)

    # Translate SIGTERM (what Railway sends on redeploy/stop) into a clean
    # shutdown instead of an abrupt kill mid-write. SIGINT (Ctrl+C) covered
    # too, for local runs.
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # not available on this platform — Ctrl+C still raises KeyboardInterrupt as before

    try:
        async with bot:
            await start_webserver()
            bot_task = asyncio.create_task(bot.start(DISCORD_TOKEN))
            stop_task = asyncio.create_task(stop_event.wait())
            await asyncio.wait({bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)

            if bot_task.done():
                # The bot task ending on its own means a real failure (e.g.
                # bad token, fatal gateway error) — surface it so the
                # process exits non-zero and Railway restarts it, instead of
                # looking like a clean shutdown.
                exc = bot_task.exception()
                if exc is not None:
                    raise exc
            else:
                log.info("Shutdown signal received, closing up...")
                await bot.close()
    finally:
        if reconcile_subscription.is_running():
            reconcile_subscription.cancel()
        for task in _mirror_worker_tasks:
            task.cancel()
        await http_session.close()


if __name__ == "__main__":
    asyncio.run(main())
