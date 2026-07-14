from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from scout.config import Settings
from scout.db import Database

logger = logging.getLogger(__name__)


class ScoutDiscordBot(commands.Bot):
    def __init__(self, db: Database, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.db = db
        self.settings = settings
        self._channel: discord.abc.Messageable | None = None

    async def setup_hook(self) -> None:
        self.tree.add_command(hunts_cmd)
        self.tree.add_command(pause_cmd)
        self.tree.add_command(resume_cmd)
        # Bind bot ref for commands
        hunts_cmd.bot_ref = self  # type: ignore[attr-defined]
        pause_cmd.bot_ref = self  # type: ignore[attr-defined]
        resume_cmd.bot_ref = self  # type: ignore[attr-defined]
        if self.settings.discord_channel_id:
            try:
                await self.tree.sync()
            except Exception:
                logger.exception("discord command sync failed")

    async def on_ready(self) -> None:
        logger.info("Discord bot ready as %s", self.user)
        if self.settings.discord_channel_id:
            channel = self.get_channel(int(self.settings.discord_channel_id))
            if channel is None:
                try:
                    channel = await self.fetch_channel(int(self.settings.discord_channel_id))
                except Exception:
                    logger.exception("could not fetch discord channel")
                    channel = None
            self._channel = channel  # type: ignore[assignment]

    def _allowed(self, user_id: int) -> bool:
        allowed = self.settings.allowed_user_ids
        return (not allowed) or (user_id in allowed)

    async def send_match_alert(
        self,
        hunt: dict[str, Any],
        listing: dict[str, Any],
        evaluation: dict[str, Any],
    ) -> str | None:
        if not self._channel:
            if self.settings.discord_channel_id:
                try:
                    self._channel = await self.fetch_channel(  # type: ignore[assignment]
                        int(self.settings.discord_channel_id)
                    )
                except Exception:
                    logger.exception("discord channel fetch failed")
                    return None
            else:
                return None

        price = listing.get("price")
        price_s = f"${price:,.0f}" if price is not None else "n/a"
        miles = evaluation.get("drive_miles")
        miles_s = f"{miles:.1f} mi" if miles is not None else "distance n/a"
        outlier = evaluation.get("price_outlier")
        outlier_s = f" · {outlier}" if outlier else ""

        embed = discord.Embed(
            title=listing["title"][:250],
            url=listing["url"],
            description=evaluation.get("reason", "")[:400],
            color=0x2F6FED,
        )
        embed.add_field(name="Hunt", value=hunt["query"][:100], inline=False)
        embed.add_field(name="Price", value=price_s, inline=True)
        embed.add_field(name="Distance", value=miles_s, inline=True)
        embed.add_field(
            name="Confidence",
            value=f"{evaluation.get('confidence', 0):.0%} ({evaluation.get('tier_used')}){outlier_s}",
            inline=True,
        )
        embed.add_field(name="Source", value=listing.get("source", "?"), inline=True)
        images = listing.get("images") or []
        if images:
            embed.set_thumbnail(url=images[0])

        msg = await self._channel.send(embed=embed)  # type: ignore[union-attr]
        return str(msg.id)

    async def _ensure_channel(self) -> discord.abc.Messageable | None:
        if self._channel:
            return self._channel
        if not self.settings.discord_channel_id:
            return None
        try:
            self._channel = await self.fetch_channel(  # type: ignore[assignment]
                int(self.settings.discord_channel_id)
            )
        except Exception:
            logger.exception("discord channel fetch failed")
            return None
        return self._channel

    async def send_status(self, message: str) -> None:
        channel = await self._ensure_channel()
        if not channel:
            return
        embed = discord.Embed(
            title="Marketplace Scout",
            description=message[:1800],
            color=0xF0C45A,
        )
        await channel.send(embed=embed)


def _bot_from(interaction: discord.Interaction) -> ScoutDiscordBot | None:
    # commands store bot_ref
    return getattr(interaction.command, "bot_ref", None) if interaction.command else None


@app_commands.command(name="hunts", description="List active Marketplace Scout hunts")
async def hunts_cmd(interaction: discord.Interaction) -> None:
    bot = interaction.client
    if not isinstance(bot, ScoutDiscordBot):
        await interaction.response.send_message("Bot not ready", ephemeral=True)
        return
    if not bot._allowed(interaction.user.id):
        await interaction.response.send_message("Not allowlisted", ephemeral=True)
        return
    hunts = bot.db.list_hunts()
    if not hunts:
        await interaction.response.send_message("No hunts yet.", ephemeral=True)
        return
    lines = []
    for h in hunts:
        status = "active" if h["active"] else "paused"
        lines.append(f"`#{h['id']}` **{h['query']}** — {status}, max ${h.get('max_price') or '∞'}, {h['max_miles']} mi")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@app_commands.command(name="pause", description="Pause a hunt by id")
@app_commands.describe(hunt_id="Hunt id")
async def pause_cmd(interaction: discord.Interaction, hunt_id: int) -> None:
    bot = interaction.client
    if not isinstance(bot, ScoutDiscordBot):
        await interaction.response.send_message("Bot not ready", ephemeral=True)
        return
    if not bot._allowed(interaction.user.id):
        await interaction.response.send_message("Not allowlisted", ephemeral=True)
        return
    updated = bot.db.update_hunt(hunt_id, {"active": False})
    if not updated:
        await interaction.response.send_message("Hunt not found", ephemeral=True)
        return
    await interaction.response.send_message(f"Paused hunt #{hunt_id}", ephemeral=True)


@app_commands.command(name="resume", description="Resume a hunt by id")
@app_commands.describe(hunt_id="Hunt id")
async def resume_cmd(interaction: discord.Interaction, hunt_id: int) -> None:
    bot = interaction.client
    if not isinstance(bot, ScoutDiscordBot):
        await interaction.response.send_message("Bot not ready", ephemeral=True)
        return
    if not bot._allowed(interaction.user.id):
        await interaction.response.send_message("Not allowlisted", ephemeral=True)
        return
    updated = bot.db.update_hunt(hunt_id, {"active": True})
    if not updated:
        await interaction.response.send_message("Hunt not found", ephemeral=True)
        return
    await interaction.response.send_message(f"Resumed hunt #{hunt_id}", ephemeral=True)
