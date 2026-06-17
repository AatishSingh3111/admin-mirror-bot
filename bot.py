import os
import discord
from discord.ext import commands
from langdetect import detect, LangDetectException
from deep_translator import GoogleTranslator

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
SOURCE_CHANNEL_ID = int(os.environ["SOURCE_CHANNEL_ID"])
MIRROR_CHANNEL_ID = int(os.environ["MIRROR_CHANNEL_ID"])

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Cache the webhook so we don't re-fetch it on every message
_webhook_cache: discord.Webhook | None = None


def is_english(text: str) -> bool:
    try:
        return detect(text) == "en"
    except LangDetectException:
        return True  # emoji-only / undetectable → treat as English


def translate_to_english(text: str) -> str:
    try:
        return GoogleTranslator(source="auto", target="en").translate(text)
    except Exception as e:
        return f"[translation failed: {e}]"


async def get_mirror_webhook() -> discord.Webhook | None:
    global _webhook_cache
    if _webhook_cache:
        return _webhook_cache

    mirror_channel = bot.get_channel(MIRROR_CHANNEL_ID)
    if not isinstance(mirror_channel, discord.TextChannel):
        return None

    # Reuse an existing webhook named "MirrorBot" or create one
    webhooks = await mirror_channel.webhooks()
    hook = next((w for w in webhooks if w.name == "MirrorBot"), None)
    if hook is None:
        hook = await mirror_channel.create_webhook(name="MirrorBot")

    _webhook_cache = hook
    return hook


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.channel.id != SOURCE_CHANNEL_ID:
        return

    hook = await get_mirror_webhook()
    if hook is None:
        print("Could not get mirror webhook")
        return

    # Translate content if non-English
    content = message.content
    if content and not is_english(content):
        content = translate_to_english(content)

    # Collect attachment files to re-upload (so they appear inline, not as links)
    files = []
    for attachment in message.attachments:
        try:
            files.append(await attachment.to_file())
        except Exception as e:
            print(f"Could not fetch attachment: {e}")

    await hook.send(
        content=content or None,
        username=message.author.display_name,
        avatar_url=message.author.display_avatar.url,
        files=files,
    )

    await bot.process_commands(message)


bot.run(DISCORD_TOKEN)
