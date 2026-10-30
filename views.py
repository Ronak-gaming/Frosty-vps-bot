import discord
from typing import Callable, Awaitable
from storage import get_nodes, delete_user_vps
from docker_utils import manage_container_power


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
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.vps = vps_info

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ Only the VPS owner can use these controls.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Start", style=discord.ButtonStyle.success, emoji="▶️")
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ok, msg = await manage_container_power(self.vps["node"], self.vps["container_name"], "start")
        await interaction.followup.send(f"{'✅ Started' if ok else '❌ Failed'}: `{msg}`", ephemeral=True)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.secondary, emoji="⏹️")
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ok, msg = await manage_container_power(self.vps["node"], self.vps["container_name"], "stop")
        await interaction.followup.send(f"{'🛑 Stopped' if ok else '❌ Failed'}: `{msg}`", ephemeral=True)

    @discord.ui.button(label="Restart", style=discord.ButtonStyle.primary, emoji="🔄")
    async def restart_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        ok, msg = await manage_container_power(self.vps["node"], self.vps["container_name"], "restart")
        await interaction.followup.send(f"{'🔄 Restarted' if ok else '❌ Failed'}: `{msg}`", ephemeral=True)

    @discord.ui.button(label="SSH / Credentials", style=discord.ButtonStyle.secondary, emoji="💻")
    async def ssh_info_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            f"**SSH Connection (`{self.vps['node']}`):**\n"
            f"```bash\nssh root@{self.vps['ip']} -p {self.vps['ssh_port']}\n```\n"
            f"**Password:** `{self.vps['root_password']}`",
            ephemeral=True
        )

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await manage_container_power(self.vps["node"], self.vps["container_name"], "rm -f")
        delete_user_vps(self.owner_id, self.vps["container_name"])
        await interaction.followup.send(f"🗑️ Deleted `{self.vps['container_name']}` from `{self.vps['node']}`.", ephemeral=True)
        self.stop()
