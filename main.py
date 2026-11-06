"""Frosty VPS Bot v4 — Docker VPS deployer for Discord."""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

import docker_utils as du
import storage
import views
import webterm
from config import (
    BOT_BRAND, BOT_VERSION, COMMAND_PREFIX, DAILY_CREDITS, DISCORD_BOT_TOKEN, ENABLE_MESSAGE_CONTENT,
    MAIN_ADMIN_ID, MAX_VPS_PER_USER, OS_IMAGES, SYNC_GUILD_ID, VPS_PLANS, WEB_PORT, is_private_ip,
    resolve_server_ip, web_base_url,
)

log = logging.getLogger("frosty")
START_TIME = time.time()


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m}m" if h else (f"{m}m {s}s" if m else f"{s}s")


class FrostyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = ENABLE_MESSAGE_CONTENT
        super().__init__(
            command_prefix=commands.when_mentioned_or(COMMAND_PREFIX), intents=intents, help_command=None,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False))
        self.deploying: set = set()
        self.web_runner = None
        self.web_error: Optional[str] = None

    async def setup_hook(self):
        storage.get_nodes()
        du.ensure_ssh_key()
        self.add_dynamic_items(views.VPSButton)
        webterm.BOT_INFO = lambda: {"name": self.user.name if self.user else "Frosty", "id": str(self.user.id) if self.user else "0"}
        try:
            self.web_runner = await webterm.start_server()
            log.info("Web terminal + pairing API listening on :%s  ->  %s", WEB_PORT, web_base_url())
            if is_private_ip(resolve_server_ip()) and not web_base_url().startswith("https"):
                log.warning("Server IP %s is private: console links only work on your LAN/VPN. "
                            "Set WEB_BASE_URL (e.g. a Cloudflare Tunnel) for access from anywhere.", resolve_server_ip())
        except OSError as e:
            self.web_error = str(e)
            log.error("Could not start web terminal on port %s: %s (is another process using it?)", WEB_PORT, e)
        await self.add_cog(UserCog(self))
        await self.add_cog(AdminCog(self))
        await self.add_cog(NodeCog(self))
        if SYNC_GUILD_ID:
            guild = discord.Object(id=SYNC_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        await self.tree.sync()

    async def close(self):
        if self.web_runner:
            await self.web_runner.cleanup()
        await super().close()

    async def on_ready(self):
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="/help • VPS cloud"))
        log.info("Logged in as %s (%s)", self.user, self.user.id)

    async def on_command_error(self, ctx: commands.Context, error: Exception):
        error = getattr(error, "original", error)
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, (commands.CheckFailure, app_commands.CheckFailure)):
            msg = "❌ You don't have permission to use this command."
        elif isinstance(error, commands.MissingRequiredArgument):
            msg = f"⚠️ Missing argument: `{error.param.name}`. Try `/help`."
        elif isinstance(error, (commands.BadArgument, commands.BadUnionArgument)):
            msg = "⚠️ Invalid argument. Try `/help`."
        elif isinstance(error, commands.CommandOnCooldown):
            msg = f"⏳ Slow down — try again in {error.retry_after:.0f}s."
        else:
            log.exception("Unhandled command error", exc_info=error)
            msg = "❌ Something went wrong. The error was logged."
        try:
            await ctx.send(msg, ephemeral=True)
        except discord.HTTPException:
            pass


def is_admin_check():
    async def pred(ctx: commands.Context) -> bool:
        return views.is_admin_id(ctx.author.id)
    return commands.check(pred)


def is_owner_check():
    async def pred(ctx: commands.Context) -> bool:
        return ctx.author.id == MAIN_ADMIN_ID
    return commands.check(pred)


def footer(bot: commands.Bot) -> str:
    return f"{BOT_BRAND} v{BOT_VERSION}"


async def resolve_vps(ctx: commands.Context, name: Optional[str]) -> Optional[Dict]:
    mine = storage.get_user_vps(ctx.author.id)
    if name:
        v = storage.get_vps(name)
        if v and (v["owner_id"] == str(ctx.author.id) or views.is_admin_id(ctx.author.id)):
            return v
        await ctx.send("❌ VPS not found (or it isn't yours).", ephemeral=True)
        return None
    if not mine:
        await ctx.send("📭 You don't have any VPS yet. Use `/plans` and `/buy`.", ephemeral=True)
        return None
    if len(mine) > 1:
        await ctx.send("You have several VPS — specify one: " + ", ".join(f"`{v['container_name']}`" for v in mine), ephemeral=True)
        return None
    return mine[0]


