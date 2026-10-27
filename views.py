"""Discord UI views for Multi-Node VPS management (buttons, live stats, and dropdowns)."""
from datetime import datetime

import discord

from storage import (
    vps_data, nodes_data, default_node_name, get_node, save_data,
    generate_password, base_embed, success_embed, error_embed, info_embed, warning_embed,
)
from docker_utils import (
    run_docker, docker_exec, create_container, get_ssh_access,
    classic_ssh_command, get_container_live_stats,
)


# ─── Node Picker (dropdown shown before creating/managing/deleting across nodes)

class NodePickerView(discord.ui.View):
    """
    Asks the user/admin which Node they want to use (using custom node names
    from nodes_data.json). Supports an optional 'All Nodes' choice for management.
    """
    def __init__(self, actor_id, on_pick, *, include_all=False, allowed_nodes=None):
        super().__init__(timeout=120)
        self.actor_id = str(actor_id)
        self.on_pick = on_pick

        options = []
        if include_all:
            options.append(
                discord.SelectOption(
                    label="All Nodes (Combined View)",
                    description="Manage VPS instances across all nodes together",
                    value="__ALL__",
                    emoji="🌐",
                )
            )

        for n_name, n_cfg in list(nodes_data.items())[:24]:
            if allowed_nodes is not None and n_name not in allowed_nodes:
                continue
            kind = f"Local Host ({n_cfg.get('ip', '?')})" if n_cfg.get("is_local") else f"Remote Node ({n_cfg.get('ip', '?')})"
            options.append(
                discord.SelectOption(
                    label=n_name,
                    description=kind[:100],
                    value=n_name,
                    emoji="🖥️",
                )
            )
        select = discord.ui.Select(placeholder="🌐 Select a Node...", options=options)
        select.callback = self._chosen
        self.add_item(select)

    async def _chosen(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.actor_id:
            await interaction.response.send_message(embed=error_embed("Access Denied", "This isn't your setup."), ephemeral=True)
            return
        node_name = interaction.data["values"][0]
        self.stop()
        await self.on_pick(interaction, node_name)


# ─── Interactive Delete Picker (Node -> VPS -> Delete) ─────────────────────

class DeleteVPSPickerView(discord.ui.View):
    """Lets an admin pick which VPS on the selected node to delete."""
    def __init__(self, actor_id, owner: discord.Member, filtered_vps: list, on_pick):
        super().__init__(timeout=120)
        self.actor_id = str(actor_id)
        self.owner = owner
        self.filtered_vps = filtered_vps
        self.on_pick = on_pick

        options = [
            discord.SelectOption(
                label=f"{v['container_name']} ({v.get('nickname', v.get('plan', 'Custom'))})",
                description=f"Node: {v.get('node', default_node_name())} | RAM: {v.get('ram')} | Status: {v.get('status')}",
                value=v["container_name"],
                emoji="🗑️",
            )
            for v in filtered_vps[:25]
        ]
        select = discord.ui.Select(placeholder="🗑️ Select VPS to delete...", options=options)
        select.callback = self._chosen
        self.add_item(select)

    async def _chosen(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.actor_id:
            await interaction.response.send_message(embed=error_embed("Access Denied", "Not authorized."), ephemeral=True)
            return
        cname = interaction.data["values"][0]
        target_vps = next((v for v in self.filtered_vps if v["container_name"] == cname), None)
        self.stop()
        await self.on_pick(interaction, target_vps)


# ─── Management view (buttons + dropdown + live telemetry) ─────────────────

class ManageView(discord.ui.View):
    def __init__(self, actor_id, owner_id, vps_list, *, is_shared=False, is_admin_view=False, selected=0):
        super().__init__(timeout=300)
        self.actor_id = str(actor_id)
        self.owner_id = str(owner_id)
        self.vps_list = vps_list
        self.is_shared = is_shared
        self.is_admin_view = is_admin_view
        self.selected = selected if 0 <= selected < len(vps_list) else 0
        self.live_stats = {}
        self._build_items()

    def _build_items(self):
        self.clear_items()
        if len(self.vps_list) > 1:
            options = [
                discord.SelectOption(
                    label=f"VPS {i + 1} — {v.get('nickname', v.get('plan', 'Custom'))[:40]}",
                    description=f"Node: {v.get('node', default_node_name())} | {v.get('ram', '?')} | {v.get('status', 'unknown').upper()}",
                    value=str(i),
                    default=(i == self.selected),
                )
                for i, v in enumerate(self.vps_list[:25])
            ]
            select = discord.ui.Select(placeholder="Select a VPS to manage", options=options)
            select.callback = self._on_select
            self.add_item(select)
        self._add_action_buttons()

    def _add_action_buttons(self):
        start = discord.ui.Button(label="▶ Start", style=discord.ButtonStyle.success)
        start.callback = lambda i: self._action(i, "start")
        stop = discord.ui.Button(label="⏸ Stop", style=discord.ButtonStyle.secondary)
        stop.callback = lambda i: self._action(i, "stop")
        restart = discord.ui.Button(label="🔁 Restart", style=discord.ButtonStyle.primary)
        restart.callback = lambda i: self._action(i, "restart")
        ssh = discord.ui.Button(label="🔑 SSH", style=discord.ButtonStyle.primary)
        ssh.callback = lambda i: self._action(i, "ssh")
        stats_btn = discord.ui.Button(label="📊 Live Stats", style=discord.ButtonStyle.secondary)
        stats_btn.callback = lambda i: self._action(i, "stats")

        self.add_item(start)
        self.add_item(stop)
        self.add_item(restart)
        self.add_item(ssh)
        self.add_item(stats_btn)

        if not self.is_shared and not self.is_admin_view:
            btn = discord.ui.Button(label="🔄 Reinstall", style=discord.ButtonStyle.danger, row=2)
            btn.callback = lambda i: self._action(i, "reinstall")
            self.add_item(btn)

    def embed(self):
        vps = self.vps_list[self.selected]
        node_name = vps.get("node", default_node_name())
        node_cfg = get_node(node_name)
        color = 0x00FF88 if vps.get("status") == "running" else 0xFF3366
        owner_text = ""
        if self.is_admin_view and self.owner_id != self.actor_id:
            owner_text = f"\n**Owner ID:** `{self.owner_id}`"

        e = base_embed(
            f"VPS Management — {vps.get('nickname', f'VPS {self.selected + 1}')}",
            f"Container: `{vps['container_name']}`\nNode: **`{node_name}`** (`{node_cfg.get('ip', '?')}:{vps.get('ssh_port', '?')}`){owner_text}",
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
            name="📊 Configuration & Node",
            value=(
                f"**🌐 Node:** `{node_name}`\n"
                f"**🖥️ OS:** {vps.get('os_label', vps.get('os_image', 'Ubuntu'))}\n"
                f"**📋 Plan:** {vps.get('plan', 'Custom')}\n"
                f"**⚡ Status:** `{vps.get('status', 'unknown').upper()}`\n"
                f"**🧠 RAM:** {vps.get('ram', '?')}\n"
                f"**⚙️ CPU:** {vps.get('cpu', '?')} Core(s)\n"
                f"**💾 Storage:** {vps.get('storage', '?')}\n"
                f"**🔌 SSH Port:** `{vps.get('ssh_port', 'N/A')}`\n"
                f"**📅 Created:** {vps.get('created_at', '?')[:10]}\n"
                f"**⏳ Expires:** {expire_str}"
            ),
            inline=False,
        )

        if self.live_stats:
            e.add_field(
                name="📈 Live Container Telemetry",
                value=(
                    f"**CPU Load:** `{self.live_stats.get('cpu', '0%')}`\n"
                    f"**Memory Usage:** `{self.live_stats.get('mem_usage', 'N/A')}` (`{self.live_stats.get('mem_perc', '0%')}`)\n"
                    f"**Network I/O:** `{self.live_stats.get('net_io', 'N/A')}`\n"
                    f"**Active Processes:** `{self.live_stats.get('pids', '0')}`"
                ),
                inline=False,
            )

        e.add_field(name="🎮 Controls", value="Use the buttons below to control this VPS on its node", inline=False)
        return e

    async def _authorized(self, interaction) -> bool:
        if str(interaction.user.id) != self.actor_id:
            await interaction.response.send_message(embed=error_embed("Access Denied", "This is not your VPS menu!"), ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        if not await self._authorized(interaction):
            return
        self.selected = int(interaction.data["values"][0])
        self.live_stats = {}
        self._build_items()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def _action(self, interaction: discord.Interaction, action: str):
        if not await self._authorized(interaction):
            return

        vps = self.vps_list[self.selected]
        container = vps["container_name"]
        node_name = vps.get("node", default_node_name())

        if action == "reinstall":
            if self.is_shared or self.is_admin_view:
                await interaction.response.send_message(embed=error_embed("Access Denied", "Only the VPS owner can reinstall."), ephemeral=True)
                return
            await interaction.response.send_message(
                embed=warning_embed(
                    "Reinstall Warning",
                    f"⚠️ This will **erase all data** on `{container}` (Node: `{node_name}`) and reinstall "
                    f"**{vps.get('os_label', vps.get('os_image', 'the original OS'))}**.\n\nAre you sure?",
                ),
                view=ReinstallConfirmView(self, vps),
                ephemeral=True,
            )
            return

        if action == "start":
            await interaction.response.defer(ephemeral=True)
            try:
                await run_docker(f"docker start {container}", node_name=node_name)
                await docker_exec(container, "/usr/sbin/sshd || true", timeout=10, node_name=node_name)
                vps["status"] = "running"
                save_data()
                await interaction.followup.send(embed=success_embed("VPS Started", f"`{container}` is now running on `{node_name}`."), ephemeral=True)
                await interaction.message.edit(embed=self.embed(), view=self)
            except Exception as e:
                await interaction.followup.send(embed=error_embed("Start Failed", str(e)), ephemeral=True)

        elif action == "stop":
            await interaction.response.defer(ephemeral=True)
            try:
                await run_docker(f"docker stop {container}", timeout=120, node_name=node_name)
                vps["status"] = "stopped"
                self.live_stats = {}
                save_data()
                await interaction.followup.send(embed=success_embed("VPS Stopped", f"`{container}` has been stopped on `{node_name}`."), ephemeral=True)
                await interaction.message.edit(embed=self.embed(), view=self)
            except Exception as e:
                await interaction.followup.send(embed=error_embed("Stop Failed", str(e)), ephemeral=True)

        elif action == "restart":
            await interaction.response.defer(ephemeral=True)
            try:
                await run_docker(f"docker restart {container}", timeout=120, node_name=node_name)
                await docker_exec(container, "/usr/sbin/sshd || true", timeout=10, node_name=node_name)
                vps["status"] = "running"
                save_data()
                await interaction.followup.send(embed=success_embed("VPS Restarted", f"`{container}` was restarted on `{node_name}`."), ephemeral=True)
                await interaction.message.edit(embed=self.embed(), view=self)
            except Exception as e:
                await interaction.followup.send(embed=error_embed("Restart Failed", str(e)), ephemeral=True)

        elif action == "stats":
            await interaction.response.defer(ephemeral=True)
            stats = await get_container_live_stats(container, node_name=node_name)
            if stats:
                self.live_stats = stats
                vps["status"] = "running"
                save_data()
                await interaction.message.edit(embed=self.embed(), view=self)
                await interaction.followup.send(embed=success_embed("Telemetry Updated", f"Live stats fetched from `{node_name}`."), ephemeral=True)
            else:
                await interaction.followup.send(embed=warning_embed("No Live Stats", f"`{container}` appears to be stopped or unreachable on `{node_name}`."), ephemeral=True)

        elif action == "ssh":
            await interaction.response.defer(ephemeral=True)
            password = vps.get("ssh_password")
            if not password:
                await interaction.followup.send(embed=error_embed("SSH Error", "No stored credentials — please reinstall this VPS."), ephemeral=True)
                return
            try:
                methods = await get_ssh_access(container, node_name=node_name)
                e = base_embed("🔑 SSH Access", f"Connection details for `{container}` on Node **`{node_name}`**:", 0x00FF88)
                port = vps.get("ssh_port")
                if port:
                    e.add_field(name="SSH (Terminal Command)", value=f"```{classic_ssh_command(port, node_name=node_name)}```", inline=False)
                if "sshx" in methods:
                    e.add_field(name="SSHX (Instant Browser Terminal)", value=f"```{methods['sshx']}```", inline=False)
                if not port and "sshx" not in methods:
                    e.add_field(name="⚠️", value="No SSH method is currently reachable — make sure the VPS is started.", inline=False)
                e.add_field(name="Password", value=f"```{password}```", inline=True)
                e.add_field(name="Node", value=f"`{node_name}`", inline=True)
                e.add_field(name="⚠️ Note", value="sshx link resets if the VPS restarts — click 🔑 SSH again anytime for a fresh link.", inline=False)
                try:
                    await interaction.user.send(embed=e)
                    await interaction.followup.send(embed=success_embed("SSH Sent", "Check your DMs!"), ephemeral=True)
                except discord.Forbidden:
                    await interaction.followup.send(embed=error_embed("DM Failed", "Please enable DMs to receive your SSH credentials."), ephemeral=True)
            except Exception as e:
                await interaction.followup.send(embed=error_embed("SSH Error", str(e)), ephemeral=True)


class ReinstallConfirmView(discord.ui.View):
    def __init__(self, parent: ManageView, vps: dict):
        super().__init__(timeout=60)
        self.parent = parent
        self.vps = vps

    @discord.ui.button(label="Confirm Reinstall", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button):
        await interaction.response.defer(ephemeral=True)
        container = self.vps["container_name"]
        node_name = self.vps.get("node", default_node_name())
        try:
            await interaction.followup.send(embed=info_embed("Reinstalling", f"Rebuilding `{container}` on `{node_name}`..."), ephemeral=True)
            try:
                await run_docker(f"docker stop {container}", node_name=node_name)
            except Exception:
                pass
            await run_docker(f"docker rm -f {container}", node_name=node_name)

            ram_mb = int(str(self.vps["ram"]).replace("GB", "")) * 1024
            disk_gb = int(str(self.vps.get("storage", "30GB")).replace("GB", ""))
            new_password = generate_password()
            await create_container(
                container, ram_mb, self.vps["cpu"], new_password, disk_gb=disk_gb,
                os_image=self.vps.get("os_image"), ssh_port=self.vps.get("ssh_port"),
                node_name=node_name,
            )

            self.vps["status"] = "running"
            self.vps["ssh_password"] = new_password
            self.vps["created_at"] = datetime.now().isoformat()
            save_data()
            await interaction.followup.send(
                embed=success_embed("Reinstall Complete", f"`{container}` reinstalled on `{node_name}`.\nNew Root Password: `{new_password}`"),
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(embed=error_embed("Reinstall Failed", str(e)), ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button):
        await interaction.response.edit_message(embed=self.parent.embed(), view=self.parent)


# ─── OS picker ─────────────────────────────────────────────────────────────

class OSPickerView(discord.ui.View):
    def __init__(self, actor_id, on_pick):
        super().__init__(timeout=120)
        self.actor_id = str(actor_id)
        self.on_pick = on_pick
        import config as _config
        options = [
            discord.SelectOption(label=label, value=image, emoji="💿")
            for label, image in _config.OS_CHOICES
        ]
        select = discord.ui.Select(placeholder="💿 Choose an Operating System...", options=options)
        select.callback = self._chosen
        self.add_item(select)

    async def _chosen(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.actor_id:
            await interaction.response.send_message(embed=error_embed("Access Denied", "This isn't your setup."), ephemeral=True)
            return
        image = interaction.data["values"][0]
        label = next((l for l, i in __import__("config").OS_CHOICES if i == image), image)
        self.stop()
        await self.on_pick(interaction, image, label)


# ─── Expiry picker ─────────────────────────────────────────────────────────

class _CustomDaysModal(discord.ui.Modal, title="Custom Expiry"):
    days = discord.ui.TextInput(label="Days until expiry", placeholder="e.g. 45")

    def __init__(self, on_days):
        super().__init__()
        self.on_days = on_days

    async def on_submit(self, interaction: discord.Interaction):
        try:
            n = int(str(self.days.value).strip())
            if n <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(embed=error_embed("Invalid", "Enter a positive whole number of days."), ephemeral=True)
            return
        await self.on_days(interaction, n)


class ExpiryPickerView(discord.ui.View):
    def __init__(self, actor_id, on_pick):
        super().__init__(timeout=120)
        self.actor_id = str(actor_id)
        self.on_pick = on_pick
        import config as _config
        options = [discord.SelectOption(label=label, value=str(days), emoji="⏳") for label, days in _config.EXPIRY_CHOICES]
        select = discord.ui.Select(placeholder="⏳ Choose an expiry duration...", options=options)
        select.callback = self._chosen
        self.add_item(select)

    async def _chosen(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.actor_id:
            await interaction.response.send_message(embed=error_embed("Access Denied", "This isn't your setup."), ephemeral=True)
            return
        value = interaction.data["values"][0]
        if value == "custom":
            async def _after_modal(modal_interaction, n):
                self.stop()
                await self.on_pick(modal_interaction, n)
            await interaction.response.send_modal(_CustomDaysModal(_after_modal))
            return
        self.stop()
        days = None if value == "None" else int(value)
        await self.on_pick(interaction, days)


# ─── Renew button ──────────────────────────────────────────────────────────

class RenewView(discord.ui.View):
    def __init__(self, on_renew):
        super().__init__(timeout=None)
        self.on_renew = on_renew

    @discord.ui.button(label="🔁 Renew Now (+30 days)", style=discord.ButtonStyle.success)
    async def renew(self, interaction: discord.Interaction, _button):
        await self.on_renew(interaction)
