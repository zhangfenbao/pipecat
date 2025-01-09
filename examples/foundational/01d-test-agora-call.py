import asyncio
import os
from datetime import datetime
from loguru import logger
from pipecat.transports.services.agora import (
    AgoraParams,
    AgoraTransport,
    AudioFrame,
    AgoraService,
    AgoraServiceConfig,
    AudioScenarioType
)

# 配置日志
logger.add("test_agora.log", rotation="500 MB")


async def main():
    # Agora配置
    APP_ID = "cb7e1cfa8fc043879d4449780763020f"  # 替换为你的APP ID
    CHANNEL_ID = "test_room"
    UID = 999999  # 服务端UID
    TOKEN = "007eJxTYPi/84D9Ju6X/1md1urZai/Zdpnvuo2k7nPTzUd+Ks1NWc2mwJCcZJ5qmJyWaJGWbGBibGFumWJiYmJpbmFgbmZsYGSQ9sOoPr0hkJHhfMtVFkYGCATxORlKUotL4ovy83MZGAApJiJQ"  # 如果你的项目开启了token验证,需要填入token

    # 创建保存录音的文件夹
    os.makedirs("log_folder", exist_ok=True)

    try:
        # 初始化参数
        params = AgoraParams(
            app_id=APP_ID,
            room_id=CHANNEL_ID,
            uid=UID,
        )

        # 创建transport
        transport = AgoraTransport(
            token=TOKEN if TOKEN else "",
            params=params
        )

        # 启动传输
        await transport.input().start(None)
        logger.info(f"开始录制频道 {CHANNEL_ID} 的音频")

        # 等待30秒,录制客户端的音频
        await asyncio.sleep(30)
        logger.info("30秒录制完成")

        # 扫描log_folder目录找到最新的pcm文件
        pcm_files = [f for f in os.listdir("log_folder") if f.endswith('.pcm')]
        if not pcm_files:
            logger.error("未找到录制文件")
            return

        # 获取最新的录制文件
        recorded_file = os.path.join("log_folder", pcm_files[-1])
        logger.info(f"找到录制文件: {recorded_file}")

        # 等待5秒
        logger.info("等待5秒后开始回放录制的音频...")
        await asyncio.sleep(5)

        # 创建退出事件
        exit_event = asyncio.Event()

        try:
            # 获取client实例
            client = transport._client

            # 开始发送录制的音频
            logger.info("开始回放录制的音频")
            await client.push_pcm_data_from_file(
                sample_rate=16000,  # PCM采样率
                num_of_channels=1,  # 单声道
                pcm_data_sender=client._pcm_data_sender,
                audio_file_path=recorded_file,
                _exit=exit_event
            )

        except Exception as e:
            logger.error(f"回放音频时发生错误: {e}")
        finally:
            # 设置退出事件
            exit_event.set()

        # 等待一段时间确保音频播放完成
        await asyncio.sleep(10)

        # 清理资源
        await transport.input().stop(None)
        await client.cleanup()

        logger.info("测试完成")

    except Exception as e:
        logger.error(f"测试过程中发生错误: {e}")


if __name__ == "__main__":
    asyncio.run(main())