async def vps_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    rows = storage.get_all_vps() if views.is_admin_id(interaction.user.id) else storage.get_user_vps(interaction.user.id)
    return [app_commands.Choice(name=f"{v['container_name']} ({v['node']})", value=v["container_name"])
            for v in rows if current.lower() in v["container_name"].lower()][:25]


async def deploy(bot: FrostyBot, interaction: discord.Interaction, owner: discord.abc.User, ram: int, cores: int, disk: int,
                 os_key: str, node_name: str, cost: int = 0, plan_label: str = "Custom") -> None:
    """Runs the provisioning and reports back through the component interaction's message."""
    os_label = OS_IMAGES[os_key]["label"]
    if owner.id in bot.deploying:
        return await interaction.response.edit_message(content="⏳ A deployment for this user is already running.", view=None)
    if len(storage.get_user_vps(owner.id)) >= MAX_VPS_PER_USER and not views.is_admin_id(interaction.user.id):
        return await interaction.response.edit_message(content=f"❌ VPS limit reached ({MAX_VPS_PER_USER}).", view=None)
    if cost and storage.change_credits(interaction.user.id, -cost, f"buy:{plan_label}") is None:
        return await interaction.response.edit_message(content="❌ Not enough credits.", view=None)

    bot.deploying.add(owner.id)
    await interaction.response.edit_message(
        content=f"⚙️ **Deploying {plan_label} on `{node_name}`** · `{os_label}` · {ram}GB / {cores}vCPU / {disk}GB\n⏳ Starting…", view=None)

    async def progress(msg: str):
        await interaction.edit_original_response(content=f"⚙️ **Deploying {plan_label} on `{node_name}`**\n{msg}")

    try:
        ok, info, err = await du.create_vps(node_name, owner.id, ram, cores, disk, os_key, progress)
    except Exception as e:  # never lose the user's credits on a crash
        log.exception("deploy crashed")
        ok, info, err = False, {}, f"Internal error: {e}"
    finally:
        bot.deploying.discard(owner.id)

    if not ok:
        if cost:
            storage.change_credits(interaction.user.id, cost, f"refund:{plan_label}", allow_negative=True)
        return await interaction.edit_original_response(
            content=f"❌ Deployment failed on `{node_name}`{' (credits refunded)' if cost else ''}:\n```{err[-900:]}```")

    storage.add_vps(owner.id, info)
    sent_dm = True
    dm = views.vps_embed(info, "running", show_password=True)
    dm.title = f"🎉 Your VPS is ready — {info['container_name']}"
    dm.add_field(name="🖥️ System", value=f"```{info.get('fetch_output', '')[:900]}```", inline=False)
    try:
        await owner.send(embed=dm)
    except discord.HTTPException:
        sent_dm = False
    note = "Credentials sent to your DMs." if sent_dm else "⚠️ Couldn't DM you — press **SSH Info** below to see your password."
    await interaction.edit_original_response(
        content=f"✅ **Deployed** `{info['container_name']}` on **{info['node']}** for {owner.mention}. {note}",
        embed=views.vps_embed(info, "running"), view=views.vps_control_view(info["container_name"]))


