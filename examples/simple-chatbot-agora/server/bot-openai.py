import asyncio
import os
import sys
from dotenv import load_dotenv
from loguru import logger
from PIL import Image

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    Frame,
    LLMMessagesFrame,
    OutputImageRawFrame,
    SpriteFrame, TranscriptionFrame, UserStartedSpeakingFrame, UserStoppedSpeakingFrame, StartInterruptionFrame,
    StopInterruptionFrame, TransportMessageUrgentFrame
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import (
    RTVIBotTranscriptionProcessor,
    RTVIConfig,
    RTVIMetricsProcessor,
    RTVIProcessor,
    RTVISpeakingProcessor,
    RTVIUserTranscriptionProcessor
)
from pipecat.services.azure import AzureSTTService
from pipecat.services.elevenlabs import ElevenLabsTTSService
from pipecat.services.openai import OpenAILLMService
from pipecat.transcriptions.language import Language
from pipecat.transports.services.agora import AgoraTransport, AgoraParams, TranscriptionStateFrame

# 创建日志输出目录
log_dir = "logs"
os.makedirs(log_dir, exist_ok=True)
# Load environment variables and configure logging
load_dotenv(override=True)
logger.remove(0)
logger.add(sys.stderr, level="DEBUG")
logger.add(f"{log_dir}/bot_agora.log", rotation="500 MB")

# 加载动画资源
sprites = []
script_dir = os.path.dirname(__file__)

for i in range(1, 26):
    full_path = os.path.join(script_dir, f"assets/robot0{i}.png")
    with Image.open(full_path) as img:
        sprites.append(OutputImageRawFrame(image=img.tobytes(), size=img.size, format=img.format))

# Create animation frames
flipped = sprites[::-1]
sprites.extend(flipped)
quiet_frame = sprites[0]
talking_frame = SpriteFrame(images=sprites)


class TranscriptionFrameEnricher(FrameProcessor):
    """Enriches TranscriptionFrame with missing information."""

    def __init__(self, user_id: str = "", language: Language = Language.ZH_CN):
        super().__init__()
        self._user_id = user_id
        self._language = language

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, TranscriptionFrame):
            # 创建一个新的TranscriptionFrame，包含所有必要的信息
            enriched_frame = TranscriptionFrame(
                text=frame.text,
                user_id=self._user_id if frame.user_id is None or frame.user_id == "" else frame.user_id,
                timestamp=frame.timestamp,
                language=self._language if frame.language is None else frame.language,
            )
            logger.info(f"Enriched transcription frame: {enriched_frame}")
            await self.push_frame(enriched_frame, direction)
            return

        # 非TranscriptionFrame直接传递
        await self.push_frame(frame, direction)


class TalkingAnimation(FrameProcessor):
    """Manages bot animation states."""

    def __init__(self):
        super().__init__()
        self._is_talking = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        logger.info(f"TalkingAnimation.Processing frame AAA:frame= {frame}")
        # await super().process_frame(frame, direction)

        # if isinstance(frame, BotStartedSpeakingFrame):
        #     if not self._is_talking:
        #         await self.push_frame(talking_frame)
        #         self._is_talking = True
        # elif isinstance(frame, BotStoppedSpeakingFrame):
        #     await self.push_frame(quiet_frame)
        #     self._is_talking = False

        await self.push_frame(frame, direction)

async def main():
    """Main bot execution."""
    # Initialize Agora transport
    transport = AgoraTransport(
        token=os.getenv("AGORA_TOKEN") or "007eJxTYFi3/u/8X+3ZLvJ+/68GuQskVPLxzTvZs/XldB22s98MfnxVYEhOMk81TE5LtEhLNjAxtjC3TDExMbE0tzAwNzM2MDJIk1Vfkt4QyMgQfpCXiZEBAkF8ToaS1OKS+KL8/FwGBgBdEyHy",
        params=AgoraParams(
            app_id=os.getenv("AGORA_APP_ID") or "cb7e1cfa8fc043879d4449780763020f",
            room_id=os.getenv("AGORA_ROOM_ID") or "test_room",
            uid=int(os.getenv("AGORA_UID", "0") or "12345"),
            app_certificate=os.getenv("AGORA_CERTIFICATE") or "7d9a2acf356745a3b119d91c2330eede",
            audio_in_enabled=True,
            audio_out_enabled=True,
        )
    )

    # 设置Azure STT服务
    stt_service = AzureSTTService(
        api_key=os.getenv("AZURE_SPEECH_API_KEY"),
        region=os.getenv("AZURE_SPEECH_REGION"),
        language=Language.ZH_CN,  # 设置为中文
        sample_rate=16000,  # Azure支持的采样率
        channels=1  # 单声道
    )

    # Initialize TTS service
    tts = ElevenLabsTTSService(
        api_key=os.getenv("ELEVENLABS_API_KEY") or "sk_ecad360f5b4888588632a9dc64eedfdb4e1f6c44d3dcb1bd",
        voice_id="29vD33N1CtxCmqQRPOHJ",
        output_format="pcm_16000"
    )

    # Initialize LLM service
    llm = OpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        model="gpt-4o"
    )

    # Set up context
    messages = [{
        "role": "system",
        "content": "You are Chatbot, a friendly, helpful robot. Your goal is to demonstrate your capabilities in a succinct way. Your output will be converted to audio so don't include special characters in your answers. Respond to what the user said in a creative and helpful way, but keep your responses brief. Start by introducing yourself.",
    }]
    context = OpenAILLMContext(messages)
    context_aggregator = llm.create_context_aggregator(context)

    # Initialize pipeline components
    rtvi = RTVIProcessor(config=RTVIConfig(config=[]))
    rtvi_speaking = RTVISpeakingProcessor()
    rtvi_user = RTVIUserTranscriptionProcessor()
    rtvi_bot = RTVIBotTranscriptionProcessor()
    rtvi_metrics = RTVIMetricsProcessor()
    ta = TalkingAnimation()

    # Build pipeline
    pipeline = Pipeline([
        transport.input(),
        stt_service,
        TranscriptionFrameEnricher(user_id="1", language=Language.ZH_CN),
        rtvi,
        rtvi_speaking,
        rtvi_user,
        context_aggregator.user(),
        llm,
        rtvi_bot,
        tts,
        ta,
        rtvi_metrics,
        transport.output(),
        context_aggregator.assistant()
    ])

    # Create pipeline task
    task = PipelineTask(
        pipeline,
        PipelineParams(
            allow_interruptions=True,
            enable_metrics=True,
            enable_usage_metrics=True,
        )
    )
    await task.queue_frame(quiet_frame)

    # Set up event handlers
    @rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        logger.info(f"on_client_ready,rtvi:{rtvi}")
        await rtvi.set_bot_ready()

    @transport.event_handler("on_user_joined")
    async def on_user_joined(transport, participant):
        logger.info(f"User joined: {participant}")
        await task.queue_frames([LLMMessagesFrame(messages)])

    @transport.event_handler("on_user_left")
    async def on_user_left(transport, participant_id):
        logger.info(f"User left: {participant_id}")
        await task.queue_frame(EndFrame())

    # Run pipeline
    runner = PipelineRunner()
    await runner.run(task)

if __name__ == "__main__":
    asyncio.run(main())