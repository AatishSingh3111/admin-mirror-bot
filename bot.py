import os
import discord
from discord.ext import commands
import aiohttp
from deep_translator import GoogleTranslator

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
SOURCE_CHANNEL_ID = int(os.environ["SOURCE_CHANNEL_ID"])
MIRROR_WEBHOOK_URL = os.environ["MIRROR_WEBHOOK_URL"]

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


def translate_to_english(text: str) -> str:
    try:
        result = GoogleTranslator(source="auto", target="en").translate(text)
        return result if result else text
    except Exception as e:
        print(f"Translation error: {e}")
        return text


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if message.channel.id != SOURCE_CHANNEL_ID:
        return

    content = message.content
    if content:
        content = translate_to_english(content)

    files = []
    for attachment in message.attachments:
        try:
            files.append(await attachment.to_file())
        except Exception as e:
            print(f"Could not fetch attachment: {e}")

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
