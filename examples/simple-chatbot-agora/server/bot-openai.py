import asyncio
import os
import sys
from dotenv import load_dotenv
from loguru import logger
from PIL import Image

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
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext, OpenAILLMContextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import (
    RTVIBotTranscriptionProcessor,
    RTVIConfig,
    RTVIMetricsProcessor,
    RTVIProcessor,
    RTVISpeakingProcessor,
    RTVIUserTranscriptionProcessor, RTVIBotLLMProcessor, RTVIBotTTSProcessor
)
from pipecat.services.azure import AzureSTTService
from pipecat.services.elevenlabs import ElevenLabsTTSService
from pipecat.services.openai import OpenAILLMService, OpenAIUserContextAggregator, BaseOpenAILLMService
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

class DebugUserAggregator(OpenAIUserContextAggregator):
    async def process_frame(self, frame, direction):
        logger.debug(f"User aggregator processing frame: {frame}")
        if isinstance(frame, TranscriptionFrame):
            logger.debug(f"Adding transcription text to context: {frame.text}")
            self._context.add_message({"role": "user", "content": frame.text})
            logger.debug(f"Current context messages: {self._context.messages}")
            # 确保生成 OpenAILLMContextFrame
            await self.push_frame(OpenAILLMContextFrame(context=self._context))

        await super().process_frame(frame, direction)

class DebugLLM(OpenAILLMService):
    async def process_frame(self, frame, direction):
        logger.debug(f"LLM processing frame: {frame}")
        await super().process_frame(frame, direction)

class DebugRTVIUserTranscriptionProcessor(RTVIUserTranscriptionProcessor):
    async def process_frame(self, frame, direction):
        logger.debug(f"RTVI User Transcription processing frame: {frame}")
        await super().process_frame(frame, direction)

async def main():
    """Main bot execution."""
    # Initialize Agora transport
    transport = AgoraTransport(
        token=os.getenv("AGORA_TOKEN"),
        params=AgoraParams(
            app_id=os.getenv("AGORA_APP_ID"),
            room_id=os.getenv("AGORA_ROOM_ID"),
            uid=int(os.getenv("AGORA_UID", "0")),
            app_certificate=os.getenv("AGORA_CERTIFICATE"),
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
        api_key=os.getenv("ELEVENLABS_API_KEY"),
        voice_id="29vD33N1CtxCmqQRPOHJ",
        output_format="pcm_16000"
    )

    # Initialize LLM service
    llm = DebugLLM(
        api_key=os.getenv("OPENAI_API_KEY"),
        model="gpt-4o",
    )

    # Set up context
    messages = [{
        "role": "system",
        "content": "You are Chatbot, a friendly, helpful robot. Your goal is to demonstrate your capabilities in a succinct way. Your output will be converted to audio so don't include special characters in your answers. Respond to what the user said in a creative and helpful way, but keep your responses brief. Start by introducing yourself.",
    }]
    context = OpenAILLMContext(messages)
    context_aggregator = llm.create_context_aggregator(context)
    user_aggregator = DebugUserAggregator(context)
    assistant_aggregator = context_aggregator.assistant()

    # 添加日志打印初始上下文
    logger.debug(f"Initial context messages: {context.messages}")

    # Initialize pipeline components
    rtvi = RTVIProcessor(config=RTVIConfig(config=[]))
    rtvi_speaking = RTVISpeakingProcessor()
    rtvi_user = DebugRTVIUserTranscriptionProcessor()
    rtvi_bot = RTVIBotTranscriptionProcessor()
    rtvi_metrics = RTVIMetricsProcessor()
    # 添加必要的处理器
    rtvi_bot_llm = RTVIBotLLMProcessor()
    rtvi_bot_tts = RTVIBotTTSProcessor(direction=FrameDirection.UPSTREAM)
    ta = TalkingAnimation()

    # Build pipeline
    pipeline = Pipeline([
        transport.input(),
        rtvi,
        rtvi_speaking,
        stt_service,
        user_aggregator,
        rtvi_user,
        llm,
        rtvi_bot_llm,
        rtvi_bot,
        tts,
        # ta,
        rtvi_metrics,
        transport.output(),
        rtvi_bot_tts,
        assistant_aggregator
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