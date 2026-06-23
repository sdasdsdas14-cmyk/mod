# Railway Moderation Bot

A standalone Discord moderation bot with slash commands, persistent strikes,
automatic 30-day strike expiration, role removal at three active strikes, DMs,
and image-backed moderation logs.

## Commands

- `/setup channel` — choose the moderation log channel (Administrator).
- `/strike member reason proof` — give a 30-day strike and DM the member.
- `/strikes member` — view all active strikes and their proof.
- `/removestrike strike_id reason` — remove one active strike.
- `/clearstrikes member reason` — clear every active strike for a member.
- `/logban username reason proof user_id` — log a ban; proof is required.
- `/logkick username reason proof user_id` — log a kick; proof is required.
- `/logwarn username reason proof user_id` — log a warning.
- `/lognote username reason proof user_id` — add a general moderation note.

When a member reaches three active strikes, the bot removes every role it can
manage. Managed integration roles, `@everyone`, and roles above the bot are
left alone.

## Discord setup

1. Open the [Discord Developer Portal](https://discord.com/developers/applications).
2. Create an application and add a bot.
3. Enable **Server Members Intent** under the bot's privileged intents.
4. Copy the bot token for Railway.
5. In OAuth2 URL Generator, select `bot` and `applications.commands`.
6. Give the bot these permissions:
   - View Channels
   - Send Messages
   - Embed Links
   - Manage Roles
   - Moderate Members
7. Invite the bot and move its role above every role it should be able to remove.

## Railway deployment

1. Put this folder in a GitHub repository.
2. In Railway, create a new project and deploy the repository.
3. If the repository contains other projects, open the Railway service settings
   and set **Root Directory** to `/railway-moderation-bot`.
4. Add the variable `DISCORD_TOKEN` with your bot token.
5. Add a Railway volume mounted at `/data`.
6. Keep `DATABASE_PATH` set to `/data/moderation.sqlite3`.
7. Deploy, then run `/setup` in your Discord server.

The `/data` volume is important. Without it, moderation records can disappear
when Railway replaces or redeploys the container.

## If the bot is offline

1. Confirm the Railway service Root Directory is `/railway-moderation-bot`.
2. Confirm the variable is named exactly `DISCORD_TOKEN`.
3. Paste only the token value, without quotes or `DISCORD_TOKEN=`.
4. In the Discord Developer Portal, enable **Server Members Intent**.
5. Redeploy the latest commit and open Railway's deployment logs.

A successful deployment prints:

```text
Starting moderation bot with database at /data/moderation.sqlite3.
Health server listening on port ...
Synced 9 global slash command(s).
Discord bot online as ...
```

If Railway prints `Improper token has been passed`, reset the token in the
Discord Developer Portal, update `DISCORD_TOKEN`, and redeploy.

## Local run

```powershell
python -m pip install -r requirements.txt
$env:DISCORD_TOKEN="your_token"
$env:DATABASE_PATH="./moderation.sqlite3"
python main.py
```
