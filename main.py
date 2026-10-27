"""Bot entrypoint: Multi-Node management, events, all commands, and run()."""
import asyncio
import os
import time
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

from config import bot, MAIN_ADMIN_ID, BOT_START_TIME, CPU_THRESHOLD, CPU_CHECK_INTERVAL, logger
import config
import storage
from storage import (
    user_data, vps_data, admin_data, invite_data, nodes_data, default_node_name, get_node,
    save_data, is_admin_id, is_admin, is_main_admin,
    generate_password, next_container_id,
    base_embed, success_embed, error_embed, info_embed, warning_embed,
)
from docker_utils import (
    run_docker, docker_exec, create_container, get_ssh_access,
    get_or_create_vps_role, pair_remote_node, check_node_online,
    ensure_node_ssh_key, start_cpu_monitor, PLANS,
)
from views import (
    ManageView, ReinstallConfirmView, OSPickerView, ExpiryPickerView,
    RenewView, NodePickerView, DeleteVPSPickerView,
)
import docker_utils

start_cpu_monitor()
ensure_node_ssh_key()

invite_cache = {}


def _tier_for_invites(n: int):
    tier = None
    for threshold, spec in config.INVITE_TIERS:
        if n >= threshold:
            tier = threshold
    return tier


def _find_vps_by_container(container_name: str, node_name: str = None):
    """Locate a VPS dict across all users by container_name (and optional node_name)."""
    for uid, vl in vps_data.items():
        for v in vl:
            if v.get("container_name") == container_name:
                if node_name is None or v.get("node", default_node_name()) == node_name:
                    return uid, v
    return None, None


# ─── Bot events ─────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    logger.info("%s connected to Discord (%d nodes configured)", bot.user, len(nodes_data))
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name=f"ApexHosting | {len(nodes_data)} Node(s)")
    )
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
        entry = invite_data.setdefault(inviter_id, {"invites": 0, "claimed_tier": None, "invited_users": []})
        entry.setdefault("invited_users", [])
        if str(member.id) not in entry["invited_users"]:
            entry["invited_users"].append(str(member.id))
            entry["invites"] += 1
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
        await ctx.send(embed=error_embed("System Error", f"Something went wrong: `{error}`"))


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

@bot.command(name="nodes")
@is_admin()
async def list_nodes(ctx):
    """!nodes — list all connected VPS nodes and their live hardware/Docker status"""
    msg = await ctx.send(embed=info_embed("Checking Nodes...", "Pinging all registered nodes simultaneously..."))
    e = base_embed("🌐 Registered VPS Nodes", "Active nodes controlled by this bot:", 0x00CCFF)

    checks = await asyncio.gather(*(check_node_online(n_name) for n_name in nodes_data.keys()))
    for (n_name, n_cfg), (ok, detail) in zip(nodes_data.items(), checks):
        status_icon = "🟢" if ok else "🔴"
        kind = "Local Host" if n_cfg.get("is_local") else f"Remote SSH (`{n_cfg.get('ip')}:{n_cfg.get('ssh_port', 22)}`)"
        vps_total = sum(1 for vl in vps_data.values() for v in vl if v.get("node", default_node_name()) == n_name)
        vps_running = sum(
            1 for vl in vps_data.values() for v in vl
            if v.get("node", default_node_name()) == n_name and v.get("status") == "running"
        )
        e.add_field(
            name=f"{status_icon} {n_name}",
            value=(
                f"**Type:** {kind}\n"
                f"**Public IP:** `{n_cfg.get('ip', '?')}`\n"
                f"**Containers:** `{vps_total}` total (`{vps_running}` running)\n"
                f"**Health:** `{detail}`"
            ),
            inline=False,
        )
    await msg.edit(embed=e)


@bot.command(name="node-add")
@is_admin()
async def add_node(ctx, name: str, ip: str, ssh_port: int = 22, *, root_password: str = "KEY_ONLY"):
    """!node-add <NodeName> <IP> [ssh_port] [root_password] — pair an additional VPS node"""
    try:
        await ctx.message.delete()
    except Exception:
        pass

    if name in nodes_data:
        await ctx.send(embed=error_embed("Node Exists", f"A node named `{name}` already exists."))
        return

    msg = await ctx.send(embed=info_embed("Pairing Node...", f"Connecting to **`{name}`** (`{ip}:{ssh_port}`) and setting up Docker & SSH keys..."))
    ok, detail = await pair_remote_node(ip, ssh_port, root_password)
    if not ok:
        await msg.edit(embed=error_embed(
            "Node Pairing Failed",
            f"Could not pair `{name}` (`{ip}:{ssh_port}`):\n```{detail}```\n"
            f"Tip: You can also run `!node-key`, paste the public key into `/root/.ssh/authorized_keys` on `{name}`, and run:\n"
            f"`!node-add {name} {ip} {ssh_port} KEY_ONLY`",
        ))
        return

    nodes_data[name] = {
        "name": name,
        "is_local": False,
        "ip": ip,
        "ssh_port": ssh_port,
        "user": "root",
    }
    save_data()
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name=f"ApexHosting | {len(nodes_data)} Node(s)")
    )
    await msg.edit(embed=success_embed("🌐 Node Paired & Online!", f"Node **`{name}`** (`{ip}`) is now linked and ready for VPS deployments!\n**Health:** `{detail}`"))


@bot.command(name="node-rename")
@is_admin()
async def rename_node(ctx, old_name: str, new_name: str):
    """!node-rename <OldName> <NewName> — rename any node"""
    if old_name not in nodes_data:
        await ctx.send(embed=error_embed("Not Found", f"Node `{old_name}` does not exist. Use `!nodes` to view node names."))
        return
    if new_name in nodes_data:
        await ctx.send(embed=error_embed("Already Exists", f"A node named `{new_name}` already exists."))
        return

    cfg = nodes_data.pop(old_name)
    cfg["name"] = new_name
    nodes_data[new_name] = cfg

    for vl in vps_data.values():
        for v in vl:
            if v.get("node", old_name) == old_name:
                v["node"] = new_name
    save_data()
    await ctx.send(embed=success_embed("Node Renamed", f"Node `{old_name}` is now named **`{new_name}`**. All existing VPS records were updated."))


