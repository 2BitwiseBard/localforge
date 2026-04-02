#!/usr/bin/env python3
"""Telegram bot bridge for AI Hub.

Bridges Telegram messages (text, voice, photos) to the MCP gateway.
Uses raw Telegram Bot API via httpx — zero extra dependencies.

Setup:
  1. Message @BotFather on Telegram → /newbot → get token
  2. Set token in config.yaml → telegram.bot_token
  3. Start: systemctl --user start telegram-bot
  4. Message your bot on Telegram

Usage:
  python3 telegram_bot.py
"""

import asyncio
import base64
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import httpx
import yaml

log = logging.getLogger("telegram-bot")

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


class TelegramBot:
    def __init__(self):
        cfg = _load_config()
        self.bot_token = cfg.get("telegram", {}).get("bot_token", "")
        self.allowed_chats = set(cfg.get("telegram", {}).get("allowed_chat_ids", []))

        gateway_cfg = cfg.get("gateway", {})
        port = gateway_cfg.get("port", 8100)
        api_keys = gateway_cfg.get("api_keys", [])

        self.gateway_url = f"http://localhost:{port}"
        self.api_key = api_keys[0] if api_keys else ""
        self.tg_api = f"https://api.telegram.org/bot{self.bot_token}"
        self.offset = 0

    def _is_allowed(self, chat_id: int) -> bool:
        if not self.allowed_chats:
            return True  # no restriction
        return chat_id in self.allowed_chats

    async def send_message(self, chat_id: int, text: str, client: httpx.AsyncClient):
        """Send a text message, splitting if > 4096 chars."""
        for i in range(0, len(text), 4096):
            chunk = text[i:i + 4096]
            await client.post(f"{self.tg_api}/sendMessage", json={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "Markdown",
            })

    async def handle_text(self, chat_id: int, text: str, client: httpx.AsyncClient):
        """Handle a text message — chat with local model."""
        # Special commands
        if text.startswith("/research "):
            query = text[10:].strip()
            return await self._gateway_tool(chat_id, "deep_research", {
                "question": query,
            }, client, timeout=120)

        if text.startswith("/search "):
            query = text[8:].strip()
            return await self._gateway_tool(chat_id, "web_search", {
                "query": query,
            }, client)

        if text == "/status":
            try:
                resp = await client.get(f"{self.gateway_url}/health", timeout=5)
                data = resp.json()
                model = data.get("model", {}).get("model_name", "unknown")
                uptime = data.get("uptime_seconds", 0)
                h, m = divmod(int(uptime) // 60, 60)
                await self.send_message(chat_id,
                    f"*AI Hub Status*\nModel: `{model}`\nUptime: {h}h {m}m\nStatus: {data.get('status', 'unknown')}",
                    client)
            except Exception as e:
                await self.send_message(chat_id, f"Error: {e}", client)
            return

        if text == "/help":
            await self.send_message(chat_id,
                "*AI Hub Bot*\n\n"
                "Send any message to chat with the AI.\n\n"
                "*Commands:*\n"
                "/research <question> — Deep web research\n"
                "/search <query> — Quick web search\n"
                "/status — System status\n"
                "/help — This message\n\n"
                "*Media:*\n"
                "Send a photo for vision analysis\n"
                "Send a voice message for transcription + chat",
                client)
            return

        # Regular chat
        try:
            resp = await client.post(
                f"{self.gateway_url}/api/chat",
                json={"prompt": text},
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                timeout=90,
            )
            # Parse SSE stream
            full_text = ""
            for line in resp.text.splitlines():
                if line.startswith("data: "):
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        if chunk.get("content"):
                            full_text += chunk["content"]
                        if chunk.get("error"):
                            full_text += f"\n[Error: {chunk['error']}]"
                    except json.JSONDecodeError:
                        continue

            await self.send_message(chat_id, full_text or "No response", client)
        except Exception as e:
            await self.send_message(chat_id, f"Chat error: {e}", client)

    async def handle_voice(self, chat_id: int, file_id: str, client: httpx.AsyncClient):
        """Handle voice message: download → transcribe → chat."""
        try:
            # Get file path from Telegram
            file_resp = await client.get(f"{self.tg_api}/getFile", params={"file_id": file_id})
            file_path = file_resp.json()["result"]["file_path"]

            # Download audio
            audio_resp = await client.get(
                f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"
            )
            audio_bytes = audio_resp.content

            # Transcribe via gateway
            transc_resp = await client.post(
                f"{self.gateway_url}/api/transcribe",
                files={"file": ("voice.ogg", audio_bytes, "audio/ogg")},
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=30,
            )
            transc_data = transc_resp.json()
            text = transc_data.get("text", "")

            if text:
                await self.send_message(chat_id, f"_Heard: {text}_", client)
                await self.handle_text(chat_id, text, client)
            else:
                await self.send_message(chat_id, "Could not transcribe audio.", client)
        except Exception as e:
            await self.send_message(chat_id, f"Voice error: {e}", client)

    async def handle_photo(self, chat_id: int, file_id: str,
                           caption: str, client: httpx.AsyncClient):
        """Handle photo: download → analyze with vision model."""
        try:
            # Get file path
            file_resp = await client.get(f"{self.tg_api}/getFile", params={"file_id": file_id})
            file_path = file_resp.json()["result"]["file_path"]

            # Download photo
            photo_resp = await client.get(
                f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"
            )
            photo_bytes = photo_resp.content

            # Upload to gateway for analysis
            resp = await client.post(
                f"{self.gateway_url}/api/upload-image",
                files={"image": ("photo.jpg", photo_bytes, "image/jpeg")},
                data={"question": caption or "Describe this image in detail."},
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=90,
            )

            # Parse SSE response
            full_text = ""
            for line in resp.text.splitlines():
                if line.startswith("data: "):
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        full_text += chunk.get("content", "")
                        if chunk.get("error"):
                            full_text += chunk["error"]
                    except json.JSONDecodeError:
                        continue

            await self.send_message(chat_id, full_text or "No analysis available", client)
        except Exception as e:
            await self.send_message(chat_id, f"Photo error: {e}", client)

    async def _gateway_tool(self, chat_id: int, tool: str, args: dict,
                            client: httpx.AsyncClient, timeout: int = 60):
        """Call an MCP tool via the gateway and send result."""
        try:
            # Initialize MCP session
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {self.api_key}",
            }
            url = f"{self.gateway_url}/mcp/"

            await client.post(url, json={
                "jsonrpc": "2.0", "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "telegram-bot", "version": "1.0"},
                },
                "id": 1,
            }, headers=headers, timeout=10)

            resp = await client.post(url, json={
                "jsonrpc": "2.0", "method": "tools/call",
                "params": {"name": tool, "arguments": args},
                "id": 2,
            }, headers=headers, timeout=timeout)

            # Parse SSE
            result_text = ""
            for line in resp.text.splitlines():
                if line.startswith("data: "):
                    try:
                        data = json.loads(line[6:])
                        if "result" in data:
                            content = data["result"].get("content", [])
                            for item in content:
                                if isinstance(item, dict) and item.get("type") == "text":
                                    result_text += item["text"]
                    except Exception:
                        continue

            await self.send_message(chat_id, result_text or "No result", client)
        except Exception as e:
            await self.send_message(chat_id, f"Tool error: {e}", client)

    async def poll(self):
        """Long-poll for Telegram updates."""
        async with httpx.AsyncClient(timeout=60) as client:
            log.info("Telegram bot started polling...")
            while True:
                try:
                    resp = await client.get(f"{self.tg_api}/getUpdates", params={
                        "offset": self.offset,
                        "timeout": 30,
                    }, timeout=40)

                    if resp.status_code != 200:
                        log.error(f"Telegram API error: {resp.status_code}")
                        await asyncio.sleep(5)
                        continue

                    updates = resp.json().get("result", [])

                    for update in updates:
                        self.offset = update["update_id"] + 1
                        message = update.get("message", {})
                        chat_id = message.get("chat", {}).get("id")

                        if not chat_id or not self._is_allowed(chat_id):
                            continue

                        # Route by message type
                        if "voice" in message:
                            await self.handle_voice(
                                chat_id, message["voice"]["file_id"], client
                            )
                        elif "photo" in message:
                            # Use highest resolution photo
                            photo = message["photo"][-1]
                            await self.handle_photo(
                                chat_id, photo["file_id"],
                                message.get("caption", ""), client
                            )
                        elif "text" in message:
                            await self.handle_text(chat_id, message["text"], client)

                except httpx.TimeoutException:
                    continue  # normal long-poll timeout
                except Exception as e:
                    log.exception(f"Poll error: {e}")
                    await asyncio.sleep(5)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    cfg = _load_config()
    bot_token = cfg.get("telegram", {}).get("bot_token", "")
    if not bot_token:
        print("No Telegram bot token configured.")
        print("Set telegram.bot_token in config.yaml")
        print("Get a token from https://t.me/BotFather")
        sys.exit(1)

    bot = TelegramBot()
    asyncio.run(bot.poll())


if __name__ == "__main__":
    main()