# ═══════════════════════════ user commands ═══════════════════════════════
class UserCog(commands.Cog, name="VPS"):
    def __init__(self, bot: FrostyBot):
        self.bot = bot

    @commands.hybrid_command(name="help", description="Show all commands")
    async def help(self, ctx: commands.Context):
        admin = views.is_admin_id(ctx.author.id)
        view = views.HelpView(ctx.author.id, self.bot.user.name, admin)
        await ctx.send(embed=view.pages["start"], view=view, ephemeral=True)

    @commands.hybrid_command(name="ping", description="Bot latency")
    async def ping(self, ctx: commands.Context):
        await ctx.send(f"🏓 Pong! `{round(self.bot.latency * 1000)}ms`", ephemeral=True)

    @commands.hybrid_command(name="about", description="Version and cluster info")
    async def about(self, ctx: commands.Context):
        e = discord.Embed(title=f"❄ {self.bot.user.name}", description=f"{BOT_BRAND} — Docker VPS deployer", color=views.PURPLE)
        e.add_field(name="Version", value=f"`v{BOT_VERSION}`")
        e.add_field(name="Uptime", value=f"`{fmt_duration(time.time() - START_TIME)}`")
        e.add_field(name="Nodes", value=f"`{len(storage.get_nodes())}`")
        e.add_field(name="VPS deployed", value=f"`{len(storage.get_all_vps())}`")
        e.add_field(name="Latency", value=f"`{round(self.bot.latency * 1000)}ms`")
        await ctx.send(embed=e, ephemeral=True)

    @commands.hybrid_command(name="plans", aliases=["shop"], description="View VPS plans")
    async def plans(self, ctx: commands.Context):
        e = discord.Embed(title=f"🛒 {BOT_BRAND} — VPS Plans", description="Use `/buy <plan>` to deploy.", color=views.PURPLE)
        for name, p in VPS_PLANS.items():
            e.add_field(name=f"📦 {name} — {p['price']} credits",
                        value=f"`{p['ram']}GB RAM` • `{p['cpu']} vCPU` • `{p['disk']}GB disk`", inline=False)
        e.set_footer(text=f"Your balance: {storage.get_credits(ctx.author.id)} credits")
        await ctx.send(embed=e, ephemeral=True)

    @commands.hybrid_command(name="buy", description="Buy and deploy a VPS")
    @app_commands.describe(plan="Plan to buy")
    @app_commands.choices(plan=[app_commands.Choice(name=f"{k} — {v['price']} credits", value=k) for k, v in VPS_PLANS.items()])
    async def buy(self, ctx: commands.Context, plan: str):
        key = next((k for k in VPS_PLANS if k.lower() == plan.lower()), None)
        if not key:
            return await ctx.send("❌ Unknown plan. See `/plans`.", ephemeral=True)
        p = VPS_PLANS[key]
        if storage.get_credits(ctx.author.id) < p["price"]:
            return await ctx.send(f"❌ **{key}** costs `{p['price']}` credits — you have `{storage.get_credits(ctx.author.id)}`.", ephemeral=True)
        if len(storage.get_user_vps(ctx.author.id)) >= MAX_VPS_PER_USER:
            return await ctx.send(f"❌ You reached the limit of {MAX_VPS_PER_USER} VPS.", ephemeral=True)

        async def on_os(i: discord.Interaction, os_key: str):
            nodes = storage.get_nodes()

            async def on_node(i2: discord.Interaction, node: str):
                await deploy(self.bot, i2, ctx.author, p["ram"], p["cpu"], p["disk"], os_key, node, p["price"], key)

            if len(nodes) == 1:
                return await on_node(i, next(iter(nodes)))
            await i.response.edit_message(content=f"🛰️ **{OS_IMAGES[os_key]['label']}** — choose a node:", view=views.NodeSelectView(ctx.author.id, on_node))

        await ctx.send(f"🐧 Choose an OS for your **{key}** VPS (`{p['ram']}GB / {p['cpu']} vCPU / {p['disk']}GB`):",
                       view=views.OSSelectView(ctx.author.id, on_os), ephemeral=True)

    @commands.hybrid_command(name="myvps", aliases=["vps"], description="Your VPS and control panel")
    async def myvps(self, ctx: commands.Context):
        mine = storage.get_user_vps(ctx.author.id)
        if not mine:
            return await ctx.send("📭 You don't have any VPS yet. Use `/plans` and `/buy`.", ephemeral=True)
        await ctx.defer(ephemeral=True)
        for v in mine:
            st = await du.container_status(v["node"], v["container_name"])
            await ctx.send(embed=views.vps_embed(v, st), view=views.vps_control_view(v["container_name"]), ephemeral=True)

    async def _session(self, ctx: commands.Context, name: Optional[str], kind: str):
        v = await resolve_vps(ctx, name)
        if not v:
            return
        await ctx.defer(ephemeral=True)
        if kind == "console":
            if await du.container_status(v["node"], v["container_name"]) != "running":
                ok, msg = await du.power(v["node"], v["container_name"], "start")
                if not ok:
                    return await ctx.send(f"❌ Could not start the container: `{msg}`", ephemeral=True)
            url = webterm.create_link(ctx.author.id, v, self.bot.user.name)
            return await ctx.send(f"🖥️ **Web console for `{v['container_name']}`** — [open terminal]({url})\n"
                                  f"Single-use, expires in 10 minutes.\n{webterm.link_hint()}", ephemeral=True)
        fn = du.get_sshx if kind == "sshx" else du.get_tmate
        ok, res = await fn(v["node"], v["container_name"])
        await ctx.send((f"⚡ {res}" if kind == "sshx" else f"```{res}```") if ok else f"❌ Failed: `{res}`", ephemeral=True)

    @commands.hybrid_command(name="console", aliases=["webssh", "terminal"], description="Open a web terminal for your VPS")
    @app_commands.autocomplete(vps=vps_autocomplete)
    async def console(self, ctx: commands.Context, vps: Optional[str] = None):
        await self._session(ctx, vps, "console")

    @commands.hybrid_command(name="sshx", description="Create an SSHX terminal link")
    @app_commands.autocomplete(vps=vps_autocomplete)
    async def sshx(self, ctx: commands.Context, vps: Optional[str] = None):
        await self._session(ctx, vps, "sshx")

    @commands.hybrid_command(name="tmate", description="Create a tmate SSH session")
    @app_commands.autocomplete(vps=vps_autocomplete)
    async def tmate(self, ctx: commands.Context, vps: Optional[str] = None):
        await self._session(ctx, vps, "tmate")

    @commands.hybrid_command(name="credits", aliases=["bal", "balance"], description="Check a credit balance")
    async def credits(self, ctx: commands.Context, user: Optional[discord.User] = None):
        t = user or ctx.author
        e = discord.Embed(title="💎 Credit Wallet", description=f"{t.mention} has **{storage.get_credits(t.id)}** credits.", color=views.GREEN)
        await ctx.send(embed=e, ephemeral=True)

    @commands.hybrid_command(name="daily", description="Claim your free daily credits")
    async def daily(self, ctx: commands.Context):
        if DAILY_CREDITS <= 0:
            return await ctx.send("Daily rewards are disabled.", ephemeral=True)
        wait = storage.claim_daily(ctx.author.id, DAILY_CREDITS)
        if wait is None:
            return await ctx.send(f"🎁 +**{DAILY_CREDITS}** credits! Balance: `{storage.get_credits(ctx.author.id)}`", ephemeral=True)
        await ctx.send(f"⏳ Already claimed. Come back in **{fmt_duration(wait)}**.", ephemeral=True)

    @commands.hybrid_command(name="pay", description="Send credits to another user")
    async def pay(self, ctx: commands.Context, user: discord.User, amount: app_commands.Range[int, 1, 1_000_000]):
        if user.id == ctx.author.id or user.bot:
            return await ctx.send("❌ Pick another (non-bot) user.", ephemeral=True)
        if not storage.transfer_credits(ctx.author.id, user.id, amount):
            return await ctx.send("❌ Not enough credits.", ephemeral=True)
        await ctx.send(f"💸 Sent `{amount}` credits to {user.mention}.")

    @commands.hybrid_command(name="leaderboard", aliases=["top"], description="Top credit holders")
    async def leaderboard(self, ctx: commands.Context):
        rows = storage.leaderboard(10)
        lines = [f"`{i}.` <@{r['id']}> — **{r['credits']}**" for i, r in enumerate(rows, 1)] or ["No data yet."]
        await ctx.send(embed=discord.Embed(title="🏆 Credit Leaderboard", description="\n".join(lines), color=views.GOLD), ephemeral=True)


