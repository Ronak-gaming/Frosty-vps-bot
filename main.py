import asyncio
from datetime import datetime
import discord
from discord.ext import commands

from config import (
    DISCORD_BOT_TOKEN,
    MAIN_ADMIN_ID,
    WEBSSH_PORT,
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
    create_vps_container,
    get_sshx_session
)
from views import HelpPaginatorView, NodeSelectView, OSSelectView, VPSControlView
from webssh_server import handle_http_request, create_webssh_link, get_public_webssh_base

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


def is_admin(user_id: int) -> bool:
    return user_id == MAIN_ADMIN_ID or user_id in get_admins()


def footer_text() -> str:
    bot_name = bot.user.name if bot.user else "ApexHosting"
    return f"{bot_name} | WebSSH & SSHX Cloud Manager • {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"


@bot.event
async def on_ready():
    ensure_ssh_key()
    get_nodes()
    bot.loop.create_task(
        asyncio.start_server(lambda r, w: handle_http_request(r, w, bot), "0.0.0.0", WEBSSH_PORT)
    )
    print(f"[✓] Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"[✓] WebSSH listening on 0.0.0.0:{WEBSSH_PORT} -> Public URL: {get_public_webssh_base()}")


@bot.command(name="help", aliases=["commands", "h"])
async def cmd_help(ctx: commands.Context):
    bot_name = bot.user.name if bot.user else "ApexHosting"
    view = HelpPaginatorView(ctx.author.id, bot_name)
    await ctx.reply(embed=view.pages[0], view=view)


@bot.command(name="ping")
async def cmd_ping(ctx: commands.Context):
    embed = discord.Embed(
        title="🏓 Gateway & Tunnel Status",
        description=(
            f"**Discord Latency:** `{round(bot.latency * 1000)}ms`\n"
            f"**WebSSH Endpoint:** `{get_public_webssh_base()}`\n"
            f"**Local Listener Port:** `{WEBSSH_PORT}`"
        ),
        color=0x2ECC71
    )
    embed.set_footer(text=footer_text())
    await ctx.reply(embed=embed)


@bot.command(name="webssh", aliases=["console", "terminal"])
async def cmd_webssh(ctx: commands.Context):
    await ctx.reply("⏳ **WebSSH browser console is Coming Soon!** Please use the **`!sshx`** command or click **SSHX Link** in `!myvps` for instant web terminal access.")


@bot.command(name="sshx")
async def cmd_sshx(ctx: commands.Context):
    vps_list = get_user_vps(ctx.author.id)
    if not vps_list:
        return await ctx.reply("📭 You don't have any active VPS containers.")
    status = await ctx.reply("⚡ Generating live **SSHX** terminal link...")
    for v in vps_list:
        ok, url = await get_sshx_session(v.get("node", "Node-1"), v["container_name"])
        if ok:
            embed = discord.Embed(title=f"⚡ SSHX Terminal — {v['container_name']}", description=f"**🔗 Click to Open:**\n{url}", color=0x2ECC71)
            embed.set_footer(text=footer_text())
            try:
                await ctx.author.send(embed=embed)
                await status.edit(content=f"📬 Sent your **SSHX** terminal link for `{v['container_name']}` to your DMs!")
            except Exception:
                await status.edit(content=None, embed=embed)
        else:
            await status.edit(content=f"❌ Failed to generate SSHX: `{url}`")


@bot.command(name="credits", aliases=["bal", "balance"])
async def cmd_credits(ctx: commands.Context, member: discord.Member = None):
    target = member or ctx.author
    bal = get_credits(target.id)
    embed = discord.Embed(title="💎 Credit Wallet", description=f"{target.mention} has **`{bal}` Credits**.", color=0x2ECC71)
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
        description="Use `!buy <PlanName>` to deploy instantly!",
        color=0x7C6AFF
    )
    for name, p in VPS_PLANS.items():
        embed.add_field(name=f"📦 {name} — `{p['price']} Credits`", value=f"**RAM:** `{p['ram']} GB` • **vCPU:** `{p['cpu']} Cores` • **Disk:** `{p['disk']} GB`", inline=False)
    embed.set_footer(text=footer_text())
    await ctx.reply(embed=embed)


@bot.command(name="buy")
async def cmd_buy(ctx: commands.Context, plan_name: str = None):
    if not plan_name:
        return await ctx.reply("⚠️ **Usage:** `!buy <Starter|Basic|Pro|Ultra>`")
    matched = next((k for k in VPS_PLANS if k.lower() == plan_name.lower()), None)
    if not matched:
        return await ctx.reply("❌ Plan not found.")

    plan = VPS_PLANS[matched]
    if get_credits(ctx.author.id) < plan["price"]:
        return await ctx.reply(f"❌ You need `{plan['price']}` credits.")

    async def on_os_picked(interaction: discord.Interaction, picked_os: str):
        async def on_node_picked(node_interaction: discord.Interaction, picked_node: str):
            if get_credits(ctx.author.id) < plan["price"]:
                return await node_interaction.response.edit_message(content="❌ Insufficient credits.", view=None)
            add_credits(ctx.author.id, -plan["price"])
            await node_interaction.response.edit_message(
                content=f"⚙️ Provisioning **{matched}** on **{picked_node}** (Running `apt update && upgrade`, installing neofetch & sshx)...",
                view=None
            )
            ok, vps_info, err = await create_vps_container(picked_node, ctx.author.id, plan["ram"], plan["cpu"], plan["disk"], picked_os)
            if not ok:
                add_credits(ctx.author.id, plan["price"])
                return await node_interaction.edit_original_response(content=f"❌ Failed: `{err}` (Credits refunded)")

            add_user_vps(ctx.author.id, vps_info)
            sshx_ok, sshx_url = await get_sshx_session(picked_node, vps_info["container_name"])
            sshx_line = sshx_url if sshx_ok else "Click **⚡ SSHX Link** in `!myvps`"

            try:
                dm_embed = discord.Embed(
                    title=f"🎉 Your {matched} VPS is Ready!",
                    description=(
                        f"**🆔 Container:** `{vps_info['container_name']}`\n"
                        f"**🐧 OS:** `{vps_info['os_label']}`\n"
                        f"**🔑 Root Password:** `{vps_info['root_password']}`\n\n"
                        f"**⚡ SSHX Terminal:**\n{sshx_line}"
                    ),
                    color=0x2ECC71
                )
                dm_embed.set_footer(text=footer_text())
                await ctx.author.send(embed=dm_embed)
            except Exception:
                pass

            await node_interaction.edit_original_response(content=f"✅ Provisioned `{vps_info['container_name']}`! Details sent to DMs.", view=VPSControlView(ctx.author.id, vps_info))

        await interaction.response.edit_message(content=f"❄ Select Node for **{matched}**:", view=NodeSelectView(ctx.author.id, on_node_picked))

    await ctx.reply("🐧 Choose your Operating System:", view=OSSelectView(ctx.author.id, on_os_picked))


