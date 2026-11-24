# community_bot.py | discord.py 2.x | Python 3.11
# poll, reaction roles, welcome, giveaway, /panel
# all config from os.environ

import discord
import asyncio
import os
import random
import datetime
from discord.ext import commands, tasks
from discord import app_commands

TOKEN            = os.environ["DISCORD_TOKEN"]
WELCOME_CHANNEL  = int(os.environ.get("WELCOME_CHANNEL_ID", 0))
LOG_CHANNEL      = int(os.environ.get("LOG_CHANNEL_ID", 0))

# reaction role IDs — fill in Railway env
ROLE_TOURNAMENT    = int(os.environ.get("ROLE_TOURNAMENT",    0))
ROLE_RESTOCK       = int(os.environ.get("ROLE_RESTOCK",       0))
ROLE_GIVEAWAY      = int(os.environ.get("ROLE_GIVEAWAY",      0))
ROLE_ANNOUNCEMENT  = int(os.environ.get("ROLE_ANNOUNCEMENT",  0))
ROLE_UPDATE        = int(os.environ.get("ROLE_UPDATE",        0))
ROLE_ALL_NOTIF     = int(os.environ.get("ROLE_ALL_NOTIF",     0))

# panel message ID storage (in-memory — persists until restart)
panel_message_ids: dict[int, int] = {}  # channel_id -> message_id

# active giveaways: message_id -> {end_time, prize, winners, channel_id}
giveaways: dict[int, dict] = {}

REACTION_ROLES = [
    ("🏆", "Tournament Ping",   "Get notified when we host community tournaments, custom games, or 1v1s.", ROLE_TOURNAMENT,   discord.ButtonStyle.danger),
    ("📦", "Restock Ping",      "Be the first to know when new accounts are added to the store.",          ROLE_RESTOCK,      discord.ButtonStyle.secondary),
    ("🎁", "Giveaway Ping",     "Pinged whenever we drop a free account or host a giveaway.",              ROLE_GIVEAWAY,     discord.ButtonStyle.primary),
    ("📣", "Announcement Ping", "For major server news, policy changes, or important updates.",            ROLE_ANNOUNCEMENT, discord.ButtonStyle.secondary),
    ("🔄", "Update Ping",       "Get alerted for new website features, new games, or server changes.",     ROLE_UPDATE,       discord.ButtonStyle.primary),
    ("🔔", "All Notifications", "Don't miss a thing: get notified about everything the server posts.",     ROLE_ALL_NOTIF,    discord.ButtonStyle.primary),
]

# ── bot setup ─────────────────────────────────────────────────
class CommunityBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())

    async def setup_hook(self):
        await self.tree.sync()
        check_giveaways.start()
        print("[+] commands synced")

bot = CommunityBot()

# ── welcome ───────────────────────────────────────────────────
@bot.event
async def on_member_join(member: discord.Member):
    if not WELCOME_CHANNEL:
        return
    ch = member.guild.get_channel(WELCOME_CHANNEL)
    if not ch:
        return
    embed = discord.Embed(
        title=f"👋 Welcome to {member.guild.name}!",
        description=(
            f"Hey {member.mention}, glad you're here!\n\n"
            f"📌 Check out our channels to get started.\n"
            f"🎭 Head to the roles channel to pick your notifications.\n"
            f"🏆 Join tournaments, grab giveaway pings, and stay updated!"
        ),
        color=discord.Color.purple(),
        timestamp=datetime.datetime.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"Member #{member.guild.member_count}")
    await ch.send(embed=embed)

