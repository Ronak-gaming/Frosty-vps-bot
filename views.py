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
        options = []
        for key, info in OS_IMAGES.items():
            options.append(discord.SelectOption(label=info["label"], value=key))
        super().__init__(placeholder="🐧 Select Operating System...", min_values=1, max_values=1, options=options)
        self.callback_fn = callback_fn

    async def callback(self, interaction: discord.Interaction):
        await self.callback_fn(interaction, self.values[0])


class OSSelectView(discord.ui.View):
    def __init__(self, author_id: int, callback_fn: Callable[[discord.Interaction, str], Awaitable[None]]):
        super().__init__(timeout=60)
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
        footer = f"{self.bot_name} v{BOT_VERSION} • Multi-Node Cloud Infrastructure"
        p1 = discord.Embed(
            title=f"⚡ {self.bot_name} — Help Menu (1/4: Instances & SSHX)",
            description="Manage your containers, run automated post-setup updates, and open **SSHX** browser terminals.",
            color=0x7C6AFF
        )
        p1.add_field(name="`!myvps` *(or `!vps`)*", value="Open live interactive controls (**Start**, **Stop**, **Restart**, **SSHX Link**, **Reset Pass**, **Delete**).", inline=False)
        p1.add_field(name="`!sshx`", value="Generate an instant **sshx.io** web terminal session.", inline=False)
        p1.add_field(name="`!webssh` *(or `!console`)*", value="🔔 *WebSSH browser console is **Coming Soon**! Use `!sshx` in the meantime.*", inline=False)
        p1.add_field(name="`!create [@user] [ram] [cpu] [disk]`", value="Provision a new container with automated OS selection (Ubuntu 22-26 / Debian 11-13).", inline=False)
        p1.set_footer(text=f"{footer} • Page 1/4")

        p2 = discord.Embed(
            title=f"💎 {self.bot_name} — Help Menu (2/4: Credits & Plans)",
            description="Inspect balances, transfer credits, and shop hardware tiers.",
            color=0x2ECC71
        )
        p2.add_field(name="`!credits` / `!bal` [@user]", value="Check credit wallet balance.", inline=False)
        p2.add_field(name="`!pay <@user> <amount>`", value="Transfer credits to other users.", inline=False)
        p2.add_field(name="`!plans` / `!shop`", value="Display all available VPS plans.", inline=False)
        p2.set_footer(text=f"{footer} • Page 2/4")

        p3 = discord.Embed(
            title=f"🛰️ {self.bot_name} — Help Menu (3/4: Cluster Nodes)",
            description="Manage worker nodes and cluster telemetry.",
            color=0x00A8FF
        )
        p3.add_field(name="`!nodes`", value="View live CPU, RAM, and container metrics for all paired nodes.", inline=False)
        p3.add_field(name="`!node-add <Name> <IP> <Port> [Pass|KEY_ONLY]`", value="Pair a remote worker node.", inline=False)
        p3.set_footer(text=f"{footer} • Page 3/4")

        p4 = discord.Embed(
            title=f"🛡️ {self.bot_name} — Help Menu (4/4: Administration)",
            description="Staff tools and credit minting.",
            color=0xF0B34B
        )
        p4.add_field(name="`!create`", value="Provision containers as an admin.", inline=False)
        p4.add_field(name="`!vps-list` / `!allvps`", value="Audit all active containers cluster-wide.", inline=False)
        p4.add_field(name="`!addcredits` / `!removecredits`", value="Manage user wallet balances.", inline=False)
        p4.set_footer(text=f"{footer} • Page 4/4")

        return [p1, p2, p3, p4]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Not your menu.", ephemeral=True)
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
            options.append(discord.SelectOption(label=name, description=f"{kind} • {cfg.get('ip')}", value=name))
        super().__init__(placeholder="❄ Select Node...", min_values=1, max_values=1, options=options)
        self.callback_fn = callback_fn

    async def callback(self, interaction: discord.Interaction):
        await self.callback_fn(interaction, self.values[0])


class NodeSelectView(discord.ui.View):
    def __init__(self, author_id: int, callback_fn: Callable[[discord.Interaction, str], Awaitable[None]]):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.add_item(NodeSelectDropdown(callback_fn))


class VPSControlView(discord.ui.View):
    def __init__(self, owner_id: int, vps_info: dict):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.vps = vps_info

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ Owner only.", ephemeral=True)
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

    @discord.ui.button(label="WebSSH (Coming Soon)", style=discord.ButtonStyle.secondary, emoji="⏳", row=1, disabled=True)
    async def webssh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(label="SSHX Link", style=discord.ButtonStyle.success, emoji="⚡", row=1)
    async def sshx_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ok, url = await get_sshx_session(self.vps["node"], self.vps["container_name"])
        if ok:
            embed = discord.Embed(title=f"⚡ SSHX Terminal — {self.vps['container_name']}", description=f"**🔗 Click to Open:**\n{url}", color=0x2ECC71)
            try:
                await interaction.user.send(embed=embed)
            except Exception:
                pass
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(f"❌ SSHX failed: `{url}`", ephemeral=True)

    @discord.ui.button(label="SSH Info", style=discord.ButtonStyle.secondary, emoji="💻", row=2)
    async def ssh_info_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"**SSH Connection (`{self.vps['node']}`):**\n"
            f"```bash\nssh root@{self.vps['ip']} -p {self.vps['ssh_port']}\n```\n"
            f"**Password:** `{self.vps['root_password']}`\n"
            f"**OS:** `{self.vps.get('os_label', 'Ubuntu')}`",
            ephemeral=True
        )

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️", row=2)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await manage_container_power(self.vps["node"], self.vps["container_name"], "rm -f")
        delete_user_vps(self.owner_id, self.vps["container_name"])
        await interaction.followup.send(f"🗑️ Deleted `{self.vps['container_name']}`.", ephemeral=True)
        self.stop()