@bot.command(name="node-remove")
@is_admin()
async def remove_node(ctx, name: str):
    """!node-remove <NodeName> — remove a remote node from the bot"""
    if name not in nodes_data:
        await ctx.send(embed=error_embed("Not Found", f"Node `{name}` not found."))
        return
    if nodes_data[name].get("is_local"):
        await ctx.send(embed=error_embed("Cannot Remove Local Node", "You cannot remove the primary host node (use `!node-rename` to rename it)."))
        return
    del nodes_data[name]
    save_data()
    await ctx.send(embed=success_embed("Node Removed", f"Node `{name}` was removed from the registry."))


@bot.command(name="node-key")
@is_admin()
async def show_node_key(ctx):
    """!node-key — DMs you the bot's SSH public key for manual node pairing"""
    pubkey = ensure_node_ssh_key()
    e = info_embed(
        "🔑 Bot Node SSH Public Key",
        f"Add this line to `/root/.ssh/authorized_keys` on any remote VPS node, then run:\n"
        f"`!node-add <NodeName> <NodeIP> <SSHPort> KEY_ONLY`\n\n"
        f"```\n{pubkey}\n```",
    )
    try:
        await ctx.author.send(embed=e)
        await ctx.send(embed=success_embed("Sent", "Bot SSH public key sent to your DMs."))
    except discord.Forbidden:
        await ctx.send(embed=e)


# ─── VPS creation & management commands (Node-Aware) ───────────────────────

