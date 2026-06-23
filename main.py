import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks


TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "/data/moderation.sqlite3"))
STRIKE_LIFETIME_DAYS = 30
STRIKE_LIMIT = 3

intents = discord.Intents.default()
intents.guilds = True
intents.members = True


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def discord_timestamp(value: datetime, style: str = "F") -> str:
    return f"<t:{int(value.timestamp())}:{style}>"


class ModerationDatabase:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._create_tables()

    def _create_tables(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id INTEGER PRIMARY KEY,
                log_channel_id INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS strikes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                moderator_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                proof_url TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                removed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_active_strikes
            ON strikes (guild_id, user_id, expires_at, removed_at);

            CREATE TABLE IF NOT EXISTS moderation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                username TEXT NOT NULL,
                target_user_id INTEGER,
                moderator_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                proof_url TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def set_log_channel(self, guild_id: int, channel_id: int) -> None:
        self.connection.execute(
            """
            INSERT INTO guild_config (guild_id, log_channel_id)
            VALUES (?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                log_channel_id = excluded.log_channel_id
            """,
            (guild_id, channel_id),
        )
        self.connection.commit()

    def get_log_channel_id(self, guild_id: int) -> int | None:
        row = self.connection.execute(
            "SELECT log_channel_id FROM guild_config WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        return int(row["log_channel_id"]) if row else None

    def add_strike(
        self,
        guild_id: int,
        user_id: int,
        moderator_id: int,
        reason: str,
        proof_url: str,
    ) -> tuple[int, datetime]:
        created_at = utc_now()
        expires_at = created_at + timedelta(days=STRIKE_LIFETIME_DAYS)
        cursor = self.connection.execute(
            """
            INSERT INTO strikes (
                guild_id,
                user_id,
                moderator_id,
                reason,
                proof_url,
                created_at,
                expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                user_id,
                moderator_id,
                reason,
                proof_url,
                created_at.isoformat(),
                expires_at.isoformat(),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid), expires_at

    def get_active_strikes(self, guild_id: int, user_id: int) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT * FROM strikes
                WHERE guild_id = ?
                  AND user_id = ?
                  AND removed_at IS NULL
                  AND expires_at > ?
                ORDER BY created_at ASC
                """,
                (guild_id, user_id, utc_now().isoformat()),
            ).fetchall()
        )

    def remove_strike(self, guild_id: int, strike_id: int) -> sqlite3.Row | None:
        strike = self.connection.execute(
            """
            SELECT * FROM strikes
            WHERE guild_id = ? AND id = ? AND removed_at IS NULL
            """,
            (guild_id, strike_id),
        ).fetchone()
        if strike is None:
            return None

        self.connection.execute(
            "UPDATE strikes SET removed_at = ? WHERE id = ?",
            (utc_now().isoformat(), strike_id),
        )
        self.connection.commit()
        return strike

    def clear_strikes(self, guild_id: int, user_id: int) -> int:
        cursor = self.connection.execute(
            """
            UPDATE strikes
            SET removed_at = ?
            WHERE guild_id = ?
              AND user_id = ?
              AND removed_at IS NULL
              AND expires_at > ?
            """,
            (utc_now().isoformat(), guild_id, user_id, utc_now().isoformat()),
        )
        self.connection.commit()
        return cursor.rowcount

    def expire_old_strikes(self) -> int:
        cursor = self.connection.execute(
            """
            UPDATE strikes
            SET removed_at = expires_at
            WHERE removed_at IS NULL AND expires_at <= ?
            """,
            (utc_now().isoformat(),),
        )
        self.connection.commit()
        return cursor.rowcount

    def add_moderation_log(
        self,
        guild_id: int,
        action: str,
        username: str,
        target_user_id: int | None,
        moderator_id: int,
        reason: str,
        proof_url: str | None,
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO moderation_logs (
                guild_id,
                action,
                username,
                target_user_id,
                moderator_id,
                reason,
                proof_url,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                action,
                username,
                target_user_id,
                moderator_id,
                reason,
                proof_url,
                utc_now().isoformat(),
            ),
        )
        self.connection.commit()
        return int(cursor.lastrowid)


class ModerationBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents)
        self.database = ModerationDatabase(DATABASE_PATH)

    async def setup_hook(self) -> None:
        expire_strikes.start()
        await self.tree.sync()


