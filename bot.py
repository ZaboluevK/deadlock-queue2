import os
import sqlite3
import time

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ["DISCORD_TOKEN"]
PARTY_SIZE = int(os.getenv("PARTY_SIZE", "6"))
TIMEOUT_SEC = float(os.getenv("QUEUE_TIMEOUT_HOURS", "3")) * 3600

db = sqlite3.connect(os.getenv("DB_PATH", "queue.db"))
db.executescript("""
CREATE TABLE IF NOT EXISTS queue(
    guild_id INTEGER, user_id INTEGER, joined_at REAL,
    PRIMARY KEY(guild_id, user_id));
CREATE TABLE IF NOT EXISTS panels(
    guild_id INTEGER PRIMARY KEY, channel_id INTEGER, message_id INTEGER);
CREATE TABLE IF NOT EXISTS stats(
    guild_id INTEGER, user_id INTEGER, games INTEGER DEFAULT 0,
    PRIMARY KEY(guild_id, user_id));
""")
db.commit()


def queue_users(guild_id):
    rows = db.execute(
        "SELECT user_id FROM queue WHERE guild_id=? ORDER BY joined_at", (guild_id,)
    ).fetchall()
    return [r[0] for r in rows]


def panel_embed(guild_id):
    users = queue_users(guild_id)
    party, waiting = users[:PARTY_SIZE], users[PARTY_SIZE:]
    embed = discord.Embed(title="🎮 Очередь в Deadlock", color=0xE8A33D)
    slots = [f"{i}. <@{party[i - 1]}>" if i <= len(party) else f"{i}. *свободно*"
             for i in range(1, PARTY_SIZE + 1)]
    embed.add_field(name=f"Пачка ({len(party)}/{PARTY_SIZE})",
                    value="\n".join(slots), inline=False)
    embed.add_field(
        name=f"Очередь ({len(waiting)})",
        value="\n".join(f"{i}. <@{u}>" for i, u in enumerate(waiting, 1)) or "*пусто*",
        inline=False)
    return embed


def party_notice(before, after):
    """Текст пинга, если после изменения в пачке появились новые люди."""
    party_before, party_after = before[:PARTY_SIZE], after[:PARTY_SIZE]
    new = [u for u in party_after if u not in party_before]
    if len(party_after) < PARTY_SIZE or not new:
        return None
    if len(party_before) < PARTY_SIZE:
        mentions = " ".join(f"<@{u}>" for u in party_after)
        return f"🔥 Пачка собрана! {mentions} — го в Deadlock!"
    mentions = " ".join(f"<@{u}>" for u in new)
    return f"🔔 {mentions} — освободилось место, ты в пачке!"


async def refresh_panel(client, guild_id, notice=None):
    row = db.execute(
        "SELECT channel_id, message_id FROM panels WHERE guild_id=?", (guild_id,)
    ).fetchone()
    if not row:
        return
    try:
        channel = client.get_channel(row[0]) or await client.fetch_channel(row[0])
        msg = await channel.fetch_message(row[1])
        await msg.edit(embed=panel_embed(guild_id), view=QueueView())
        if notice:
            await channel.send(notice)
    except discord.Forbidden:
        pass
    except discord.NotFound:
        db.execute("DELETE FROM panels WHERE guild_id=?", (guild_id,))
        db.commit()


