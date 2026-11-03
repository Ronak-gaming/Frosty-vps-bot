import discord
from datetime import datetime
from typing import Callable, Awaitable, List
from config import BOT_VERSION, WEBSSH_BASE_URL
from storage import get_nodes, delete_user_vps, update_user_vps
from docker_utils import (
    manage_container_power,
    get_tmate_session,
    get_sshx_session,
    reset_container_password
)
from webssh_server import create_webssh_link, get_public_webssh_base


class HelpPaginatorView(discord.ui.View):
    def __init__(self, author_id: int, bot_name: str, avatar_url: str = None):
        super().__init__(timeout=240)
        self.author_id = author_id
        self.bot_name = bot_name
        self.avatar_url = avatar_url
        self.current_page = 0
        self.pages = self._build_pages()

    def _build_pages(self) -> List[discord.Embed]:
        webssh_domain = get_public_webssh_base()
        footer = f"{self.bot_name} v{BOT_VERSION} • Cloud & Private IPv4 Multi-Node Infrastructure"

        # Page 1: Instance & Console Management
        p1 = discord.Embed(
            title=f"⚡ {self.bot_name} — Command Center",
            description=(
                f"> **Welcome to {self.bot_name}!** Manage your high-performance containers with instant "
                f"**Cloudflare WebSSH** (`{webssh_domain}`) and **SSHX** browser terminals.\n\n"
                f"### 🖥️ Instance & Console Commands"
            ),
            color=0x7C6AFF
        )
        p1.add_field(
            name="🔹 `!myvps` *(or `!vps`)*",
            value="Open the live interactive dashboard for your instances (**Start**, **Stop**, **Restart**, **WebSSH**, **SSHX**, **Reset Pass**, **Delete**).",
            inline=False
        )
        p1.add_field(
            name="🔹 `!webssh` *(or `!console`)*",
            value=f"Generate a PIN-protected **Live Browser WebSSH** session via `{webssh_domain}` (Works on Private IPv4 & NAT).",
            inline=False
        )
        p1.add_field(
            name="🔹 `!sshx`",
            value="Spawn an instant, encrypted **sshx.io** collaborative browser terminal link for your VPS.",
            inline=False
        )
        p1.add_field(
            name="🔹 `!plans` & `!buy <Plan>`",
            value="Browse available hardware tiers and deploy an instance automatically using your credits.",
            inline=False
        )
        p1.set_footer(text=f"{footer} • Page 1 of 4")

        # Page 2: Billing & Credits Economy
        p2 = discord.Embed(
            title=f"💎 {self.bot_name} — Credits & Storefront",
            description=(
                "> **Instant Cloud Provisioning:** Check your balance, transfer credits to teammates, "
                "and upgrade your container specifications.\n\n"
                "### 💰 Economy Commands"
            ),
            color=0x2ECC71
        )
        p2.add_field(
            name="🔹 `!credits [@user]` *(or `!bal`)*",
            value="Inspect your current credit wallet or view another member's balance.",
            inline=False
        )
        p2.add_field(
            name="🔹 `!pay <@user> <amount>`",
            value="Securely transfer credits from your wallet to another Discord member.",
            inline=False
        )
        p2.add_field(
            name="🔹 `!plans` *(or `!shop`)*",
            value="Display all VPS packages (`Starter`, `Basic`, `Pro`, `Ultra`) with RAM, vCPU, and Disk allocations.",
            inline=False
        )
        p2.add_field(
            name="🔹 `!ping`",
            value="Check gateway latency, WebSSH tunnel status, and cluster responsiveness.",
            inline=False
        )
        p2.set_footer(text=f"{footer} • Page 2 of 4")

        # Page 3: Multi-Node Cluster Management
        p3 = discord.Embed(
            title=f"🛰️ {self.bot_name} — Multi-Node Cluster",
            description=(
                "> **Distributed Cluster Control:** Pair unlimited Public or Private IPv4 worker nodes "
                "and monitor real-time CPU, RAM, and container telemetry.\n\n"
                "### 🌐 Cluster & Node Commands"
            ),
            color=0x00A8FF
        )
        p3.add_field(
            name="🔹 `!nodes`",
            value="Live health check of all paired nodes (`Node-1`, `Node-2`, etc.) with CPU cores, memory load, and active containers.",
            inline=False
        )
        p3.add_field(
            name="🔹 `!node-add <Name> <IP> <Port> [Pass|KEY_ONLY]`",
            value="Pair a remote Public or Private IPv4 worker node directly to the cluster.",
            inline=False
        )
        p3.add_field(
            name="🔹 `!node-rename <OldName> <NewName>`",
            value="Rename a registered node and automatically migrate all linked container records.",
            inline=False
        )
        p3.add_field(
            name="🔹 `!node-remove <Name>` & `!node-key`",
            value="Detach a worker node or display the master Ed25519 SSH public key.",
            inline=False
        )
        p3.set_footer(text=f"{footer} • Page 3 of 4")

        # Page 4: Administrator & Staff Controls
        p4 = discord.Embed(
            title=f"🛡️ {self.bot_name} — Administrator Suite",
            description=(
                "> **Staff & Root Operations:** Provision custom containers on any node, audit active VPS instances, "
                "and manage staff privileges.\n\n"
                "### ⚙️ Provisioning & Admin Commands"
            ),
            color=0xF0B34B
        )
        p4.add_field(
            name="🔹 `!create <@user> [ram_gb] [cores] [disk_gb]`",
            value="Provision a custom VPS container on any node via interactive dropdown (Auto-sends WebSSH + SSHX access in DM).",
            inline=False
        )
        p4.add_field(
            name="🔹 `!vps-list` *(or `!allvps`)*",
            value="Audit all running containers across every user and node in the cluster.",
            inline=False
        )
        p4.add_field(
            name="🔹 `!addcredits <@user> <amount>` / `!removecredits <@user> <amount>`",
            value="Mint or deduct credits from any user's account balance.",
            inline=False
        )
        p4.add_field(
            name="🔹 `!admin-add <@user>` / `!admin-remove <@user>`",
            value="Grant or revoke bot administrator permissions (Main Owner only).",
            inline=False
        )
        p4.set_footer(text=f"{footer} • Page 4 of 4")

        return [p1, p2, p3, p4]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Run `!help` yourself to navigate this menu.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=0)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = (self.current_page - 1) % len(self.pages)
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)

    @discord.ui.button(label="🖥️ Instances", style=discord.ButtonStyle.primary, row=0)
    async def page1_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = 0
        await interaction.response.edit_message(embed=self.pages[0], view=self)

    @discord.ui.button(label="💎 Economy", style=discord.ButtonStyle.success, row=0)
    async def page2_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = 1
        await interaction.response.edit_message(embed=self.pages[1], view=self)

    @discord.ui.button(label="🛰️ Nodes", style=discord.ButtonStyle.primary, row=0)
    async def page3_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = 2
        await interaction.response.edit_message(embed=self.pages[2], view=self)

    @discord.ui.button(label="🛡️ Admin", style=discord.ButtonStyle.danger, row=0)
    async def page4_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = 3
        await interaction.response.edit_message(embed=self.pages[3], view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = (self.current_page + 1) % len(self.pages)
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)