bot = ModerationBot()


async def send_response(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    ephemeral: bool = True,
) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, embed=embed, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(content, embed=embed, ephemeral=ephemeral)


def get_guild(interaction: discord.Interaction) -> discord.Guild | None:
    return interaction.guild


def make_log_embed(
    action: str,
    username: str,
    moderator: discord.abc.User,
    reason: str,
    proof_url: str | None,
    case_id: int,
    target_user_id: int | None = None,
) -> discord.Embed:
    colors = {
        "BAN": discord.Color.red(),
        "KICK": discord.Color.orange(),
        "WARN": discord.Color.gold(),
        "NOTE": discord.Color.blue(),
        "STRIKE": discord.Color.dark_orange(),
        "STRIKE REMOVED": discord.Color.green(),
        "STRIKES CLEARED": discord.Color.green(),
    }
    embed = discord.Embed(
        title=f"{action} • Case #{case_id}",
        color=colors.get(action, discord.Color.blurple()),
        timestamp=utc_now(),
    )
    embed.add_field(name="Username", value=username, inline=True)
    if target_user_id:
        embed.add_field(name="User ID", value=str(target_user_id), inline=True)
    embed.add_field(
        name="Moderator",
        value=f"{moderator.mention} (`{moderator.id}`)",
        inline=False,
    )
    embed.add_field(name="Reason", value=reason[:1024], inline=False)
    if proof_url:
        embed.add_field(name="Proof", value=f"[Open attachment]({proof_url})", inline=False)
        embed.set_image(url=proof_url)
    embed.set_footer(text="Moderation log")
    return embed


async def post_log(guild: discord.Guild, embed: discord.Embed) -> bool:
    channel_id = bot.database.get_log_channel_id(guild.id)
    if channel_id is None:
        return False
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return False
    try:
        await channel.send(embed=embed)
        return True
    except discord.HTTPException:
        return False


async def remove_member_roles(member: discord.Member, reason: str) -> list[discord.Role]:
    bot_member = member.guild.me
    if bot_member is None:
        return []
    removable_roles = [
        role
        for role in member.roles
        if role != member.guild.default_role
        and not role.managed
        and role < bot_member.top_role
    ]
    if removable_roles:
        await member.remove_roles(*removable_roles, reason=reason)
    return removable_roles


async def log_generic_action(
    interaction: discord.Interaction,
    action: str,
    username: str,
    reason: str,
    proof: discord.Attachment | None,
    user_id: str | None,
) -> None:
    guild = get_guild(interaction)
    if guild is None:
        await send_response(interaction, "This command can only be used in a server.")
        return

    parsed_user_id: int | None = None
    if user_id:
        try:
            parsed_user_id = int(user_id)
        except ValueError:
            await send_response(interaction, "The user ID must contain numbers only.")
            return

    proof_url = proof.url if proof else None
    case_id = bot.database.add_moderation_log(
        guild.id,
        action,
        username,
        parsed_user_id,
        interaction.user.id,
        reason,
        proof_url,
    )
    embed = make_log_embed(
        action,
        username,
        interaction.user,
        reason,
        proof_url,
        case_id,
        parsed_user_id,
    )
    logged = await post_log(guild, embed)
    message = f"{action.title()} saved as case #{case_id}."
    if not logged:
        message += " Run `/setup` to choose a log channel."
    await send_response(interaction, message)


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} ({bot.user.id if bot.user else 'unknown'})")


@tasks.loop(hours=1)
async def expire_strikes() -> None:
    expired = bot.database.expire_old_strikes()
    if expired:
        print(f"Expired {expired} old strike(s).")