class QueueView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _update(self, interaction, before):
        gid = interaction.guild_id
        await interaction.response.edit_message(embed=panel_embed(gid), view=self)
        notice = party_notice(before, queue_users(gid))
        if notice:
            await interaction.followup.send(notice)

    @discord.ui.button(label="Записаться", style=discord.ButtonStyle.success,
                       emoji="✅", custom_id="queue:join")
    async def join(self, interaction: discord.Interaction, _):
        gid = interaction.guild_id
        before = queue_users(gid)
        cur = db.execute(
            "INSERT OR IGNORE INTO queue VALUES(?,?,?)",
            (gid, interaction.user.id, time.time()))
        db.commit()
        if cur.rowcount == 0:
            await interaction.response.send_message("Ты уже в списке.", ephemeral=True)
            return
        await self._update(interaction, before)

    @discord.ui.button(label="Выйти", style=discord.ButtonStyle.danger,
                       emoji="🚪", custom_id="queue:leave")
    async def leave(self, interaction: discord.Interaction, _):
        gid = interaction.guild_id
        before = queue_users(gid)
        cur = db.execute("DELETE FROM queue WHERE guild_id=? AND user_id=?",
                         (gid, interaction.user.id))
        db.commit()
        if cur.rowcount == 0:
            await interaction.response.send_message("Тебя нет в списке.", ephemeral=True)
            return
        await self._update(interaction, before)

    @discord.ui.button(label="Закончили игру", style=discord.ButtonStyle.primary,
                       emoji="🏁", custom_id="queue:finish")
    async def finish(self, interaction: discord.Interaction, _):
        gid = interaction.guild_id
        before = queue_users(gid)
        party = before[:PARTY_SIZE]
        if interaction.user.id not in party:
            await interaction.response.send_message(
                "Завершить игру может только тот, кто в пачке.", ephemeral=True)
            return
        if len(party) < PARTY_SIZE:
            await interaction.response.send_message(
                f"Пачка ещё не собрана ({len(party)}/{PARTY_SIZE}).", ephemeral=True)
            return
        db.executemany("DELETE FROM queue WHERE guild_id=? AND user_id=?",
                       [(gid, u) for u in party])
        db.executemany(
            "INSERT INTO stats(guild_id,user_id,games) VALUES(?,?,1) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET games=games+1",
            [(gid, u) for u in party])
        db.commit()
        await self._update(interaction, [])


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_view(QueueView())
        await self.tree.sync()
        self.expire_loop.start()

    @tasks.loop(minutes=1)
    async def expire_loop(self):
        cutoff = time.time() - TIMEOUT_SEC
        guilds = [r[0] for r in db.execute(
            "SELECT DISTINCT guild_id FROM queue WHERE joined_at<?", (cutoff,))]
        for gid in guilds:
            before = queue_users(gid)
            db.execute("DELETE FROM queue WHERE guild_id=? AND joined_at<?", (gid, cutoff))
            db.commit()
            await refresh_panel(self, gid, party_notice(before, queue_users(gid)))


bot = Bot()


@bot.tree.command(name="setup", description="Создать панель очереди в этом канале")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def setup(interaction: discord.Interaction):
    await interaction.response.send_message(
        embed=panel_embed(interaction.guild_id), view=QueueView())
    msg = await interaction.original_response()
    db.execute("INSERT OR REPLACE INTO panels VALUES(?,?,?)",
               (interaction.guild_id, msg.channel.id, msg.id))
    db.commit()


@bot.tree.command(name="clear", description="Очистить очередь")
@app_commands.default_permissions(manage_guild=True)
@app_commands.guild_only()
async def clear(interaction: discord.Interaction):
    db.execute("DELETE FROM queue WHERE guild_id=?", (interaction.guild_id,))
    db.commit()
    await interaction.response.send_message("Очередь очищена.", ephemeral=True)
    await refresh_panel(bot, interaction.guild_id)


@bot.tree.command(name="stats", description="Топ игроков по числу собранных пачек")
@app_commands.guild_only()
async def stats(interaction: discord.Interaction):
    rows = db.execute(
        "SELECT user_id, games FROM stats WHERE guild_id=? ORDER BY games DESC LIMIT 10",
        (interaction.guild_id,)).fetchall()
    if not rows:
        await interaction.response.send_message("Пока нет ни одной пачки.", ephemeral=True)
        return
    text = "\n".join(f"{i}. <@{u}> — {g}" for i, (u, g) in enumerate(rows, 1))
    await interaction.response.send_message(
        embed=discord.Embed(title="🏆 Статистика пачек", description=text, color=0xE8A33D))


bot.run(TOKEN)