# ═══════════════════════════ admin commands ══════════════════════════════
class AdminCog(commands.Cog, name="Admin"):
    def __init__(self, bot: FrostyBot):
        self.bot = bot

    @commands.hybrid_group(name="admin", description="Administrator tools", invoke_without_command=True)
    @is_admin_check()
    async def admin(self, ctx: commands.Context):
        await ctx.send("Use a subcommand: `create`, `vpslist`, `deletevps`, `credits`, `webterm`, `sync`, `addadmin`, `removeadmin`.", ephemeral=True)

    @admin.command(name="create", description="Deploy a VPS for a user (free)")
    @is_admin_check()
    async def create(self, ctx: commands.Context, user: discord.User, ram: app_commands.Range[int, 1, 128] = 2,
                     cpu: app_commands.Range[int, 1, 64] = 1, disk: app_commands.Range[int, 1, 2000] = 15):
        async def on_os(i: discord.Interaction, os_key: str):
            async def on_node(i2: discord.Interaction, node: str):
                await deploy(self.bot, i2, user, ram, cpu, disk, os_key, node, 0, "Admin VPS")
            await i.response.edit_message(content=f"🛰️ **{OS_IMAGES[os_key]['label']}** — choose a node:", view=views.NodeSelectView(ctx.author.id, on_node))
        await ctx.send(f"🐧 OS for {user.mention}'s VPS (`{ram}GB / {cpu} vCPU / {disk}GB`):", view=views.OSSelectView(ctx.author.id, on_os), ephemeral=True)

    @admin.command(name="vpslist", description="List every VPS")
    @is_admin_check()
    async def vpslist(self, ctx: commands.Context):
        rows = storage.get_all_vps()
        if not rows:
            return await ctx.send("📭 No VPS deployed.", ephemeral=True)
        lines = [f"• `{v['container_name']}` — <@{v['owner_id']}> · **{v['node']}** · {v['ram_gb']}GB/{v['cores']}c" for v in rows]
        await ctx.send(embed=discord.Embed(title=f"📋 All VPS ({len(rows)})", description="\n".join(lines)[:4000], color=views.PURPLE), ephemeral=True)

    @admin.command(name="deletevps", description="Delete any VPS")
    @is_admin_check()
    @app_commands.autocomplete(name=vps_autocomplete)
    async def deletevps(self, ctx: commands.Context, name: str):
        v = storage.get_vps(name)
        if not v:
            return await ctx.send("❌ Not found.", ephemeral=True)
        await ctx.send(f"⚠️ Delete **{name}** (owner <@{v['owner_id']}>)?", view=views.ConfirmDeleteView(ctx.author.id, v), ephemeral=True)

    @admin.command(name="credits", description="Add, remove or set a user's credits")
    @is_admin_check()
    @app_commands.choices(action=[app_commands.Choice(name=n, value=n) for n in ("add", "remove", "set")])
    async def credits_cmd(self, ctx: commands.Context, action: str, user: discord.User, amount: app_commands.Range[int, 0, 10_000_000]):
        if action == "add":
            new = storage.change_credits(user.id, amount, f"admin:{ctx.author.id}")
        elif action == "remove":
            new = storage.change_credits(user.id, -amount, f"admin:{ctx.author.id}", allow_negative=False) or storage.set_credits(user.id, 0, f"admin:{ctx.author.id}")
        else:
            new = storage.set_credits(user.id, amount, f"admin:{ctx.author.id}")
        await ctx.send(f"✅ {user.mention} now has **{new}** credits.", ephemeral=True)

    @admin.command(name="addadmin", description="Owner: grant admin")
    @is_owner_check()
    async def addadmin(self, ctx: commands.Context, user: discord.User):
        storage.add_admin(user.id)
        await ctx.send(f"✅ {user.mention} is now an administrator.", ephemeral=True)

    @admin.command(name="removeadmin", description="Owner: revoke admin")
    @is_owner_check()
    async def removeadmin(self, ctx: commands.Context, user: discord.User):
        storage.remove_admin(user.id)
        await ctx.send(f"🗑️ Removed {user.mention} from administrators.", ephemeral=True)

    @admin.command(name="sync", description="Re-sync slash commands")
    @is_admin_check()
    async def sync(self, ctx: commands.Context):
        synced = await self.bot.tree.sync()
        await ctx.send(f"✅ Synced `{len(synced)}` commands.", ephemeral=True)

    @admin.command(name="webterm", description="Web terminal & pairing diagnostics")
    @is_admin_check()
    async def webterm_cmd(self, ctx: commands.Context):
        ip = resolve_server_ip()
        e = discord.Embed(title="🖥️ Web Terminal / Pairing", color=views.BLUE)
        e.add_field(name="Link base URL", value=f"`{web_base_url()}`", inline=False)
        e.add_field(name="Server IP", value=f"`{ip}` ({'private' if is_private_ip(ip) else 'public'})", inline=True)
        e.add_field(name="Port", value=f"`{WEB_PORT}`", inline=True)
        e.add_field(name="Server", value="🟢 running" if self.bot.web_runner else f"🔴 `{self.bot.web_error}`", inline=True)
        e.add_field(name="Pairing secret", value=f"||`{storage.get_pair_secret()}`||", inline=False)
        if is_private_ip(ip) and not web_base_url().startswith("https"):
            e.add_field(name="⚠️ Private IP", value=f"Links only open from your LAN/VPN. For anywhere-access set `WEB_BASE_URL` "
                        f"(Cloudflare Tunnel → `http://localhost:{WEB_PORT}`) or open port {WEB_PORT}.", inline=False)
        await ctx.send(embed=e, ephemeral=True)


