#
# Copyright (c) 2024
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import os
import sys

from dotenv import load_dotenv
from loguru import logger

from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.azure import AzureSTTService
from pipecat.transcriptions.language import Language

# 从agora.py导入所需的类
from pipecat.transports.services.agora import (
    AgoraParams, AgoraTransport
)

load_dotenv(override=True)
load_dotenv("../../.env")
logger.remove(0)
logger.add(sys.stderr, level="DEBUG")


# 参考Daily的TranscriptionLogger实现转录日志
class TranscriptionLogger(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            logger.info(f"转录文本: {frame.text}")
        else:
            logger.debug(f"Received non-transcription frame: {type(frame)}")


async def main():
    try:
        # 设置Azure STT服务
        stt_service  = AzureSTTService(
            api_key=os.getenv("AZURE_SPEECH_API_KEY"),
            region=os.getenv("AZURE_SPEECH_REGION"),
            language=Language.ZH_CN,  # 设置为中文
            sample_rate=16000,  # Azure支持的采样率
            channels=1  # 单声道
        )

        # 创建带转录功能的Agora传输对象
        transport = AgoraTransport(
            token=os.getenv("AGORA_TOKEN") or "007eJxTYIizW1zCVqLcqWb2dO3q85NPHLvsXvlF9c43hkvpC2e93j5dgSE5yTzVMDkt0SIt2cDE2MLcMsXExMTS3MLA3MzYwMggTdKmO70hkJFh5cQuFkYGCATxORlKUotL4ovy83MZGABqUSLW",
            params=AgoraParams(
                app_id=os.getenv("AGORA_APP_ID") or "cb7e1cfa8fc043879d4449780763020f",
                room_id=os.getenv("AGORA_ROOM_ID") or "test_room",
                uid=12345,  # 设置用户ID
                audio_in_enabled=True,
            )
        )

        # 创建日志器
        tl = TranscriptionLogger()

        # 创建 processors 列表并构造 pipeline
        processors = [
            transport.input(),  # 输入节点
            stt_service,  # STT 服务节点
            tl  # 日志节点
        ]

        # 正确创建 pipeline，传入处理器列表
        pipeline = Pipeline(processors)
        logger.info("Pipeline created successfully")

        # 创建任务
        task = PipelineTask(pipeline)
        runner = PipelineRunner()

        logger.info("Starting pipeline execution...")
        await runner.run(task)
    except Exception as e:
        logger.error(f"程序运行出错: {e}")
        raise
    finally:
        logger.info("程序运行结束")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except Exception as e:
        logger.error(f"程序异常退出: {e}")