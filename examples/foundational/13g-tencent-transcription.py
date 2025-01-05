#
# Copyright (c) 2024, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import os
import sys

import aiohttp
from dotenv import load_dotenv
from loguru import logger
from runner import configure

from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.tencent import TencentSTTService
from pipecat.transports.services.daily import DailyParams, DailyTransport

load_dotenv(override=True)
load_dotenv("../../.env")

logger.remove(0)
logger.add(sys.stderr, level="DEBUG")


class TranscriptionLogger(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            print(f"Transcription: {frame.text}")


async def main():
    async with aiohttp.ClientSession() as session:
        (room_url, _) = await configure(session)

        transport = DailyTransport(
            room_url, None, "Transcription bot", DailyParams(audio_in_enabled=True)
        )

        stt = TencentSTTService(
            app_id=os.getenv("TENCENT_APP_ID"),
            secret_id=os.getenv("TENCENT_SECRET_ID"),
            secret_key=os.getenv("TENCENT_SECRET_KEY"),
            engine_model_type="16k_zh",  # 使用中文16k采样率模型
            voice_format=1,  # PCM格式
            need_vad=1,      # 启用VAD
            filter_dirty=1,  # 过滤脏话
            filter_modal=1,  # 过滤语气词
            filter_punc=1,   # 过滤标点符号
            convert_num_mode=1,  # 数字转换模式
            word_info=1,     # 启用词信息
        )

        tl = TranscriptionLogger()

        pipeline = Pipeline([transport.input(), stt, tl])

        task = PipelineTask(pipeline)

        runner = PipelineRunner()

        await runner.run(task)


if __name__ == "__main__":
    asyncio.run(main()) 