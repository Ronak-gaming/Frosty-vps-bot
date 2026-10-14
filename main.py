"""Bot entrypoint: events, multi-node management, all VPS commands, and run()."""
import asyncio
import os
import time
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

from config import bot, MAIN_ADMIN_ID, BOT_START_TIME, CPU_THRESHOLD, CPU_CHECK_INTERVAL, logger
import config
from storage import (
    user_data, vps_data, admin_data, invite_data, nodes_data,
    get_default_node, get_node_info, find_vps_by_container,
    save_data, is_admin_id, is_admin, is_main_admin,
    generate_password, next_container_id,
    base_embed, success_embed, error_embed, info_embed, warning_embed,
)
from docker_utils import (
    run_docker, docker_exec, create_container, get_ssh_access,
    get_or_create_vps_role, pair_remote_node, get_node_live_stats, PLANS,
)
from views import ManageView, NodePickerView, OSPickerView, ExpiryPickerView, RenewView
import docker_utils

invite_cache = {}


def _tier_for_invites(n: int):
    tier = None
    for threshold, _spec in config.INVITE_TIERS:
        if n >= threshold:
            tier = threshold
    return tier


# Helper: if there are multiple nodes, show NodePickerView; if only 1 node exists,
# still show NodePickerView so the user explicitly sees and confirms the custom node name!
async def prompt_node_selection(ctx, title: str, prompt_text: str, on_node_picked):
    if not nodes_data:
        await ctx.send(embed=error_embed("No Nodes Configured", "Add a node first with `!node-add`."))
        return
    await ctx.send(
        embed=info_embed(title, prompt_text),
        view=NodePickerView(ctx.author.id, on_node_picked),
    )


# ─── Bot events ─────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    logger.info("%s connected to Discord (%d nodes configured)", bot.user, len(nodes_data))
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="ApexHosting | Multi-Node VPS"))
    for guild in bot.guilds:
        try:
            invite_cache[guild.id] = {inv.code: inv.uses for inv in await guild.invites()}
        except discord.Forbidden:
            invite_cache[guild.id] = {}
    if not auto_expire_check.is_running():
        auto_expire_check.start()


@bot.event
async def on_invite_create(invite: discord.Invite):
    invite_cache.setdefault(invite.guild.id, {})[invite.code] = invite.uses


@bot.event
async def on_invite_delete(invite: discord.Invite):
    invite_cache.get(invite.guild.id, {}).pop(invite.code, None)


@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    try:
        current = {inv.code: (inv.uses, inv.inviter) for inv in await guild.invites()}
    except discord.Forbidden:
        return
    cached = invite_cache.get(guild.id, {})
    used_code = None
    inviter_id = None
    for code, (uses, inviter) in current.items():
        if uses > cached.get(code, 0):
            used_code = code
            inviter_id = str(inviter.id) if inviter else None
            break
    invite_cache[guild.id] = {code: uses for code, (uses, _) in current.items()}
    if used_code and inviter_id and inviter_id != str(member.id):
        rec = invite_data.setdefault(inviter_id, {"invites": 0, "claimed_tier": None, "invited_users": []})
        rec.setdefault("invited_users", [])
        if str(member.id) not in rec["invited_users"]:
            rec["invited_users"].append(str(member.id))
            rec["invites"] += 1
            save_data()


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=error_embed("Missing Argument", "See `!help` for command usage."))
    elif isinstance(error, commands.BadArgument):
        await ctx.send(embed=error_embed("Invalid Argument", "Check your input and try again."))
    elif isinstance(error, commands.CheckFailure):
        pass
    else:
        logger.exception("Command error", exc_info=error)
        await ctx.send(embed=error_embed("System Error", "Something went wrong. Please try again."))


@bot.check
async def maintenance_check(ctx):
    if not config.maintenance_mode:
        return True
    if is_admin_id(ctx.author.id) and ctx.command and ctx.command.name == "maintenance":
        return True
    if not is_admin_id(ctx.author.id):
        await ctx.send(embed=warning_embed("🔴 Under Maintenance", "The bot is currently under maintenance. Please try again later."))
        return False
    return True


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if isinstance(message.channel, discord.DMChannel) and message.content.startswith(bot.command_prefix):
        await message.channel.send(embed=warning_embed(
            "❌ DM Commands Disabled", "Bot commands only work in the server — please use them there."
        ))
        return
    await bot.process_commands(message)


# ─── Multi-Node Management Commands ─────────────────────────────────────────

