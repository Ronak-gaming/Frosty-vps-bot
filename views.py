"""Discord UI: persistent VPS control panel, deploy wizard, paginated help."""
from __future__ import annotations

import re
from typing import Awaitable, Callable, Dict, List, Optional

import discord

import docker_utils as du
import storage
import webterm
from config import BOT_BRAND, BOT_VERSION, OS_IMAGES, resolve_server_ip, ssh_exposed

PURPLE, GREEN, BLUE, GOLD, RED = 0x7C6AFF, 0x2ECC71, 0x00A8FF, 0xF0B34B, 0xE74C3C


def is_admin_id(uid: int) -> bool:
    from config import MAIN_ADMIN_ID
    return uid == MAIN_ADMIN_ID or uid in storage.get_admins()


def vps_embed(v: Dict, status: Optional[str] = None, show_password: bool = False) -> discord.Embed:
    icon = {"running": "🟢", "exited": "🔴", "restarting": "🟡", "missing": "⚫"}.get(status or "", "⚪")
    e = discord.Embed(title=f"{icon} {v['container_name']}", color=PURPLE)
    e.add_field(name="🛰️ Node", value=f"`{v['node']}`", inline=True)
    e.add_field(name="🐧 OS", value=v.get("os_label") or "Linux", inline=True)
    e.add_field(name="⚙️ Status", value=f"`{status or 'unknown'}`", inline=True)
    e.add_field(name="📊 Resources", value=f"`{v['ram_gb']}GB RAM` • `{v['cores']} vCPU` • `{v['disk_gb']}GB disk`", inline=False)
    if ssh_exposed(v["ip"]):
        e.add_field(name="🔌 SSH", value=f"```ssh root@{v['ip']} -p {v['ssh_port']}```", inline=False)
        if show_password:
            e.add_field(name="🔑 Root password", value=f"||`{v['root_password']}`||", inline=False)
    else:
        e.add_field(name="🔒 Access", inline=False,
                    value="Private network — direct SSH is not available.\nUse **🖥️ Web Console** or **⚡ SSHX** below "
                          "(or `/console`, `/sshx`). System info shows automatically when you connect.")
    e.set_footer(text=f"{BOT_BRAND} v{BOT_VERSION}")
    return e


# ── persistent control buttons (survive bot restarts) ────────────────────
ACTIONS = {
    "start": ("Start", discord.ButtonStyle.success, "▶️", 0),
    "stop": ("Stop", discord.ButtonStyle.secondary, "⏹️", 0),
    "restart": ("Restart", discord.ButtonStyle.primary, "🔄", 0),
    "stats": ("Stats", discord.ButtonStyle.secondary, "📈", 0),
    "console": ("Web Console", discord.ButtonStyle.success, "🖥️", 1),
    "sshx": ("SSHX", discord.ButtonStyle.secondary, "⚡", 1),
    "info": ("Details", discord.ButtonStyle.secondary, "💻", 2),
    "passwd": ("Reset Password", discord.ButtonStyle.secondary, "🔑", 2),
    "delete": ("Delete", discord.ButtonStyle.danger, "🗑️", 2),
}