@bot.command(name="create")
@is_admin()
async def create_vps(ctx, user: discord.Member, ram: int, cpu: int, disk: int = 30):
    """!create @user <ram_GB> <cpu_cores> [disk_GB] — asks Node -> OS -> Expiry"""
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
                ssh_port = await docker_utils.next_ssh_port(node_name=node_name)
                expires = "Never" if expiry_days is None else (datetime.utcnow() + timedelta(days=expiry_days)).isoformat()

                try:
                    await create_container(
                        container_name, ram * 1024, cpu, password,
                        disk_gb=disk, os_image=image, ssh_port=ssh_port, node_name=node_name,
                    )

                    vps_data[user_id].append({
                        "container_name": container_name,
                        "node": node_name,
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
                    e.add_field(name="🌐 Node", value=f"`{node_name}`", inline=True)
                    e.add_field(name="🆔 VPS ID", value=f"#{display_number}", inline=True)
                    e.add_field(name="📦 Container", value=f"`{container_name}`", inline=True)
                    e.add_field(name="🖥️ OS", value=os_label, inline=True)
                    e.add_field(name="🧠 RAM", value=f"{ram} GB", inline=True)
                    e.add_field(name="⚙️ CPU", value=f"{cpu} core(s)", inline=True)
                    e.add_field(name="💾 Disk", value=f"{disk} GB", inline=True)
                    e.add_field(name="⏳ Expiry", value=expires if expires == "Never" else f"{expires[:10]}", inline=True)
                    e.add_field(name="🎮 Manage", value="`!manage` → Start / Stop / Restart / Reinstall / SSH", inline=False)
                    await ctx.send(embed=e)

                    try:
                        methods = await get_ssh_access(container_name, node_name=node_name)
                        dm = base_embed("🎉 Your VPS is Ready!", "Connect using the info below.", 0x5865F2)
                        dm.add_field(name="🌐 Node", value=f"`{node_name}`", inline=True)
                        dm.add_field(name="🆔 VPS ID", value=f"#{display_number}", inline=True)
                        dm.add_field(name="🖥️ OS", value=os_label, inline=True)
                        dm.add_field(name="🧠 RAM", value=f"{ram} GB", inline=True)
                        dm.add_field(name="⚙️ CPU", value=f"{cpu} core(s)", inline=True)
                        dm.add_field(name="🔑 SSH (classic)", value=f"```{docker_utils.classic_ssh_command(ssh_port, node_name=node_name)}```\nPassword: ```{password}```", inline=False)
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

    await ctx.send(
        embed=info_embed("🌐 Choose a Node", f"Select which Node to deploy {user.mention}'s VPS on:"),
        view=NodePickerView(ctx.author.id, on_node_picked),
    )


@bot.command(name="manage")
async def manage_vps(ctx, user: discord.Member = None):
    """!manage [@user] — asks which Node to manage (or All Nodes)"""
    target = user if user else ctx.author
    is_admin_mode = bool(user)

    if is_admin_mode and not is_admin_id(ctx.author.id):
        await ctx.send(embed=error_embed("Access Denied", "Only admins can manage another user's VPS."))
        return

    vps_list = vps_data.get(str(target.id), [])
    if not vps_list:
        if is_admin_mode:
            await ctx.send(embed=error_embed("No VPS Found", f"{target.mention} doesn't have any VPS."))
        else:
            e = error_embed("No VPS Found", "You don't have any VPS yet.")
            e.add_field(name="Quick Actions", value="`!plans` — view plans\n`!buywc <plan> <processor>` — purchase", inline=False)
            await ctx.send(embed=e)
        return

    if len(nodes_data) > 1:
        async def _on_manage_node(interaction, picked_node):
            if picked_node == "__ALL__":
                filtered = vps_list
            else:
                filtered = [v for v in vps_list if v.get("node", default_node_name()) == picked_node]
            if not filtered:
                await interaction.response.edit_message(
                    embed=error_embed("No VPS on Node", f"{target.mention} has no VPS on node **`{picked_node}`**."),
                    view=None,
                )
                return
            view = ManageView(ctx.author.id, target.id, filtered, is_admin_view=is_admin_mode)
            await interaction.response.edit_message(embed=view.embed(), view=view)

        await ctx.send(
            embed=info_embed("🌐 Select Node to Manage", f"Which Node do you want to manage for {target.mention}?"),
            view=NodePickerView(ctx.author.id, _on_manage_node, include_all=True),
        )
    else:
        view = ManageView(ctx.author.id, target.id, vps_list, is_admin_view=is_admin_mode)
        await ctx.send(embed=view.embed(), view=view)


async def _perform_vps_deletion(ctx_or_interaction, user: discord.Member, target_vps: dict, reason: str):
    """Shared helper that deletes a specific VPS dict from its node and updates storage."""
    user_id = str(user.id)
    vps_list = vps_data.get(user_id, [])
    container = target_vps["container_name"]
    node_name = target_vps.get("node", default_node_name())
    vps_num = (vps_list.index(target_vps) + 1) if target_vps in vps_list else "?"

    try:
        try:
            await run_docker(f"docker stop {container}", node_name=node_name)
        except Exception:
            pass
        await run_docker(f"docker rm -f {container}", node_name=node_name)
        if target_vps in vps_list:
            vps_list.remove(target_vps)
        if not vps_list and user_id in vps_data:
            del vps_data[user_id]
            guild = ctx_or_interaction.guild
            if guild:
                role = await get_or_create_vps_role(guild)
                if role and role in user.roles:
                    try:
                        await user.remove_roles(role, reason="No VPS ownership left")
                    except discord.Forbidden:
                        pass
        save_data()

        e = success_embed("VPS Deleted")
        e.add_field(name="Owner", value=user.mention, inline=True)
        e.add_field(name="Node", value=f"`{node_name}`", inline=True)
        e.add_field(name="VPS ID", value=f"#{vps_num}", inline=True)
        e.add_field(name="Container", value=f"`{container}`", inline=True)
        e.add_field(name="Reason", value=reason, inline=False)
        return e
    except Exception as ex:
        return error_embed("Deletion Failed", str(ex))


@bot.command(name="delete-vps")
@is_admin()
async def delete_vps(ctx, user: discord.Member, vps_number: int = None, *, reason: str = "No reason given"):
    """
    !delete-vps @user [vps_number] [reason]
    If vps_number is omitted, asks which Node -> which VPS to delete interactively!
    """
    user_id = str(user.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list:
        await ctx.send(embed=error_embed("No VPS", f"{user.mention} does not have any VPS."))
        return

    if vps_number is not None:
        if not (1 <= vps_number <= len(vps_list)):
            await ctx.send(embed=error_embed("Invalid VPS", f"Invalid VPS number (1-{len(vps_list)})."))
            return
        target_vps = vps_list[vps_number - 1]
        node_name = target_vps.get("node", default_node_name())
        msg = await ctx.send(embed=info_embed("Deleting VPS", f"Removing VPS #{vps_number} (`{target_vps['container_name']}`) from Node **`{node_name}`**..."))
        res_embed = await _perform_vps_deletion(ctx, user, target_vps, reason)
        await msg.edit(embed=res_embed)
        return

    # Interactive Node -> VPS deletion flow
    async def _on_node_selected(node_inter, picked_node):
        if picked_node == "__ALL__":
            filtered = list(vps_list)
        else:
            filtered = [v for v in vps_list if v.get("node", default_node_name()) == picked_node]
        if not filtered:
            await node_inter.response.edit_message(
                embed=error_embed("No VPS on Node", f"{user.mention} has no VPS on node **`{picked_node}`**."),
                view=None,
            )
            return

        async def _on_vps_selected(vps_inter, chosen_vps):
            await vps_inter.response.edit_message(
                embed=info_embed("Deleting VPS", f"Removing `{chosen_vps['container_name']}` from Node **`{chosen_vps.get('node', default_node_name())}`**..."),
                view=None,
            )
            res_embed = await _perform_vps_deletion(ctx, user, chosen_vps, reason)
            await vps_inter.message.edit(embed=res_embed, view=None)

        await node_inter.response.edit_message(
            embed=info_embed("🗑️ Select VPS to Delete", f"Pick which of {user.mention}'s VPS instances to delete:"),
            view=DeleteVPSPickerView(ctx.author.id, user, filtered, _on_vps_selected),
        )

    await ctx.send(
        embed=info_embed("🌐 Select Node for Deletion", f"Which Node is {user.mention}'s VPS on?"),
        view=NodePickerView(ctx.author.id, _on_node_selected, include_all=True),
    )


@bot.command(name="manage-shared")
async def manage_shared_vps(ctx, owner: discord.Member, vps_number: int):
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
    await ctx.send(embed=success_embed("VPS Shared", f"VPS #{vps_number} shared with {target.mention}."))
    try:
        await target.send(embed=info_embed(
            "VPS Access Granted",
            f"You now have access to {ctx.author.mention}'s VPS #{vps_number} (Node: `{vps.get('node', default_node_name())}`). "
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


# ─── Credits & purchasing (Node-Aware) ──────────────────────────────────────

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
    """!buywc <plan> <Intel/AMD> — asks Node -> OS, then deploys"""
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
                    embed=error_embed("Insufficient Credits", f"Need {cost} credits."), view=None
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
            ssh_port = await docker_utils.next_ssh_port(node_name=node_name)

            try:
                await create_container(
                    container_name, int(ram_str.replace("GB", "")) * 1024, cpu_str,
                    password, os_image=image, ssh_port=ssh_port, node_name=node_name,
                )

                vps_data[user_id].append({
                    "plan": plan,
                    "node": node_name,
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
                    methods = await get_ssh_access(container_name, node_name=node_name)
                    dm = base_embed("🎉 Your VPS is Ready!", f"Node: **`{node_name}`**\nConnect using the info below.", 0x5865F2)
                    dm.add_field(name="🔑 SSH (classic)", value=f"```{docker_utils.classic_ssh_command(ssh_port, node_name=node_name)}```\nPassword: ```{password}```", inline=False)
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

    await ctx.send(
        embed=info_embed("🌐 Choose a Node", f"Select which Node to deploy your {plan} VPS on:"),
        view=NodePickerView(ctx.author.id, on_node_picked),
    )


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
            node_name = v.get("node", default_node_name())
            icon = "🟢" if v.get("status") == "running" else "🔴"
            note = f" — _{v['note']}_" if v.get("note") else ""
            lines.append(f"{icon} **{nickname}** (`{v['container_name']}` on `{node_name}`){note}")
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
    node_name = vps.get("node", default_node_name())
    msg = await ctx.send(embed=info_embed("Pinging...", f"Checking `{container}` on `{node_name}`..."))
    start = time.time()
    try:
        out = await run_docker(f"docker inspect --format={{{{.State.Running}}}} {container}", timeout=10, node_name=node_name)
        elapsed_ms = int((time.time() - start) * 1000)
        if out.strip() == "true":
            await msg.edit(embed=success_embed("🏓 Pong!", f"Alive on `{node_name}` — response `{elapsed_ms}ms`"))
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
    node_name = vps.get("node", default_node_name())
    try:
        started_at_str = await run_docker(f"docker inspect --format={{{{.State.StartedAt}}}} {container}", timeout=10, node_name=node_name)
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
    e.add_field(name="🌐 Nodes", value=f"{len(nodes_data)} ({', '.join(nodes_data.keys())})", inline=True)
    e.add_field(name="🖥️ Total VPS", value=f"{total} ({running} running)", inline=True)
    e.add_field(name="👥 Users", value=str(len(user_data)), inline=True)
    e.add_field(name="🔧 Maintenance", value="🔴 ON" if config.maintenance_mode else "🟢 OFF", inline=True)
    e.add_field(name="📡 Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    await ctx.send(embed=e)


# ─── Admin tools (Node-Aware) ───────────────────────────────────────────────

@bot.command(name="list-all")
@is_admin()
async def list_all_vps(ctx):
    total = running = 0
    summary, details = [], []
    for user_id, vps_list in vps_data.items():
        try:
            user = await bot.fetch_user(int(user_id))
            name = user.name
        except Exception:
            name = f"Unknown ({user_id})"
        r = sum(1 for v in vps_list if v.get("status") == "running")
        total += len(vps_list)
        running += r
        summary.append(f"**{name}** — {len(vps_list)} VPS ({r} running)")
        for i, v in enumerate(vps_list):
            icon = "🟢" if v.get("status") == "running" else "🔴"
            node_name = v.get("node", default_node_name())
            details.append(f"{icon} {name} — VPS {i + 1}: `{v['container_name']}` [`{node_name}`] — {v.get('status', '?').upper()}")

    e = base_embed("All VPS Information", "")
    e.add_field(name="System Overview", value=f"Nodes: {len(nodes_data)}\nUsers: {len(vps_data)}\nTotal VPS: {total}\nRunning: {running}\nStopped: {total - running}", inline=False)
    if summary:
        e.add_field(name="User Summary", value="\n".join(summary[:10]), inline=False)
    for i in range(0, min(len(details), 30), 15):
        chunk = details[i:i + 15]
        e.add_field(name=f"Deployments ({i + 1}-{min(i + 15, len(details))})", value="\n".join(chunk), inline=False)
    await ctx.send(embed=e)


@bot.command(name="userinfo")
@is_admin()
async def user_info(ctx, user: discord.Member):
    user_id = str(user.id)
    credits = user_data.get(user_id, {}).get("credits", 0)
    e = base_embed(f"👤 User Info — {user.name}", "")
    e.add_field(name="User", value=f"{user.mention}\nID: {user.id}", inline=False)
    e.add_field(name="💰 Credits", value=str(credits), inline=True)
    e.add_field(name="🛡️ Admin", value="Yes" if is_admin_id(user_id) else "No", inline=True)
    vps_list = vps_data.get(user_id, [])
    if vps_list:
        e.add_field(name="🖥️ VPS", value="\n".join(
            f"VPS {i + 1}: `{v['container_name']}` [`{v.get('node', default_node_name())}`] — {v.get('status', '?').upper()}"
            for i, v in enumerate(vps_list)
        ), inline=False)
    else:
        e.add_field(name="🖥️ VPS", value="None", inline=False)
    await ctx.send(embed=e)


@bot.command(name="serverstats")
@is_admin()
async def server_stats(ctx):
    total = sum(len(v) for v in vps_data.values())
    running = sum(1 for vl in vps_data.values() for v in vl if v.get("status") == "running")
    total_credits = sum(u.get("credits", 0) for u in user_data.values())
    total_ram = sum(int(str(v["ram"]).replace("GB", "")) for vl in vps_data.values() for v in vl)
    total_cpu = sum(int(v["cpu"]) for vl in vps_data.values() for v in vl)
    e = base_embed("📊 Server Statistics", "")
    e.add_field(name="🌐 Active Nodes", value=", ".join(f"`{k}` ({v.get('ip', '?')})" for k, v in nodes_data.items()), inline=False)
    e.add_field(name="👥 Users", value=f"Total: {len(user_data)}\nAdmins: {len(admin_data.get('admins', []))}", inline=False)
    e.add_field(name="🖥️ VPS", value=f"Total: {total}\nRunning: {running}\nStopped: {total - running}", inline=False)
    e.add_field(name="💰 Economy", value=f"Total credits in circulation: {total_credits}", inline=False)
    e.add_field(name="📈 Allocated Resources", value=f"Total RAM: {total_ram}GB\nTotal CPU: {total_cpu} cores", inline=False)
    await ctx.send(embed=e)


@bot.command(name="exec")
@is_admin()
async def execute_command(ctx, container_name: str, *, command: str):
    _, found = _find_vps_by_container(container_name)
    node_name = found.get("node", default_node_name()) if found else default_node_name()
    await ctx.send(embed=info_embed("Executing", f"Running in `{container_name}` on Node **`{node_name}`**..."))
    try:
        stdout, stderr, rc = await docker_exec(container_name, command, timeout=30, node_name=node_name)
        e = base_embed(f"Output — {container_name} ({node_name})", f"`{command}`")
        if stdout:
            e.add_field(name="📤 stdout", value=f"```\n{stdout[:1000]}\n```", inline=False)
        if stderr:
            e.add_field(name="⚠️ stderr", value=f"```\n{stderr[:1000]}\n```", inline=False)
        e.add_field(name="🔄 Exit Code", value=str(rc), inline=False)
        await ctx.send(embed=e)
    except Exception as e:
        await ctx.send(embed=error_embed("Execution Failed", str(e)))


@bot.command(name="restart-vps")
@is_admin()
async def restart_vps(ctx, container_name: str):
    _, found = _find_vps_by_container(container_name)
    node_name = found.get("node", default_node_name()) if found else default_node_name()
    await ctx.send(embed=info_embed("Restarting", f"Restarting `{container_name}` on Node **`{node_name}`**..."))
    try:
        await run_docker(f"docker restart {container_name}", node_name=node_name)
        await asyncio.sleep(3)
        await docker_exec(container_name, "/usr/sbin/sshd || true", timeout=10, node_name=node_name)
        if found:
            found["status"] = "running"
            save_data()
        await ctx.send(embed=success_embed("VPS Restarted", f"`{container_name}` restarted on `{node_name}`."))
    except Exception as e:
        await ctx.send(embed=error_embed("Restart Failed", str(e)))


@bot.command(name="backup-vps")
@is_admin()
async def backup_vps(ctx, container_name: str):
    _, found = _find_vps_by_container(container_name)
    node_name = found.get("node", default_node_name()) if found else default_node_name()
    snapshot = f"{container_name}-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    await ctx.send(embed=info_embed("Creating Backup", f"Committing snapshot of `{container_name}` on `{node_name}`..."))
    try:
        await run_docker(f"docker commit {container_name} {snapshot}", node_name=node_name)
        await ctx.send(embed=success_embed("Backup Created", f"Image `{snapshot}` created on `{node_name}`."))
    except Exception as e:
        await ctx.send(embed=error_embed("Backup Failed", str(e)))


@bot.command(name="restore-vps")
@is_admin()
async def restore_vps(ctx, container_name: str, snapshot_name: str):
    _, found = _find_vps_by_container(container_name)
    if not found:
        await ctx.send(embed=error_embed("Not Found", f"No VPS data for `{container_name}`."))
        return

    node_name = found.get("node", default_node_name())
    ram_mb = int(str(found.get("ram", "4GB")).replace("GB", "")) * 1024
    cpu_cnt = found.get("cpu", "1")
    ssh_port = found.get("ssh_port")
    port_flag = f"-p {ssh_port}:22 " if ssh_port else ""

    await ctx.send(embed=info_embed("Restoring", f"Restoring `{container_name}` from `{snapshot_name}` on `{node_name}`..."))
    try:
        try:
            await run_docker(f"docker stop {container_name}", node_name=node_name)
        except Exception:
            pass
        await run_docker(f"docker rm -f {container_name}", node_name=node_name)
        await run_docker(
            f"docker run -d --name {container_name} --hostname {container_name} {port_flag}"
            f"--memory={ram_mb}m --cpus={cpu_cnt} --restart=unless-stopped {snapshot_name} sleep infinity",
            node_name=node_name,
        )
        await asyncio.sleep(2)
        await docker_exec(container_name, "/usr/sbin/sshd || true", timeout=10, node_name=node_name)
        found["status"] = "running"
        save_data()
        await ctx.send(embed=success_embed("VPS Restored", f"`{container_name}` restored from `{snapshot_name}` on `{node_name}`."))
    except Exception as e:
        await ctx.send(embed=error_embed("Restore Failed", str(e)))


@bot.command(name="stop-vps-all")
@is_admin()
async def stop_all_vps(ctx):
    class ConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)

        @discord.ui.button(label="Stop All VPS", style=discord.ButtonStyle.danger)
        async def confirm(self, interaction: discord.Interaction, _button):
            await interaction.response.defer()
            stopped, errors = 0, []
            for vl in vps_data.values():
                for v in vl:
                    if v.get("status") == "running":
                        try:
                            n_name = v.get("node", default_node_name())
                            await run_docker(f"docker stop {v['container_name']}", node_name=n_name)
                            v["status"] = "stopped"
                            stopped += 1
                        except Exception as e:
                            errors.append(str(e))
            save_data()
            e = success_embed("All VPS Stopped", f"Stopped {stopped} container(s) across all nodes.")
            if errors:
                e.add_field(name="Errors", value="\n".join(errors[:5]), inline=False)
            await interaction.followup.send(embed=e)

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, _button):
            await interaction.response.edit_message(embed=info_embed("Cancelled", "No changes made."), view=None)

    await ctx.send(embed=warning_embed("Stop All VPS", "⚠️ This will stop every running VPS across all nodes. Continue?"), view=ConfirmView())


@bot.command(name="cpu-monitor")
@is_admin()
async def cpu_monitor_control(ctx, action: str = "status"):
    action = action.lower()
    if action == "status":
        e = base_embed("CPU Monitor Status", f"Status: **{'Active' if config.cpu_monitor_active else 'Inactive'}**")
        e.add_field(name="Threshold", value=f"{CPU_THRESHOLD}%", inline=True)
        e.add_field(name="Check Interval", value=f"{CPU_CHECK_INTERVAL}s", inline=True)
        await ctx.send(embed=e)
    elif action == "enable":
        config.cpu_monitor_active = True
        await ctx.send(embed=success_embed("CPU Monitor Enabled"))
    elif action == "disable":
        config.cpu_monitor_active = False
        await ctx.send(embed=warning_embed("CPU Monitor Disabled"))
    else:
        await ctx.send(embed=error_embed("Invalid Action", "Use: `!cpu-monitor <status|enable|disable>`"))


@bot.command(name="announce")
@is_admin()
async def announce(ctx, *, message: str):
    sent = failed = 0
    e = base_embed("📢 Announcement", message, 0xFFAA00)
    e.add_field(name="From", value=f"ApexHosting Team ({ctx.author.mention})", inline=False)
    status = await ctx.send(embed=info_embed("Sending", "Broadcasting to all VPS owners..."))
    for uid in vps_data.keys():
        try:
            user = await bot.fetch_user(int(uid))
            await user.send(embed=e)
            sent += 1
            await asyncio.sleep(0.5)
        except Exception:
            failed += 1
    await status.edit(embed=success_embed("Announcement Sent", f"✅ Delivered: {sent}\n❌ Failed: {failed}"))


@bot.command(name="maintenance")
@is_admin()
async def maintenance_toggle(ctx, mode: str):
    mode = mode.lower()
    if mode == "on":
        config.maintenance_mode = True
        await bot.change_presence(status=discord.Status.idle, activity=discord.Activity(type=discord.ActivityType.watching, name="🔴 Under Maintenance"))
        await ctx.send(embed=warning_embed("🔴 Maintenance Mode ON", "Non-admin commands are now blocked."))
    elif mode == "off":
        config.maintenance_mode = False
        await bot.change_presence(status=discord.Status.online, activity=discord.Activity(type=discord.ActivityType.watching, name=f"ApexHosting | {len(nodes_data)} Node(s)"))
        await ctx.send(embed=success_embed("🟢 Maintenance Mode OFF", "Bot is back to normal."))
    else:
        await ctx.send(embed=error_embed("Invalid", "Use: `!maintenance on` or `!maintenance off`"))


@bot.command(name="admin-add")
@is_main_admin()
async def admin_add(ctx, user: discord.Member):
    user_id = str(user.id)
    admin_data.setdefault("admins", [])
    if user_id not in admin_data["admins"]:
        admin_data["admins"].append(user_id)
        save_data()
    await ctx.send(embed=success_embed("Admin Added", f"{user.mention} is now an admin."))


@bot.command(name="admin-remove")
@is_main_admin()
async def admin_remove(ctx, user: discord.Member):
    user_id = str(user.id)
    admins = admin_data.setdefault("admins", [])
    if user_id in admins:
        admins.remove(user_id)
        save_data()
    await ctx.send(embed=success_embed("Admin Removed", f"{user.mention} is no longer an admin."))


@bot.command(name="admin-list")
@is_main_admin()
async def admin_list(ctx):
    lines = []
    for aid in admin_data.get("admins", []):
        try:
            u = await bot.fetch_user(int(aid))
            lines.append(f"• {u.mention} ({u.name})")
        except Exception:
            lines.append(f"• Unknown ({aid})")
    await ctx.send(embed=info_embed("Admin List", "\n".join(lines) if lines else "No admins"))


# ─── Invite rewards (Node-Aware) ───────────────────────────────────────────

@bot.command(name="myinvites")
async def my_invites(ctx):
    data = invite_data.get(str(ctx.author.id), {"invites": 0, "claimed_tier": None})
    n = data["invites"]
    tier = _tier_for_invites(n)
    next_threshold = next((t for t, _ in config.INVITE_TIERS if t > n), None)
    e = base_embed("🚀 VPS Invite Rewards", f"{ctx.author.mention}'s progress", 0x00FF88)
    e.add_field(name="Invites", value=str(n), inline=True)
    e.add_field(name="Unlocked Tier", value=f"{tier} invites" if tier else "None yet", inline=True)
    e.add_field(name="Claimed", value=f"{data['claimed_tier']} invites" if data.get("claimed_tier") else "Not claimed", inline=True)
    if next_threshold:
        e.add_field(name="Next Tier", value=f"{next_threshold - n} more invite(s) to go", inline=False)
    if tier and data.get("claimed_tier") != tier:
        e.add_field(name="🎁", value="You have an unclaimed reward — run `!claimvps`!", inline=False)
    await ctx.send(embed=e)


@bot.command(name="claimvps")
async def claim_vps(ctx):
    data = invite_data.setdefault(str(ctx.author.id), {"invites": 0, "claimed_tier": None})
    n = data["invites"]
    tier = _tier_for_invites(n)
    if not tier:
        await ctx.send(embed=error_embed("Not Enough Invites", f"You need at least {config.INVITE_TIERS[0][0]} invites. You have {n}."))
        return
    if data.get("claimed_tier") == tier:
        await ctx.send(embed=error_embed("Already Claimed", f"You've already claimed the {tier}-invite reward. Invite more to unlock the next tier."))
        return

    spec = dict(next(s for t, s in config.INVITE_TIERS if t == tier))

    async def on_node_picked(node_interaction, node_name):
        async def on_os_picked(os_interaction, image, os_label):
            if data.get("claimed_tier") == tier:
                await os_interaction.response.edit_message(
                    embed=error_embed("Already Claimed", f"You already claimed the {tier}-invite tier."), view=None
                )
                return

            data["claimed_tier"] = tier
            save_data()

            await os_interaction.response.edit_message(
                embed=info_embed("Deploying Reward VPS", f"Building your {tier}-invite VPS on Node **`{node_name}`** (**{os_label}**)..."),
                view=None,
            )
            user_id = str(ctx.author.id)
            vps_data.setdefault(user_id, [])
            display_number = len(vps_data[user_id]) + 1
            container_name = f"vps-{user_id}-{next_container_id(user_id)}"
            password = generate_password()
            ssh_port = await docker_utils.next_ssh_port(node_name=node_name)
            try:
                await create_container(
                    container_name, spec["ram"] * 1024, spec["cpu"], password,
                    disk_gb=spec["storage"], os_image=image, ssh_port=ssh_port, node_name=node_name,
                )
                vps_data[user_id].append({
                    "container_name": container_name,
                    "node": node_name,
                    "ram": f"{spec['ram']}GB",
                    "cpu": str(spec["cpu"]),
                    "storage": f"{spec['storage']}GB",
                    "status": "running",
                    "created_at": datetime.now().isoformat(),
                    "expires": "Never",
                    "auto_renew": False,
                    "ssh_password": password,
                    "ssh_port": ssh_port,
                    "os_image": image,
                    "os_label": os_label,
                    "plan": f"Invite Reward ({tier})",
                    "shared_with": [],
                })
                save_data()

                if ctx.guild:
                    role = await get_or_create_vps_role(ctx.guild)
                    if role:
                        try:
                            await ctx.author.add_roles(role, reason="VPS invite reward claimed")
                        except discord.Forbidden:
                            pass

                e = success_embed("🎉 Reward Claimed!", f"Your {tier}-invite VPS is live on **`{node_name}`**.")
                e.add_field(name="Node", value=f"`{node_name}`", inline=True)
                e.add_field(name="VPS ID", value=f"#{display_number}", inline=True)
                e.add_field(name="RAM", value=f"{spec['ram']} GB", inline=True)
                e.add_field(name="CPU", value=f"{spec['cpu']} core(s)", inline=True)
                e.add_field(name="Storage", value=f"{spec['storage']} GB", inline=True)
                await ctx.send(embed=e)

                try:
                    methods = await get_ssh_access(container_name, node_name=node_name)
                    dm = base_embed("🎉 Your Reward VPS is Ready!", f"Node: **`{node_name}`**\nConnect using the info below.", 0x5865F2)
                    dm.add_field(name="🔑 SSH (classic)", value=f"```{docker_utils.classic_ssh_command(ssh_port, node_name=node_name)}```\nPassword: ```{password}```", inline=False)
                    if "sshx" in methods:
                        dm.add_field(name="🔗 SSHX (browser link)", value=f"```{methods['sshx']}```", inline=False)
                    dm.add_field(name="🎮 Manage", value="`!manage` in the server", inline=False)
                    await ctx.author.send(embed=dm)
                except discord.Forbidden:
                    pass
            except Exception as e:
                data["claimed_tier"] = None
                save_data()
                await ctx.send(embed=error_embed("Deployment Failed", str(e)))

        await node_interaction.response.edit_message(
            embed=info_embed("Choose an OS", f"Selected Node: **`{node_name}`**\nPick the OS for your {tier}-invite reward VPS:"),
            view=OSPickerView(ctx.author.id, on_os_picked),
        )

    await ctx.send(
        embed=info_embed("🌐 Choose a Node", f"Select which Node to deploy your {tier}-invite reward VPS on:"),
        view=NodePickerView(ctx.author.id, on_node_picked),
    )


@bot.command(name="cancel-renew")
@is_admin()
async def cancel_renew(ctx, user: discord.Member, vps_number: int):
    vps_list = vps_data.get(str(user.id), [])
    if not vps_list or not (1 <= vps_number <= len(vps_list)):
        await ctx.send(embed=error_embed("Not Found", f"{user.mention} has no VPS #{vps_number}."))
        return
    vps_list[vps_number - 1]["auto_renew"] = False
    save_data()
    await ctx.send(embed=success_embed("Auto-Renew Cancelled", f"VPS #{vps_number} will stop instead of auto-renewing at its next expiry."))


@bot.command(name="enable-renew")
@is_admin()
async def enable_renew(ctx, user: discord.Member, vps_number: int):
    vps_list = vps_data.get(str(user.id), [])
    if not vps_list or not (1 <= vps_number <= len(vps_list)):
        await ctx.send(embed=error_embed("Not Found", f"{user.mention} has no VPS #{vps_number}."))
        return
    vps_list[vps_number - 1]["auto_renew"] = True
    save_data()
    await ctx.send(embed=success_embed("Auto-Renew Enabled", f"VPS #{vps_number} will auto-renew by {config.RENEW_EXTEND_DAYS} days at expiry."))


# ─── Expiry system ──────────────────────────────────────────────────────────

@bot.command(name="setexpire")
@is_admin()
async def set_expire(ctx, user: discord.Member, vps_number: int, days: int):
    vps_list = vps_data.get(str(user.id), [])
    if not vps_list or not (1 <= vps_number <= len(vps_list)):
        await ctx.send(embed=error_embed("Not Found", f"{user.mention} has no VPS #{vps_number}."))
        return
    exp = (datetime.utcnow() + timedelta(days=days)).isoformat()
    vps_list[vps_number - 1]["expires"] = exp
    save_data()
    await ctx.send(embed=success_embed("Expiry Set", f"{user.mention}'s VPS #{vps_number} expires on `{exp[:10]}` ({days}d)."))
    try:
        await user.send(embed=warning_embed("⏳ VPS Expiry Set", f"Your VPS #{vps_number} expires on **{exp[:10]}**."))
    except discord.Forbidden:
        pass


@bot.command(name="extendexpire")
@is_admin()
async def extend_expire(ctx, user: discord.Member, vps_number: int, days: int):
    vps_list = vps_data.get(str(user.id), [])
    if not vps_list or not (1 <= vps_number <= len(vps_list)):
        await ctx.send(embed=error_embed("Not Found", f"{user.mention} has no VPS #{vps_number}."))
        return
    vps = vps_list[vps_number - 1]
    current = vps.get("expires", "Never")
    base = datetime.utcnow()
    if current and current != "Never":
        try:
            parsed = datetime.fromisoformat(current)
            base = max(parsed, base)
        except Exception:
            pass
    new_exp = (base + timedelta(days=days)).isoformat()
    vps["expires"] = new_exp
    save_data()
    await ctx.send(embed=success_embed("Expiry Extended", f"VPS #{vps_number} extended by {days}d. New expiry: `{new_exp[:10]}`"))
    try:
        await user.send(embed=success_embed("✅ VPS Extended", f"Your VPS #{vps_number} was extended to **{new_exp[:10]}**."))
    except discord.Forbidden:
        pass


@bot.command(name="removeexpire")
@is_admin()
async def remove_expire(ctx, user: discord.Member, vps_number: int):
    vps_list = vps_data.get(str(user.id), [])
    if not vps_list or not (1 <= vps_number <= len(vps_list)):
        await ctx.send(embed=error_embed("Not Found", f"{user.mention} has no VPS #{vps_number}."))
        return
    vps_list[vps_number - 1]["expires"] = "Never"
    save_data()
    await ctx.send(embed=success_embed("Expiry Removed", f"VPS #{vps_number} now never expires."))


@bot.command(name="checkexpire")
async def check_expire(ctx, user: discord.Member = None):
    target = user if (user and is_admin_id(ctx.author.id)) else ctx.author
    vps_list = vps_data.get(str(target.id), [])
    if not vps_list:
        await ctx.send(embed=error_embed("Not Found", f"{target.mention} has no VPS."))
        return
    e = info_embed(f"⏳ VPS Expiry — {target.display_name}", "")
    for i, vps in enumerate(vps_list):
        expires = vps.get("expires", "Never")
        node_name = vps.get("node", default_node_name())
        if expires and expires != "Never":
            try:
                days_left = (datetime.fromisoformat(expires) - datetime.utcnow()).days
                if days_left < 0:
                    status = f"❌ EXPIRED {abs(days_left)}d ago"
                elif days_left <= 3:
                    status = f"⚠️ Expires in {days_left}d — {expires[:10]}"
                else:
                    status = f"✅ Expires {expires[:10]} ({days_left}d left)"
            except Exception:
                status = expires
        else:
            status = "♾️ Never"
        e.add_field(name=f"VPS #{i + 1} — `{vps['container_name']}` [`{node_name}`]", value=status, inline=False)
    await ctx.send(embed=e)


@tasks.loop(hours=24)
async def auto_expire_check():
    now = datetime.utcnow()
    for user_id, vps_list in list(vps_data.items()):
        for vps in vps_list:
            expires = vps.get("expires", "Never")
            if not expires or expires == "Never":
                continue
            try:
                days_left = (datetime.fromisoformat(expires) - now).days
            except Exception:
                continue

            container = vps["container_name"]
            node_name = vps.get("node", default_node_name())

            try:
                if days_left in config.EXPIRY_WARN_DAYS:
                    u = await bot.fetch_user(int(user_id))

                    async def on_renew(interaction, _vps=vps):
                        new_exp = (datetime.utcnow() + timedelta(days=config.RENEW_EXTEND_DAYS)).isoformat()
                        _vps["expires"] = new_exp
                        save_data()
                        await interaction.response.edit_message(
                            embed=success_embed("✅ Renewed", f"`{_vps['container_name']}` extended to `{new_exp[:10]}`."),
                            view=None,
                        )

                    e = warning_embed(
                        "⏳ VPS Expiring Soon" if days_left > 1 else "🚨 VPS Expires Tomorrow",
                        f"`{container}` (Node: `{node_name}`) expires in **{days_left} day{'s' if days_left != 1 else ''}** ({expires[:10]}).\n"
                        f"Click below to renew now, or it will auto-renew by {config.RENEW_EXTEND_DAYS} days on the expiry date "
                        f"unless an admin cancels auto-renew for it.",
                    )
                    await u.send(embed=e, view=RenewView(on_renew))

                elif days_left < 0:
                    u = await bot.fetch_user(int(user_id))
                    if vps.get("auto_renew", True):
                        new_exp = (datetime.utcnow() + timedelta(days=config.RENEW_EXTEND_DAYS)).isoformat()
                        vps["expires"] = new_exp
                        await u.send(embed=success_embed("🔁 Auto-Renewed", f"`{container}` on `{node_name}` was automatically renewed to `{new_exp[:10]}`."))
                    else:
                        if vps.get("status") == "running":
                            try:
                                await run_docker(f"docker stop {container}", node_name=node_name)
                            except Exception:
                                pass
                            vps["status"] = "stopped"
                        await u.send(embed=error_embed("❌ VPS Expired", f"`{container}` on `{node_name}` has expired and been stopped."))
            except Exception:
                continue
    save_data()


# ─── Help ───────────────────────────────────────────────────────────────────

def build_help_pages(user_is_admin: bool, user_is_main_admin: bool):
    pages = {}

    user_embed = base_embed("👤 User Commands", "VPS management for everyone:", 0x00FF88)
    user_embed.add_field(name="🖥️ VPS", value=(
        "`!manage` — manage your VPS (select Node & VPS)\n"
        "`!manage @user` — (admin) manage another user's VPS\n"
        "`!share-user @user <#>` / `!share-ruser @user <#>`\n"
        "`!manage-shared @owner <#>`"
    ), inline=False)
    user_embed.add_field(name="🏷️ Tools & Invites", value=(
        "`!rename-vps <#> <name>` · `!vps-note <#> <text>`\n"
        "`!ping-vps <#>` · `!uptime-vps <#>` · `!myinfo`\n"
        "`!myinvites` · `!claimvps`"
    ), inline=False)
    pages["user"] = user_embed

    credits_embed = base_embed("💰 Credits & Plans", "", 0xFFAA00)
    credits_embed.add_field(name="Commands", value=(
        "`!plans` · `!buyc` · `!buywc <plan> <Intel/AMD>`\n"
        "`!credits` · `!transfer @user <amount>` · `!leaderboard`"
    ), inline=False)
    pages["credits"] = credits_embed

    extras_embed = base_embed("📢 Extras", "", 0xFF6B9D)
    extras_embed.add_field(name="Info", value="`!help` · `!botstatus` · `!checkexpire`", inline=False)
    pages["extras"] = extras_embed

    if user_is_admin:
        admin_embed = base_embed("🛡️ Admin Panel", "", 0xFF3366)
        admin_embed.add_field(name="🌐 Multi-Node System", value=(
            "`!nodes` — list all nodes & live hardware status\n"
            "`!node-add <Name> <IP> [port] [root_pass]` — pair a second VPS node\n"
            "`!node-rename <OldName> <NewName>` — rename any node\n"
            "`!node-remove <Name>` · `!node-key`"
        ), inline=False)
        admin_embed.add_field(name="VPS Control", value=(
            "`!create @user <ram_GB> <cpu_cores> <disk_GB>`\n"
            "`!delete-vps @user [#] [reason]` (omit `#` for interactive Node/VPS picker)\n"
            "`!restart-vps <container>` · `!stop-vps-all` · `!exec <container> <cmd>`"
        ), inline=False)
        admin_embed.add_field(name="Backup & Expiry", value=(
            "`!backup-vps <container>` · `!restore-vps <container> <snapshot>`\n"
            "`!setexpire @user <#> <days>` · `!extendexpire @user <#> <days>`\n"
            "`!removeexpire @user <#>` · `!cancel-renew @user <#>` · `!enable-renew @user <#>`"
        ), inline=False)
        admin_embed.add_field(name="Info & Economy", value=(
            "`!userinfo @user` · `!serverstats` · `!list-all`\n"
            "`!adminc @user <amount>` · `!adminrc @user <amount|all>`\n"
            "`!announce <msg>` · `!cpu-monitor <status|enable|disable>` · `!maintenance <on|off>`"
        ), inline=False)
        pages["admin"] = admin_embed

    if user_is_main_admin:
        main_embed = base_embed("👑 Main Admin", "", 0xFFD700)
        main_embed.add_field(name="Admin Management", value="`!admin-add @user` · `!admin-remove @user` · `!admin-list`", inline=False)
        pages["mainadmin"] = main_embed

    return pages


@bot.command(name="help")
async def show_help(ctx):
    user_admin = is_admin_id(ctx.author.id)
    user_main_admin = str(ctx.author.id) == str(MAIN_ADMIN_ID)
    pages = build_help_pages(user_admin, user_main_admin)

    options = [
        discord.SelectOption(label="👤 User Commands", value="user", emoji="👤"),
        discord.SelectOption(label="💰 Credits & Plans", value="credits", emoji="💰"),
        discord.SelectOption(label="📢 Extras", value="extras", emoji="📢"),
    ]
    if user_admin:
        options.append(discord.SelectOption(label="🛡️ Admin Panel", value="admin", emoji="🛡️"))
    if user_main_admin:
        options.append(discord.SelectOption(label="👑 Main Admin", value="mainadmin", emoji="👑"))

    class HelpSelect(discord.ui.Select):
        def __init__(self):
            super().__init__(placeholder="📂 Select a category...", options=options)

        async def callback(self, interaction: discord.Interaction):
            await interaction.response.edit_message(embed=pages[self.values[0]], view=self.view)

    class HelpView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=180)
            self.add_item(HelpSelect())

    await ctx.send(embed=pages["user"], view=HelpView())


# ─── Entrypoint ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not TOKEN:
        raise SystemExit(
            "Set the DISCORD_BOT_TOKEN environment variable before running "
            "(never hardcode your token in the source file)."
        )
    bot.run(TOKEN)