# ═══════════════════════════ node commands ═══════════════════════════════
class NodeCog(commands.Cog, name="Nodes"):
    def __init__(self, bot: FrostyBot):
        self.bot = bot

    @commands.hybrid_group(name="node", description="Cluster node management", invoke_without_command=True)
    @is_admin_check()
    async def node(self, ctx: commands.Context):
        await ctx.send("Use: `list`, `add`, `remove`, `rename`, `setip`, `key`.", ephemeral=True)

    @node.command(name="list", description="All nodes with live stats")
    @is_admin_check()
    async def node_list(self, ctx: commands.Context):
        await ctx.defer(ephemeral=True)
        nodes = storage.get_nodes()
        stats = await asyncio.gather(*(du.node_stats(n) for n in nodes))
        e = discord.Embed(title=f"🌐 Cluster Nodes ({len(nodes)})", color=views.BLUE)
        for (name, cfg), st in zip(nodes.items(), stats):
            role = "🏠 Primary" if cfg["is_local"] else "🛰️ Worker"
            if st["online"]:
                e.add_field(name=f"🟢 {name} · {cfg['ip']}", inline=False,
                            value=f"{role}\nCPU `{st['cores']}` · RAM `{st['ram']}` · Disk `{st['disk']}`\nContainers `{st['containers']}` · {st['uptime']}")
            else:
                e.add_field(name=f"🔴 {name} · {cfg['ip']}", value=f"{role}\n`{str(st.get('error'))[:200]}`", inline=False)
        await ctx.send(embed=e, ephemeral=True)

    @node.command(name="add", description="Pair a worker node")
    @is_admin_check()
    @app_commands.describe(password="Root password (needs sshpass) — leave empty if the bot key is already authorised")
    async def node_add(self, ctx: commands.Context, name: str, ip: str, port: app_commands.Range[int, 1, 65535] = 22, password: Optional[str] = None):
        if not du.NAME_RE.match(name):
            return await ctx.send("❌ Invalid node name (letters, digits, `_ . -`).", ephemeral=True)
        await ctx.defer(ephemeral=True)
        ok, res = await du.pair_remote_node(name, ip, port, password or "")
        if not ok:
            return await ctx.send(embed=discord.Embed(title="❌ Pairing failed", description=f"```{res[:900]}```\nTip: `/node key` shows the key to add to the worker's `~/.ssh/authorized_keys`.", color=views.RED), ephemeral=True)
        storage.register_node(name, ip, port)
        await ctx.send(embed=discord.Embed(title="✅ Node paired", description=f"**{name}** (`{ip}:{port}`) · Docker `{res}`", color=views.GREEN), ephemeral=True)

    @node.command(name="remove", description="Remove a worker node")
    @is_admin_check()
    async def node_remove(self, ctx: commands.Context, name: str):
        err = storage.remove_node(name)
        await ctx.send(f"❌ {err}" if err else f"🗑️ Removed `{name}`.", ephemeral=True)

    @node.command(name="rename", description="Rename a node")
    @is_admin_check()
    async def node_rename(self, ctx: commands.Context, old: str, new: str):
        if not du.NAME_RE.match(new):
            return await ctx.send("❌ Invalid new name.", ephemeral=True)
        err = storage.rename_node(old, new)
        await ctx.send(f"❌ {err}" if err else f"✅ `{old}` → `{new}` (VPS records updated).", ephemeral=True)

    @node.command(name="setip", description="Set the IP users see/connect to for a node")
    @is_admin_check()
    async def node_setip(self, ctx: commands.Context, name: str, ip: str):
        await ctx.send(f"✅ `{name}` IP set to `{ip}`." if storage.set_node_ip(name, ip) else "❌ Node not found.", ephemeral=True)

    @node.command(name="key", description="Show the bot's SSH public key")
    @is_admin_check()
    async def node_key(self, ctx: commands.Context):
        await ctx.send(f"```\n{du.ensure_ssh_key()}\n```", ephemeral=True)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not DISCORD_BOT_TOKEN or DISCORD_BOT_TOKEN in ("YOUR_BOT_TOKEN", ""):
        sys.exit("DISCORD_BOT_TOKEN is not set (put it in .env or the environment).")
    if not MAIN_ADMIN_ID:
        log.warning("MAIN_ADMIN_ID is not set — nobody will have admin rights.")
    FrostyBot().run(DISCORD_BOT_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
