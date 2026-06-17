import os
import discord
from discord.ext import commands
from langdetect import detect, LangDetectException
from deep_translator import GoogleTranslator

# ---- Config (set these as environment variables on Railway) ----
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
SOURCE_CHANNEL_ID = int(os.environ["SOURCE_CHANNEL_ID"])   # the #admin channel
MIRROR_CHANNEL_ID = int(os.environ["MIRROR_CHANNEL_ID"])   # the new translated channel

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


def is_english(text: str) -> bool:
    """Returns True if text is detected as English (or can't be detected, e.g. emoji-only)."""
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
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    print(f"Watching channel {SOURCE_CHANNEL_ID} -> mirroring to {MIRROR_CHANNEL_ID}")


@bot.event
async def on_message(message: discord.Message):
    # Ignore the bot's own messages and other bots
    if message.author.bot:
        return

    # Only act on messages from the source (admin) channel
    if message.channel.id != SOURCE_CHANNEL_ID:
        return

    mirror_channel = bot.get_channel(MIRROR_CHANNEL_ID)
    if mirror_channel is None:
        print(f"Could not find mirror channel {MIRROR_CHANNEL_ID}")
        return

    embed = discord.Embed(color=discord.Color.blurple())
    embed.set_author(
        name=message.author.display_name,
        icon_url=message.author.display_avatar.url,
    )

    content = message.content
    if content:
        if is_english(content):
            embed.description = content
        else:
            embed.description = translate_to_english(content)

    # Mirror any attachments (images, files, etc.)
    if message.attachments:
        urls = "\n".join(a.url for a in message.attachments)
        embed.add_field(name="Attachments", value=urls, inline=False)
        first = message.attachments[0]
        if first.content_type and first.content_type.startswith("image"):
            embed.set_image(url=first.url)

    embed.set_footer(text=f"from #{message.channel.name}")
    embed.timestamp = message.created_at

    await mirror_channel.send(embed=embed)

    await bot.process_commands(message)


bot.run(DISCORD_TOKEN)