@bot.command(name="node-add")
@is_admin()
async def node_add(ctx, name: str, ip: str, ssh_port: int = 22, *, root_password: str = None):
    """
    !node-add <NodeName> <IP> [ssh_port] [root_password]
    - If root_password is omitted and IP matches this machine, registers/renames the local node.
    - If root_password is provided, pairs with the remote VPS over SSH and sets up Docker automatically.
    """
    try:
        await ctx.message.delete()  # Delete command message immediately so root password isn't left in chat
    except Exception:
        pass

    clean_name = "".join(c for c in name if c.isalnum() or c in ("-", "_"))[:32]
    if not clean_name:
        await ctx.send(embed=error_embed("Invalid Node Name", "Use letters, numbers, hyphens, or underscores."))
        return

    if not root_password or ip in ("127.0.0.1", "localhost", config.SERVER_PUBLIC_IP):
        actual_ip = config.SERVER_PUBLIC_IP if ip in ("127.0.0.1", "localhost") else ip
        nodes_data[clean_name] = {
            "name": clean_name,
            "ip": actual_ip,
            "ssh_port": ssh_port,
            "user": "root",
            "is_local": True,
            "added_at": datetime.now().isoformat(),
        }
        save_data()
        await ctx.send(embed=success_embed("Local Node Registered", f"Node **`{clean_name}`** (`{actual_ip}`) is registered as a local node."))
        return

    status_msg = await ctx.send(embed=info_embed("Pairing Remote Node...", f"Connecting to **`{clean_name}`** (`{ip}:{ssh_port}`), installing SSH key, and verifying Docker..."))
    ok, detail = await pair_remote_node(ip, ssh_port, root_password.strip())
    if not ok:
        await status_msg.edit(embed=error_embed("Node Pairing Failed", f"Could not link **`{clean_name}`** (`{ip}:{ssh_port}`):\n```{detail[:800]}```"))
        return

    nodes_data[clean_name] = {
        "name": clean_name,
        "ip": ip,
        "ssh_port": ssh_port,
        "user": "root",
        "is_local": False,
        "added_at": datetime.now().isoformat(),
    }
    save_data()
    await status_msg.edit(embed=success_embed("🟢 Remote Node Connected!", f"Node **`{clean_name}`** (`{ip}:{ssh_port}`) is now online and controlled by this bot.\n{detail}"))


@bot.command(name="node-rename")
@is_admin()
async def node_rename(ctx, old_name: str, new_name: str):
    """!node-rename <OldName> <NewName> — rename an existing node and update all its VPS records."""
    if old_name not in nodes_data:
        await ctx.send(embed=error_embed("Not Found", f"Node `{old_name}` does not exist."))
        return
    clean_new = "".join(c for c in new_name if c.isalnum() or c in ("-", "_"))[:32]
    if not clean_new or clean_new in nodes_data:
        await ctx.send(embed=error_embed("Invalid Name", f"Name `{clean_new}` is invalid or already exists."))
        return

    info = nodes_data.pop(old_name)
    info["name"] = clean_new
    nodes_data[clean_new] = info

    for vl in vps_data.values():
        for v in vl:
            if v.get("node") == old_name:
                v["node"] = clean_new
    save_data()
    await ctx.send(embed=success_embed("Node Renamed", f"Node `{old_name}` has been renamed to **`{clean_new}`**."))


@bot.command(name="node-remove")
@is_admin()
async def node_remove(ctx, name: str):
    """!node-remove <NodeName> — remove a node from the bot."""
    if name not in nodes_data:
        await ctx.send(embed=error_embed("Not Found", f"Node `{name}` does not exist."))
        return
    if len(nodes_data) <= 1:
        await ctx.send(embed=error_embed("Cannot Remove", "You must keep at least one node registered."))
        return
    active_on_node = sum(1 for vl in vps_data.values() for v in vl if v.get("node") == name)
    if active_on_node > 0:
        await ctx.send(embed=error_embed("Node In Use", f"Node `{name}` still has **{active_on_node}** VPS container(s) registered on it. Delete them first."))
        return
    del nodes_data[name]
    save_data()
    await ctx.send(embed=success_embed("Node Removed", f"Node **`{name}`** was removed."))


@bot.command(name="node-list")
async def node_list(ctx):
    """!node-list — view all connected nodes and their live status."""
    msg = await ctx.send(embed=info_embed("Querying Nodes...", "Checking live status across all connected nodes..."))
    e = base_embed("🌐 Connected VPS Nodes", f"Total Nodes: **{len(nodes_data)}**", 0x00CCFF)
    for name, info in nodes_data.items():
        stats = await get_node_live_stats(name)
        vps_count = sum(1 for vl in vps_data.values() for v in vl if v.get("node") == name)
        status_icon = "🟢 ONLINE" if stats["online"] else "🔴 OFFLINE"
        mode_str = "Local Host" if info.get("is_local") else f"Remote SSH (Port {info.get('ssh_port', 22)})"
        e.add_field(
            name=f"🖥️ {name} — {status_icon}",
            value=(
                f"**IP:** `{info.get('ip', '?')}`\n"
                f"**Type:** {mode_str}\n"
                f"**RAM Usage:** `{stats['ram']}`\n"
                f"**CPU Cores:** `{stats['cpus']}`\n"
                f"**Bot VPS Count:** `{vps_count}` (Running Containers: `{stats['containers']}`)"
            ),
            inline=False,
        )
    await msg.edit(embed=e)