@expire_strikes.before_loop
async def before_expire_strikes() -> None:
    await bot.wait_until_ready()


@bot.tree.command(name="setup", description="Set the channel used for moderation logs.")
@app_commands.describe(channel="Channel where moderation cases will be posted")
@app_commands.checks.has_permissions(administrator=True)
async def setup_command(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
) -> None:
    guild = get_guild(interaction)
    if guild is None:
        await send_response(interaction, "This command can only be used in a server.")
        return
    bot.database.set_log_channel(guild.id, channel.id)
    await send_response(interaction, f"Moderation logs will be posted in {channel.mention}.")


@bot.tree.command(name="strike", description="Give a member a 30-day strike.")
@app_commands.describe(
    member="Member receiving the strike",
    reason="Reason for the strike",
    proof="Required screenshot or other proof",
)
@app_commands.checks.has_permissions(moderate_members=True)
async def strike_command(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: app_commands.Range[str, 1, 1000],
    proof: discord.Attachment,
) -> None:
    guild = get_guild(interaction)
    if guild is None:
        await send_response(interaction, "This command can only be used in a server.")
        return
    if member.bot:
        await send_response(interaction, "Bots cannot receive strikes.")
        return
    if member == interaction.user:
        await send_response(interaction, "You cannot strike yourself.")
        return
    if not proof.content_type or not proof.content_type.startswith("image/"):
        await send_response(interaction, "Proof must be an image attachment.")
        return

    await interaction.response.defer(ephemeral=True)
    strike_id, expires_at = bot.database.add_strike(
        guild.id,
        member.id,
        interaction.user.id,
        reason,
        proof.url,
    )
    active_strikes = bot.database.get_active_strikes(guild.id, member.id)
    strike_count = len(active_strikes)
    roles_removed: list[discord.Role] = []
    role_error: str | None = None

    if strike_count >= STRIKE_LIMIT:
        try:
            roles_removed = await remove_member_roles(
                member,
                f"Reached {STRIKE_LIMIT} active strikes; latest case #{strike_id}",
            )
        except discord.Forbidden:
            role_error = "I could not remove roles. Put my bot role above the member's roles."
        except discord.HTTPException:
            role_error = "Discord returned an error while I was removing roles."

    dm_embed = discord.Embed(
        title=f"You received a strike in {guild.name}",
        color=discord.Color.dark_orange(),
        timestamp=utc_now(),
    )
    dm_embed.add_field(name="Reason", value=reason, inline=False)
    dm_embed.add_field(name="Proof", value=f"[View proof]({proof.url})", inline=False)
    dm_embed.add_field(
        name="Active strikes",
        value=f"{strike_count}/{STRIKE_LIMIT}",
        inline=True,
    )
    dm_embed.add_field(
        name="Expires",
        value=discord_timestamp(expires_at),
        inline=True,
    )
    if strike_count >= STRIKE_LIMIT:
        dm_embed.add_field(
            name="Action",
            value="Your removable server roles were removed because you reached 3 strikes.",
            inline=False,
        )
    dm_embed.set_image(url=proof.url)

    dm_sent = True
    try:
        await member.send(embed=dm_embed)
    except (discord.Forbidden, discord.HTTPException):
        dm_sent = False

    case_id = bot.database.add_moderation_log(
        guild.id,
        "STRIKE",
        str(member),
        member.id,
        interaction.user.id,
        reason,
        proof.url,
    )
    log_embed = make_log_embed(
        "STRIKE",
        str(member),
        interaction.user,
        reason,
        proof.url,
        case_id,
        member.id,
    )
    log_embed.add_field(
        name="Strike",
        value=f"#{strike_id} • {strike_count}/{STRIKE_LIMIT} active",
        inline=False,
    )
    log_embed.add_field(
        name="Expires",
        value=discord_timestamp(expires_at),
        inline=True,
    )
    if roles_removed:
        log_embed.add_field(
            name="Roles removed",
            value=", ".join(role.name for role in roles_removed)[:1024],
            inline=False,
        )
    if role_error:
        log_embed.add_field(name="Role removal error", value=role_error, inline=False)
    await post_log(guild, log_embed)

    result = (
        f"{member.mention} now has **{strike_count}/{STRIKE_LIMIT}** active strikes. "
        f"Strike ID: **#{strike_id}**."
    )
    if not dm_sent:
        result += " Their DMs are closed, so the notification could not be delivered."
    if roles_removed:
        result += f" Removed {len(roles_removed)} role(s)."
    if role_error:
        result += f" {role_error}"
    await send_response(interaction, result)


