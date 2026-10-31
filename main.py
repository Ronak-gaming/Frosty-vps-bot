import asyncio
import json
from datetime import datetime
import discord
from discord.ext import commands

from config import (
    DISCORD_BOT_TOKEN,
    MAIN_ADMIN_ID,
    PAIR_API_PORT,
    DEFAULT_RAM_GB,
    DEFAULT_CPU_CORES,
    DEFAULT_DISK_GB,
    VPS_PLANS
)
from storage import (
    get_nodes,
    register_node,
    remove_node,
    rename_node,
    get_all_vps,
    get_user_vps,
    add_user_vps,
    get_admins,
    add_admin,
    remove_admin,
    get_credits,
    add_credits
)
from docker_utils import (
    ensure_ssh_key,
    pair_remote_node,
    get_node_stats,
    create_vps_container
)
from views import HelpPaginatorView, NodeSelectView, VPSControlView

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


def is_admin(user_id: int) -> bool:
    return user_id == MAIN_ADMIN_ID or user_id in get_admins()


def footer_text() -> str:
    bot_name = bot.user.name if bot.user else "FrostyHosting"
    return f"{bot_name} | Multi-Node VPS Manager • {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}"


# --- Zero-Discord VPS-to-VPS Auto-Pairing Server (Port 8765) ---
async def _handle_pair_request(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        data = await asyncio.wait_for(reader.read(8192), timeout=5.0)
        text = data.decode("utf-8", errors="ignore")
        if "\r\n\r\n" not in text:
            writer.close()
            return
        headers, body = text.split("\r\n\r\n", 1)
        if not headers.startswith("POST /pair"):
            writer.close()
            return

        payload = json.loads(body.strip())
        req_token = payload.get("token", "").strip()
        node_name = payload.get("node_name", "Node-2").strip()
        node_ip = payload.get("ip", "").strip()
        ssh_port = int(payload.get("ssh_port", 22))
        step = payload.get("step", "init")

        if req_token != DISCORD_BOT_TOKEN.strip():
            resp_json = json.dumps({"ok": False, "error": "Bot token mismatch."})
            status = "401 Unauthorized"
        else:
            pub_key = ensure_ssh_key()
            bot_tag = str(bot.user) if bot.user else "Frosty-VPS-Bot"
            bot_id = str(bot.user.id) if bot.user else "Unknown"

            if step == "confirm" and node_ip:
                register_node(node_name, node_ip, ssh_port, is_local=False)

            resp_json = json.dumps({
                "ok": True,
                "bot_name": bot_tag,
                "bot_id": bot_id,
                "pubkey": pub_key,
                "node_name": node_name
            })
            status = "200 OK"

        http_resp = (
            f"HTTP/1.1 {status}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(resp_json)}\r\n"
            f"Connection: close\r\n\r\n{resp_json}"
        )
        writer.write(http_resp.encode("utf-8"))
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()


@bot.event
async def on_ready():
    ensure_ssh_key()
    get_nodes()
    bot.loop.create_task(asyncio.start_server(_handle_pair_request, "0.0.0.0", PAIR_API_PORT))
    print(f"[✓] Logged in as {bot.user} (ID: {bot.user.id}) | VPS Auto-Pair API on port {PAIR_API_PORT}")


# --- Multi-Page Interactive Help Command ---
@bot.command(name="help", aliases=["commands", "h"])
async def cmd_help(ctx: commands.Context):
    view = HelpPaginatorView(ctx.author.id, bot.user.name if bot.user else "Frosty Bot")
    await ctx.reply(embed=view.pages[0], view=view)


@bot.command(name="ping")
async def cmd_ping(ctx: commands.Context):
    await ctx.reply(f"🏓 Pong! Latency: `{round(bot.latency * 1000)}ms`")


# --- Credits & Shop Commands ---
@bot.command(name="credits", aliases=["bal", "balance"])
async def cmd_credits(ctx: commands.Context, member: discord.Member = None):
    target = member or ctx.author
    bal = get_credits(target.id)
    embed = discord.Embed(
        title="💰 Credit Balance",
        description=f"{target.mention} currently has **`{bal}` Credits**.",
        color=0x00FF88
    )
    embed.set_footer(text=footer_text())
    await ctx.reply(embed=embed)


@bot.command(name="addcredits", aliases=["givecredits"])
async def cmd_addcredits(ctx: commands.Context, member: discord.Member, amount: int):
    if not is_admin(ctx.author.id):
        return await ctx.reply("❌ Admin only.")
    new_bal = add_credits(member.id, amount)
    await ctx.reply(f"✅ Added `{amount}` credits to {member.mention}. New balance: **`{new_bal}`**.")


@bot.command(name="removecredits", aliases=["takecredits"])
async def cmd_removecredits(ctx: commands.Context, member: discord.Member, amount: int):
    if not is_admin(ctx.author.id):
        return await ctx.reply("❌ Admin only.")
    new_bal = add_credits(member.id, -abs(amount))
    await ctx.reply(f"✅ Removed `{amount}` credits from {member.mention}. New balance: **`{new_bal}`**.")


@bot.command(name="pay")
async def cmd_pay(ctx: commands.Context, member: discord.Member, amount: int):
    if amount <= 0 or member.id == ctx.author.id:
        return await ctx.reply("❌ Specify a valid positive amount and recipient.")
    if get_credits(ctx.author.id) < amount:
        return await ctx.reply("❌ You don't have enough credits.")
    add_credits(ctx.author.id, -amount)
    add_credits(member.id, amount)
    await ctx.reply(f"💸 Sent `{amount}` credits to {member.mention}!")


@bot.command(name="plans", aliases=["shop"])
async def cmd_plans(ctx: commands.Context):
    embed = discord.Embed(
        title=f"🛒 {bot.user.name} — VPS Plans",
        description="Use `!buy <PlanName>` to deploy instantly on any available node!",
        color=0x00A8FF
    )
    for name, p in VPS_PLANS.items():
        embed.add_field(
            name=f"📦 {name} — `{p['price']} Credits`",
            value=f"**RAM:** `{p['ram']} GB` | **CPU:** `{p['cpu']} Cores` | **Disk:** `{p['disk']} GB`",
            inline=False
        )
    embed.set_footer(text=footer_text())
    await ctx.reply(embed=embed)


@bot.command(name="buy")
async def cmd_buy(ctx: commands.Context, plan_name: str = None):
    if not plan_name:
        return await ctx.reply("⚠️ **Usage:** `!buy <Starter|Basic|Pro|Ultra>` (See `!plans`)")
    matched = next((k for k in VPS_PLANS if k.lower() == plan_name.lower()), None)
    if not matched:
        return await ctx.reply("❌ Plan not found. Run `!plans` to see available plans.")

    plan = VPS_PLANS[matched]
    if get_credits(ctx.author.id) < plan["price"]:
        return await ctx.reply(f"❌ You need `{plan['price']}` credits to buy **{matched}**.")

    async def on_node_picked(interaction: discord.Interaction, picked_node: str):
        if get_credits(ctx.author.id) < plan["price"]:
            return await interaction.response.edit_message(content="❌ Insufficient credits.", view=None)
        add_credits(ctx.author.id, -plan["price"])
        await interaction.response.edit_message(
            content=f"⚙️ Deploying **{matched}** (`{plan['ram']}GB RAM / {plan['cpu']} CPU`) on **{picked_node}**...",
            view=None
        )
        ok, vps_info, err = await create_vps_container(picked_node, ctx.author.id, plan["ram"], plan["cpu"], plan["disk"])
        if not ok:
            add_credits(ctx.author.id, plan["price"])
            return await interaction.edit_original_response(content=f"❌ Deployment failed on `{picked_node}` (Credits refunded):\n`{err}`")

        add_user_vps(ctx.author.id, vps_info)
        try:
            dm_embed = discord.Embed(
                title=f"🎉 Your {matched} VPS on {picked_node} is Ready!",
                description=(
                    f"**Node:** `{picked_node}`\n"
                    f"**SSH Command:** `ssh root@{vps_info['ip']} -p {vps_info['ssh_port']}`\n"
                    f"**Root Password:** `{vps_info['root_password']}`\n"
                    f"**Specs:** `{plan['ram']}GB RAM` | `{plan['cpu']} vCPU` | `{plan['disk']}GB Disk`"
                ),
                color=0x00FF88
            )
            dm_embed.set_footer(text=footer_text())
            await ctx.author.send(embed=dm_embed)
        except Exception:
            pass

        await interaction.edit_original_response(
            content=f"✅ Deployed `{vps_info['container_name']}` on **{picked_node}**! (Check your DMs for SSH credentials)",
            view=VPSControlView(ctx.author.id, vps_info)
        )

    await ctx.reply(
        f"❄ Select a node to deploy your **{matched}** VPS (`{plan['ram']}GB RAM / {plan['cpu']} CPU`):",
        view=NodeSelectView(ctx.author.id, on_node_picked)
    )


# --- Multi-Node Commands ---
@bot.command(name="node-key")
async def cmd_node_key(ctx: commands.Context):
    if not is_admin(ctx.author.id):
        return await ctx.reply("❌ Only bot admins can view the SSH key.")
    pub_key = ensure_ssh_key()
    embed = discord.Embed(
        title=f"🔑 Primary Bot SSH Public Key ({bot.user.name})",
        description=f"```\n{pub_key}\n```",
        color=0x00A8FF
    )
    embed.set_footer(text=footer_text())
    await ctx.reply(embed=embed)


@bot.command(name="node-add")
async def cmd_node_add(ctx: commands.Context, node_name: str = None, ip: str = None, port: int = 22, *, auth_secret: str = "KEY_ONLY"):
    if not is_admin(ctx.author.id):
        return await ctx.reply("❌ Only bot admins can add nodes.")
    if not node_name or not ip:
        return await ctx.reply("⚠️ **Usage:** `!node-add <NodeName> <IP> <Port> [RootPassword | KEY_ONLY]`")

    msg = await ctx.reply(f"🔄 Pairing **{node_name}** (`{ip}:{port}`) with **{bot.user.name}**...")
    ok, result = await pair_remote_node(node_name, ip, port, auth_secret.strip())

    if not ok:
        err_embed = discord.Embed(
            title="▌ Node Pairing Failed",
            description=f"Could not pair **{node_name}** (`{ip}:{port}`):\n`{result}`",
            color=0xFF4444
        )
        err_embed.set_footer(text=footer_text())
        return await msg.edit(content=None, embed=err_embed)

    register_node(node_name, ip, port, is_local=False)
    ok_embed = discord.Embed(
        title="✅ Node Paired Successfully",
        description=f"**{node_name}** (`{ip}:{port}`) is now paired with **{bot.user.name}**!\n🐳 **Remote Docker Version:** `{result}`",
        color=0x00FF88
    )
    ok_embed.set_footer(text=footer_text())
    await msg.edit(content=None, embed=ok_embed)


@bot.command(name="node-rename")
async def cmd_node_rename(ctx: commands.Context, old_name: str = None, new_name: str = None):
    if not is_admin(ctx.author.id):
        return await ctx.reply("❌ Admin only.")
    if not old_name or not new_name:
        return await ctx.reply("⚠️ **Usage:** `!node-rename <OldName> <NewName>`")
    if rename_node(old_name, new_name):
        await ctx.reply(f"✅ Renamed node `{old_name}` ➔ `{new_name}`.")
    else:
        await ctx.reply(f"❌ Node `{old_name}` not found.")


@bot.command(name="node-remove")
async def cmd_node_remove(ctx: commands.Context, node_name: str = None):
    if not is_admin(ctx.author.id):
        return await ctx.reply("❌ Admin only.")
    if not node_name:
        return await ctx.reply("⚠️ **Usage:** `!node-remove <NodeName>`")
    if remove_node(node_name):
        await ctx.reply(f"🗑️ Removed node `{node_name}`.")
    else:
        await ctx.reply(f"❌ Cannot remove `{node_name}` (not found or is Primary Local Node).")


@bot.command(name="nodes")
async def cmd_nodes(ctx: commands.Context):
    nodes = get_nodes()
    embed = discord.Embed(title=f"🌐 Registered Nodes — {bot.user.name}", color=0x00A8FF)
    for name, cfg in nodes.items():
        stats = await get_node_stats(name)
        role = "🏠 Primary Host" if cfg.get("is_local") else "🛰️ Worker Node"
        if stats["online"]:
            embed.add_field(
                name=f"🟢 {name} ({cfg.get('ip')}:{cfg.get('ssh_port', 22)})",
                value=f"**Role:** {role}\n**CPU:** `{stats['cores']}` Cores | **RAM:** `{stats['ram']}`\n**Containers:** `{stats['containers']}` | `{stats['uptime']}`",
                inline=False
            )
        else:
            embed.add_field(
                name=f"🔴 {name} ({cfg.get('ip')}:{cfg.get('ssh_port', 22)})",
                value=f"**Role:** {role}\n**Status:** Offline (`{stats.get('error')}`)",
                inline=False
            )
    embed.set_footer(text=footer_text())
    await ctx.reply(embed=embed)


# --- Admin & Deployment Commands ---
@bot.command(name="deploy", aliases=["create-vps", "create"])
async def cmd_deploy(ctx: commands.Context, member: discord.Member = None, ram_gb: int = DEFAULT_RAM_GB, cores: int = DEFAULT_CPU_CORES, disk_gb: int = DEFAULT_DISK_GB):
    if not is_admin(ctx.author.id):
        return await ctx.reply("❌ Only admins can deploy new VPS containers.")
    target = member or ctx.author

    async def on_node_picked(interaction: discord.Interaction, picked_node: str):
        await interaction.response.edit_message(
            content=f"⚙️ Deploying `{ram_gb}GB RAM / {cores} CPU` VPS for {target.mention} on **{picked_node}**...",
            view=None
        )
        ok, vps_info, err = await create_vps_container(picked_node, target.id, ram_gb, cores, disk_gb)
        if not ok:
            return await interaction.edit_original_response(content=f"❌ Deployment failed on `{picked_node}`:\n`{err}`")

        add_user_vps(target.id, vps_info)
        try:
            dm_embed = discord.Embed(
                title=f"🎉 Your VPS on {picked_node} is Ready!",
                description=(
                    f"**Node:** `{picked_node}`\n"
                    f"**SSH Command:** `ssh root@{vps_info['ip']} -p {vps_info['ssh_port']}`\n"
                    f"**Root Password:** `{vps_info['root_password']}`\n"
                    f"**Specs:** `{ram_gb}GB RAM` | `{cores} vCPU` | `{disk_gb}GB Disk`"
                ),
                color=0x00FF88
            )
            dm_embed.set_footer(text=footer_text())
            await target.send(embed=dm_embed)
        except Exception:
            pass

        await interaction.edit_original_response(
            content=f"✅ Deployed `{vps_info['container_name']}` on **{picked_node}** for {target.mention}! (Credentials sent via DM)",
            view=VPSControlView(target.id, vps_info)
        )

    await ctx.reply(
        f"❄ Choose which Node to host {target.mention}'s VPS (`{ram_gb}GB RAM / {cores} Cores`):",
        view=NodeSelectView(ctx.author.id, on_node_picked)
    )


@bot.command(name="myvps", aliases=["vps"])
async def cmd_myvps(ctx: commands.Context):
    vps_list = get_user_vps(ctx.author.id)
    if not vps_list:
        return await ctx.reply("📭 You don't have any active VPS containers.")
    for v in vps_list:
        embed = discord.Embed(
            title=f"🖥️ {v['container_name']} ({v.get('node', 'Node-1')})",
            description=f"**SSH:** `ssh root@{v['ip']} -p {v['ssh_port']}`\n**Specs:** `{v['ram_gb']}GB RAM` / `{v['cores']} CPU`",
            color=0x00A8FF
        )
        embed.set_footer(text=footer_text())
        await ctx.reply(embed=embed, view=VPSControlView(ctx.author.id, v))


@bot.command(name="vps-list", aliases=["allvps"])
async def cmd_allvps(ctx: commands.Context):
    if not is_admin(ctx.author.id):
        return await ctx.reply("❌ Admin only.")
    all_v = get_all_vps()
    lines = []
    for uid, vlist in all_v.items():
        for v in vlist:
            lines.append(f"• `{v['container_name']}` | Owner: <@{uid}> | Node: **{v.get('node')}** (`{v['ip']}:{v['ssh_port']}`)")
    if not lines:
        return await ctx.reply("📭 No VPS containers deployed yet.")
    embed = discord.Embed(title="📋 All Deployed VPS Containers", description="\n".join(lines[:40]), color=0x00A8FF)
    embed.set_footer(text=footer_text())
    await ctx.reply(embed=embed)


@bot.command(name="admin-add")
async def cmd_admin_add(ctx: commands.Context, member: discord.Member):
    if ctx.author.id != MAIN_ADMIN_ID:
        return await ctx.reply("❌ Only the Main Owner can add admins.")
    add_admin(member.id)
    await ctx.reply(f"✅ Added {member.mention} as a Bot Administrator.")


@bot.command(name="admin-remove")
async def cmd_admin_remove(ctx: commands.Context, member: discord.Member):
    if ctx.author.id != MAIN_ADMIN_ID:
        return await ctx.reply("❌ Only the Main Owner can remove admins.")
    remove_admin(member.id)
    await ctx.reply(f"🗑️ Removed {member.mention} from Bot Administrators.")


if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)