# ─── VPS creation & management commands ────────────────────────────────────

@bot.command(name="create")
@is_admin()
async def create_vps(ctx, user: discord.Member, ram: int, cpu: int, disk: int = 30):
    """!create @user <ram_GB> <cpu_cores> <disk_GB> — asks which Node, OS, and Expiry to deploy on."""
    if ram <= 0 or cpu <= 0 or disk <= 0:
        await ctx.send(embed=error_embed("Invalid Specs", "RAM, CPU and disk must all be positive integers."))
        return

    async def on_node_picked(node_interaction, node_name):
        async def on_os_picked(os_interaction, image, os_label):
            async def on_expiry_picked(exp_interaction, expiry_days):
                await exp_interaction.response.edit_message(
                    embed=info_embed(
                        "Creating VPS",
                        f"Deploying for {user.mention} on Node **`{node_name}`** (**{os_label}**): `{ram}GB` RAM, `{cpu}` core(s), `{disk}GB` disk...",
                    ),
                    view=None,
                )

                user_id = str(user.id)
                vps_data.setdefault(user_id, [])
                display_number = len(vps_data[user_id]) + 1
                container_name = f"vps-{user_id}-{next_container_id(user_id)}"
                password = generate_password()
                ssh_port = docker_utils.next_ssh_port(node=node_name)
                expires = "Never" if expiry_days is None else (datetime.utcnow() + timedelta(days=expiry_days)).isoformat()

                try:
                    await create_container(
                        container_name, ram * 1024, cpu, password,
                        disk_gb=disk, os_image=image, ssh_port=ssh_port, node=node_name,
                    )

                    vps_data[user_id].append({
                        "node": node_name,
                        "container_name": container_name,
                        "ram": f"{ram}GB",
                        "cpu": str(cpu),
                        "storage": f"{disk}GB",
                        "status": "running",
                        "created_at": datetime.now().isoformat(),
                        "expires": expires,
                        "auto_renew": True,
                        "ssh_password": password,
                        "ssh_port": ssh_port,
                        "os_image": image,
                        "os_label": os_label,
                        "shared_with": [],
                    })
                    save_data()

                    if ctx.guild:
                        role = await get_or_create_vps_role(ctx.guild)
                        if role:
                            try:
                                await user.add_roles(role, reason="VPS ownership granted")
                            except discord.Forbidden:
                                pass

                    e = base_embed("🚀 VPS Deployed!", f"{user.mention}'s VPS is live on **`{node_name}`** — SSH info sent via DM.", 0x00FF88)
                    e.add_field(name="👤 Owner", value=user.mention, inline=True)
                    e.add_field(name="🖥️ Node", value=f"`{node_name}`", inline=True)
                    e.add_field(name="🆔 VPS ID", value=f"#{display_number}", inline=True)
                    e.add_field(name="📦 Container", value=f"`{container_name}`", inline=True)
                    e.add_field(name="💿 OS", value=os_label, inline=True)
                    e.add_field(name="🧠 RAM", value=f"{ram} GB", inline=True)
                    e.add_field(name="⚙️ CPU", value=f"{cpu} core(s)", inline=True)
                    e.add_field(name="💾 Disk", value=f"{disk} GB", inline=True)
                    e.add_field(name="⏳ Expiry", value=expires if expires == "Never" else f"{expires[:10]}", inline=True)
                    e.add_field(name="🎮 Manage", value="`!manage` → Start / Stop / Reinstall / SSH", inline=False)
                    await ctx.send(embed=e)

                    try:
                        methods = await get_ssh_access(container_name, node=node_name)
                        dm = base_embed("🎉 Your VPS is Ready!", "Connect using the info below.", 0x5865F2)
                        dm.add_field(name="🖥️ Node", value=f"`{node_name}`", inline=True)
                        dm.add_field(name="🆔 VPS ID", value=f"#{display_number}", inline=True)
                        dm.add_field(name="💿 OS", value=os_label, inline=True)
                        dm.add_field(name="🧠 RAM", value=f"{ram} GB", inline=True)
                        dm.add_field(name="⚙️ CPU", value=f"{cpu} core(s)", inline=True)
                        dm.add_field(name="🔑 SSH (classic)", value=f"```{docker_utils.classic_ssh_command(ssh_port, node=node_name)}```\nPassword: ```{password}```", inline=False)
                        if "sshx" in methods:
                            dm.add_field(name="🔗 SSHX (browser link)", value=f"```{methods['sshx']}```", inline=False)
                        dm.add_field(name="🎮 Manage", value="`!manage` in the server", inline=False)
                        await user.send(embed=dm)
                    except discord.Forbidden:
                        pass

                except Exception as e:
                    await ctx.send(embed=error_embed("Creation Failed", str(e)))

            await os_interaction.response.edit_message(
                embed=info_embed("Choose Expiry", f"Node: **`{node_name}`** | OS: **{os_label}**\nPick an expiry for {user.mention}'s VPS:"),
                view=ExpiryPickerView(ctx.author.id, on_expiry_picked),
            )

        await node_interaction.response.edit_message(
            embed=info_embed("Choose an OS", f"Selected Node: **`{node_name}`**\nPick the OS to install for {user.mention}'s VPS:"),
            view=OSPickerView(ctx.author.id, on_os_picked),
        )

    await prompt_node_selection(
        ctx,
        "Choose a Node",
        f"Select which Node to deploy {user.mention}'s VPS (`{ram}GB` RAM / `{cpu}` CPU) on:",
        on_node_picked,
    )