@bot.tree.command(name="strikes", description="View a member's active strikes.")
@app_commands.describe(member="Member whose strikes you want to view")
@app_commands.checks.has_permissions(moderate_members=True)
async def strikes_command(
    interaction: discord.Interaction,
    member: discord.Member,
) -> None:
    guild = get_guild(interaction)
    if guild is None:
        await send_response(interaction, "This command can only be used in a server.")
        return

    strikes = bot.database.get_active_strikes(guild.id, member.id)
    if not strikes:
        await send_response(interaction, f"{member.mention} has no active strikes.")
        return

    lines = []
    for strike in strikes:
        expires_at = datetime.fromisoformat(strike["expires_at"])
        lines.append(
            f"**#{strike['id']}** • {strike['reason'][:180]}\n"
            f"Expires {discord_timestamp(expires_at, 'R')} • "
            f"[Proof]({strike['proof_url']})"
        )
    embed = discord.Embed(
        title=f"Active strikes for {member}",
        description="\n\n".join(lines)[:4000],
        color=discord.Color.dark_orange(),
    )
    embed.set_footer(text=f"{len(strikes)}/{STRIKE_LIMIT} active strikes")
    await send_response(interaction, embed=embed)


@bot.tree.command(name="removestrike", description="Remove one strike by its ID.")
@app_commands.describe(strike_id="Strike number shown by /strikes", reason="Why it was removed")
@app_commands.checks.has_permissions(moderate_members=True)
async def remove_strike_command(
    interaction: discord.Interaction,
    strike_id: int,
    reason: app_commands.Range[str, 1, 1000],
) -> None:
    guild = get_guild(interaction)
    if guild is None:
        await send_response(interaction, "This command can only be used in a server.")
        return
    strike = bot.database.remove_strike(guild.id, strike_id)
    if strike is None:
        await send_response(interaction, "That active strike does not exist in this server.")
        return

    case_id = bot.database.add_moderation_log(
        guild.id,
        "STRIKE REMOVED",
        str(strike["user_id"]),
        int(strike["user_id"]),
        interaction.user.id,
        reason,
        strike["proof_url"],
    )
    embed = make_log_embed(
        "STRIKE REMOVED",
        str(strike["user_id"]),
        interaction.user,
        reason,
        strike["proof_url"],
        case_id,
        int(strike["user_id"]),
    )
    embed.add_field(name="Removed strike", value=f"#{strike_id}", inline=False)
    await post_log(guild, embed)
    await send_response(interaction, f"Strike **#{strike_id}** was removed.")


@bot.tree.command(name="clearstrikes", description="Remove all active strikes from a member.")
@app_commands.describe(member="Member whose strikes will be cleared", reason="Why they were cleared")
@app_commands.checks.has_permissions(moderate_members=True)
async def clear_strikes_command(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: app_commands.Range[str, 1, 1000],
) -> None:
    guild = get_guild(interaction)
    if guild is None:
        await send_response(interaction, "This command can only be used in a server.")
        return
    removed = bot.database.clear_strikes(guild.id, member.id)
    if removed == 0:
        await send_response(interaction, f"{member.mention} has no active strikes.")
        return

    case_id = bot.database.add_moderation_log(
        guild.id,
        "STRIKES CLEARED",
        str(member),
        member.id,
        interaction.user.id,
        reason,
        None,
    )
    embed = make_log_embed(
        "STRIKES CLEARED",
        str(member),
        interaction.user,
        reason,
        None,
        case_id,
        member.id,
    )
    embed.add_field(name="Strikes removed", value=str(removed), inline=False)
    await post_log(guild, embed)
    await send_response(interaction, f"Cleared **{removed}** strike(s) from {member.mention}.")


