import os, json, asyncio, unicodedata
from dataclasses import dataclass, asdict
from typing import Optional, Dict, List
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands

CONFIG = {
    "TZ": timezone(timedelta(hours=9)),
    "ROLE_MENTION": "",
    "DEFAULT_FC": "",
    "DEFAULT_BOAT": "",
    "FC_CHANNEL_MAP": {}
}
DATA_FILE = "submarine_tasks.json"
JST = CONFIG["TZ"]

FC_ALIASES = {
    "alexander": {"a","al","ale","alex","alexan","alexander"},
    "pandemonium": {"p","pa","pan","pand","pande","pandemo","pandemonium"},
}
def normalize_fc(s: str) -> str:
    if not s: return ""
    t = unicodedata.normalize("NFKC", s).strip().lower()
    for canon, aliases in FC_ALIASES.items():
        if t == canon or t in aliases:
            return "Alexander" if canon == "alexander" else "Pandemonium"
    if "alexander".startswith(t): return "Alexander"
    if "pandemonium".startswith(t): return "Pandemonium"
    return s

def normalize_boat(s: str) -> str:
    t = unicodedata.normalize("NFKC", s or "").strip()
    return t if t in {"1","2","3","4"} else t

def parse_delta(s: str) -> timedelta:
    s = unicodedata.normalize("NFKC", (s or "")).strip().lower()
    s = s.replace("minutes","min").replace("minute","min").replace("分","min")
    h=m=0; buf=""; i=0; last=None
    while i < len(s):
        ch = s[i]
        if ch.isdigit(): buf+=ch; i+=1; continue
        if ch=="h":
            if not buf: raise ValueError("h の前に数字が必要です")
            h += int(buf); buf=""; last="h"; i+=1; continue
        if s.startswith("min", i):
            if not buf: raise ValueError("min の前に数字が必要です")
            m += int(buf); buf=""; last="m"; i+=3; continue
        if ch=="m":
            if not buf: raise ValueError("m の前に数字が必要です")
            m += int(buf); buf=""; last="m"; i+=1; continue
        if ch in (" ",":","/","+"): i+=1; continue
        raise ValueError("時間指定が不正です（例: 18h10min / 90min / 30分）")
    if buf and last!="m": m += int(buf)
    return timedelta(hours=h, minutes=m)

def boat_label(boat_raw: str) -> str:
    s = normalize_boat(boat_raw)
    return f"{s}号" if s else "-"

def jstfmt(epoch_utc: float) -> str:
    return datetime.fromtimestamp(epoch_utc, tz=timezone.utc).astimezone(JST).strftime("%Y-%m-%d %H:%M JST")

@dataclass
class Task:
    id: str
    guild_id: int
    channel_id: int
    user_id: int
    fc: str
    boat: str
    note: str
    arrive_utc: float
    done_arrive: bool = False

class TaskStore:
    def __init__(self, path: str):
        self.path = path; self.tasks: Dict[str, Task] = {}; self.load()
    def load(self):
        if os.path.exists(self.path):
            with open(self.path,"r",encoding="utf-8") as f: raw=json.load(f)
            for tid, rec in raw.items(): self.tasks[tid]=Task(**rec)
        else: self.save()
    def save(self):
        with open(self.path,"w",encoding="utf-8") as f:
            json.dump({tid: vars(t) for tid,t in self.tasks.items()}, f, ensure_ascii=False, indent=2)
    def add(self, t: Task): self.tasks[t.id]=t; self.save()
    def remove(self, tid: str):
        if tid in self.tasks: del self.tasks[tid]; self.save()
    def by_guild(self, gid: int) -> list:
        return [t for t in self.tasks.values() if t.guild_id==gid]

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

async def send_arrival_notice(channel: discord.TextChannel, t: Task):
    mention = CONFIG["ROLE_MENTION"] or None
    title = f"🛥️ {t.fc or '-'} {boat_label(t.boat)}が帰ってきました"
    embed = discord.Embed(title=title, description=t.note or "")
    embed.add_field(name="到着時刻", value=jstfmt(t.arrive_utc), inline=False)
    await channel.send(content=mention, embed=embed)

