import asyncio
import os
from asyncio import Event

from loguru import logger

from pipecat.transports.services.agora import AgoraParams, AgoraTransport


# 定义回调函数
async def on_connected():
    logger.info("Connected to Agora room!")

async def on_disconnected():
    logger.info("Disconnected from Agora room.")

async def on_error(error: str):
    logger.error(f"Error: {error}")

async def on_participant_joined(participant_id: str):
    logger.info(f"Participant joined: {participant_id}")

async def on_participant_left(participant_id: str):
    logger.info(f"Participant left: {participant_id}")

async def on_first_participant_joined(participant_id: str):
    logger.info(f"First participant joined: {participant_id}")

async def on_message_received(message: str, sender: str):
    logger.info(f"Message received from {sender}: {message}")

async def on_audio_started(participant_id: str):
    logger.info(f"Audio started for participant: {participant_id}")

async def on_audio_stopped(participant_id: str):
    logger.info(f"Audio stopped for participant: {participant_id}")

async def on_audio_frame(participant_id: str, audio_frame):
    logger.info(f"收到音频帧 - 参与者: {participant_id}")
    logger.info(f"音频帧详情: samples={getattr(audio_frame, 'samples_per_channel', 'N/A')}, "
               f"bytes={getattr(audio_frame, 'bytes_per_sample', 'N/A')}")

async def main():
    # Agora 配置
    app_id = "cb7e1cfa8fc043879d4449780763020f"  # 替换为你的 App ID
    room_id = "test_room"

    token = "007eJxTYDjP+SxlVg5v+EtermtrIgXn/ORMeRzXefL5m4eJjuqLP31VYEhOMk81TE5LtEhLNjAxtjC3TDExMbE0tzAwNzM2MDJIW9nWkN4QyMiw/dpKFkYGCATxORlKUotL4ovy83MZGABxhSOe"  # 替换为你的 Token

    # 创建 AgoraParams
    params = AgoraParams(
        app_id=app_id,  # 替换为你的 App ID
        room_id=room_id,
        uid=1
    )

    # 创建事件循环
    # loop = asyncio.get_event_loop()

    # 创建 AgoraTransport
    transport = AgoraTransport(
        token=token,
        params=params
    )

    # 注册回调事件
    transport.on_connected = on_connected
    transport.on_disconnected = on_disconnected
    transport.on_error = on_error
    transport.on_participant_joined = on_participant_joined
    transport.on_participant_left = on_participant_left
    transport.on_first_participant_joined = on_first_participant_joined
    transport.on_message_received = on_message_received
    transport.on_audio_started = on_audio_started
    transport.on_audio_stopped = on_audio_stopped
    transport.on_audio_frame = on_audio_frame

    try:
        # 启动连接
        logger.info("Connecting to Agora room...")
        await transport._client.connect()
        # 准备发送 PCM 音频数据
        audio_file_path = "/home/python_workspace/pipecat/log_folder/test_room_3021077768.pcm"  # 替换为你的 PCM 音频文件路径
        # audio_file_path = "/home/qcc_python/test_data_202408221437/test_data/demo.pcm"  # 替换为你的 PCM 音频文件路径
        sample_rate = 16000  # 替换为你的音频采样率
        num_of_channels = 1  # 替换为音频的声道数量
        _exit_event = Event()

        if transport._client._connected:
            logger.info("Connected. Sending audio...")
            try:
                file_size = os.path.getsize(audio_file_path)
                logger.info(f"准备发送音频文件，大小: {file_size} bytes")

                await transport._client.push_pcm_data_from_file(
                    sample_rate=sample_rate,
                    num_of_channels=num_of_channels,
                    pcm_data_sender=transport._client._pcm_data_sender,
                    audio_file_path=audio_file_path,
                    _exit=_exit_event
                )
                logger.info("音频发送完成")

                # 等待一段时间确保音频发送完成
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"发送音频时出错: {e}")
        else:
            logger.error("Failed to connect to Agora room")

    except Exception as e:
        logger.error(f"Test failed: {e}")
    finally:
        # 清理资源
        await transport._client.cleanup()

# 运行测试
if __name__ == "__main__":
    asyncio.run(main())