@bot.event
async def on_member_remove(member: discord.Member):
    if not WELCOME_CHANNEL:
        return
    ch = member.guild.get_channel(WELCOME_CHANNEL)
    if not ch:
        return
    embed = discord.Embed(
        title="👋 Member Left",
        description=f"**{member}** has left the server.",
        color=discord.Color.red(),
        timestamp=datetime.datetime.utcnow()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    await ch.send(embed=embed)

# ── reaction role view ────────────────────────────────────────
class RoleButton(discord.ui.Button):
    def __init__(self, emoji, label, role_id, style):
        super().__init__(emoji=emoji, label=label, style=style, custom_id=f"role_{role_id}")
        self.role_id = role_id

    async def callback(self, interaction: discord.Interaction):
        if not self.role_id:
            await interaction.response.send_message("❌ Role not configured.", ephemeral=True)
            return
        role = interaction.guild.get_role(self.role_id)
        if not role:
            await interaction.response.send_message("❌ Role not found.", ephemeral=True)
            return
        if role in interaction.user.roles:
            await interaction.user.remove_roles(role)
            await interaction.response.send_message(f"✅ Removed **{role.name}**", ephemeral=True)
        else:
            await interaction.user.add_roles(role)
            await interaction.response.send_message(f"✅ Assigned **{role.name}**", ephemeral=True)

class RolePanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for emoji, label, _, role_id, style in REACTION_ROLES:
            self.add_item(RoleButton(emoji, label, role_id, style))

def build_panel_embed(guild: discord.Guild) -> discord.Embed:
    embed = discord.Embed(
        title="🎭 Reaction Roles",
        description=(
            "⚠️ **New here? Click any one of these to unlock the rest of the server.** "
            "Keep at least one role to keep your access, and get notified for whichever role you picked.\n"
        ),
        color=discord.Color.purple()
    )
    for emoji, label, desc, role_id, _ in REACTION_ROLES:
        role = guild.get_role(role_id)
        mention = role.mention if role else f"@{label}"
        embed.add_field(
            name=f"{emoji} {mention}",
            value=desc,
            inline=False
        )
    embed.set_footer(text="React below to assign your roles!")
    return embed

# ── /panel command ────────────────────────────────────────────
@bot.tree.command(name="panel", description="send the reaction role panel to a channel")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(channel="channel to send panel to")
async def panel(interaction: discord.Interaction, channel: discord.TextChannel = None):
    ch = channel or interaction.channel
    embed = build_panel_embed(interaction.guild)
    view  = RolePanel()
    msg   = await ch.send(embed=embed, view=view)
    panel_message_ids[ch.id] = msg.id
    await interaction.response.send_message(f"✅ panel sent to {ch.mention}", ephemeral=True)

# ── poll ──────────────────────────────────────────────────────
class PollView(discord.ui.View):
    def __init__(self, options: list[str], duration_minutes: int):
        super().__init__(timeout=duration_minutes * 60)
        self.options  = options
        self.votes: dict[str, set[int]] = {o: set() for o in options}
        for i, opt in enumerate(options):
            btn = discord.ui.Button(label=opt, custom_id=f"poll_{i}",
                                    style=discord.ButtonStyle.secondary)
            btn.callback = self._make_callback(opt)
            self.add_item(btn)

    def _make_callback(self, option: str):
        async def callback(interaction: discord.Interaction):
            uid = interaction.user.id
            # remove from other options
            for opt, voters in self.votes.items():
                voters.discard(uid)
            self.votes[option].add(uid)
            total  = sum(len(v) for v in self.votes.values())
            result = "\n".join(
                f"{opt}: {'█' * int(len(v)/max(total,1)*20):<20} {len(v)} votes"
                for opt, v in self.votes.items()
            )
            embed = interaction.message.embeds[0]
            embed.description = f"```\n{result}\n```\nTotal votes: {total}"
            await interaction.response.edit_message(embed=embed, view=self)
        return callback

@bot.tree.command(name="poll", description="create a poll")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(
    question="poll question",
    options="comma separated options (up to 5)",
    duration="duration in minutes (default 60)"
)
async def poll_cmd(interaction: discord.Interaction, question: str,
                   options: str, duration: int = 60):
    opt_list = [o.strip() for o in options.split(",") if o.strip()][:5]
    if len(opt_list) < 2:
        await interaction.response.send_message("❌ need at least 2 options", ephemeral=True)
        return
    embed = discord.Embed(
        title=f"📊 {question}",
        description="```\nLoading...```",
        color=discord.Color.blurple(),
        timestamp=datetime.datetime.utcnow()
    )
    embed.set_footer(text=f"Poll by {interaction.user} • ends in {duration}m")
    view = PollView(opt_list, duration)
    await interaction.response.send_message(embed=embed, view=view)

# ── giveaway ──────────────────────────────────────────────────
class GiveawayView(discord.ui.View):
    def __init__(self, msg_id: int):
        super().__init__(timeout=None)
        self.msg_id = msg_id
        self.entries: set[int] = set()

    @discord.ui.button(label="🎉 Enter Giveaway", style=discord.ButtonStyle.success,
                       custom_id="giveaway_enter")
    async def enter(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        if uid in self.entries:
            self.entries.discard(uid)
            await interaction.response.send_message("❌ You left the giveaway.", ephemeral=True)
        else:
            self.entries.add(uid)
            await interaction.response.send_message(
                f"✅ You're entered! Total entries: **{len(self.entries)}**", ephemeral=True)

        # update embed entry count
        try:
            msg = await interaction.channel.fetch_message(self.msg_id)
            embed = msg.embeds[0]
            # update footer with entry count
            embed.set_footer(text=f"🎉 {len(self.entries)} entries")
            await msg.edit(embed=embed, view=self)
        except: pass

@bot.tree.command(name="giveaway", description="start a giveaway")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(
    prize="what you're giving away",
    duration="duration in minutes",
    winners="number of winners (default 1)",
    channel="channel to post in"
)
async def giveaway_cmd(interaction: discord.Interaction, prize: str,
                       duration: int, winners: int = 1,
                       channel: discord.TextChannel = None):
    ch = channel or interaction.channel
    end_time = datetime.datetime.utcnow() + datetime.timedelta(minutes=duration)
    embed = discord.Embed(
        title="🎉 GIVEAWAY",
        description=(
            f"**Prize:** {prize}\n"
            f"**Winners:** {winners}\n"
            f"**Ends:** <t:{int(end_time.timestamp())}:R>\n\n"
            f"Click the button below to enter!"
        ),
        color=discord.Color.gold(),
        timestamp=end_time
    )
    embed.set_footer(text="🎉 0 entries")
    embed.set_author(name=f"Hosted by {interaction.user}", icon_url=interaction.user.display_avatar.url)

    await interaction.response.send_message("✅ giveaway started!", ephemeral=True)
    msg = await ch.send(embed=embed)
    view = GiveawayView(msg.id)
    await msg.edit(view=view)

    giveaways[msg.id] = {
        "end_time":  end_time,
        "prize":     prize,
        "winners":   winners,
        "channel_id":ch.id,
        "view":      view,
    }

@tasks.loop(seconds=10)
async def check_giveaways():
    now = datetime.datetime.utcnow()
    ended = [mid for mid, g in giveaways.items() if now >= g["end_time"]]
    for mid in ended:
        g = giveaways.pop(mid)
        guild = next((gu for gu in bot.guilds), None)
        if not guild: continue
        ch = guild.get_channel(g["channel_id"])
        if not ch: continue
        try:
            msg = await ch.fetch_message(mid)
        except: continue

        entries = list(g["view"].entries)
        if not entries:
            await ch.send(f"🎉 Giveaway for **{g['prize']}** ended — no entries!")
            continue

        w_ids   = random.sample(entries, min(g["winners"], len(entries)))
        w_mentions = " ".join(f"<@{uid}>" for uid in w_ids)

        embed = discord.Embed(
            title="🎉 GIVEAWAY ENDED",
            description=(
                f"**Prize:** {g['prize']}\n"
                f"**Winners:** {w_mentions}\n"
                f"Congratulations! 🎊"
            ),
            color=discord.Color.green()
        )
        await msg.edit(embed=embed, view=None)
        await ch.send(f"🎉 Congratulations {w_mentions}! You won **{g['prize']}**!")

@bot.tree.command(name="reroll", description="reroll a giveaway")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(message_id="message ID of the giveaway")
async def reroll(interaction: discord.Interaction, message_id: str):
    try:
        msg = await interaction.channel.fetch_message(int(message_id))
    except:
        await interaction.response.send_message("❌ message not found", ephemeral=True)
        return
    # re-pick from embed description isn't possible without stored entries
    # so notify user
    await interaction.response.send_message(
        "⚠️ Reroll only works on active giveaways. Start a new one if this ended.",
        ephemeral=True
    )

# ── utility ───────────────────────────────────────────────────
@bot.tree.command(name="announce", description="send an announcement embed")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(title="title", message="body", channel="channel", ping="role to ping")
async def announce(interaction: discord.Interaction, title: str, message: str,
                   channel: discord.TextChannel = None, ping: discord.Role = None):
    ch = channel or interaction.channel
    embed = discord.Embed(title=title, description=message,
                          color=discord.Color.purple(),
                          timestamp=datetime.datetime.utcnow())
    embed.set_footer(text=f"Announcement by {interaction.user}")
    content = ping.mention if ping else None
    await ch.send(content=content, embed=embed)
    await interaction.response.send_message("✅ announced", ephemeral=True)

@bot.tree.command(name="embed", description="send a custom embed")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(title="title", description="body", color="hex color e.g. ff0000",
                       channel="channel to send to")
async def embed_cmd(interaction: discord.Interaction, title: str, description: str,
                    color: str = "7c3aed", channel: discord.TextChannel = None):
    ch = channel or interaction.channel
    try:    c = discord.Color(int(color.strip("#"), 16))
    except: c = discord.Color.purple()
    embed = discord.Embed(title=title, description=description, color=c)
    await ch.send(embed=embed)
    await interaction.response.send_message("✅ sent", ephemeral=True)

@bot.tree.command(name="say", description="make the bot say something")
@app_commands.checks.has_permissions(manage_messages=True)
@app_commands.describe(message="message to send", channel="channel")
async def say(interaction: discord.Interaction, message: str,
              channel: discord.TextChannel = None):
    ch = channel or interaction.channel
    await ch.send(message)
    await interaction.response.send_message("✅", ephemeral=True)

@bot.tree.command(name="ping", description="bot latency")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 {round(bot.latency * 1000)}ms")

@bot.tree.command(name="membercount", description="current member count")
async def membercount(interaction: discord.Interaction):
    await interaction.response.send_message(f"👥 **{interaction.guild.member_count}** members")

@bot.tree.command(name="serverinfo", description="server info")
async def serverinfo(interaction: discord.Interaction):
    g = interaction.guild
    embed = discord.Embed(title=g.name, color=discord.Color.purple())
    embed.set_thumbnail(url=g.icon.url if g.icon else None)
    embed.add_field(name="Members",  value=g.member_count,     inline=True)
    embed.add_field(name="Channels", value=len(g.channels),    inline=True)
    embed.add_field(name="Roles",    value=len(g.roles),        inline=True)
    embed.add_field(name="Owner",    value=g.owner,             inline=True)
    embed.add_field(name="Created",  value=g.created_at.strftime("%Y-%m-%d"), inline=True)
    embed.add_field(name="Boosts",   value=g.premium_subscription_count, inline=True)
    await interaction.response.send_message(embed=embed)

@bot.event
async def on_ready():
    # re-add persistent views so buttons work after restart
    bot.add_view(RolePanel())
    print(f"[+] logged in as {bot.user}")

bot.run(TOKEN)