@bot.tree.command(name="logban", description="Log a ban with required image proof.")
@app_commands.describe(
    username="Banned account's username",
    reason="Reason for the ban",
    proof="Required screenshot proof",
    user_id="Optional Discord user ID",
)
@app_commands.checks.has_permissions(ban_members=True)
async def log_ban_command(
    interaction: discord.Interaction,
    username: app_commands.Range[str, 1, 100],
    reason: app_commands.Range[str, 1, 1000],
    proof: discord.Attachment,
    user_id: str | None = None,
) -> None:
    if not proof.content_type or not proof.content_type.startswith("image/"):
        await send_response(interaction, "Proof must be an image attachment.")
        return
    await log_generic_action(interaction, "BAN", username, reason, proof, user_id)


@bot.tree.command(name="logkick", description="Log a kick with image proof.")
@app_commands.describe(
    username="Kicked account's username",
    reason="Reason for the kick",
    proof="Required screenshot proof",
    user_id="Optional Discord user ID",
)
@app_commands.checks.has_permissions(kick_members=True)
async def log_kick_command(
    interaction: discord.Interaction,
    username: app_commands.Range[str, 1, 100],
    reason: app_commands.Range[str, 1, 1000],
    proof: discord.Attachment,
    user_id: str | None = None,
) -> None:
    if not proof.content_type or not proof.content_type.startswith("image/"):
        await send_response(interaction, "Proof must be an image attachment.")
        return
    await log_generic_action(interaction, "KICK", username, reason, proof, user_id)


@bot.tree.command(name="logwarn", description="Log a warning with optional image proof.")
@app_commands.describe(
    username="Warned account's username",
    reason="Reason for the warning",
    proof="Optional screenshot proof",
    user_id="Optional Discord user ID",
)
@app_commands.checks.has_permissions(moderate_members=True)
async def log_warn_command(
    interaction: discord.Interaction,
    username: app_commands.Range[str, 1, 100],
    reason: app_commands.Range[str, 1, 1000],
    proof: discord.Attachment | None = None,
    user_id: str | None = None,
) -> None:
    if proof and (not proof.content_type or not proof.content_type.startswith("image/")):
        await send_response(interaction, "Proof must be an image attachment.")
        return
    await log_generic_action(interaction, "WARN", username, reason, proof, user_id)


@bot.tree.command(name="lognote", description="Add a general moderation note.")
@app_commands.describe(
    username="Account the note is about",
    reason="The moderation note",
    proof="Optional screenshot proof",
    user_id="Optional Discord user ID",
)
@app_commands.checks.has_permissions(moderate_members=True)
async def log_note_command(
    interaction: discord.Interaction,
    username: app_commands.Range[str, 1, 100],
    reason: app_commands.Range[str, 1, 1000],
    proof: discord.Attachment | None = None,
    user_id: str | None = None,
) -> None:
    if proof and (not proof.content_type or not proof.content_type.startswith("image/")):
        await send_response(interaction, "Proof must be an image attachment.")
        return
    await log_generic_action(interaction, "NOTE", username, reason, proof, user_id)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        await send_response(interaction, "You do not have permission to use that command.")
        return
    if isinstance(error, app_commands.CommandOnCooldown):
        await send_response(interaction, f"Try again in {error.retry_after:.1f} seconds.")
        return
    original = getattr(error, "original", error)
    print(f"Command error: {original!r}")
    await send_response(interaction, "Something went wrong while running that command.")


def main() -> None:
    if not TOKEN:
        raise RuntimeError("Set the DISCORD_TOKEN environment variable before starting the bot.")
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
