import random
import aiofiles

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

    async def _translate_to_japanese(self, text: str) -> str:
        """调用插件关联的 LLM 将文本翻译为日语"""
        if not text.strip():
            return ""
        prompt = f"请将以下内容翻译成日语，仅输出翻译结果，不要包含任何解释或引号：\n{text}"
        try:
            # 使用 AstrBot 核心提供的主 LLM 实例进行翻译
            result = await self.context.get_main_llm().request(prompt)
            return result.completion_text.strip()
        except Exception as e:
            logger.error(f"翻译至日语失败: {e}")
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

        # 文件 -> 语音
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

        # 文本 -> 语音
        elif text:
            group_id = self.cfg.ship_gid or event.get_group_id()
            audio_url = await event.bot.get_ai_record(
                character=self.cfg.record.character_id,
                group_id=int(group_id),
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

        # 情况 A: 引用的是语音 (需要 STT)
        if isinstance(seg, Record):
            # 注意：某些平台 bot 实例可能不直接暴露识别接口，这里依赖 AstrBot 的 LLM 多模态能力或特定 API
            # 简化逻辑：尝试从事件获取识别出的文本（如果平台已处理）
            source_text = getattr(seg, 'text', "") 
            if not source_text:
                yield event.plain_result("暂不支持直接识别该语音内容，请尝试引用文本。")
                return

        # 情况 B: 引用的是文本
        elif isinstance(seg, Plain):
            source_text = seg.text

        # 情况 C: 直接输入的文本
        else:
            source_text = event.message_str.partition(" ")[2]

        if not source_text:
            yield event.plain_result("请提供一段文本或引用一条消息")
            return

        # 1. 翻译逻辑
        jp_text = await self._translate_to_japanese(source_text)
        
        # 2. 生成语音
        group_id = self.cfg.ship_gid or event.get_group_id()
        audio_url = await event.bot.get_ai_record(
            character=self.cfg.record.character_id, 
            group_id=int(group_id),
            text=jp_text,
        )

        if audio_url:
            # 返回译文参考和语音
            yield event.chain_result([
                Plain(f"🇯🇵 日语翻译：{jp_text}"),
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

        # 语音 -> 文件
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
            return

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AiocqhttpMessageEvent):
        """将文本按概率生成语音并发送"""
        result = event.get_result()
        if not result:
            return
        chain = result.chain
        if not chain:
            return
        # 仅处理LLM消息
        if self.cfg.only_llm_result and not result.is_llm_result():
            return
        # 概率控制
        if random.random() > self.cfg.record.record_prob:
            return

        seg = chain[0]
        # 纯短文本
        if (
            len(chain) == 1
            and isinstance(seg, Plain)
            and len(seg.text) < self.cfg.record.max_text_len
        ):
            group_id = self.cfg.ship_gid or event.get_group_id()
            audio_url = await event.bot.get_ai_record(
                character=self.cfg.record.character_id,
                group_id=int(group_id),
                text=seg.text,
            )
            if audio_url:
                chain.clear()
                chain.append(Record.fromURL(audio_url))