async def schedule_runner():
    await client.wait_until_ready()
    while not client.is_closed():
        now = datetime.now(timezone.utc).timestamp()
        changed=False
        for t in list(client.store.tasks.values()):
            if not t.done_arrive and now >= t.arrive_utc:
                ch = client.get_channel(t.channel_id)
                if isinstance(ch, discord.TextChannel):
                    await send_arrival_notice(ch, t)
                t.done_arrive=True; changed=True
            if t.done_arrive: client.store.remove(t.id)
        if changed: client.store.save()
        await asyncio.sleep(5)

group = app_commands.Group(name="sub", description="潜水艦リマインダー（到着のみ通知）")

@group.command(name="help", description="使い方の説明（日本語）")
async def help_cmd(inter: discord.Interaction):
    text=(
        "⚓ **潜水艦リマインダー（到着のみ通知）**\\n"
        "• `/sub add duration:18h10min fc:a boat:1 note:メモ`\\n"
        "  - `fc`: a→Alexander, p→Pandemonium（前方一致OK）\\n"
        "  - `duration`: 18h10min / 90min / 30分\\n"
        "• `/sub list` / `/sub cancel id:<ID>` / `/sub defer id:<ID> delta:30min>` / `/sub edit ...`"
    )
    await inter.response.send_message(text, ephemeral=True)

@group.command(name="add", description="出航時に登録（duration または arrive のどちらか必須）")
@app_commands.describe(duration="18h10min / 90min / 30分", arrive="YYYY-MM-DD HH:MM（JST）",
                       fc="a/p（前方一致OK）", boat="艦番号 1～4", note="メモ")
async def add(inter: discord.Interaction, duration: Optional[str]=None, arrive: Optional[str]=None,
              fc: Optional[str]=None, boat: Optional[str]=None, note: Optional[str]=None):
    await inter.response.defer(ephemeral=True)
    ch=inter.channel
    if not isinstance(ch, discord.TextChannel):
        return await inter.followup.send("テキストチャンネルで実行してください。", ephemeral=True)
    if duration:
        td=parse_delta(duration); arrive_dt=datetime.now(JST)+td
    elif arrive:
        try:
            arrive_dt=datetime.strptime(unicodedata.normalize("NFKC", arrive), "%Y-%m-%d %H:%M").replace(tzinfo=JST)
        except Exception:
            return await inter.followup.send("arrive は 'YYYY-MM-DD HH:MM'（JST）で指定してください。", ephemeral=True)
    else:
        return await inter.followup.send("duration または arrive のどちらかを指定してください。", ephemeral=True)

    fc_name=normalize_fc((fc or CONFIG["DEFAULT_FC"]).strip())
    boat_name=normalize_boat((boat or CONFIG["DEFAULT_BOAT"]).strip())
    channel_id=CONFIG["FC_CHANNEL_MAP"].get(fc_name, ch.id)

    tid=os.urandom(4).hex()
    t=Task(id=tid, guild_id=inter.guild.id, channel_id=channel_id, user_id=inter.user.id,
           fc=fc_name, boat=boat_name, note=note or "", arrive_utc=arrive_dt.astimezone(timezone.utc).timestamp())
    embed = discord.Embed(
            title="✅ 登録しました",
            description="到着時刻になったらこのチャンネルに通知します。",
        )
    embed.add_field(name="FC", value=f"{t.fc}", inline=True)
    embed.add_field(name="艦番号", value=f"{t.boat}号", inline=True)
    embed.add_field(name="到着予定", value=jstfmt(t.arrive_utc), inline=False)
if t.note:
        embed.add_field(name="メモ", value=t.note, inline=False)

await inter.followup.send(embed=embed, ephemeral=True)

await inter.followup.send(embed=embed, ephemeral=True)