class VPSButton(discord.ui.DynamicItem[discord.ui.Button], template=r"vps:(?P<action>[a-z]+):(?P<name>[A-Za-z0-9_.-]+)"):
    def __init__(self, action: str, name: str):
        label, style, emoji, row = ACTIONS[action]
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji, row=row, custom_id=f"vps:{action}:{name}"))
        self.action, self.name = action, name

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match, /):
        if match["action"] not in ACTIONS:
            raise ValueError("unknown action")
        return cls(match["action"], match["name"])

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        v = storage.get_vps(self.name)
        if not v:
            await interaction.response.send_message("❌ This VPS no longer exists.", ephemeral=True)
            return False
        if str(interaction.user.id) != v["owner_id"] and not is_admin_id(interaction.user.id):
            await interaction.response.send_message("❌ Only the VPS owner can use these controls.", ephemeral=True)
            return False
        return True

    async def callback(self, interaction: discord.Interaction):
        v = storage.get_vps(self.name)
        if not v:
            return await interaction.response.send_message("❌ This VPS no longer exists.", ephemeral=True)
        node, name = v["node"], v["container_name"]
        act = self.action

        if act == "info":
            return await interaction.response.send_message(embed=vps_embed(v, show_password=True), ephemeral=True)
        if act == "delete":
            return await interaction.response.send_message(
                f"⚠️ Permanently delete **{name}** and all its data? This cannot be undone.",
                view=ConfirmDeleteView(interaction.user.id, v), ephemeral=True)

        await interaction.response.defer(ephemeral=True, thinking=True)
        if act in ("start", "stop", "restart"):
            ok, msg = await du.power(node, name, act)
            await interaction.followup.send(f"{'✅' if ok else '❌'} {act.title()}: `{msg or 'done'}`", ephemeral=True)
        elif act == "stats":
            st = await du.container_status(node, name)
            s = await du.container_stats(node, name) if st == "running" else None
            e = vps_embed(v, st)
            if s:
                e.add_field(name="📈 Live usage", value=f"CPU `{s['cpu']}` • RAM `{s['mem']}` (`{s['mem_pct']}`)\nNet `{s['net']}` • PIDs `{s['pids']}`", inline=False)
            await interaction.followup.send(embed=e, ephemeral=True)
        elif act == "console":
            st = await du.container_status(node, name)
            if st != "running":
                ok, msg = await du.power(node, name, "start")
                if not ok:
                    return await interaction.followup.send(f"❌ Container is not running and could not be started: `{msg}`", ephemeral=True)
            url = webterm.create_link(interaction.user.id, v, interaction.client.user.name)
            hint = webterm.link_hint()
            await interaction.followup.send(
                f"🖥️ **Web console for `{name}`** — [open terminal]({url})\n"
                f"Single-use link, expires in 10 minutes.\n{hint}", ephemeral=True)
        elif act == "sshx":
            ok, res = await du.get_sshx(node, name)
            await interaction.followup.send(f"⚡ **SSHX:** {res}" if ok else f"❌ SSHX failed: `{res}`", ephemeral=True)
        elif act == "passwd":
            if not ssh_exposed(v["ip"]):
                return await interaction.followup.send("🔒 SSH is not available on this (private) node — passwords aren't used. Use the Web Console or SSHX.", ephemeral=True)
            ok, res = await du.reset_password(node, name)
            if ok:
                storage.update_vps(name, root_password=res)
                await interaction.followup.send(f"🔑 New root password: ||`{res}`||", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ Failed: `{res}`", ephemeral=True)


def vps_control_view(v: Dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action in ACTIONS:
        if action == "passwd" and not ssh_exposed(v["ip"]):
            continue  # no SSH on private nodes -> nothing to reset
        view.add_item(VPSButton(action, v["container_name"]))
    return view


class ConfirmDeleteView(discord.ui.View):
    def __init__(self, user_id: int, vps: Dict):
        super().__init__(timeout=30)
        self.user_id, self.vps = user_id, vps

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    @discord.ui.button(label="Yes, delete", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ok, msg = await du.power(self.vps["node"], self.vps["container_name"], "delete")
        if ok or "No such container" in msg:
            storage.delete_vps(self.vps["container_name"])
            await interaction.edit_original_response(content=f"🗑️ Deleted `{self.vps['container_name']}`.", view=None)
        else:
            await interaction.edit_original_response(content=f"❌ Delete failed: `{msg}`", view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()


# ── deploy wizard ────────────────────────────────────────────────────────
class _OwnedView(discord.ui.View):
    def __init__(self, author_id: int, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ This menu isn't for you.", ephemeral=True)
            return False
        return True


class OSSelectView(_OwnedView):
    def __init__(self, author_id: int, cb: Callable[[discord.Interaction, str], Awaitable[None]]):
        super().__init__(author_id)
        self.cb = cb
        sel = discord.ui.Select(
            placeholder="🐧 Select an operating system…",
            options=[discord.SelectOption(label=i["label"], description=f"Image: {i['image']}", value=k, emoji="🐧")
                     for k, i in OS_IMAGES.items()])
        sel.callback = self._picked
        self.sel = sel
        self.add_item(sel)

    async def _picked(self, interaction: discord.Interaction):
        await self.cb(interaction, self.sel.values[0])


class NodeSelectView(_OwnedView):
    def __init__(self, author_id: int, cb: Callable[[discord.Interaction, str], Awaitable[None]]):
        super().__init__(author_id)
        self.cb = cb
        opts = []
        for name, cfg in list(storage.get_nodes().items())[:25]:
            opts.append(discord.SelectOption(
                label=name, value=name, emoji="🛰️",
                description=f"{'Primary' if cfg['is_local'] else 'Worker'} • {cfg['ip']}"))
        sel = discord.ui.Select(placeholder="🛰️ Select a node…", options=opts)
        sel.callback = self._picked
        self.sel = sel
        self.add_item(sel)

    async def _picked(self, interaction: discord.Interaction):
        await self.cb(interaction, self.sel.values[0])


# ── help ─────────────────────────────────────────────────────────────────
def help_pages(bot_name: str, admin: bool) -> Dict[str, discord.Embed]:
    foot = f"{bot_name} v{BOT_VERSION} • {BOT_BRAND}"

    def emb(title: str, desc: str, color: int) -> discord.Embed:
        e = discord.Embed(title=title, description=desc, color=color)
        e.set_footer(text=foot)
        return e

    pages: Dict[str, discord.Embed] = {}
    e = emb(f"❄ {bot_name} — Getting Started",
            "Deploy **Ubuntu 22.04–26.04** and **Debian 11–13** containers with SSH, a **browser terminal**, "
            "SSHX and one-click controls.\n\n"
            "**Quick start**\n"
            "1️⃣ `/daily` or ask an admin for credits\n"
            "2️⃣ `/plans` → `/buy plan:Starter`\n"
            "3️⃣ Pick OS + node → your VPS deploys in about a minute\n"
            "4️⃣ `/myvps` → press **🖥️ Web Console** (works from any browser)\n\n"
            "Every command works as a slash command (`/`) — type `/` to browse them.", PURPLE)
    pages["start"] = e

    e = emb("🖥️ VPS & Terminal", "", PURPLE)
    e.add_field(name="`/myvps`", value="Your VPS list with a persistent control panel: Start, Stop, Restart, Stats, Web Console, SSHX, Details, Reset Password (public-IP nodes), Delete. Private-IP nodes hide raw SSH and use Web Console / SSHX only.", inline=False)
    e.add_field(name="`/console [vps]`", value="Open a **web terminal** link (single-use, 10 min). Uses `docker exec`, so it works for private IPs, NAT and closed SSH ports.", inline=False)
    e.add_field(name="`/sshx [vps]`", value="Alternative shareable terminal session.", inline=False)
    e.add_field(name="`/buy <plan>`", value="Buy and deploy a VPS (pick OS and node).", inline=False)
    pages["vps"] = e

    e = emb("💎 Economy", "", GREEN)
    e.add_field(name="`/credits [user]`", value="Check a wallet.", inline=False)
    e.add_field(name="`/daily`", value="Claim free credits every 24h.", inline=False)
    e.add_field(name="`/pay <user> <amount>`", value="Send credits to someone.", inline=False)
    e.add_field(name="`/plans`", value="Show all VPS plans and prices.", inline=False)
    e.add_field(name="`/leaderboard`", value="Top credit holders.", inline=False)
    pages["eco"] = e

    e = emb("ℹ️ General", "", BLUE)
    e.add_field(name="`/ping`", value="Bot latency.", inline=False)
    e.add_field(name="`/about`", value="Version, cluster size and uptime.", inline=False)
    e.add_field(name="`/help`", value="This menu.", inline=False)
    pages["general"] = e

    if admin:
        e = emb("🛰️ Node Management (admin)", "", BLUE)
        e.add_field(name="`/node list`", value="All nodes with live CPU / RAM / disk / containers.", inline=False)
        e.add_field(name="`/node add <name> <ip> [port] [password]`", value="Pair a worker. Leave password empty if the bot key is already authorised.", inline=False)
        e.add_field(name="`/node key`", value="Show the bot's SSH public key.", inline=False)
        e.add_field(name="`/node setip <node> <ip>`", value="Change the IP shown to users (fix private/public IP).", inline=False)
        e.add_field(name="`/node rename` · `/node remove`", value="Manage nodes.", inline=False)
        pages["nodes"] = e

        e = emb("🛡️ Administrator Suite", "", GOLD)
        e.add_field(name="`/admin create <user> [ram] [cpu] [disk]`", value="Deploy a VPS for a user without charging credits.", inline=False)
        e.add_field(name="`/admin vpslist` · `/admin deletevps <name>`", value="See and remove any VPS.", inline=False)
        e.add_field(name="`/admin credits <add|remove|set> <user> <amount>`", value="Manage wallets.", inline=False)
        e.add_field(name="`/admin addadmin` · `/admin removeadmin`", value="Owner only.", inline=False)
        e.add_field(name="`/admin webterm`", value="Show web terminal / pairing configuration and diagnostics.", inline=False)
        e.add_field(name="`/admin seturl <url>`", value="Set the public console URL (e.g. your Cloudflare Tunnel). Empty = reset.", inline=False)
        e.add_field(name="`/admin sync`", value="Re-sync slash commands.", inline=False)
        pages["admin"] = e
    return pages


class HelpView(_OwnedView):
    def __init__(self, author_id: int, bot_name: str, admin: bool):
        super().__init__(author_id, timeout=300)
        self.pages = help_pages(bot_name, admin)
        labels = {"start": ("Getting Started", "🚀"), "vps": ("VPS & Terminal", "🖥️"), "eco": ("Economy", "💎"),
                  "general": ("General", "ℹ️"), "nodes": ("Nodes (admin)", "🛰️"), "admin": ("Admin", "🛡️")}
        sel = discord.ui.Select(
            placeholder="Choose a category…",
            options=[discord.SelectOption(label=labels[k][0], value=k, emoji=labels[k][1]) for k in self.pages])
        sel.callback = self._picked
        self.sel = sel
        self.add_item(sel)

    async def _picked(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=self.pages[self.sel.values[0]], view=self)