class NodeSelectDropdown(discord.ui.Select):
    def __init__(self, callback_fn: Callable[[discord.Interaction, str], Awaitable[None]]):
        nodes = get_nodes()
        options = []
        for name, cfg in list(nodes.items())[:25]:
            kind = "Primary Host (Private/Local)" if cfg.get("is_local") else "Worker Node"
            options.append(
                discord.SelectOption(
                    label=name,
                    description=f"{kind} • {cfg.get('ip')}:{cfg.get('ssh_port', 22)}",
                    value=name
                )
            )
        super().__init__(placeholder="❄ Select a Node to provision this VPS on...", min_values=1, max_values=1, options=options)
        self.callback_fn = callback_fn

    async def callback(self, interaction: discord.Interaction):
        await self.callback_fn(interaction, self.values[0])


class NodeSelectView(discord.ui.View):
    def __init__(self, author_id: int, callback_fn: Callable[[discord.Interaction, str], Awaitable[None]]):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.add_item(NodeSelectDropdown(callback_fn))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ This dropdown isn't for you.", ephemeral=True)
            return False
        return True


class VPSControlView(discord.ui.View):
    def __init__(self, owner_id: int, vps_info: dict):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.vps = vps_info

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ Only the VPS owner can use these controls.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Start", style=discord.ButtonStyle.success, emoji="▶️", row=0)
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ok, msg = await manage_container_power(self.vps["node"], self.vps["container_name"], "start")
        await interaction.followup.send(f"{'✅ Started' if ok else '❌ Failed'}: `{msg}`", ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.secondary, emoji="⏹️", row=0)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ok, msg = await manage_container_power(self.vps["node"], self.vps["container_name"], "stop")
        await interaction.followup.send(f"{'🛑 Stopped' if ok else '❌ Failed'}: `{msg}`", ephemeral=True)

    @discord.ui.button(label="Restart", style=discord.ButtonStyle.primary, emoji="🔄", row=0)
    async def restart_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ok, msg = await manage_container_power(self.vps["node"], self.vps["container_name"], "restart")
        await interaction.followup.send(f"{'🔄 Restarted' if ok else '❌ Failed'}: `{msg}`", ephemeral=True)

    @discord.ui.button(label="WebSSH Console", style=discord.ButtonStyle.primary, emoji="🖥️", row=1)
    async def webssh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot_name = interaction.client.user.name if interaction.client.user else "ApexHosting"
        session = create_webssh_link(bot_name, self.vps, require_pin=True)
        embed = discord.Embed(
            title=f"🖥️ Cloudflare WebSSH Console — {self.vps['container_name']}",
            description=(
                f"**🔗 Launch Live Console:**\n{session['url']}\n\n"
                f"**🛡️ Security Unlock PIN:** `{session['pin']}`\n"
                f"**🛰️ Node:** `{self.vps.get('node', 'Node-1')}` *(Private IPv4 Tunnel Active)*"
            ),
            color=0x7C6AFF
        )
        try:
            await interaction.user.send(embed=embed)
        except Exception:
            pass
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="SSHX Link", style=discord.ButtonStyle.success, emoji="⚡", row=1)
    async def sshx_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ok, url = await get_sshx_session(self.vps["node"], self.vps["container_name"])
        if ok:
            embed = discord.Embed(
                title=f"⚡ SSHX Live Web Terminal — {self.vps['container_name']}",
                description=f"**🔗 Click to Open SSHX Terminal:**\n{url}\n\n*Works instantly across Private IPv4 & NAT!*",
                color=0x2ECC71
            )
            try:
                await interaction.user.send(embed=embed)
            except Exception:
                pass
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(f"❌ SSHX failed: `{url}`", ephemeral=True)

    @discord.ui.button(label="Tmate", style=discord.ButtonStyle.secondary, emoji="🌐", row=1)
    async def tmate_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ok, session = await get_tmate_session(self.vps["node"], self.vps["container_name"])
        if ok:
            await interaction.followup.send(f"🌐 **Tmate SSH Session:**\n```bash\n{session}\n```", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Tmate failed: `{session}`", ephemeral=True)

    @discord.ui.button(label="SSH Info", style=discord.ButtonStyle.secondary, emoji="💻", row=2)
    async def ssh_info_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"**Internal / Node SSH (`{self.vps['node']}`):**\n"
            f"```bash\nssh root@{self.vps['ip']} -p {self.vps['ssh_port']}\n```\n"
            f"**Root Password:** `{self.vps['root_password']}`\n"
            f"*Tip: Since this node uses Private IPv4, use **🖥️ WebSSH Console** or **⚡ SSHX Link** for direct browser access!*",
            ephemeral=True
        )

    @discord.ui.button(label="Reset Password", style=discord.ButtonStyle.secondary, emoji="🔑", row=2)
    async def pass_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ok, new_pass = await reset_container_password(self.vps["node"], self.vps["container_name"])
        if ok:
            self.vps["root_password"] = new_pass
            update_user_vps(self.owner_id, self.vps["container_name"], {"root_password": new_pass})
            await interaction.followup.send(f"🔑 **New Root Password:** `{new_pass}`", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Failed: `{new_pass}`", ephemeral=True)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️", row=2)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await manage_container_power(self.vps["node"], self.vps["container_name"], "rm -f")
        delete_user_vps(self.owner_id, self.vps["container_name"])
        await interaction.followup.send(f"🗑️ Deleted `{self.vps['container_name']}` from `{self.vps['node']}`.", ephemeral=True)
        self.stop()
