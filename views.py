import discord
from typing import Callable, Awaitable, List
from config import BOT_VERSION, OS_IMAGES
from storage import get_nodes, delete_user_vps, update_user_vps
from docker_utils import (
    manage_container_power,
    get_tmate_session,
    get_sshx_session,
    reset_container_password
)


class OSSelectDropdown(discord.ui.Select):
    def __init__(self, callback_fn: Callable[[discord.Interaction, str], Awaitable[None]]):
        options = [
            discord.SelectOption(label=info["label"], description=f"Docker Image: {info['image']}", value=key, emoji="🐧")
            for key, info in OS_IMAGES.items()
        ]
        super().__init__(placeholder="🐧 Select Operating System (Ubuntu 22-26 / Debian 11-13)...", min_values=1, max_values=1, options=options)
        self.callback_fn = callback_fn

    async def callback(self, interaction: discord.Interaction):
        await self.callback_fn(interaction, self.values[0])


class OSSelectView(discord.ui.View):
    def __init__(self, author_id: int, callback_fn: Callable[[discord.Interaction, str], Awaitable[None]]):
        super().__init__(timeout=90)
        self.author_id = author_id
        self.add_item(OSSelectDropdown(callback_fn))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ This menu isn't for you.", ephemeral=True)
            return False
        return True


