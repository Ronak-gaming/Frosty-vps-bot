import discord
from datetime import datetime
from typing import Callable, Awaitable, List
from storage import get_nodes, delete_user_vps, update_user_vps
from docker_utils import manage_container_power, get_tmate_session, reset_container_password


class HelpPaginatorView(discord.ui.View):
    def __init__(self, author_id: int, bot_name: str):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.bot_name = bot_name
        self.current_page = 0
        self.pages = self._build_pages()

    def _build_pages(self) -> List[discord.Embed]:
        footer = f"{self.bot_name} | Multi-Node VPS Manager"

        p1 = discord.Embed(
            title=f"❄️ {self.bot_name} — Help Menu (Page 1/4: 🖥️ User VPS Commands)",
            description="Manage your personal VPS containers, check resources, and connect via SSH or Tmate.",
            color=0x00A8FF
        )
        p1.add_field(name="`!myvps` / `!vps`", value="Open interactive control panel (Start/Stop/Restart/Tmate/Password/Delete) for your VPS.", inline=False)
        p1.add_field(name="`!plans` / `!shop`", value="View available VPS plans and credit prices.", inline=False)
        p1.add_field(name="`!buy <PlanName>`", value="Purchase and auto-deploy a VPS on your chosen node using credits.", inline=False)
        p1.add_field(name="`!ping`", value="Check bot latency and node responsiveness.", inline=False)
        p1.set_footer(text=f"{footer} • Page 1/4")

        p2 = discord.Embed(
            title=f"❄️ {self.bot_name} — Help Menu (Page 2/4: 💰 Credits & Invites)",
            description="Earn or check credits and purchase instant multi-node VPS hosting.",
            color=0x00FF88
        )
        p2.add_field(name="`!credits` / `!bal` [@user]", value="Check your current credit balance.", inline=False)
        p2.add_field(name="`!pay <@user> <amount>`", value="Transfer credits to another user.", inline=False)
        p2.add_field(name="`!invites` [@user]", value="Check your tracked server invites and earned bonus credits.", inline=False)
        p2.set_footer(text=f"{footer} • Page 2/4")

        p3 = discord.Embed(
            title=f"❄️ {self.bot_name} — Help Menu (Page 3/4: 🛰️ Multi-Node Management)",
            description="Connect, monitor, rename, or remove remote VPS worker nodes across the globe.",
            color=0x9B59B6
        )
        p3.add_field(name="`!nodes`", value="View all paired nodes (`Node-1`, `Tcodes`, `Node-2`, etc.), live CPU/RAM, and container counts.", inline=False)
        p3.add_field(name="`!node-add <Name> <IP> <Port> [Pass|KEY_ONLY]`", value="Pair a new remote worker VPS node.", inline=False)
        p3.add_field(name="`!node-rename <OldName> <NewName>`", value="Rename an existing node and migrate all VPS records.", inline=False)
        p3.add_field(name="`!node-remove <Name>`", value="Unpair and remove a remote worker node.", inline=False)
        p3.add_field(name="`!node-key`", value="View the Primary Bot's SSH public key for manual pairing.", inline=False)
        p3.set_footer(text=f"{footer} • Page 3/4")

        p4 = discord.Embed(
            title=f"❄️ {self.bot_name} — Help Menu (Page 4/4: 🛡️ Admin & Staff Commands)",
            description="Administrator tools for deploying custom containers, managing user credits, and staff access.",
            color=0xFFAA00
        )
        p4.add_field(name="`!deploy <@user> [ram_gb] [cores] [disk_gb]`", value="Deploy a custom VPS on any node via interactive dropdown.", inline=False)
        p4.add_field(name="`!vps-list` / `!allvps`", value="List all active VPS containers across all users and nodes.", inline=False)
        p4.add_field(name="`!addcredits <@user> <amount>`", value="Give credits to a user.", inline=False)
        p4.add_field(name="`!removecredits <@user> <amount>`", value="Remove credits from a user.", inline=False)
        p4.add_field(name="`!admin-add <@user>` / `!admin-remove <@user>`", value="Grant or revoke bot administrator permissions.", inline=False)
        p4.set_footer(text=f"{footer} • Page 4/4")

        return [p1, p2, p3, p4]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Run `!help` yourself to use the page buttons.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = (self.current_page - 1) % len(self.pages)
        await interaction.response.edit_message(embed=self.pages[self.current_page], view=self)

    @discord.ui.button(label="🖥️ VPS", style=discord.ButtonStyle.primary)
    async def page1_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = 0
        await interaction.response.edit_message(embed=self.pages[0], view=self)

    @discord.ui.button(label="💰 Credits", style=discord.ButtonStyle.success)
    async def page2_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = 1
        await interaction.response.edit_message(embed=self.pages[1], view=self)

    @discord.ui.button(label="🛰️ Nodes", style=discord.ButtonStyle.primary)
    async def page3_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = 2
        await interaction.response.edit_message(embed=self.pages[2], view=self)

    @discord.ui.button(label="🛡️ Admin", style=discord.ButtonStyle.danger)
    async def page4_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = 3
        await interaction.response.edit_message(embed=self.pages[3], view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
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
                    value=name
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
            await interaction.response.send_message("❌ This menu isn't for you.", ephemeral=True)
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

    @discord.ui.button(label="SSH Details", style=discord.ButtonStyle.secondary, emoji="💻", row=1)
    async def ssh_info_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"**SSH Connection (`{self.vps['node']}`):**\n"
            f"```bash\nssh root@{self.vps['ip']} -p {self.vps['ssh_port']}\n```\n"
            f"**Password:** `{self.vps['root_password']}`",
            ephemeral=True
        )

    @discord.ui.button(label="Tmate Web SSH", style=discord.ButtonStyle.primary, emoji="🌐", row=1)
    async def tmate_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ok, session = await get_tmate_session(self.vps["node"], self.vps["container_name"])
        if ok:
            await interaction.followup.send(f"🌐 **Instant Tmate SSH Session:**\n```bash\n{session}\n```", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Tmate failed: `{session}`", ephemeral=True)

    @discord.ui.button(label="Reset Password", style=discord.ButtonStyle.secondary, emoji="🔑", row=1)
    async def pass_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ok, new_pass = await reset_container_password(self.vps["node"], self.vps["container_name"])
        if ok:
            self.vps["root_password"] = new_pass
            update_user_vps(self.owner_id, self.vps["container_name"], {"root_password": new_pass})
            await interaction.followup.send(f"🔑 **New Root Password:** `{new_pass}`", ephemeral=True)
        else:
            await interaction.followup.send(f"❌ Failed: `{new_pass}`", ephemeral=True)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️", row=1)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await manage_container_power(self.vps["node"], self.vps["container_name"], "rm -f")
        delete_user_vps(self.owner_id, self.vps["container_name"])
        await interaction.followup.send(f"🗑️ Deleted `{self.vps['container_name']}` from `{self.vps['node']}`.", ephemeral=True)
        self.stop()
