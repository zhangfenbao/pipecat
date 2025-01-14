import sys
import asyncio
import os
from dotenv import load_dotenv
from loguru import logger
from pipecat.frames.frames import EndFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.services.elevenlabs import ElevenLabsTTSService
from pipecat.transports.services.agora import AgoraTransport, AgoraParams

# 基本配置
SAMPLE_RATE = 16000
NUM_CHANNELS = 1


async def main():
    load_dotenv(override=True)

    # 初始化传输和服务
    transport = AgoraTransport(
        token=os.getenv(
            "AGORA_TOKEN") or "007eJxTYPhamiZhXuq+4gVT3uLnEvcKvj91Lz7AZVB+pGuWWe35uyYKDMlJ5qmGyWmJFmnJBibGFuaWKSYmJpbmFgbmZsYGRgZpF1Xa0hsCGRmandpZGBkgEMTnZChJLS6JL8rPz2VgAAAfYSG5",
        params=AgoraParams(
            app_id=os.getenv("AGORA_APP_ID") or "cb7e1cfa8fc043879d4449780763020f",
            room_id="test_room",
            uid=12345,
            audio_out_enabled=True,
            audio_out_sample_rate=SAMPLE_RATE,
            audio_out_channels=NUM_CHANNELS,
            audio_out_bitrate=32000
        )
    )



    tts = ElevenLabsTTSService(
        api_key=os.getenv("ELEVENLABS_API_KEY") or "sk_ecad360f5b4888588632a9dc64eedfdb4e1f6c44d3dcb1bd",
        voice_id="29vD33N1CtxCmqQRPOHJ",
        output_format="pcm_16000"
    )

    runner = PipelineRunner()
    task = PipelineTask(Pipeline([tts, transport.output()]))

    @transport.event_handler("on_user_joined")
    async def on_user_joined(transport, participant_id):
        logger.info(f"用户加入: {participant_id}")
        await task.queue_frames([
            TTSSpeakFrame("Hello, welcome to the Agora room!"),
            EndFrame()
        ])

    try:
        logger.info("启动服务...")
        await runner.run(task)
    except Exception as e:
        logger.error(f"运行时发生错误: {e}")


if __name__ == "__main__":
    asyncio.run(main())