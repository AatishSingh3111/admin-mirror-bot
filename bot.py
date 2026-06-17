import os
import discord
from discord.ext import commands
from discord import SyncWebhook
import aiohttp
from langdetect import detect, LangDetectException
from deep_translator import GoogleTranslator

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
SOURCE_CHANNEL_ID = int(os.environ["SOURCE_CHANNEL_ID"])
MIRROR_WEBHOOK_URL = os.environ["MIRROR_WEBHOOK_URL"]  # paste the webhook URL here

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
_webhook: discord.Webhook | None = None


def is_english(text: str) -> bool:
    try:
        return detect(text) == "en"
    except LangDetectException:
        return True


def translate_to_english(text: str) -> str:
    try:
        return GoogleTranslator(source="auto", target="en").translate(text)
    except Exception as e:
        return f"[translation failed: {e}]"


@bot.event
async def on_ready():
    global _webhook
    # Create an async webhook from the URL — no permissions needed
    async with aiohttp.ClientSession() as session:
        _webhook = discord.Webhook.from_url(MIRROR_WEBHOOK_URL, session=session)
    print(f"Logged in as {bot.user}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.channel.id != SOURCE_CHANNEL_ID:
        return

    content = message.content
    if content and not is_english(content):
        content = translate_to_english(content)

    files = []
    for attachment in message.attachments:
        try:
            files.append(await attachment.to_file())
        except Exception as e:
            print(f"Could not fetch attachment: {e}")

    # Each send needs its own session
    async with aiohttp.ClientSession() as session:
        hook = discord.Webhook.from_url(MIRROR_WEBHOOK_URL, session=session)
        await hook.send(
            content=content or None,
            username=message.author.display_name,
            avatar_url=message.author.display_avatar.url,
            files=files,
        )

    await bot.process_commands(message)


bot.run(DISCORD_TOKEN)
