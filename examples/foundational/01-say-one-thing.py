#
# Copyright (c) 2024–2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#
import sys
sys.path.append("../../src")

import asyncio
import os
import sys

import aiohttp
from dotenv import load_dotenv
from loguru import logger
from runner import configure

from pipecat.frames.frames import EndFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.services.bytedance import ByteDanceTTSService
from pipecat.transports.services.daily import DailyParams, DailyTransport

load_dotenv(override=True)
load_dotenv("../../.env")
logger.remove(0)
logger.add(sys.stderr, level="DEBUG")


async def main():
    async with aiohttp.ClientSession() as session:
        (room_url, _) = await configure(session)

        transport = DailyTransport(
            room_url, None, "Say One Thing", DailyParams(audio_out_enabled=True)
        )

        tts = ByteDanceTTSService(
            app_id=os.getenv("VOLC_APP_ID"),
            access_token=os.getenv("VOLC_ACCESS_TOKEN"),
            params=ByteDanceTTSService.InputParams(
                voice_type="zh_male_M392_conversation_wvae_bigtts",  # 使用中文男声
                speed_ratio=1.0,
                volume_ratio=1.0,
                pitch_ratio=1.0,
                debug=True
            )
        )

        runner = PipelineRunner()

        task = PipelineTask(Pipeline([tts, transport.output()]))

        # Register an event handler so we can play the audio when the
        # participant joins.
        @transport.event_handler("on_first_participant_joined")
        async def on_first_participant_joined(transport, participant):
            participant_name = participant.get("info", {}).get("userName", "")
            await task.queue_frames(
                [TTSSpeakFrame("你好，很高兴见到你，你今天感觉如何？"), EndFrame()]  # 使用中文问候语
            )

        await runner.run(task)


if __name__ == "__main__":
    asyncio.run(main())
