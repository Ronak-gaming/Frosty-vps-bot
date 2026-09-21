"""Discord UI views for VPS management (buttons + dropdown)."""
from datetime import datetime

import discord

from storage import vps_data, save_data, generate_password, base_embed, success_embed, error_embed, info_embed, warning_embed
from docker_utils import run_docker, docker_exec, create_container, get_tmate_session

# ─── Management view (buttons + dropdown) ──────────────────────────────────

class ManageView(discord.ui.View):
    def __init__(self, actor_id, owner_id, vps_list, *, is_shared=False, is_admin_view=False, selected=0):
        super().__init__(timeout=300)
        self.actor_id = str(actor_id)
        self.owner_id = str(owner_id)
        self.vps_list = vps_list
        self.is_shared = is_shared
        self.is_admin_view = is_admin_view
        self.selected = selected if 0 <= selected < len(vps_list) else 0
        self._build_items()

    def _build_items(self):
        self.clear_items()
        if len(self.vps_list) > 1:
            options = [
                discord.SelectOption(
                    label=f"VPS {i + 1} ({v.get('plan', 'Custom')})",
                    description=f"Status: {v.get('status', 'unknown')}",
                    value=str(i),
                    default=(i == self.selected),
                )
                for i, v in enumerate(self.vps_list)
            ]
            select = discord.ui.Select(placeholder="Select a VPS to manage", options=options)
            select.callback = self._on_select
            self.add_item(select)
        self._add_action_buttons()

    def _add_action_buttons(self):
        if not self.is_shared and not self.is_admin_view:
            btn = discord.ui.Button(label="🔄 Reinstall", style=discord.ButtonStyle.danger)
            btn.callback = lambda i: self._action(i, "reinstall")
            self.add_item(btn)

        start = discord.ui.Button(label="▶ Start", style=discord.ButtonStyle.success)
        start.callback = lambda i: self._action(i, "start")
        stop = discord.ui.Button(label="⏸ Stop", style=discord.ButtonStyle.secondary)
        stop.callback = lambda i: self._action(i, "stop")
        ssh = discord.ui.Button(label="🔑 SSH", style=discord.ButtonStyle.primary)
        ssh.callback = lambda i: self._action(i, "ssh")
        self.add_item(start)
        self.add_item(stop)
        self.add_item(ssh)

    def embed(self):
        vps = self.vps_list[self.selected]
        color = 0x00FF88 if vps.get("status") == "running" else 0xFF3366
        owner_text = ""
        if self.is_admin_view and self.owner_id != self.actor_id:
            owner_text = f"\n**Owner ID:** {self.owner_id}"

        e = base_embed(
            f"VPS Management — VPS {self.selected + 1}",
            f"Managing container: `{vps['container_name']}`{owner_text}",
            color,
        )

        expires = vps.get("expires", "Never")
        if expires and expires != "Never":
            try:
                days_left = (datetime.fromisoformat(expires) - datetime.utcnow()).days
                expire_str = f"{expires[:10]} ({days_left}d left)" if days_left >= 0 else f"{expires[:10]} (**EXPIRED**)"
            except Exception:
                expire_str = expires
        else:
            expire_str = "Never"

        e.add_field(
            name="📊 Resources",
            value=(
                f"**Plan:** {vps.get('plan', 'Custom')}\n"
                f"**Status:** `{vps.get('status', 'unknown').upper()}`\n"
                f"**RAM:** {vps.get('ram', '?')}\n"
                f"**CPU:** {vps.get('cpu', '?')} Core(s)\n"
                f"**Storage:** {vps.get('storage', '?')} (best-effort, not a hard quota)\n"
                f"**Created:** {vps.get('created_at', '?')[:10]}\n"
                f"**Expires:** {expire_str}"
            ),
            inline=False,
        )
        e.add_field(name="🎮 Controls", value="Use the buttons below to manage your VPS", inline=False)
        return e

    async def _authorized(self, interaction) -> bool:
        if str(interaction.user.id) != self.actor_id:
            await interaction.response.send_message(embed=error_embed("Access Denied", "This is not your VPS!"), ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        if not await self._authorized(interaction):
            return
        self.selected = int(interaction.data["values"][0])
        self._build_items()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _action(self, interaction: discord.Interaction, action: str):
        if not await self._authorized(interaction):
            return

        vps = vps_data[self.owner_id][self.selected]
        container = vps["container_name"]

        if action == "reinstall":
            if self.is_shared or self.is_admin_view:
                await interaction.response.send_message(embed=error_embed("Access Denied", "Only the VPS owner can reinstall."), ephemeral=True)
                return
            await interaction.response.send_message(
                embed=warning_embed("Reinstall Warning", f"⚠️ This will **erase all data** on `{container}` and reinstall Ubuntu 22.04. Continue?"),
                view=ReinstallConfirmView(self, vps),
                ephemeral=True,
            )
            return

        if action == "start":
            await interaction.response.defer(ephemeral=True)
            try:
                await run_docker(f"docker start {container}")
                await docker_exec(container, "/usr/sbin/sshd || true", timeout=10)
                vps["status"] = "running"
                save_data()
                await interaction.followup.send(embed=success_embed("VPS Started", f"`{container}` is now running."), ephemeral=True)
                await interaction.message.edit(embed=self.embed(), view=self)
            except Exception as e:
                await interaction.followup.send(embed=error_embed("Start Failed", str(e)), ephemeral=True)

        elif action == "stop":
            await interaction.response.defer(ephemeral=True)
            try:
                await run_docker(f"docker stop {container}", timeout=120)
                vps["status"] = "stopped"
                save_data()
                await interaction.followup.send(embed=success_embed("VPS Stopped", f"`{container}` has been stopped."), ephemeral=True)
                await interaction.message.edit(embed=self.embed(), view=self)
            except Exception as e:
                await interaction.followup.send(embed=error_embed("Stop Failed", str(e)), ephemeral=True)

        elif action == "ssh":
            await interaction.response.defer(ephemeral=True)
            password = vps.get("ssh_password")
            if not password:
                await interaction.followup.send(embed=error_embed("SSH Error", "No stored credentials — please reinstall this VPS."), ephemeral=True)
                return
            try:
                tmate_cmd = await get_tmate_session(container)
                e = base_embed("🔑 SSH Access", f"SSH connection for `{container}`:", 0x00FF88)
                e.add_field(name="SSH Command (tmate)", value=f"```{tmate_cmd}```", inline=False)
                e.add_field(name="Password", value=f"```{password}```", inline=True)
                e.add_field(name="⚠️ Note", value="tmate session ends when the VPS restarts — click SSH again afterward.", inline=False)
                try:
                    await interaction.user.send(embed=e)
                    await interaction.followup.send(embed=success_embed("SSH Sent", "Check your DMs."), ephemeral=True)
                except discord.Forbidden:
                    await interaction.followup.send(embed=error_embed("DM Failed", "Enable DMs to receive SSH credentials."), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=error_embed("SSH Error", str(e)), ephemeral=True)


class ReinstallConfirmView(discord.ui.View):
    def __init__(self, parent: ManageView, vps: dict):
        super().__init__(timeout=60)
        self.parent = parent
        self.vps = vps

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button):
        await interaction.response.defer(ephemeral=True)
        container = self.vps["container_name"]
        try:
            await interaction.followup.send(embed=info_embed("Reinstalling", f"Removing `{container}`..."), ephemeral=True)
            try:
                await run_docker(f"docker stop {container}")
            except Exception:
                pass
            await run_docker(f"docker rm -f {container}")

            ram_mb = int(self.vps["ram"].replace("GB", "")) * 1024
            disk_gb = int(str(self.vps.get("storage", "30GB")).replace("GB", ""))
            new_password = generate_password()
            await create_container(container, ram_mb, self.vps["cpu"], new_password, disk_gb=disk_gb)

            self.vps["status"] = "running"
            self.vps["ssh_password"] = new_password
            self.vps["created_at"] = datetime.now().isoformat()
            save_data()
            await interaction.followup.send(embed=success_embed("Reinstall Complete", f"`{container}` reinstalled."), ephemeral=True)
        except Exception as e:
            await interaction.followup.send(embed=error_embed("Reinstall Failed", str(e)), ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button):
        await interaction.response.edit_message(embed=self.parent.embed(), view=self.parent)