@bot.command(name="manage")
async def manage_vps(ctx, user: discord.Member = None):
    """!manage [@user] — asks which Node (if on multiple nodes) or shows all your VPSs."""
    target = user if user else ctx.author
    if user and not is_admin_id(ctx.author.id):
        await ctx.send(embed=error_embed("Access Denied", "Only admins can manage another user's VPS."))
        return

    vps_list = vps_data.get(str(target.id), [])
    if not vps_list:
        if user:
            await ctx.send(embed=error_embed("No VPS Found", f"{user.mention} doesn't have any VPS."))
        else:
            e = error_embed("No VPS Found", "You don't have any VPS yet.")
            e.add_field(name="Quick Actions", value="`!plans` — view plans\n`!buywc <plan> <processor>` — purchase", inline=False)
            await ctx.send(embed=e)
        return

    # If the user has VPSs across multiple nodes (or admin wants to pick by node first)
    user_nodes = sorted({v.get("node", get_default_node()) for v in vps_list})
    if len(user_nodes) > 1:
        options = [discord.SelectOption(label="🌐 All Nodes", description=f"Show all {len(vps_list)} VPSs", value="__ALL__")]
        for n_name in user_nodes:
            c = sum(1 for v in vps_list if v.get("node") == n_name)
            options.append(discord.SelectOption(label=f"🖥️ {n_name}", description=f"{c} VPS(s) on this node", value=n_name))

        class ManageNodeFilterView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=120)
                sel = discord.ui.Select(placeholder="Select which Node to manage...", options=options)
                sel.callback = self._picked
                self.add_item(sel)

            async def _picked(self, interaction: discord.Interaction):
                if str(interaction.user.id) != str(ctx.author.id):
                    await interaction.response.send_message(embed=error_embed("Access Denied", "Not your menu."), ephemeral=True)
                    return
                chosen = interaction.data["values"][0]
                filtered = vps_list if chosen == "__ALL__" else [v for v in vps_list if v.get("node") == chosen]
                view = ManageView(ctx.author.id, target.id, filtered, is_admin_view=bool(user))
                await interaction.response.edit_message(embed=view.embed(), view=view)

        await ctx.send(
            embed=info_embed("Select Node to Manage", f"{target.mention} has VPS instances across **{len(user_nodes)}** nodes. Pick a node:"),
            view=ManageNodeFilterView(),
        )
    else:
        view = ManageView(ctx.author.id, target.id, vps_list, is_admin_view=bool(user))
        await ctx.send(embed=view.embed(), view=view)


