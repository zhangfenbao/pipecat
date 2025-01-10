#
# Copyright (c) 2024, Test
#
# SPDX-License-Identifier: BSD 2-Clause License
#
import sys
from asyncio import Event

from agora.rtc.audio_pcm_data_sender import AudioPcmDataSender

sys.path.append("../../src")

import asyncio
import os
from dotenv import load_dotenv
from loguru import logger

from pipecat.frames.frames import EndFrame, TTSSpeakFrame, OutputAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.services.elevenlabs import ElevenLabsTTSService
from pipecat.transports.services.agora import AgoraTransport, AgoraParams

load_dotenv(override=True)

logger.remove(0)
logger.add(sys.stderr, level="DEBUG")

def load_pcm_file(filepath: str) -> bytes:
    """加载 PCM 文件"""
    with open(filepath, 'rb') as f:
        return f.read()

async def main():
    # 配置 Agora 参数 - 暂时不使用 token
    app_id = os.getenv("AGORA_APP_ID", "cb7e1cfa8fc043879d4449780763020f")  # 如果环境变量未设置，使用空字符串
    room_id = "test_room"  # 频道名
    uid = 12345  # 用户ID
    token = "007eJxTYDjP+SxlVg5v+EtermtrIgXn/ORMeRzXefL5m4eJjuqLP31VYEhOMk81TE5LtEhLNjAxtjC3TDExMbE0tzAwNzM2MDJIW9nWkN4QyMiw/dpKFkYGCATxORlKUotL4ovy83MZGABxhSOe"  # 替换为你的 Token

    pcm_file_path = "/home/qcc_python/test_data_202408221437/test_data/demo.pcm"  # PCM 文件路径
    sample_rate = 16000  # PCM 文件采样率
    num_of_channels = 1  # 声道数 (单声道)

    # 初始化 TTS 服务
    tts = ElevenLabsTTSService(
        api_key=os.getenv("ELEVENLABS_API_KEY") or "sk_ecad360f5b4888588632a9dc64eedfdb4e1f6c44d3dcb1bd",
        voice_id="29vD33N1CtxCmqQRPOHJ",
    )

    # 初始化 Agora 参数，启用音视频
    agora_params = AgoraParams(
        app_id=app_id,
        room_id=room_id,
        uid=uid,
        audio_out_enabled=True,
        video_enabled=True  # 启用视频功能
    )

    # 初始化 transport，暂时不使用 token
    transport = AgoraTransport(token=token, params=agora_params)

    # 创建 PCM 数据发送器
    _exit_event = Event()

    runner = PipelineRunner()
    # 创建包含 transport 的管道（不再使用 TTS 服务）
    task = PipelineTask(Pipeline([transport.output()]))

    # 注册连接事件处理器
    @transport.event_handler("on_connected")
    async def on_connected(transport):
        logger.info("成功连接到 Agora 频道")

    @transport.event_handler("on_error")
    async def on_error(transport, error):
        logger.error(f"Agora 错误: {error}")

    # 处理参与者加入/离开事件
    @transport.event_handler("on_user_joined")
    async def on_user_joined(transport, participant_id):
        logger.info(f"参与者加入: {participant_id}")
        # try:
        #     logger.info(f"开始为参与者 {participant_id} 播放欢迎音频")
        #     # 加载 PCM 文件
        #     pcm_filepath = "/home/qcc_python/test_data_202408221437/test_data/demo.pcm"  # 替换为实际 PCM 文件路径
        #     pcm_data = load_pcm_file(pcm_filepath)
        #     # 创建 PCM 音频帧
        #     sample_rate = 16000  # 替换为实际采样率
        #     num_channels = 1  # 替换为实际声道数
        #     audio_frame = OutputAudioRawFrame(
        #         audio=pcm_data,
        #         sample_rate=sample_rate,
        #         num_channels=num_channels
        #     )
        #     # 将 PCM 音频帧入队到管道
        #     await task.queue_frames([audio_frame])
        #     # 延时等待播放完成（根据 PCM 数据时长计算）
        #     playback_duration = len(pcm_data) / (sample_rate * num_channels * 2)  # 数据长度 / 采样率 / 声道数 / 每采样字节数
        #     logger.info(f"播放时长: {playback_duration:.2f} 秒，延时等待播放完成...")
        #     await asyncio.sleep(playback_duration + 1)  # 加1秒缓冲
        #     logger.info("欢迎音频播放完成")
        # except Exception as e:
        #     logger.error(f"播放欢迎语失败: {e}")
        logger.info(f"参与者加入: {participant_id}")
        try:
            logger.info("开始播放欢迎音频")
            # 调用 push_pcm_data_from_file 方法播放 PCM 数据
            await transport._client.push_pcm_data_from_file(
                sample_rate=sample_rate,
                num_of_channels=num_of_channels,
                pcm_data_sender=transport._client._pcm_data_sender,
                audio_file_path=pcm_file_path,
                _exit=_exit_event,
            )
            logger.info("欢迎音频播放完成")
        except Exception as e:
            logger.error(f"播放欢迎音频失败: {e}")

    @transport.event_handler("on_user_left")
    async def on_user_left(transport, participant_id):
        logger.info(f"参与者离开: {participant_id}")

    try:
        # 运行管道
        await runner.run(task)
    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except Exception as e:
        logger.error(f"运行出错: {e}")
    finally:
        # 清理资源
        await transport._client.cleanup()

if __name__ == "__main__":
    asyncio.run(main())