class HelpPaginatorView(discord.ui.View):
    def __init__(self, author_id: int, bot_name: str):
        super().__init__(timeout=240)
        self.author_id = author_id
        self.bot_name = bot_name
        self.current_page = 0
        self.pages = self._build_pages()

    def _build_pages(self) -> List[discord.Embed]:
        footer = f"{self.bot_name} v{BOT_VERSION} • Multi-Node SSHX Cloud Manager"

        p1 = discord.Embed(
            title=f"⚡ {self.bot_name} — Command Center (1/4)",
            description=(
                f"> **Welcome to {self.bot_name}!** Deploy Ubuntu (22.04–26.04) & Debian (11–13) containers "
                f"with automatic post-setup updates, system fetch, and instant **SSHX** web terminals.\n\n"
                f"### 🖥️ Instance & Terminal Commands"
            ),
            color=0x7C6AFF
        )
        p1.add_field(name="🔹 `!myvps` *(or `!vps`)*", value="Interactive control panel (**Start**, **Stop**, **Restart**, **⚡ SSHX Link**, **🌐 Tmate**, **🔑 Reset Pass**, **🗑️ Delete**).", inline=False)
        p1.add_field(name="🔹 `!sshx`", value="Generate an instant, encrypted **sshx.io** collaborative browser terminal link for your VPS.", inline=False)
        p1.add_field(name="🔹 `!webssh`", value="⏳ *Browser WebSSH Console is **Coming Soon**! Use `!sshx` for instant web terminal access.*", inline=False)
        p1.add_field(name="🔹 `!plans` & `!buy <Plan>`", value="View VPS plans and deploy your chosen OS using credits.", inline=False)
        p1.set_footer(text=f"{footer} • Page 1 of 4")

        p2 = discord.Embed(
            title=f"💎 {self.bot_name} — Economy & Storefront (2/4)",
            description="> **Instant Cloud Provisioning:** Check your wallet, send credits, and view hardware tiers.\n\n### 💰 Economy Commands",
            color=0x2ECC71
        )
        p2.add_field(name="🔹 `!credits [@user]` *(or `!bal`)*", value="Check your current credit wallet balance.", inline=False)
        p2.add_field(name="🔹 `!pay <@user> <amount>`", value="Transfer credits to another Discord member.", inline=False)
        p2.add_field(name="🔹 `!plans` *(or `!shop`)*", value="View `Starter`, `Basic`, `Pro`, and `Ultra` VPS specifications.", inline=False)
        p2.add_field(name="🔹 `!ping`", value="Check bot latency and node responsiveness.", inline=False)
        p2.set_footer(text=f"{footer} • Page 2 of 4")

        p3 = discord.Embed(
            title=f"🛰️ {self.bot_name} — Multi-Node Cluster (3/4)",
            description="> **Distributed Node Management:** Name, rename, or pair nodes across your infrastructure.\n\n### 🌐 Node Commands",
            color=0x00A8FF
        )
        p3.add_field(name="🔹 `!nodes`", value="View all paired nodes (`cuba32`, `Node-1`, etc.), live CPU/RAM usage, and running containers.", inline=False)
        p3.add_field(name="🔹 `!node-rename <OldName> <NewName>`", value="Rename any node (including your primary node) and auto-update all VPS containers.", inline=False)
        p3.add_field(name="🔹 `!node-add <Name> <IP> <Port> [Pass|KEY_ONLY]`", value="Pair a remote worker VPS node.", inline=False)
        p3.add_field(name="🔹 `!node-remove <Name>` & `!node-key`", value="Remove a remote node or display the bot's SSH public key.", inline=False)
        p3.set_footer(text=f"{footer} • Page 3 of 4")

        p4 = discord.Embed(
            title=f"🛡️ {self.bot_name} — Administrator Suite (4/4)",
            description="> **Staff Provisioning:** Create custom VPS instances with automatic `apt update/upgrade`, `neofetch`, and `sshx`.\n\n### ⚙️ Admin Commands",
            color=0xF0B34B
        )
        p4.add_field(name="🔹 `!create <@user> [ram_gb] [cores] [disk_gb]`", value="Pick OS (Ubuntu 22–26 / Debian 11–13) + Node, auto-run updates & neofetch, and DM the SSHX link.", inline=False)
        p4.add_field(name="🔹 `!vps-list` *(or `!allvps`)*", value="List all deployed VPS containers across all users and nodes.", inline=False)
        p4.add_field(name="🔹 `!addcredits <@user> <amount>` / `!removecredits`", value="Add or remove credits from a user's wallet.", inline=False)
        p4.add_field(name="🔹 `!admin-add <@user>` / `!admin-remove <@user>`", value="Grant or revoke bot administrator permissions.", inline=False)
        p4.set_footer(text=f"{footer} • Page 4 of 4")

        return [p1, p2, p3, p4]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Run `!help` yourself to use these buttons.", ephemeral=True)
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
            kind = "Primary Host" if cfg.get("is_local") else "Worker Node"
            options.append(
                discord.SelectOption(
                    label=name,
                    description=f"{kind} • {cfg.get('ip')}:{cfg.get('ssh_port', 22)}",
                    value=name,
                    emoji="🛰️"
                )
            )
        super().__init__(placeholder="❄ Select a Node to deploy on...", min_values=1, max_values=1, options=options)
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
        ok, msg = await manage_container_power(self.vps.get("node", "Node-1"), self.vps["container_name"], "start")
        await interaction.followup.send(f"{'✅ Started' if ok else '❌ Failed'}: `{msg}`", ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.secondary, emoji="⏹️", row=0)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ok, msg = await manage_container_power(self.vps.get("node", "Node-1"), self.vps["container_name"], "stop")
        await interaction.followup.send(f"{'🛑 Stopped' if ok else '❌ Failed'}: `{msg}`", ephemeral=True)

    @discord.ui.button(label="Restart", style=discord.ButtonStyle.primary, emoji="🔄", row=0)
    async def restart_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ok, msg = await manage_container_power(self.vps.get("node", "Node-1"), self.vps["container_name"], "restart")
        await interaction.followup.send(f"{'🔄 Restarted' if ok else '❌ Failed'}: `{msg}`", ephemeral=True)

    @discord.ui.button(label="SSHX Link", style=discord.ButtonStyle.success, emoji="⚡", row=1)
    async def sshx_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ok, url = await get_sshx_session(self.vps.get("node", "Node-1"), self.vps["container_name"])
        if ok:
            embed = discord.Embed(
                title=f"⚡ SSHX Live Web Terminal — {self.vps['container_name']}",
                description=(
                    f"**🔗 Click to Open SSHX Terminal:**\n{url}\n\n"
                    f"**🛰️ Node:** `{self.vps.get('node', 'Node-1')}`\n"
                    f"**🐧 OS:** `{self.vps.get('os_label', 'Ubuntu')}`"
                ),
                color=0x2ECC71
            )
            try:
                await interaction.user.send(embed=embed)
            except Exception:
                pass
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(f"❌ SSHX failed: `{url}`", ephemeral=True)

    @discord.ui.button(label="WebSSH (Coming Soon)", style=discord.ButtonStyle.secondary, emoji="⏳", row=1, disabled=True)
    async def webssh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("⏳ WebSSH is Coming Soon! Use **⚡ SSHX Link** for instant browser terminal access.", ephemeral=True)

    @discord.ui.button(label="Tmate", style=discord.ButtonStyle.secondary, emoji="🌐", row=1)
    async def tmate_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ok, session = await get_tmate_session(self.vps.get("node", "Node-1"), self.vps["container_name"])
        if ok:
            await interaction.followup.send(f"🌐 **Tmate SSH Session:**\n```bash\n{session}\n```", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Tmate failed: `{session}`", ephemeral=True)

    @discord.ui.button(label="SSH Info", style=discord.ButtonStyle.secondary, emoji="💻", row=2)
    async def ssh_info_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"**SSH Connection (`{self.vps.get('node', 'Node-1')}`):**\n"
            f"```bash\nssh root@{self.vps['ip']} -p {self.vps['ssh_port']}\n```\n"
            f"**Root Password:** `{self.vps['root_password']}`\n"
            f"**OS:** `{self.vps.get('os_label', 'Ubuntu')}`",
            ephemeral=True
        )

    @discord.ui.button(label="Reset Password", style=discord.ButtonStyle.secondary, emoji="🔑", row=2)
    async def pass_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ok, new_pass = await reset_container_password(self.vps.get("node", "Node-1"), self.vps["container_name"])
        if ok:
            self.vps["root_password"] = new_pass
            update_user_vps(self.owner_id, self.vps["container_name"], {"root_password": new_pass})
            await interaction.followup.send(f"🔑 **New Root Password:** `{new_pass}`", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Failed: `{new_pass}`", ephemeral=True)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️", row=2)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await manage_container_power(self.vps.get("node", "Node-1"), self.vps["container_name"], "rm -f")
        delete_user_vps(self.owner_id, self.vps["container_name"])
        await interaction.followup.send(f"🗑️ Deleted `{self.vps['container_name']}`.", ephemeral=True)
        self.stop()