@group.command(name="list", description="予約一覧を表示")
async def list_cmd(inter: discord.Interaction):
    tasks = client.store.by_guild(inter.guild.id)
    if not tasks:
    return await inter.response.send_message("予約はありません。", ephemeral=True)

    # 到着が近い順に並べる
    tasks = sorted(tasks, key=lambda t: t.arrive_utc)

    # まずは応答枠を確保（時間がかかってもエラーにしない）
    await inter.response.defer(ephemeral=True)

    # 1メッセージ10個までの制限があるので分割送信
    chunk = 10
    for i in range(0, len(tasks), chunk):
        embeds = []
        for t in tasks[i:i+chunk]:
            e = discord.Embed(
                title=f"🛳️ {t.fc or '-'} {boat_label(t.boat)}",
                description="",
            )
            e.add_field(name="到着予定", value=jstfmt(t.arrive_utc), inline=False)
            if t.note:
                e.add_field(name="メモ", value=t.note, inline=False)
            embeds.append(e)

        # 一覧はチャンネルに表示
        await inter.channel.send(embeds=embeds)

    # コマンド実行者には控えめに完了通知
    await inter.followup.send(f"{len(tasks)}件の予約を表示しました。", ephemeral=True)


@group.command(name="cancel", description="予約を取消")
@app_commands.describe(id="予約ID")
async def cancel(inter: discord.Interaction, id: str):
    if id not in client.store.tasks:
        return await inter.response.send_message("IDが見つかりません。`/sub list` で確認してください。", ephemeral=True)
    client.store.remove(id)
    await inter.response.send_message("キャンセルしました。")

@group.command(name="defer", description="予約を遅延（+30min / +1h など）")
@app_commands.describe(id="予約ID", delta="例: 30min / 1h / 30分")
async def defer(inter: discord.Interaction, id: str, delta: str):
    if id not in client.store.tasks:
        return await inter.response.send_message("IDが見つかりません。`/sub list` で確認してください。", ephemeral=True)
    td=parse_delta(delta)
    t=client.store.tasks[id]; t.arrive_utc += td.total_seconds(); client.store.save()
    await inter.response.send_message(f"遅延しました。新しい到着は **{jstfmt(t.arrive_utc)}** です。")

@group.command(name="edit", description="登録内容の編集")
@app_commands.describe(id="予約ID", duration="所要時間で再計算", arrive="到着日時（JST）",
                       fc="a/p", boat="1〜4", note="メモ上書き")
async def edit_cmd(inter: discord.Interaction, id: str, duration: Optional[str]=None, arrive: Optional[str]=None,
                   fc: Optional[str]=None, boat: Optional[str]=None, note: Optional[str]=None):
    if id not in client.store.tasks:
        return await inter.response.send_message("IDが見つかりません。`/sub list` で確認してください。", ephemeral=True)
    t=client.store.tasks[id]
    if duration:
        td=parse_delta(duration); new_dt=datetime.now(JST)+td; t.arrive_utc=new_dt.astimezone(timezone.utc).timestamp()
    elif arrive:
        try:
            new_dt=datetime.strptime(unicodedata.normalize("NFKC", arrive), "%Y-%m-%d %H:%M").replace(tzinfo=JST)
            t.arrive_utc=new_dt.astimezone(timezone.utc).timestamp()
        except Exception:
            return await inter.response.send_message("arrive は 'YYYY-MM-DD HH:MM'（JST）で指定してください。", ephemeral=True)
    if fc is not None: t.fc=normalize_fc(fc)
    if boat is not None: t.boat=normalize_boat(boat)
    if note is not None: t.note=note
    client.store.save()
    await inter.response.send_message(f"更新しました。到着: **{jstfmt(t.arrive_utc)}** / FC:{t.fc or '-'} / 艦:{boat_label(t.boat)} / メモ:{t.note or '-'}")

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    try:
        await tree.sync(); print("Commands synced")
    except Exception as e:
        print("Sync error:", e)
    client.loop.create_task(schedule_runner())

tree.add_command(group)
client.store = TaskStore(DATA_FILE)

if __name__ == "__main__":
    token=os.getenv("DISCORD_TOKEN")
    if not token: raise SystemExit("環境変数 DISCORD_TOKEN が未設定です。Railway の Variables に設定してください。")
    client.run(token)
