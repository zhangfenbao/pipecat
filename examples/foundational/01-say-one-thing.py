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
from pipecat.services.cartesia import CartesiaTTSService
from pipecat.services.elevenlabs import ElevenLabsTTSService
from pipecat.transports.services.daily import DailyParams, DailyTransport

load_dotenv(override=True)

logger.remove(0)
logger.add(sys.stderr, level="DEBUG")


async def main():
    async with aiohttp.ClientSession() as session:
        (room_url, _) = await configure(session)

        transport = DailyTransport(
            room_url, None, "Say One Thing", DailyParams(audio_out_enabled=True)
        )

        # tts = CartesiaTTSService(
        #     api_key=os.getenv("CARTESIA_API_KEY") or "sk_car_z1y3lIJljARkhPvE3rtvI",
        #     voice_id="79a125e8-cd45-4c13-8a67-188112f4dd22",  # British Lady
        # )

        tts = ElevenLabsTTSService(
            api_key=os.getenv("ELEVENLABS_API_KEY") or "sk_ecad360f5b4888588632a9dc64eedfdb4e1f6c44d3dcb1bd",
            voice_id="29vD33N1CtxCmqQRPOHJ",
        )

        runner = PipelineRunner()

        task = PipelineTask(Pipeline([tts, transport.output()]))

        # Register an event handler so we can play the audio when the
        # participant joins.
        @transport.event_handler("on_first_participant_joined")
        async def on_first_participant_joined(transport, participant):
            participant_name = participant.get("info", {}).get("userName", "")
            await task.queue_frames(
                [TTSSpeakFrame(f"Hello there, nice to meet you, how are you doing?"), EndFrame()]
            )

        await runner.run(task)


if __name__ == "__main__":
    asyncio.run(main())