@bot.command(name="delete-vps")
@is_admin()
async def delete_vps(ctx, user: discord.Member, vps_number: int = None, *, reason: str = "No reason given"):
    """!delete-vps @user [#] [reason] — delete a user's VPS on its node (admin only)"""
    user_id = str(user.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list:
        await ctx.send(embed=error_embed("No VPS", f"{user.mention} has no VPS."))
        return

    async def _perform_delete(vps_obj, reply_target):
        container = vps_obj["container_name"]
        node_name = vps_obj.get("node", get_default_node())
        try:
            try:
                await run_docker(f"docker stop {container}", node=node_name)
            except Exception:
                pass
            await run_docker(f"docker rm -f {container}", node=node_name)
            if vps_obj in vps_list:
                vps_list.remove(vps_obj)
            if not vps_list:
                vps_data.pop(user_id, None)
                if ctx.guild:
                    role = await get_or_create_vps_role(ctx.guild)
                    if role and role in user.roles:
                        try:
                            await user.remove_roles(role, reason="No VPS ownership left")
                        except discord.Forbidden:
                            pass
            save_data()

            e = success_embed("VPS Deleted")
            e.add_field(name="Owner", value=user.mention, inline=True)
            e.add_field(name="Node", value=f"`{node_name}`", inline=True)
            e.add_field(name="Container", value=f"`{container}`", inline=True)
            e.add_field(name="Reason", value=reason, inline=False)
            await reply_target(embed=e)
        except Exception as e:
            await reply_target(embed=error_embed("Deletion Failed", str(e)))

    if vps_number is not None:
        if not (1 <= vps_number <= len(vps_list)):
            await ctx.send(embed=error_embed("Invalid VPS", f"Invalid VPS number (user has {len(vps_list)})."))
            return
        target_vps = vps_list[vps_number - 1]
        await ctx.send(embed=info_embed("Deleting VPS", f"Removing `{target_vps['container_name']}` from node **`{target_vps.get('node')}`**..."))
        await _perform_delete(target_vps, ctx.send)
        return

    # If no vps_number given, let admin pick the Node & VPS via dropdown
    options = [
        discord.SelectOption(
            label=f"VPS #{i + 1} — {v['container_name']} [{v.get('node', '?')}]"[:100],
            description=f"Node: {v.get('node', '?')} | RAM: {v.get('ram')} | Status: {v.get('status')}",
            value=str(i),
        )
        for i, v in enumerate(vps_list[:25])
    ]

    class DeletePickerView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            sel = discord.ui.Select(placeholder="Select VPS & Node to delete...", options=options)
            sel.callback = self._picked
            self.add_item(sel)

        async def _picked(self, interaction: discord.Interaction):
            if str(interaction.user.id) != str(ctx.author.id):
                await interaction.response.send_message(embed=error_embed("Access Denied", "Not your command."), ephemeral=True)
                return
            idx = int(interaction.data["values"][0])
            target_vps = vps_list[idx]
            await interaction.response.edit_message(
                embed=info_embed("Deleting VPS", f"Removing `{target_vps['container_name']}` from node **`{target_vps.get('node')}`**..."),
                view=None,
            )
            await _perform_delete(target_vps, interaction.followup.send)

    await ctx.send(embed=info_embed("Select VPS to Delete", f"Choose which of {user.mention}'s VPSs to delete:"), view=DeletePickerView())


@bot.command(name="manage-shared")
async def manage_shared_vps(ctx, owner: discord.Member, vps_number: int):
    """!manage-shared @owner <#> — manage a VPS shared with you"""
    owner_id = str(owner.id)
    vps_list = vps_data.get(owner_id, [])
    if not vps_list or not (1 <= vps_number <= len(vps_list)):
        await ctx.send(embed=error_embed("Invalid VPS", "Invalid VPS number."))
        return
    vps = vps_list[vps_number - 1]
    if str(ctx.author.id) not in vps.get("shared_with", []):
        await ctx.send(embed=error_embed("Access Denied", "You don't have access to this VPS."))
        return
    view = ManageView(ctx.author.id, owner_id, [vps], is_shared=True)
    await ctx.send(embed=view.embed(), view=view)


@bot.command(name="share-user")
async def share_user(ctx, target: discord.Member, vps_number: int):
    user_id = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or not (1 <= vps_number <= len(vps_list)):
        await ctx.send(embed=error_embed("Invalid VPS", "Invalid VPS number."))
        return
    vps = vps_list[vps_number - 1]
    vps.setdefault("shared_with", [])
    if str(target.id) in vps["shared_with"]:
        await ctx.send(embed=error_embed("Already Shared", f"{target.mention} already has access."))
        return
    vps["shared_with"].append(str(target.id))
    save_data()
    await ctx.send(embed=success_embed("VPS Shared", f"VPS #{vps_number} (`{vps.get('node')}`) shared with {target.mention}."))
    try:
        await target.send(embed=info_embed(
            "VPS Access Granted",
            f"You now have access to {ctx.author.mention}'s VPS #{vps_number} on node `{vps.get('node')}`. "
            f"Use `!manage-shared {ctx.author.mention} {vps_number}`.",
        ))
    except discord.Forbidden:
        pass


@bot.command(name="share-ruser")
async def revoke_share(ctx, target: discord.Member, vps_number: int):
    user_id = str(ctx.author.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or not (1 <= vps_number <= len(vps_list)):
        await ctx.send(embed=error_embed("Invalid VPS", "Invalid VPS number."))
        return
    vps = vps_list[vps_number - 1]
    if str(target.id) not in vps.get("shared_with", []):
        await ctx.send(embed=error_embed("Not Shared", f"{target.mention} doesn't have access."))
        return
    vps["shared_with"].remove(str(target.id))
    save_data()
    await ctx.send(embed=success_embed("Access Revoked", f"Revoked {target.mention}'s access to VPS #{vps_number}."))


# ─── Credits & purchasing ───────────────────────────────────────────────────

@bot.command(name="plans")
async def show_plans(ctx):
    e = base_embed("💎 VPS Plans — ApexHosting", "Choose your plan:")
    for name, p in PLANS.items():
        e.add_field(
            name=name,
            value=f"**RAM:** {p['ram']}\n**CPU:** {p['cpu']} core(s)\n**Storage:** {p['storage']}\n"
                  f"**Intel:** {p['price']['Intel']} cr | **AMD:** {p['price']['AMD']} cr",
            inline=True,
        )
    e.add_field(name="Available Nodes", value=", ".join(f"`{n}`" for n in nodes_data.keys()), inline=False)
    e.add_field(name="How to Buy", value="`!buywc <plan> <Intel/AMD>`\n`!buyc` for payment info", inline=False)
    await ctx.send(embed=e)


@bot.command(name="buyc")
async def buy_credits(ctx):
    e = base_embed("💳 Purchase Credits", "Choose a payment method:")
    e.add_field(name="🇮🇳 UPI", value="```\nyour-upi@bank\n```", inline=False)
    e.add_field(name="💰 PayPal", value="```\nyou@example.com\n```", inline=False)
    e.add_field(name="₿ Crypto", value="BTC, ETH, USDT accepted", inline=False)
    e.add_field(name="📋 Next Steps", value="1. Pay\n2. DM an admin your transaction ID\n3. Receive credits", inline=False)
    try:
        await ctx.author.send(embed=e)
        await ctx.send(embed=success_embed("Sent", "Payment details sent to your DMs."))
    except discord.Forbidden:
        await ctx.send(embed=error_embed("DM Failed", "Enable DMs to receive payment info."))


@bot.command(name="buywc")
async def buy_with_credits(ctx, plan: str, processor: str = "Intel"):
    """!buywc <plan> <Intel/AMD> — asks which Node and OS to deploy on."""
    plan = plan.capitalize()
    if plan not in PLANS:
        await ctx.send(embed=error_embed("Invalid Plan", "Available: " + ", ".join(PLANS)))
        return
    if processor not in ("Intel", "AMD"):
        await ctx.send(embed=error_embed("Invalid Processor", "Choose: Intel or AMD"))
        return

    cost = PLANS[plan]["price"][processor]
    user_id = str(ctx.author.id)
    user_data.setdefault(user_id, {"credits": 0})
    if user_data[user_id]["credits"] < cost:
        await ctx.send(embed=error_embed("Insufficient Credits", f"Need {cost}, you have {user_data[user_id]['credits']}."))
        return

    async def on_node_picked(node_interaction, node_name):
        async def on_os_picked(os_interaction, image, os_label):
            if user_data.get(user_id, {}).get("credits", 0) < cost:
                await os_interaction.response.edit_message(
                    embed=error_embed("Insufficient Credits", f"Need {cost}, you have {user_data[user_id]['credits']}."),
                    view=None,
                )
                return

            user_data[user_id]["credits"] -= cost
            save_data()

            await os_interaction.response.edit_message(
                embed=info_embed("Processing Purchase", f"Deploying {plan} VPS on Node **`{node_name}`** (**{os_label}**)..."),
                view=None,
            )

            vps_data.setdefault(user_id, [])
            display_number = len(vps_data[user_id]) + 1
            container_name = f"vps-{user_id}-{next_container_id(user_id)}"
            ram_str = PLANS[plan]["ram"]
            cpu_str = PLANS[plan]["cpu"]
            password = generate_password()
            ssh_port = docker_utils.next_ssh_port(node=node_name)

            try:
                await create_container(
                    container_name, int(ram_str.replace("GB", "")) * 1024, cpu_str, password,
                    os_image=image, ssh_port=ssh_port, node=node_name,
                )

                vps_data[user_id].append({
                    "node": node_name,
                    "plan": plan,
                    "container_name": container_name,
                    "ram": ram_str,
                    "cpu": cpu_str,
                    "storage": PLANS[plan]["storage"],
                    "status": "running",
                    "created_at": datetime.now().isoformat(),
                    "processor": processor,
                    "expires": "Never",
                    "ssh_password": password,
                    "ssh_port": ssh_port,
                    "os_image": image,
                    "os_label": os_label,
                    "shared_with": [],
                })
                save_data()

                if ctx.guild:
                    role = await get_or_create_vps_role(ctx.guild)
                    if role:
                        try:
                            await ctx.author.add_roles(role, reason="VPS purchase")
                        except discord.Forbidden:
                            pass

                e = success_embed("VPS Purchased")
                e.add_field(name="Node", value=f"`{node_name}`", inline=True)
                e.add_field(name="Plan", value=f"{plan} ({processor})", inline=True)
                e.add_field(name="OS", value=os_label, inline=True)
                e.add_field(name="VPS ID", value=f"#{display_number}", inline=True)
                e.add_field(name="Cost", value=f"{cost} credits", inline=True)
                await ctx.send(embed=e)

                try:
                    methods = await get_ssh_access(container_name, node=node_name)
                    dm = base_embed("🎉 Your VPS is Ready!", "Connect using the info below.", 0x5865F2)
                    dm.add_field(name="🖥️ Node", value=f"`{node_name}`", inline=True)
                    dm.add_field(name="🔑 SSH (classic)", value=f"```{docker_utils.classic_ssh_command(ssh_port, node=node_name)}```\nPassword: ```{password}```", inline=False)
                    if "sshx" in methods:
                        dm.add_field(name="🔗 SSHX (browser link)", value=f"```{methods['sshx']}```", inline=False)
                    dm.add_field(name="🎮 Manage", value="`!manage` in the server", inline=False)
                    await ctx.author.send(embed=dm)
                except discord.Forbidden:
                    pass

            except Exception as e:
                user_data[user_id]["credits"] += cost
                save_data()
                await ctx.send(embed=error_embed("Purchase Failed", f"{e}\n\nYour credits were refunded."))

        await node_interaction.response.edit_message(
            embed=info_embed("Choose an OS", f"Selected Node: **`{node_name}`**\nPick the OS for your {plan} VPS:"),
            view=OSPickerView(ctx.author.id, on_os_picked),
        )

    await prompt_node_selection(ctx, "Choose a Node", f"Select which Node to deploy your **{plan}** VPS on:", on_node_picked)


@bot.command(name="credits")
async def check_credits(ctx):
    user_id = str(ctx.author.id)
    user_data.setdefault(user_id, {"credits": 0})
    save_data()
    await ctx.send(embed=info_embed("💰 Credit Balance", f"{ctx.author.mention}, you have **{user_data[user_id]['credits']}** credits."))


@bot.command(name="transfer")
async def transfer_credits(ctx, target: discord.Member, amount: int):
    if amount <= 0:
        await ctx.send(embed=error_embed("Invalid Amount", "Amount must be positive."))
        return
    if target.id == ctx.author.id:
        await ctx.send(embed=error_embed("Invalid Target", "You can't transfer to yourself."))
        return
    sender, receiver = str(ctx.author.id), str(target.id)
    user_data.setdefault(sender, {"credits": 0})
    user_data.setdefault(receiver, {"credits": 0})
    if user_data[sender]["credits"] < amount:
        await ctx.send(embed=error_embed("Insufficient Credits", f"You only have {user_data[sender]['credits']} credits."))
        return
    user_data[sender]["credits"] -= amount
    user_data[receiver]["credits"] += amount
    save_data()
    await ctx.send(embed=success_embed("💸 Transfer Complete", f"{ctx.author.mention} sent **{amount}** credits to {target.mention}."))
    try:
        await target.send(embed=info_embed("💰 Credits Received", f"You received **{amount}** credits from {ctx.author.mention}."))
    except discord.Forbidden:
        pass


@bot.command(name="leaderboard")
async def leaderboard(ctx):
    top = sorted(user_data.items(), key=lambda kv: kv[1].get("credits", 0), reverse=True)[:10]
    medals = ["🥇", "🥈", "🥉"] + ["🏅"] * 7
    lines = []
    for i, (uid, data) in enumerate(top):
        try:
            u = await bot.fetch_user(int(uid))
            name = u.name
        except Exception:
            name = f"User#{uid[:4]}"
        lines.append(f"{medals[i]} **{name}** — {data.get('credits', 0)} credits")
    e = base_embed("🏆 Credit Leaderboard", "", 0xFFD700)
    e.add_field(name="Rankings", value="\n".join(lines) if lines else "No data yet.", inline=False)
    await ctx.send(embed=e)


@bot.command(name="adminc")
@is_admin()
async def admin_add_credits(ctx, user: discord.Member, amount: int):
    user_id = str(user.id)
    user_data.setdefault(user_id, {"credits": 0})
    user_data[user_id]["credits"] += amount
    save_data()
    await ctx.send(embed=success_embed("Credits Added", f"Added {amount} to {user.mention}. New balance: {user_data[user_id]['credits']}"))


@bot.command(name="adminrc")
@is_admin()
async def admin_remove_credits(ctx, user: discord.Member, amount: str):
    user_id = str(user.id)
    user_data.setdefault(user_id, {"credits": 0})
    if amount.lower() == "all":
        removed = user_data[user_id]["credits"]
        user_data[user_id]["credits"] = 0
    else:
        try:
            removed = int(amount)
            if removed <= 0:
                raise ValueError
        except ValueError:
            await ctx.send(embed=error_embed("Invalid Amount", "Enter a positive integer or `all`."))
            return
        user_data[user_id]["credits"] = max(0, user_data[user_id]["credits"] - removed)
    save_data()
    await ctx.send(embed=success_embed("Credits Removed", f"Removed {removed} from {user.mention}. New balance: {user_data[user_id]['credits']}"))


# ─── Info / stats ───────────────────────────────────────────────────────────

@bot.command(name="myinfo")
async def my_info(ctx):
    user_id = str(ctx.author.id)
    credits = user_data.get(user_id, {}).get("credits", 0)
    vps_list = vps_data.get(user_id, [])
    e = base_embed(f"👤 {ctx.author.name}'s Dashboard", "", 0x5865F2)
    e.set_thumbnail(url=ctx.author.display_avatar.url)
    e.add_field(name="💰 Credits", value=str(credits), inline=True)
    e.add_field(name="🖥️ VPS Count", value=str(len(vps_list)), inline=True)
    if vps_list:
        lines = []
        for i, v in enumerate(vps_list):
            nickname = v.get("nickname", f"VPS {i + 1}")
            icon = "🟢" if v.get("status") == "running" else "🔴"
            node_tag = v.get("node", get_default_node())
            note = f" — _{v['note']}_" if v.get("note") else ""
            lines.append(f"{icon} **{nickname}** (`{v['container_name']}` on `{node_tag}`){note}")
        e.add_field(name="🖥️ Your VPS", value="\n".join(lines), inline=False)
    else:
        e.add_field(name="🖥️ Your VPS", value="No VPS yet — try `!buywc`.", inline=False)
    await ctx.send(embed=e)


@bot.command(name="rename-vps")
async def rename_vps(ctx, vps_number: int, *, new_name: str):
    vps_list = vps_data.get(str(ctx.author.id), [])
    if not vps_list or not (1 <= vps_number <= len(vps_list)):
        await ctx.send(embed=error_embed("Invalid VPS", "VPS not found."))
        return
    if len(new_name) > 30:
        await ctx.send(embed=error_embed("Name Too Long", "Nicknames must be 30 characters or fewer."))
        return
    vps_list[vps_number - 1]["nickname"] = new_name
    save_data()
    await ctx.send(embed=success_embed("VPS Renamed", f"VPS #{vps_number} is now **{new_name}**."))


@bot.command(name="vps-note")
async def vps_note(ctx, vps_number: int, *, note: str):
    vps_list = vps_data.get(str(ctx.author.id), [])
    if not vps_list or not (1 <= vps_number <= len(vps_list)):
        await ctx.send(embed=error_embed("Invalid VPS", "VPS not found."))
        return
    vps_list[vps_number - 1]["note"] = note[:200]
    save_data()
    await ctx.send(embed=success_embed("Note Saved", f"Note added to VPS #{vps_number}."))


@bot.command(name="ping-vps")
async def ping_vps(ctx, vps_number: int):
    vps_list = vps_data.get(str(ctx.author.id), [])
    if not vps_list or not (1 <= vps_number <= len(vps_list)):
        await ctx.send(embed=error_embed("Invalid VPS", "VPS not found."))
        return
    vps = vps_list[vps_number - 1]
    container = vps["container_name"]
    node_name = vps.get("node", get_default_node())
    msg = await ctx.send(embed=info_embed("Pinging...", f"Checking `{container}` on node **`{node_name}`**..."))
    start = time.time()
    try:
        out = await run_docker(f"docker inspect --format={{{{.State.Running}}}} {container}", timeout=12, node=node_name)
        elapsed_ms = int((time.time() - start) * 1000)
        if out.strip() == "true":
            await msg.edit(embed=success_embed("🏓 Pong!", f"Alive on **`{node_name}`** — response `{elapsed_ms}ms`"))
        else:
            await msg.edit(embed=error_embed("💀 No Response", f"Container is not running on `{node_name}`."))
    except Exception as e:
        await msg.edit(embed=error_embed("Ping Failed", str(e)))


@bot.command(name="uptime-vps")
async def uptime_vps(ctx, vps_number: int):
    vps_list = vps_data.get(str(ctx.author.id), [])
    if not vps_list or not (1 <= vps_number <= len(vps_list)):
        await ctx.send(embed=error_embed("Invalid VPS", "VPS not found."))
        return
    vps = vps_list[vps_number - 1]
    container = vps["container_name"]
    node_name = vps.get("node", get_default_node())
    try:
        started_at_str = await run_docker(f"docker inspect --format={{{{.State.StartedAt}}}} {container}", timeout=12, node=node_name)
        started_at = datetime.fromisoformat(started_at_str[:19])
        delta = datetime.utcnow() - started_at
        hours, rem = divmod(delta.seconds, 3600)
        minutes, seconds = divmod(rem, 60)
        e = success_embed(f"⏱️ Uptime — {container} ({node_name})", f"```{delta.days}d {hours}h {minutes}m {seconds}s```")
        e.add_field(name="Started At", value=f"`{started_at_str[:19]} UTC`", inline=False)
        await ctx.send(embed=e)
    except Exception as e:
        await ctx.send(embed=error_embed("Uptime Error", str(e)))


@bot.command(name="botstatus")
async def bot_status(ctx):
    delta = datetime.utcnow() - BOT_START_TIME
    hours, rem = divmod(delta.seconds, 3600)
    minutes, _ = divmod(rem, 60)
    total = sum(len(v) for v in vps_data.values())
    running = sum(1 for vl in vps_data.values() for v in vl if v.get("status") == "running")
    e = base_embed("🤖 Bot Status", "ApexHosting Multi-Node VPS Manager", 0x00FF88)
    e.add_field(name="⏱️ Uptime", value=f"{delta.days}d {hours}h {minutes}m", inline=True)
    e.add
