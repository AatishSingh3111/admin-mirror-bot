# Admin Mirror + Auto-Translate Bot

This bot watches your `#admin` channel. Every message posted there gets copied
into a new channel of your choice — if it's not in English, it's translated
automatically, and the original text is kept alongside it for reference.

## 1. Create the Discord bot application

1. Go to https://discord.com/developers/applications and click **New
   Application**. Name it whatever you like (e.g. "Admin Translator").
2. In the left sidebar, click **Bot**.
3. Click **Reset Token** (or "Add Bot" if prompted) and copy the token shown.
   Save it somewhere safe — you'll need it as `DISCORD_TOKEN` later. Never
   share this token publicly.
4. On the same Bot page, scroll to **Privileged Gateway Intents** and turn ON
   **Message Content Intent**. This is required — without it, the bot cannot
   read message text. Save changes.

## 2. Invite the bot to your server

1. In the left sidebar, click **OAuth2 → URL Generator**.
2. Under **Scopes**, check `bot`.
3. Under **Bot Permissions**, check:
   - View Channels
   - Send Messages
   - Embed Links
   - Read Message History
4. Copy the generated URL at the bottom, open it in your browser, and invite
   the bot to your server (you'll need "Manage Server" permission).

## 3. Set up the channels in Discord

1. Create the new channel that will hold the translated/mirrored messages
   (e.g. `#admin-en`).
2. Make sure the bot has permission to **view** your existing `#admin`
   channel, and permission to **view + send messages** in the new channel.
   If your server uses category-level permission syncing this is usually
   automatic, but double check on private/restricted channels.
3. Turn on Developer Mode in Discord: **User Settings → Advanced → Developer
   Mode**.
4. Right-click `#admin` → **Copy Channel ID**. This is your
   `SOURCE_CHANNEL_ID`.
5. Right-click your new channel → **Copy Channel ID**. This is your
   `MIRROR_CHANNEL_ID`.

## 4. Deploy to Railway

1. Push this folder (`bot.py`, `requirements.txt`, `Procfile`) to a GitHub
   repo, then in Railway click **New Project → Deploy from GitHub repo** and
   pick it. (Alternatively, drag-and-drop deploy if Railway supports it for
   your account.)
2. In the Railway service settings, go to **Variables** and add:
   - `DISCORD_TOKEN` = the token from step 1
   - `SOURCE_CHANNEL_ID` = the ID from step 3.4
   - `MIRROR_CHANNEL_ID` = the ID from step 3.5
3. In **Settings → Deploy**, set the **Start Command** to `python bot.py`
   (Railway should also pick this up automatically from the Procfile).
4. Deploy. Check the **Logs** tab — you should see:
   `Logged in as YourBotName (id: ...)`
5. Post a message in `#admin` (try it in Spanish, like in your screenshot) —
   it should appear translated in the new channel within a couple seconds.

## How it behaves

- English messages are mirrored as-is, with the sender's name shown.
- Non-English messages are translated to English and shown under the
  sender's name — the original-language text is not displayed.
- Image/file attachments are mirrored too (linked, and inline if it's an
  image).
- The bot ignores messages from other bots, so it won't loop on itself.

## Notes / things you might want to tweak

- Translation uses Google Translate's free web endpoint via
  `deep-translator` — no API key needed, but it can occasionally rate-limit
  under heavy load. If that becomes an issue, swap in the official
  `google-cloud-translate` or DeepL API (both need an API key + billing) —
  happy to wire that up if needed.
- Right now it only mirrors one channel into one channel. If you want it to
  watch *multiple* admin channels into the same (or different) targets, that
  just needs a small dict mapping `{source_id: mirror_id}` instead of single
  IDs — let me know if you want that version.
- It currently doesn't mirror edits or deletions, only new messages. Can add
  `on_message_edit` handling if you want translated messages to update too.