@bot.command(name="create", aliases=["deploy", "create-vps"])
async def cmd_create(ctx: commands.Context, member: discord.Member = None, ram_gb: int = DEFAULT_RAM_GB, cores: int = DEFAULT_CPU_CORES, disk_gb: int = DEFAULT_DISK_GB):
    if not is_admin(ctx.author.id):
        return await ctx.reply("❌ Admin only.")
    target = member or ctx.author

    async def on_os_picked(interaction: discord.Interaction, picked_os: str):
        async def on_node_picked(node_interaction: discord.Interaction, picked_node: str):
            await node_interaction.response.edit_message(content=f"⚙️ Creating VPS for {target.mention}...", view=None)
            ok, vps_info, err = await create_vps_container(picked_node, target.id, ram_gb, cores, disk_gb, picked_os)
            if not ok:
                return await node_interaction.edit_original_response(content=f"❌ Failed: `{err}`")

            add_user_vps(target.id, vps_info)
            sshx_ok, sshx_url = await get_sshx_session(picked_node, vps_info["container_name"])
            sshx_line = sshx_url if sshx_ok else "Click **⚡ SSHX Link** in `!myvps`"

            try:
                dm_embed = discord.Embed(title=f"🎉 Your VPS is Ready!", description=f"**Password:** `{vps_info['root_password']}`\n**SSHX:** {sshx_line}", color=0x2ECC71)
                await target.send(embed=dm_embed)
            except Exception:
                pass
            await node_interaction.edit_original_response(content=f"✅ Created `{vps_info['container_name']}` for {target.mention}!", view=VPSControlView(target.id, vps_info))

        await interaction.response.edit_message(content=f"❄ Select Node:", view=NodeSelectView(ctx.author.id, on_node_picked))

    await ctx.reply("🐧 Choose Operating System:", view=OSSelectView(ctx.author.id, on_os_picked))


@bot.command(name="myvps", aliases=["vps"])
async def cmd_myvps(ctx: commands.Context):
    vps_list = get_user_vps(ctx.author.id)
    if not vps_list:
        return await ctx.reply("📭 You don't have any active VPS containers.")
    for v in vps_list:
        embed = discord.Embed(
            title=f"🖥️ {v['container_name']} • {v.get('node', 'Node-1')}",
            description=f"**OS:** `{v.get('os_label', 'Ubuntu')}`\n**Specs:** `{v['ram_gb']}GB RAM` • `{v['cores']} vCPU`",
            color=0x7C6AFF
        )
        embed.set_footer(text=footer_text())
        await ctx.reply(embed=embed, view=VPSControlView(ctx.author.id, v))


@bot.command(name="nodes")
async def cmd_nodes(ctx: commands.Context):
    nodes = get_nodes()
    embed = discord.Embed(title=f"🌐 Cluster Nodes", color=0x00A8FF)
    for name, cfg in nodes.items():
        stats = await get_node_stats(name)
        embed.add_field(name=f"🟢 {name} ({cfg.get('ip')})", value=f"CPU: `{stats['cores']}` | RAM: `{stats['ram']}` | Containers: `{stats['containers']}`", inline=False)
    embed.set_footer(text=footer_text())
    await ctx.reply(embed=embed)


@bot.command(name="vps-list", aliases=["allvps"])
async def cmd_allvps(ctx: commands.Context):
    if not is_admin(ctx.author.id):
        return await ctx.reply("❌ Admin only.")
    all_v = get_all_vps()
    lines = []
    for uid, vlist in all_v.items():
        for v in vlist:
            lines.append(f"• `{v['container_name']}` | Owner: <@{uid}> | Node: **{v.get('node')}**")
    if not lines:
        return await ctx.reply("📭 No VPS containers.")
    embed = discord.Embed(title="📋 All Containers", description="\n".join(lines[:40]), color=0x7C6AFF)
    await ctx.reply(embed=embed)


@bot.command(name="admin-add")
async def cmd_admin_add(ctx: commands.Context, member: discord.Member):
    if ctx.author.id != MAIN_ADMIN_ID:
        return await ctx.reply("❌ Owner only.")
    add_admin(member.id)
    await ctx.reply(f"✅ Added {member.mention}.")


@bot.command(name="admin-remove")
async def cmd_admin_remove(ctx: commands.Context, member: discord.Member):
    if ctx.author.id != MAIN_ADMIN_ID:
        return await ctx.reply("❌ Owner only.")
    remove_admin(member.id)
    await ctx.reply(f"🗑️ Removed {member.mention}.")


if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)
