"""Bot entrypoint: events, all commands, and run()."""
import asyncio
import os
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

from config import bot, MAIN_ADMIN_ID, BOT_START_TIME, logger
import config
import storage
from storage import (
    user_data, vps_data, admin_data, save_data, is_admin_id, is_admin, is_main_admin,
    generate_password, next_container_id,
    base_embed, success_embed, error_embed, info_embed, warning_embed,
)
from docker_utils import (
    run_docker, docker_exec, create_container, get_tmate_session,
    get_or_create_vps_role, PLANS,
)
from views import ManageView, ReinstallConfirmView
import threading
import docker_utils

threading.Thread(target=docker_utils.cpu_monitor_loop, daemon=True).start()

# ─── Bot events ─────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    logger.info("%s connected to Discord", bot.user)
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="PrimeCloud | VPS Manager"))
    if not auto_expire_check.is_running():
        auto_expire_check.start()


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


# ─── VPS creation & management commands ────────────────────────────────────

@bot.command(name="create")
@is_admin()
async def create_vps(ctx, user: discord.Member, ram: int, cpu: int, disk: int = 30):
    """!create @user <ram_GB> <cpu_cores> <disk_GB> — create a custom VPS (admin only)"""
    if ram <= 0 or cpu <= 0 or disk <= 0:
        await ctx.send(embed=error_embed("Invalid Specs", "RAM, CPU and disk must all be positive integers."))
        return

    user_id = str(user.id)
    vps_data.setdefault(user_id, [])
    display_number = len(vps_data[user_id]) + 1
    container_name = f"vps-{user_id}-{next_container_id(user_id)}"
    password = generate_password()

    await ctx.send(embed=info_embed("Creating VPS", f"Deploying for {user.mention}: `{ram}GB` RAM, `{cpu}` core(s), `{disk}GB` disk..."))

    try:
        await create_container(container_name, ram * 1024, cpu, password, disk_gb=disk)

        vps_data[user_id].append({
            "container_name": container_name,
            "ram": f"{ram}GB",
            "cpu": str(cpu),
            "storage": f"{disk}GB",
            "status": "running",
            "created_at": datetime.now().isoformat(),
            "expires": "Never",
            "ssh_password": password,
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

        e = base_embed("🚀 VPS Deployed!", f"{user.mention}'s VPS is live — SSH info sent via DM.", 0x00FF88)
        e.add_field(name="👤 Owner", value=user.mention, inline=True)
        e.add_field(name="🆔 VPS ID", value=f"#{display_number}", inline=True)
        e.add_field(name="📦 Container", value=f"`{container_name}`", inline=True)
        e.add_field(name="🧠 RAM", value=f"{ram} GB", inline=True)
        e.add_field(name="⚙️ CPU", value=f"{cpu} core(s)", inline=True)
        e.add_field(name="💾 Disk", value=f"{disk} GB", inline=True)
        e.add_field(name="🎮 Manage", value="`!manage` → Start / Stop / Reinstall / SSH", inline=False)
        await ctx.send(embed=e)

        try:
            tmate_cmd = await get_tmate_session(container_name)
            dm = base_embed("🎉 Your VPS is Ready!", "Connect using the command below.", 0x5865F2)
            dm.add_field(name="🆔 VPS ID", value=f"#{display_number}", inline=True)
            dm.add_field(name="🧠 RAM", value=f"{ram} GB", inline=True)
            dm.add_field(name="⚙️ CPU", value=f"{cpu} core(s)", inline=True)
            dm.add_field(name="🔗 SSH Command", value=f"```{tmate_cmd}```", inline=False)
            dm.add_field(name="📌 How to Connect", value="1️⃣ Copy the command\n2️⃣ Paste in a terminal\n3️⃣ You're in!", inline=False)
            dm.add_field(name="🎮 Manage", value="`!manage` in the server", inline=False)
            await user.send(embed=dm)
        except discord.Forbidden:
            pass

    except Exception as e:
        await ctx.send(embed=error_embed("Creation Failed", str(e)))


@bot.command(name="manage")
async def manage_vps(ctx, user: discord.Member = None):
    """!manage [@user] — manage your VPS, or (admin only) another user's"""
    if user:
        if not is_admin_id(ctx.author.id):
            await ctx.send(embed=error_embed("Access Denied", "Only admins can manage another user's VPS."))
            return
        vps_list = vps_data.get(str(user.id), [])
        if not vps_list:
            await ctx.send(embed=error_embed("No VPS Found", f"{user.mention} doesn't have any VPS."))
            return
        view = ManageView(ctx.author.id, user.id, vps_list, is_admin_view=True)
        await ctx.send(embed=view.embed(), view=view)
    else:
        vps_list = vps_data.get(str(ctx.author.id), [])
        if not vps_list:
            e = error_embed("No VPS Found", "You don't have any VPS yet.")
            e.add_field(name="Quick Actions", value="`!plans` — view plans\n`!buywc <plan> <processor>` — purchase", inline=False)
            await ctx.send(embed=e)
            return
        view = ManageView(ctx.author.id, ctx.author.id, vps_list)
        await ctx.send(embed=view.embed(), view=view)


@bot.command(name="delete-vps")
@is_admin()
async def delete_vps(ctx, user: discord.Member, vps_number: int, *, reason: str = "No reason given"):
    """!delete-vps @user <#> [reason] — delete a user's VPS (admin only)"""
    user_id = str(user.id)
    vps_list = vps_data.get(user_id, [])
    if not vps_list or not (1 <= vps_number <= len(vps_list)):
        await ctx.send(embed=error_embed("Invalid VPS", "Invalid VPS number, or user has no VPS."))
        return

    vps = vps_list[vps_number - 1]
    container = vps["container_name"]
    await ctx.send(embed=info_embed("Deleting VPS", f"Removing VPS #{vps_number}..."))

    try:
        try:
            await run_docker(f"docker stop {container}")
        except Exception:
            pass
        await run_docker(f"docker rm -f {container}")
        del vps_list[vps_number - 1]
        if not vps_list:
            del vps_data[user_id]
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
        e.add_field(name="VPS ID", value=f"#{vps_number}", inline=True)
        e.add_field(name="Container", value=f"`{container}`", inline=True)
        e.add_field(name="Reason", value=reason, inline=False)
        await ctx.send(embed=e)
    except Exception as e:
        await ctx.send(embed=error_embed("Deletion Failed", str(e)))


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
    """!share-user @user <#> — share your VPS with another user"""
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
            f"You now have access to {ctx.author.mention}'s VPS #{vps_number}. "
            f"Use `!manage-shared {ctx.author.mention} {vps_number}`.",
        ))
    except discord.Forbidden:
        pass


