import asyncio
import logging
from datetime import datetime
from pipecat.transports.services.agora import (
    AgoraTransport,
    AgoraParams,
    AgoraCallbacks,
    AgoraTransportClient
)

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class TestAgoraReceiver:
    def __init__(self):
        # Agora配置参数
        self.app_id = "cb7e1cfa8fc043879d4449780763020f"  # 替换为你的APP ID
        self.app_certificate = "7d9a2acf356745a3b119d91c2330eede"  # 替换为你的证书
        self.room_id = "test_room"
        self.uid = 2  # 接收端UID
        self.token = "007eJxTYDjP+SxlVg5v+EtermtrIgXn/ORMeRzXefL5m4eJjuqLP31VYEhOMk81TE5LtEhLNjAxtjC3TDExMbE0tzAwNzM2MDJIW9nWkN4QyMiw/dpKFkYGCATxORlKUotL4ovy83MZGABxhSOe"  # 替换为你的Token

        # 初始化参数
        self.params = AgoraParams(
            app_id=self.app_id,
            room_id=self.room_id,
            uid=self.uid,
            app_certificate=self.app_certificate,
            video_enabled=True,
            audio_enabled=True,
            audio_in_enabled=True,
            # 添加音频配置
            audio_sample_rate=16000,
            audio_channels=1,
            audio_bytes_per_sample=2
        )

        self.transport = None
        self.exit_event = asyncio.Event()

    async def setup_callbacks(self):
        """设置回调函数"""

        # 定义所有回调处理函数
        async def on_connected():
            logger.info("已连接到 Agora 服务器")

        async def on_disconnected():
            logger.info("与 Agora 服务器断开连接")

        async def on_error(err: Exception):
            logger.error(f"发生错误: {err}")

        async def on_participant_joined(participant_id: str):
            logger.info(f"参与者加入: {participant_id}")

        async def on_participant_left(participant_id: str):
            logger.info(f"参与者离开: {participant_id}")

        async def on_first_participant_joined(participant_id: str):
            logger.info(f"第一个参与者加入: {participant_id}")

        async def on_audio_started():
            logger.info("音频开始")

        async def on_audio_stopped():
            logger.info("音频停止")

        async def on_audio_frame(participant_id: str, audio_frame):
            """处理音频帧回调"""
            logger.info(f"收到来自参与者 {participant_id} 的音频帧")
            try:
                # 可以添加基本的音频帧信息记录
                if hasattr(audio_frame, 'samples_per_channel'):
                    logger.debug(f"音频帧采样率: {audio_frame.samples_per_channel}")
                if hasattr(audio_frame, 'bytes_per_sample'):
                    logger.debug(f"每样本字节数: {audio_frame.bytes_per_sample}")
            except Exception as e:
                logger.error(f"处理音频帧时出错: {e}")

        # 创建回调对象
        self.callbacks = AgoraCallbacks(
            on_connected=on_connected,
            on_disconnected=on_disconnected,
            on_error=on_error,
            on_participant_joined=on_participant_joined,
            on_participant_left=on_participant_left,
            on_first_participant_joined=on_first_participant_joined,
            on_audio_started=on_audio_started,
            on_audio_stopped=on_audio_stopped,
            on_audio_frame=on_audio_frame,
            on_message_received=lambda msg, sender: None  # 如果不需要处理消息,可以使用空函数
        )

    async def start(self):
        """启动接收服务"""
        try:
            # 设置回调
            await self.setup_callbacks()

            # 创建transport
            self.transport = AgoraTransport(
                token=self.token,
                params=self.params
            )

            # 获取输入transport并启动
            input_transport = self.transport.input()
            await input_transport.start(None)

            logger.info(f"开始接收来自房间 {self.room_id} 的流")

            # 等待退出信号
            while not self.exit_event.is_set():
                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"发生错误: {e}")
            raise
        finally:
            if self.transport:
                # 停止并清理资源
                await self.cleanup()

    async def cleanup(self):
        """清理资源"""
        try:
            if self.transport:
                # 先停止输入传输
                input_transport = self.transport.input()
                if input_transport:
                    await input_transport.stop(None)
                # 清理 transport 客户端
                if hasattr(self.transport, '_client'):
                    client = self.transport._client
                    if client:
                        # 确保音频观察器被正确注销
                        if client._local_user and client._audio_frame_observer:
                            client._local_user.unregister_audio_frame_observer()
                        await client.cleanup()
            logger.info("清理完成")
        except Exception as e:
            logger.error(f"清理过程中发生错误: {e}")

    async def run(self, duration_seconds=60):
        """运行指定时间后退出"""
        try:
            receiver_task = asyncio.create_task(self.start())
            # 定期检查状态
            start_time = datetime.now()
            while (datetime.now() - start_time).seconds < duration_seconds:
                await asyncio.sleep(5)
                logger.info("检查音频接收状态...")
                # 添加音频状态检查
                if self.transport and self.transport._client:
                    client = self.transport._client
                    if client._audio_frame_observer:
                        logger.info("音频观察器状态: 活跃")
                    if client._local_user:
                        logger.info("本地用户状态: 已连接")
            self.exit_event.set()
            await receiver_task
        except asyncio.CancelledError:
            logger.info("接收任务被取消")
        except Exception as e:
            logger.error(f"运行过程中发生错误: {e}")
        finally:
            await self.cleanup()


async def main():
    # 创建接收器实例
    receiver = TestAgoraReceiver()

    # 运行60秒后退出
    await receiver.run(60)


if __name__ == "__main__":
    import signal
    # 创建事件循环
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # 信号处理
    def signal_handler():
        logger.info("接收到终止信号")
        loop.stop()
    try:
        loop.add_signal_handler(signal.SIGINT, signal_handler)
        loop.add_signal_handler(signal.SIGTERM, signal_handler)
        # 运行主程序
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except Exception as e:
        logger.error(f"程序运行出错: {e}")
    finally:
        loop.close()