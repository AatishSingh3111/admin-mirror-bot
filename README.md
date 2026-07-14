# Admin Mirror + Auto-Translate Bot (subscription-gated)

This bot watches your `#admin` channel and mirrors messages into English and
Spanish channels, translating as needed. **Translation only runs while a
subscription is active** — the bot stays connected and live in your server
either way, but it won't mirror anything into either channel until someone
with admin rights runs `/subscribe` and completes payment.

## 1. Create the Discord bot application

1. Go to https://discord.com/developers/applications and click **New
   Application**.
2. In the left sidebar, click **Bot** → **Reset Token** (or "Add Bot") and
   copy the token. Save it — you'll need it as `DISCORD_TOKEN`. Never share
   this token publicly.
3. On the same Bot page, scroll to **Privileged Gateway Intents** and turn ON
   **Message Content Intent** and **Server Members Intent**. Save changes.

## 2. Invite the bot to your server

1. **OAuth2 → URL Generator**.
2. Under **Scopes**, check `bot` and `applications.commands` (the second one
   is required for `/subscribe` and `/subscription_status` to work).
3. Under **Bot Permissions**, check: View Channels, Send Messages, Embed
   Links, Read Message History.
4. Open the generated URL and invite the bot to your server.

## 3. Set up the channels in Discord

1. Create your English mirror channel (e.g. `#admin-en`) and, if you want
   it, a Spanish one (e.g. `#admin-es`).
2. For each, create a webhook: **Channel Settings → Integrations →
   Webhooks → New Webhook → Copy Webhook URL**.
3. Turn on Developer Mode: **User Settings → Advanced → Developer Mode**.
4. Right-click `#admin` → **Copy Channel ID** → this is `SOURCE_CHANNEL_ID`.
5. Right-click your server icon → **Copy Server ID** → this is `GUILD_ID`.
6. Right-click your own username → **Copy User ID** → add this to
   `ADMIN_USER_IDS` (comma-separate if more than one person should be able
   to run `/subscribe`). If you already have Administrator permission on the
   server, this is optional — admins can always run it.

## 4. Set up Dodo Payments

1. Sign up at https://dodopayments.com and complete verification (usually
   fast — a couple of hours to a day for solo/individual accounts).
2. In the dashboard, create a **Product** for your subscription (e.g.
   "Translation Bot — Monthly", recurring price, your currency). Copy its
   **Product ID** (`prod_...`) → this is `DODO_PRODUCT_ID`.
3. Go to **Developer → API Keys** and copy your API key → this is
   `DODO_PAYMENTS_API_KEY`. Start in **test mode** while you get everything
   working, then switch to live mode when ready.
4. Go to **Developer → Webhooks → Add Webhook**. You won't have your Railway
   URL yet — come back to this after step 5. When you do:
   - Endpoint URL: `https://<your-railway-domain>/webhooks/dodo`
   - Events to enable: `subscription.active`, `subscription.renewed`,
     `subscription.cancelled`, `subscription.expired`,
     `subscription.failed`, `subscription.on_hold`
   - Copy the **Webhook Secret** → this is `DODO_PAYMENTS_WEBHOOK_SECRET`.

## 5. Deploy to Railway

1. Push this folder to a GitHub repo, then in Railway: **New Project →
   Deploy from GitHub repo**.
2. In **Settings → Networking**, click **Generate Domain** so Railway gives
   you a public HTTPS URL — this is required for Dodo's webhook to reach
   you. Use that URL (plus `/webhooks/dodo`) in step 4 above.
3. In **Variables**, add:
   - `DISCORD_TOKEN`
   - `SOURCE_CHANNEL_ID`
   - `MIRROR_WEBHOOK_URL`
   - `SPANISH_MIRROR_WEBHOOK_URL` (optional)
   - `GUILD_ID`
   - `ADMIN_USER_IDS` (optional, comma-separated Discord user IDs)
   - `DODO_PAYMENTS_API_KEY`
   - `DODO_PAYMENTS_WEBHOOK_SECRET`
   - `DODO_PAYMENTS_ENVIRONMENT` = `test_mode` (switch to `live_mode` later)
   - `DODO_PRODUCT_ID`
   - `DODO_RETURN_URL` (optional — where the customer lands after paying;
     defaults to a generic Discord URL)

   Railway sets `PORT` automatically — you don't need to add it.
4. **Settings → Deploy**: Start Command `python bot.py` (should also be
   picked up from the Procfile automatically, which now runs as a `web`
   process so Railway routes public traffic to it).
5. Deploy. Check **Logs** — you should see `Logged in as YourBotName` and
   `Webhook server listening on 0.0.0.0:<port>`.
6. Go back to the Dodo webhook you started in step 4 and save the real
   Railway URL if you hadn't yet. Use Dodo's **"Send test event"** button and
   confirm you see `Received Dodo webhook: ...` in the Railway logs.

## 6. Try it

1. In Discord, run `/subscribe` (as an admin). You'll get a payment link
   (ephemeral, only you see it).
2. Complete payment in **test mode** using Dodo's test card numbers (see
   their docs) — a few seconds after it succeeds, translation switches on
   automatically.
3. Run `/subscription_status` any time to check the current state.
4. Post a non-English message in `#admin` — it should now appear translated
   in your mirror channel(s).
5. Once you're confident it's working, switch `DODO_PAYMENTS_ENVIRONMENT` to
   `live_mode`, update the API key/webhook secret to your live ones, and
   redeploy.

## How it behaves

- While inactive: the bot stays online and connected, but posts nothing to
  either mirror channel.
- While active: every non-bot message in `#admin` is mirrored — translated
  to English, and to Spanish too if that webhook is configured — with the
  sender's name and avatar shown, attachments included.
- Subscription state is cached locally and updated instantly by Dodo's
  webhook. An hourly background check also re-syncs directly with Dodo's
  API, so a missed webhook can't leave the bot stuck in the wrong state for
  long.
- The bot ignores messages from other bots, so it won't loop on itself.

## Notes / things you might want to tweak

- **Field names in the webhook handler** (`data.subscription_id`, etc.) are
  based on Dodo's published examples but not independently verified against
  a live payload — after your first real test event, check the Railway logs
  and adjust `handle_dodo_webhook` in `bot.py` if the field names differ.
- State is stored in a local file (`subscription_state.json`), which is
  wiped on a fresh Railway redeploy. The hourly reconcile loop re-derives it
  from Dodo automatically, but if you want it to survive redeploys
  instantly, attach a Railway Volume and point `STATE_FILE` at a path inside
  it.
- Translation uses Google Translate's free web endpoint via
  `deep-translator` — no API key needed, but it can occasionally rate-limit
  under heavy load.
- This is currently single-server (one `GUILD_ID`, one product, one
  on/off switch). If you ever want to sell this to multiple Discord servers
  as a real SaaS, that needs a proper per-guild database instead of the
  single state file — let me know if you want that version.
