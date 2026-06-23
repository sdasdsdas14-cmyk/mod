import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks


TOKEN = (
    os.getenv("DISCORD_TOKEN")
    or os.getenv("DISCORD_BOT_TOKEN")
    or os.getenv("BOT_TOKEN")
)
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "/data/moderation.sqlite3"))
PORT = int(os.getenv("PORT", "8080"))
STRIKE_LIFETIME_DAYS = 30
STRIKE_LIMIT = 3
BUILD_VERSION = "2026-06-23-role-command-access-1"

CONFIGURABLE_COMMANDS = {
    "strike": "Give members strikes",
    "strikes": "View active strikes",
    "removestrike": "Remove one strike",
    "clearstrikes": "Clear all active strikes",
    "logban": "Log bans",
    "logkick": "Log kicks",
    "logwarn": "Log warnings",
    "lognote": "Add moderation notes",
}
COMMAND_CHOICES = [
    app_commands.Choice(name=f"/{name} - {description}", value=name)
    for name, description in CONFIGURABLE_COMMANDS.items()
]

intents = discord.Intents.none()
intents.guilds = True


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

            CREATE TABLE IF NOT EXISTS command_role_access (
                guild_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                command_name TEXT NOT NULL,
                PRIMARY KEY (guild_id, role_id, command_name)
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

    def set_command_role_access(
        self,
        guild_id: int,
        role_id: int,
        command_name: str,
        allowed: bool,
    ) -> None:
        if allowed:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO command_role_access (
                    guild_id,
                    role_id,
                    command_name
                ) VALUES (?, ?, ?)
                """,
                (guild_id, role_id, command_name),
            )
        else:
            self.connection.execute(
                """
                DELETE FROM command_role_access
                WHERE guild_id = ? AND role_id = ? AND command_name = ?
                """,
                (guild_id, role_id, command_name),
            )
        self.connection.commit()

    def role_can_use_command(
        self,
        guild_id: int,
        role_ids: list[int],
        command_name: str,
    ) -> bool:
        if not role_ids:
            return False
        placeholders = ",".join("?" for _ in role_ids)
        row = self.connection.execute(
            f"""
            SELECT 1 FROM command_role_access
            WHERE guild_id = ?
              AND command_name = ?
              AND role_id IN ({placeholders})
            LIMIT 1
            """,
            (guild_id, command_name, *role_ids),
        ).fetchone()
        return row is not None

    def get_role_commands(self, guild_id: int, role_id: int) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT command_name FROM command_role_access
            WHERE guild_id = ? AND role_id = ?
            ORDER BY command_name
            """,
            (guild_id, role_id),
        ).fetchall()
        return [str(row["command_name"]) for row in rows]


class ModerationBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents)
        self.database = ModerationDatabase(DATABASE_PATH)
        self.web_runner: web.AppRunner | None = None

    async def setup_hook(self) -> None:
        await self.start_health_server()
        expire_strikes.start()
        commands_synced = await self.tree.sync()
        print(f"Synced {len(commands_synced)} global slash command(s).", flush=True)

    async def start_health_server(self) -> None:
        async def health_check(_: web.Request) -> web.Response:
            status = "online" if self.is_ready() else "starting"
            return web.json_response({"status": status})

        application = web.Application()
        application.router.add_get("/", health_check)
        application.router.add_get("/health", health_check)
        self.web_runner = web.AppRunner(application)
        await self.web_runner.setup()
        site = web.TCPSite(self.web_runner, "0.0.0.0", PORT)
        await site.start()
        print(f"Health server listening on port {PORT}.", flush=True)

    async def close(self) -> None:
        if self.web_runner is not None:
            await self.web_runner.cleanup()
        await super().close()


bot = ModerationBot()


def command_access(command_name: str, native_permission: str):
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False

        permissions = interaction.user.guild_permissions
        if permissions.administrator or interaction.guild.owner_id == interaction.user.id:
            return True
        if getattr(permissions, native_permission, False):
            return True

        role_ids = [role.id for role in interaction.user.roles]
        return bot.database.role_can_use_command(
            interaction.guild.id,
            role_ids,
            command_name,
        )

    return app_commands.check(predicate)


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
    print(
        f"Discord bot online as {bot.user} ({bot.user.id if bot.user else 'unknown'}).",
        flush=True,
    )


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
@command_access("strike", "moderate_members")
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
@command_access("strikes", "moderate_members")
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
@command_access("removestrike", "moderate_members")
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
@command_access("clearstrikes", "moderate_members")
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
@command_access("logban", "ban_members")
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
@command_access("logkick", "kick_members")
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
@command_access("logwarn", "moderate_members")
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
@command_access("lognote", "moderate_members")
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


@bot.tree.command(
    name="setcommandrole",
    description="Allow or revoke a role's access to a moderation command.",
)
@app_commands.describe(
    role="Role whose command access will be changed",
    command="Moderation command to configure",
    allowed="True allows the command; false revokes it",
)
@app_commands.choices(command=COMMAND_CHOICES)
@app_commands.checks.has_permissions(administrator=True)
async def set_command_role(
    interaction: discord.Interaction,
    role: discord.Role,
    command: app_commands.Choice[str],
    allowed: bool,
) -> None:
    guild = get_guild(interaction)
    if guild is None:
        await send_response(interaction, "This command can only be used in a server.")
        return

    bot.database.set_command_role_access(
        guild.id,
        role.id,
        command.value,
        allowed,
    )
    action = "can now use" if allowed else "can no longer use"
    await send_response(
        interaction,
        f"{role.mention} {action} `/{command.value}`.",
    )


@bot.tree.command(
    name="rolecommands",
    description="Show which moderation commands a role can use.",
)
@app_commands.describe(role="Role whose configured commands will be shown")
@app_commands.checks.has_permissions(administrator=True)
async def role_commands(
    interaction: discord.Interaction,
    role: discord.Role,
) -> None:
    guild = get_guild(interaction)
    if guild is None:
        await send_response(interaction, "This command can only be used in a server.")
        return

    commands_allowed = bot.database.get_role_commands(guild.id, role.id)
    if not commands_allowed:
        await send_response(
            interaction,
            f"{role.mention} has no extra command access configured.",
        )
        return

    command_list = "\n".join(f"• `/{name}`" for name in commands_allowed)
    embed = discord.Embed(
        title=f"Command access for {role.name}",
        description=command_list,
        color=discord.Color.blurple(),
    )
    await send_response(interaction, embed=embed)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        await send_response(interaction, "You do not have permission to use that command.")
        return
    if isinstance(error, app_commands.CheckFailure):
        await send_response(
            interaction,
            "Your roles do not have access to that command.",
        )
        return
    if isinstance(error, app_commands.CommandOnCooldown):
        await send_response(interaction, f"Try again in {error.retry_after:.1f} seconds.")
        return
    original = getattr(error, "original", error)
    print(f"Command error: {original!r}")
    await send_response(interaction, "Something went wrong while running that command.")


def main() -> None:
    if not TOKEN:
        raise RuntimeError(
            "Missing bot token. Add DISCORD_TOKEN in Railway Variables, then redeploy."
        )
    print(f"Moderation bot build: {BUILD_VERSION}", flush=True)
    print(f"Starting moderation bot with database at {DATABASE_PATH}.", flush=True)
    bot.run(TOKEN.strip())


if __name__ == "__main__":
    main()
