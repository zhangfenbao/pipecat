import asyncio
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

async def main():
    # Agora 配置
    app_id = "cb7e1cfa8fc043879d4449780763020f"  # 替换为你的 App ID
    room_id = "test_room"

    token = "007eJxTYKhxfmum/yqq8/2B1l/C2/OmLFpav2y3vrhxZpzkCelTuhcUGJKTzFMNk9MSLdKSDUyMLcwtU0xMTCzNLQzMzYwNjAzSTk8uT28IZGQo2i7CwAiFID4nQ0lqcUl8UX5+LgMDACKcIbU="  # 替换为你的 Token

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

    try:
        # 启动连接
        logger.info("Connecting to Agora room...")
        transport._client.connect()
        # 等待 2 秒确保连接成功
        await asyncio.sleep(5)
    except Exception as e:
        logger.error(f"Test failed: {e}")
    # finally:
        # 清理资源
        # await transport.cleanup()

# 运行测试
if __name__ == "__main__":
    asyncio.run(main())
