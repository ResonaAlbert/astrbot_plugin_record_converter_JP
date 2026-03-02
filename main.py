import random
import aiofiles
import httpx  # 新增：用于发送 API 请求

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import File, Plain, Record
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .utils import (
    download_file,
    get_reply_chain,
    upload_file,
    get_file_name
)
from .config import PluginConfig


class RecordConverterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context)

    def _get_valid_gid(self, event: AiocqhttpMessageEvent) -> int:
        """安全获取群组ID。若为私聊或获取异常，返回 0"""
        gid = self.cfg.ship_gid or event.get_group_id()
        try:
            if gid is None or str(gid).strip() == "":
                return 0
            return int(gid)
        except (ValueError, TypeError):
            return 0

    async def _translate_to_japanese(self, text: str) -> str:
        """
        改用 MyMemory 免费翻译 API (中文 -> 日语)
        无需 API Key，有频率限制（普通使用足够）
        """
        if not text or not text.strip():
            return ""
        
        url = "https://api.mymemory.translated.net/get"
        params = {
            "q": text,
            "langpair": "zh-CN|ja" # 源语言中文(简) | 目标语言日语
        }
        
        try:
            async with httpx.AsyncClient() as client:
                # 设定 10 秒超时，防止 API 响应过慢导致插件卡死
                resp = await client.get(url, params=params, timeout=10.0)
                if resp.status_code == 200:
                    data = resp.json()
                    translated_text = data.get("responseData", {}).get("translatedText", "")
                    if translated_text:
                        return translated_text
                
                logger.warning(f"翻译 API 返回异常状态码: {resp.status_code}")
                return text # 失败则返回原文
        except Exception as e:
            logger.error(f"免费翻译 API 调用出错: {e}")
            return text

    @filter.command("转语音")
    async def to_record(self, event: AiocqhttpMessageEvent):
        """文件、文本 -> 语音"""
        reply_chain = get_reply_chain(event)
        seg = reply_chain[0] if reply_chain else None
        text = (
            seg.text
            if (isinstance(seg, Plain) and seg.text)
            else event.message_str.partition(" ")[2]
        )

        if isinstance(seg, File) and seg.url:
            record_file = await download_file(seg.url)
            if not record_file:
                yield event.plain_result("文件下载失败")
                return

            file_name = await get_file_name(event, record_file)
            audio_path = self.cfg.data_dir / file_name

            try:
                with open(audio_path, "wb") as f:
                    f.write(record_file)
            except Exception as e:
                yield event.plain_result(f"保存文件时出错: {e}")
                return

            yield event.chain_result([Record.fromFileSystem(audio_path)])
            return

        elif text:
            audio_url = await event.bot.get_ai_record(
                character=self.cfg.record.character_id,
                group_id=self._get_valid_gid(event),
                text=text,
            )
            if audio_url:
                yield event.chain_result([Record.fromURL(audio_url)])
            event.stop_event()

    @filter.command("日转语音")
    async def to_jp_record(self, event: AiocqhttpMessageEvent):
        """语音/文本 -> 翻译成日语 -> 语音"""
        reply_chain = get_reply_chain(event)
        seg = reply_chain[0] if reply_chain else None
        source_text = ""

        if isinstance(seg, Record):
            source_text = getattr(seg, 'text', "") 
            if not source_text:
                yield event.plain_result("暂不支持直接识别该语音内容，请尝试引用文本。")
                return
        elif isinstance(seg, Plain):
            source_text = seg.text
        else:
            source_text = event.message_str.partition(" ")[2]

        if not source_text:
            yield event.plain_result("请提供一段文本或引用一条消息")
            return

        # 1. 免费翻译逻辑
        jp_text = await self._translate_to_japanese(source_text)
        
        # 2. 生成语音
        audio_url = await event.bot.get_ai_record(
            character=self.cfg.record.character_id, 
            group_id=self._get_valid_gid(event),
            text=jp_text,
        )

        if audio_url:
            yield event.chain_result([
                Plain(f"🇯🇵 译文：{jp_text}"),
                Record.fromURL(audio_url)
            ])
        else:
            yield event.plain_result("生成日文语音失败")

    @filter.command("转文件")
    async def to_file(self, event: AiocqhttpMessageEvent):
        """语音 -> 文件"""
        reply_chain = get_reply_chain(event)
        if not reply_chain:
            yield event.plain_result("需引用一段语音")
            return

        seg = reply_chain[0]
        if isinstance(seg, Record) and seg.url:
            file_name = await get_file_name(event)
            audio_path = self.cfg.data_dir / file_name
            if file := await download_file(seg.url):
                async with aiofiles.open(audio_path, "wb") as fp:
                    await fp.write(file)
            await upload_file(
                event,
                path=audio_path,
                name=file_name,
                send_private=self.cfg.send_private,
            )
            if not event.is_private_chat() and self.cfg.send_private:
                yield event.plain_result("私发给你了")
            logger.info(f"成功转化语音文件: {seg.file} -> {file_name}")
            event.stop_event()

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AiocqhttpMessageEvent):
        """将 LLM 文本按概率生成语音并发送"""
        result = event.get_result()
        if not result or not result.chain:
            return
        
        if self.cfg.only_llm_result and not result.is_llm_result():
            return
        
        if random.random() > self.cfg.record.record_prob:
            return

        plain_text = "".join([seg.text for seg in result.chain if isinstance(seg, Plain)])

        if plain_text and len(plain_text) < self.cfg.record.max_text_len:
            audio_url = await event.bot.get_ai_record(
                character=self.cfg.record.character_id,
                group_id=self._get_valid_gid(event),
                text=plain_text,
            )
            if audio_url:
                result.chain.clear()
                result.chain.append(Record.fromURL(audio_url))
