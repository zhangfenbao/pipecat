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
from pipecat.services.iflytek import IflytekSTTService
from pipecat.transcriptions.language import Language
from pipecat.transports.services.daily import DailyParams, DailyTransport

load_dotenv(override=True)
load_dotenv("../../.env")
logger.remove(0)
logger.add(sys.stderr, level="DEBUG")


class TranscriptionLogger(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            print(f"转写结果: {frame.text}")


async def main():
    async with aiohttp.ClientSession() as session:
        (room_url, _) = await configure(session)

        transport = DailyTransport(
            room_url, None, "讯飞转写机器人", DailyParams(audio_in_enabled=True)
        )

        stt = IflytekSTTService(
            app_id=os.getenv("IFLYTEK_APP_ID"),
            api_key=os.getenv("IFLYTEK_API_KEY"),
            api_secret=os.getenv("IFLYTEK_API_SECRET"),
            language=Language.ZH,  # 默认使用中文
            accent="mandarin",     # 使用普通话
        )

        tl = TranscriptionLogger()

        pipeline = Pipeline([transport.input(), stt, tl])

        task = PipelineTask(pipeline)

        runner = PipelineRunner()

        await runner.run(task)


if __name__ == "__main__":
    asyncio.run(main()) 