@bot.command(name="share-ruser")
async def revoke_share(ctx, target: discord.Member, vps_number: int):
    """!share-ruser @user <#> — revoke a user's shared access"""
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
    e = base_embed("💎 VPS Plans — PrimeCloud", "Choose your plan:")
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
    """!buywc <plan> <Intel/AMD> — purchase a VPS with credits"""
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

    user_data[user_id]["credits"] -= cost
    vps_data.setdefault(user_id, [])
    display_number = len(vps_data[user_id]) + 1
    container_name = f"vps-{user_id}-{next_container_id(user_id)}"
    ram_str = PLANS[plan]["ram"]
    cpu_str = PLANS[plan]["cpu"]
    password = generate_password()

    await ctx.send(embed=info_embed("Processing Purchase", f"Deploying {plan} VPS..."))

    try:
        await create_container(container_name, int(ram_str.replace("GB", "")) * 1024, cpu_str, password)

        vps_data[user_id].append({
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
        e.add_field(name="Plan", value=f"{plan} ({processor})", inline=True)
        e.add_field(name="VPS ID", value=f"#{display_number}", inline=True)
        e.add_field(name="Cost", value=f"{cost} credits", inline=True)
        await ctx.send(embed=e)

        try:
            tmate_cmd = await get_tmate_session(container_name)
            dm = base_embed("🎉 Your VPS is Ready!", "Connect using the command below.", 0x5865F2)
            dm.add_field(name="🔗 SSH Command", value=f"```{tmate_cmd}```", inline=False)
            dm.add_field(name="🎮 Manage", value="`!manage` in the server", inline=False)
            await ctx.author.send(embed=dm)
        except discord.Forbidden:
            pass

    except Exception as e:
        user_data[user_id]["credits"] += cost  # refund on failure
        save_data()
        await ctx.send(embed=error_embed("Purchase Failed", f"{e}\n\nYour credits were refunded."))


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
    """Use 'all' to zero out a user's balance."""
    user_id = str(user.id)
    user_data.setdefault(user_id, {"credits": 0})
    if amount.lower() == "all":
        removed = user_data[user_id]["credits"]
        user_data[user_id]["credits"] = 0
    else:
        removed = int(amount)
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
            note = f" — _{v['note']}_" if v.get("note") else ""
            lines.append(f"{icon} **{nickname}** (`{v['container_name']}`){note}")
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
    msg = await ctx.send(embed=info_embed("Pinging...", f"Checking `{container}`..."))
    start = time.time()
    try:
        out = await run_docker(f"docker inspect --format={{{{.State.Running}}}} {container}", timeout=10)
        elapsed_ms = int((time.time() - start) * 1000)
        if out.strip() == "true":
            await msg.edit(embed=success_embed("🏓 Pong!", f"Alive — response `{elapsed_ms}ms`"))
        else:
            await msg.edit(embed=error_embed("💀 No Response", "Container is not running."))
    except Exception as e:
        await msg.edit(embed=error_embed("Ping Failed", str(e)))


@bot.command(name="uptime-vps")
async def uptime_vps(ctx, vps_number: int):
    vps_list = vps_data.get(str(ctx.author.id), [])
    if not vps_list or not (1 <= vps_number <= len(vps_list)):
        await ctx.send(embed=error_embed("Invalid VPS", "VPS not found."))
        return
    container = vps_list[vps_number - 1]["container_name"]
    try:
        started_at_str = await run_docker(f"docker inspect --format={{{{.State.StartedAt}}}} {container}", timeout=10)
        started_at = datetime.fromisoformat(started_at_str[:19])
        delta = datetime.utcnow() - started_at
        hours, rem = divmod(delta.seconds, 3600)
        minutes, seconds = divmod(rem, 60)
        e = success_embed(f"⏱️ Uptime — {container}", f"```{delta.days}d {hours}h {minutes}m {seconds}s```")
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
    e = base_embed("🤖 Bot Status", "PrimeCloud VPS Manager", 0x00FF88)
    e.add_field(name="⏱️ Uptime", value=f"{delta.days}d {hours}h {minutes}m", inline=True)
    e.add_field(name="🖥️ Total VPS", value=f"{total} ({running} running)", inline=True)
    e.add_field(name="👥 Users", value=str(len(user_data)), inline=True)
    e.add_field(name="🔧 Maintenance", value="🔴 ON" if config.maintenance_mode else "🟢 OFF", inline=True)
    e.add_field(name="📡 Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
    await ctx.send(embed=e)


# ─── Admin tools ────────────────────────────────────────────────────────────

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
            details.append(f"{icon} {name} — VPS {i + 1}: `{v['container_name']}` — {v.get('status', '?').upper()}")

    e = base_embed("All VPS Information", "")
    e.add_field(name="System Overview", value=f"Users: {len(vps_data)}\nTotal VPS: {total}\nRunning: {running}\nStopped: {total - running}", inline=False)
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
            f"VPS {i + 1}: `{v['container_name']}` — {v.get('status', '?').upper()}" for i, v in enumerate(vps_list)
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
    total_ram = sum(int(v["ram"].replace("GB", "")) for vl in vps_data.values() for v in vl)
    total_cpu = sum(int(v["cpu"]) for vl in vps_data.values() for v in vl)
    e = base_embed("📊 Server Statistics", "")
    e.add_field(name="👥 Users", value=f"Total: {len(user_data)}\nAdmins: {len(admin_data.get('admins', []))}", inline=False)
    e.add_field(name="🖥️ VPS", value=f"Total: {total}\nRunning: {running}\nStopped: {total - running}", inline=False)
    e.add_field(name="💰 Economy", value=f"Total credits in circulation: {total_credits}", inline=False)
    e.add_field(name="📈 Resources", value=f"Total RAM: {total_ram}GB\nTotal CPU: {total_cpu} cores", inline=False)
    await ctx.send(embed=e)


@bot.command(name="exec")
@is_admin()
async def execute_command(ctx, container_name: str, *, command: str):
    await ctx.send(embed=info_embed("Executing", f"Running in `{container_name}`..."))
    try:
        stdout, stderr, rc = await docker_exec(container_name, command, timeout=30)
        e = base_embed(f"Output — {container_name}", f"`{command}`")
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
    await ctx.send(embed=info_embed("Restarting", f"Restarting `{container_name}`..."))
    try:
        await run_docker(f"docker restart {container_name}")
        await asyncio.sleep(3)
        await docker_exec(container_name, "/usr/sbin/sshd || true", timeout=10)
        for vl in vps_data.values():
            for v in vl:
                if v["container_name"] == container_name:
                    v["status"] = "running"
        save_data()
        await ctx.send(embed=success_embed("VPS Restarted", f"`{container_name}` restarted."))
    except Exception as e:
        await ctx.send(embed=error_embed("Restart Failed", str(e)))


@bot.command(name="backup-vps")
@is_admin()
async def backup_vps(ctx, container_name: str):
    snapshot = f"{container_name}-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    await ctx.send(embed=info_embed("Creating Backup", f"Committing snapshot of `{container_name}`..."))
    try:
        await run_docker(f"docker commit {container_name} {snapshot}")
        await ctx.send(embed=success_embed("Backup Created", f"Image `{snapshot}` created."))
    except Exception as e:
        await ctx.send(embed=error_embed("Backup Failed", str(e)))


@bot.command(name="restore-vps")
@is_admin()
async def restore_vps(ctx, container_name: str, snapshot_name: str):
    """Restore a VPS from a `!backup-vps` snapshot image."""
    found = None
    for vl in vps_data.values():
        for v in vl:
            if v["container_name"] == container_name:
                found = v
                break
    if not found:
        await ctx.send(embed=error_embed("Not Found", f"No VPS data for `{container_name}`."))
        return

    await ctx.send(embed=info_embed("Restoring", f"Restoring `{container_name}` from `{snapshot_name}`..."))
    try:
        try:
            await run_docker(f"docker stop {container_name}")
        except Exception:
            pass
        await run_docker(f"docker rm -f {container_name}")
        # No port mapping — this bot uses tmate for SSH, nothing is exposed on the host.
        await run_docker(
            f"docker run -d --name {container_name} --restart=unless-stopped {snapshot_name} sleep infinity"
        )
        await asyncio.sleep(2)
        await docker_exec(container_name, "/usr/sbin/sshd || true", timeout=10)
        found["status"] = "running"
        save_data()
        await ctx.send(embed=success_embed("VPS Restored", f"`{container_name}` restored from `{snapshot_name}`."))
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
                            await run_docker(f"docker stop {v['container_name']}")
                            v["status"] = "stopped"
                            stopped += 1
                        except Exception as e:
                            errors.append(str(e))
            save_data()
            e = success_embed("All VPS Stopped", f"Stopped {stopped} container(s).")
            if errors:
                e.add_field(name="Errors", value="\n".join(errors[:5]), inline=False)
            await interaction.followup.send(embed=e)

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, _button):
            await interaction.response.edit_message(embed=info_embed("Cancelled", "No changes made."), view=None)

    await ctx.send(embed=warning_embed("Stop All VPS", "⚠️ This will stop every running VPS. Continue?"), view=ConfirmView())


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
    e.add_field(name="From", value=f"PrimeCloud Team ({ctx.author.mention})", inline=False)
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
        await bot.change_presence(status=discord.Status.online, activity=discord.Activity(type=discord.ActivityType.watching, name="PrimeCloud | VPS Manager"))
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
        e.add_field(name=f"VPS #{i + 1} — `{vps['container_name']}`", value=status, inline=False)
    await ctx.send(embed=e)


@tasks.loop(hours=1)
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

            try:
                if days_left == 3:
                    u = await bot.fetch_user(int(user_id))
                    await u.send(embed=warning_embed("⚠️ VPS Expiring Soon", f"`{vps['container_name']}` expires in 3 days ({expires[:10]})."))
                elif days_left == 1:
                    u = await bot.fetch_user(int(user_id))
                    await u.send(embed=error_embed("🚨 VPS Expiring Tomorrow", f"`{vps['container_name']}` expires tomorrow ({expires[:10]})!"))
                elif days_left < 0 and vps.get("status") == "running":
                    try:
                        await run_docker(f"docker stop {vps['container_name']}")
                    except Exception:
                        pass
                    vps["status"] = "stopped"
                    u = await bot.fetch_user(int(user_id))
                    await u.send(embed=error_embed("❌ VPS Expired", f"`{vps['container_name']}` has expired and been stopped."))
            except Exception:
                continue
    save_data()


# ─── Help ───────────────────────────────────────────────────────────────────

def build_help_pages(user_is_admin: bool, user_is_main_admin: bool):
    pages = {}

    user_embed = base_embed("👤 User Commands", "VPS management for everyone:", 0x00FF88)
    user_embed.add_field(name="🖥️ VPS", value=(
        "`!manage` — manage your VPS\n"
        "`!manage @user` — (admin) manage another user's VPS\n"
        "`!share-user @user <#>` / `!share-ruser @user <#>`\n"
        "`!manage-shared @owner <#>`"
    ), inline=False)
    user_embed.add_field(name="🏷️ Tools", value=(
        "`!rename-vps <#> <name>` · `!vps-note <#> <text>`\n"
        "`!ping-vps <#>` · `!uptime-vps <#>` · `!myinfo`"
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
        admin_embed.add_field(name="VPS Control", value=(
            "`!create @user <ram_GB> <cpu_cores> <disk_GB>`\n"
            "`!delete-vps @user <#> <reason>` · `!restart-vps <container>`\n"
            "`!stop-vps-all` · `!exec <container> <cmd>`"
        ), inline=False)
        admin_embed.add_field(name="Backup", value="`!backup-vps <container>` · `!restore-vps <container> <snapshot>`", inline=False)
        admin_embed.add_field(name="Info & Economy", value=(
            "`!userinfo @user` · `!serverstats` · `!list-all`\n"
            "`!adminc @user <amount>` · `!adminrc @user <amount|all>`\n"
            "`!announce <msg>` · `!cpu-monitor <status|enable|disable>` · `!maintenance <on|off>`"
        ), inline=False)
        admin_embed.add_field(name="Expiry", value=(
            "`!setexpire @user <#> <days>` · `!extendexpire @user <#> <days>`\n"
            "`!removeexpire @user <#>` · `!checkexpire [@user]`"
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
