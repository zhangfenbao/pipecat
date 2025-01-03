import asyncio
from asyncio import Event
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List, Optional
import time
from agora.rtc.video_frame_sender import VideoFrameSender
from pydantic import BaseModel
from loguru import logger

from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    StartFrame,
    TransportMessageFrame,
    TransportMessageUrgentFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

# import agora的sdk
from agora.rtc.agora_base import (
    AgoraServiceConfig,
    AudioScenarioType,
    RTCConnConfig,
    ClientRoleType,
    ChannelProfileType, PcmAudioFrame, ExternalVideoFrame)
from agora.rtc.agora_service import AgoraService
from agora.rtc.rtc_connection_observer import IRTCConnectionObserver
from agora.rtc.audio_pcm_data_sender import AudioPcmDataSender

@dataclass
class AgoraTransportMessageFrame(TransportMessageFrame):
    participant_id: str | None = None

@dataclass
class AgoraTransportMessageUrgentFrame(TransportMessageUrgentFrame):
    participant_id: str | None = None

class AgoraParams(TransportParams):
    app_id: str = ""
    room_id: str = ""
    uid: int = 0
    app_certificate: str = ""  # 用于生成 token

class AgoraCallbacks(BaseModel):
    # 基础事件回调
    on_connected: Callable[[], Awaitable[None]]
    on_disconnected: Callable[[], Awaitable[None]]
    on_error: Callable[[str], Awaitable[None]]
    
    # 参与者相关事件
    on_participant_joined: Callable[[str], Awaitable[None]]
    on_participant_left: Callable[[str], Awaitable[None]]
    on_first_participant_joined: Callable[[str], Awaitable[None]]
    
    # 消息事件
    on_message_received: Callable[[Any, str], Awaitable[None]]
    
    # 音频事件
    on_audio_started: Callable[[str], Awaitable[None]]
    on_audio_stopped: Callable[[str], Awaitable[None]]

class AgoraTransportClient:
    def __init__(
        self,
        app_id: str,
        room_id: str,
        token: str,
        params: AgoraParams,
        callbacks: AgoraCallbacks,
        loop: None,
    ):
        self._room_id = room_id
        self._token = token
        self._params = params
        self._callbacks = callbacks
        self._loop = asyncio.get_event_loop()  # 自动获取事件循环
        self._participant_id = str(params.uid)  # 从参数中获取 uid
        self._connected = False
        self._connect_counter = 0
        self._connection = None
        self._observer = None

        # 初始化媒体节点工厂
        self._media_node_factory = None
        self._pcm_data_sender = None
        self._audio_sender = None
        self._video_sender = None
        self._video_encoded_sender = None

        # 添加媒体轨道相关变量
        self._audio_track_pcm = None
        self._audio_track_encoded = None
        self._video_track_frame = None
        self._video_track_encoded = None
        self._local_user = None

        try:
            self._agora_service = AgoraService()
            config = AgoraServiceConfig()
            config.audio_scenario = AudioScenarioType.AUDIO_SCENARIO_CHORUS
            config.appid = app_id
            result = self._agora_service.initialize(config)
            if result != 0:
                raise Exception(f"Agora service initialization failed with code: {result}")
            else:
                logger.debug(f"Agora service initialization succeeded")
            # 创建媒体节点工厂
            self._media_node_factory = self._agora_service.create_media_node_factory()
            if not self._media_node_factory:
                logger.error("Create media node factory failed")
                raise Exception("Failed to create media node factory")

            # 初始化媒体发送器
            self._init_media_senders()

            logger.debug(f"Agora service initialization succeeded")
        except Exception as e:
            raise Exception(f"Failed to initialize Agora service: {str(e)}")

    @property
    def participant_id(self) -> str:
        return self._participant_id

    # 与频道建立连接
    async def connect(self):
        """连接到 Agora 房间"""
        if self._connected:
            self._connect_counter += 1
            return

        try:
            # 1. 创建 RTCConnection 配置
            conn_config = RTCConnConfig(
                client_role_type=ClientRoleType.CLIENT_ROLE_BROADCASTER,
                channel_profile=ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING,
            )
            # 2. 创建 RTC 连接
            self._connection = self._agora_service.create_rtc_connection(conn_config)
            if not self._connection:
                raise Exception("create connection failed")
            else:
                logger.info(f"Agora connection established: {self._connection}")

            # 3. 注册连接观察者
            self._observer = AGORAConnectionObserver()
            self._connection.register_observer(self._observer)

            logger.info(f"Connecting to Agora room {self._room_id} with uid {self._participant_id}")
            logger.debug(f"Using token: {self._token}")

            # 4. 建立与频道的连接
            ret = self._connection.connect(
                token=self._token,
                chan_id=self._room_id,
                user_id=self._participant_id
            )

            if ret < 0:
                error_msg = f"Connect failed with code: {ret}"
                logger.error(error_msg)
                raise Exception(f"connect failed: {ret}")
            # 创建和发布媒体轨道
            await self._create_and_publish_tracks()
            logger.info("Successfully connected to Agora channel")
            self._connected = True
            if self._callbacks.on_connected:
                await self._callbacks.on_connected()

        except Exception as e:
            self._connected = False
            error_msg = f"Failed to connect to channel: {str(e)}"
            logger.error(error_msg)
            if self._callbacks.on_error:
                await self._callbacks.on_error(error_msg)
            raise Exception(error_msg)

    async def disconnect(self):
        """断开与 Agora 房间的连接"""
        self._connect_counter -= 1
        if not self._connected or self._connect_counter > 0:
            return
        try:
            logger.info(f"Disconnecting from room {self._room_id}")
            if self._connection:
                self._connection.disconnect()
                self._connection.unregister_observer(self._observer)
                self._connection = None
            self._connected = False
            await self._callbacks.on_disconnected()
        except Exception as e:
            logger.error(f"Error disconnecting from room {self._room_id}: {e}")
            await self._callbacks.on_error(str(e))

    def _init_media_senders(self):
        """初始化媒体发送器"""
        try:
            # 创建 PCM 格式的音频数据发送器
            self._pcm_data_sender = self._media_node_factory.create_audio_pcm_data_sender()
            if not self._pcm_data_sender:
                logger.error("Create pcm data sender failed")
                return

            # 创建已编码的音频数据发送器
            self._audio_sender = self._media_node_factory.create_audio_encoded_frame_sender()
            if not self._audio_sender:
                logger.error("Create audio sender failed")
                return

            # 创建 YUV 格式的视频数据发送器
            self._video_sender = self._media_node_factory.create_video_frame_sender()
            if not self._video_sender:
                logger.error("Create video frame sender failed")
                return

            # 创建已编码的视频数据发送器
            self._video_encoded_sender = self._media_node_factory.create_video_encoded_image_sender()
            if not self._video_encoded_sender:
                logger.error("Create video sender failed")
                return

            logger.info("All media senders initialized successfully")
        except Exception as e:
            logger.error(f"Error initializing media senders: {e}")
            raise

    def _create_and_publish_tracks(self):
        """创建并发布媒体轨道"""
        try:
            # 创建音频轨道（PCM）
            self._audio_track_pcm = self._agora_service.create_custom_audio_track_pcm(self._pcm_data_sender)
            if not self._audio_track_pcm:
                logger.error("create audio track pcm failed")
                return

            # 创建音频轨道（已编码）
            self._audio_track_encoded = self._agora_service.create_custom_audio_track_encoded(self._audio_sender, 1)
            if not self._audio_track_encoded:
                logger.error("create audio track encoded failed")
                return

            # 创建视频轨道（YUV）
            self._video_track_frame = self._agora_service.create_custom_video_track_frame(self._video_sender)
            if not self._video_track_frame:
                logger.error("create video track frame failed")
                return

            # 创建视频轨道（已编码）
            self._video_track_encoded = self._agora_service.create_custom_video_track_encoded(
                self._video_encoded_sender, {})
            if not self._video_track_encoded:
                logger.error("create video track encoded failed")
                return

            # 启用轨道
            self._audio_track_pcm.set_enabled(1)
            self._video_track_frame.set_enabled(1)

            # 获取本地用户并发布轨道
            self._local_user = self._connection.get_local_user()
            if self._local_user:
                self._local_user.publish_audio(self._audio_track_pcm)
                self._local_user.publish_video(self._video_track_frame)
                logger.info("Successfully published audio and video tracks")
            else:
                logger.error("Failed to get local user")

        except Exception as e:
            logger.error(f"Error in creating and publishing tracks: {e}")
            raise

    async def push_pcm_data_from_file(self, sample_rate, num_of_channels, pcm_data_sender: AudioPcmDataSender,
                                      audio_file_path, _exit: Event):
        """从文件发送PCM音频数据"""
        with open(audio_file_path, "rb") as audio_file:
            pcm_sendinterval = 0.1
            pacer_pcm = Pacer(pcm_sendinterval)
            pcm_count = 0
            send_size = int(sample_rate * num_of_channels * pcm_sendinterval * 2)
            frame_buf = bytearray(send_size)

            while not _exit.is_set():
                success = audio_file.readinto(frame_buf)
                if not success:
                    audio_file.seek(0)
                    continue

                frame = PcmAudioFrame()
                frame.data = frame_buf
                frame.timestamp = 0
                frame.samples_per_channel = int(sample_rate * pcm_sendinterval)
                frame.bytes_per_sample = 2
                frame.number_of_channels = num_of_channels
                frame.sample_rate = sample_rate

                ret = pcm_data_sender.send_audio_pcm_data(frame)
                pcm_count += 1
                logger.info(f"send pcm: count,ret={pcm_count}, {ret}, {send_size}, {pcm_sendinterval}")

                await pacer_pcm.apace_interval(0.1)

            frame_buf = None

    # 发送 YUV 视频数据
    async def push_yuv_data_from_file(self,width, height, fps, video_sender: VideoFrameSender, video_file_path,
                                      _exit: Event):
        with open(video_file_path, "rb") as video_file:
            yuv_sendinterval = 1.0 / fps
            pacer_yuv = Pacer(yuv_sendinterval)
            yuv_count = 0
            yuv_len = int(width * height * 3 / 2)
            frame_buf = bytearray(yuv_len)
            while not _exit.is_set():
                success = video_file.readinto(frame_buf)
                if not success:
                    video_file.seek(0)
                    continue
                frame = ExternalVideoFrame()
                frame.buffer = frame_buf
                frame.type = 1
                frame.format = 1
                frame.stride = width
                frame.height = height
                frame.timestamp = 0
                frame.metadata = "hello metadata"
                ret = video_sender.send_video_frame(frame)
                yuv_count += 1
                logger.info("send yuv: count,ret=%d, %s", yuv_count, ret)
                await pacer_yuv.apace_interval(yuv_sendinterval)
            frame_buf = None

    async def send_message(self, message: Any, participant_id: str | None = None):
        """发送消息到房间或特定参与者"""
        if not self._connected:
            return
            
        try:
            # TODO: 实现实际的消息发送逻辑
            logger.debug(f"Sending message to {participant_id if participant_id else 'all'}")
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            await self._callbacks.on_error(str(e))

    async def send_audio_pcm(self, audio_data: bytes):
        """发送 PCM 格式的音频数据"""
        if not self._connected or not self._pcm_data_sender:
            return
        try:
            ret = 0
            if ret < 0:
                logger.error(f"Failed to send PCM data: {ret}")
            return ret
        except Exception as e:
            logger.error(f"Error sending PCM data: {e}")

    def get_participants(self) -> List[str]:
        """获取当前房间的参与者列表"""
        # TODO: 实现获取参与者列表的逻辑
        return []

    async def cleanup(self):
        """清理资源"""
        try:
            await self.disconnect()
            # 清理媒体轨道
            self._audio_track_pcm = None
            self._audio_track_encoded = None
            self._video_track_frame = None
            self._video_track_encoded = None
            # 清理媒体发送器
            self._pcm_data_sender = None
            self._audio_sender = None
            self._video_sender = None
            self._video_encoded_sender = None
            # 清理媒体节点工厂
            self._media_node_factory = None
            # 清理声网服务
            if self._agora_service:
                self._agora_service.release()
            logger.info("Successfully cleaned up all resources")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")

class AgoraInputTransport(BaseInputTransport):
    def __init__(self, client: AgoraTransportClient, params: AgoraParams, **kwargs):
        super().__init__(params, **kwargs)
        self._client = client
        self._audio_task = None

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self._client.connect()
        # 如果需要启动音频处理
        if self._params.audio_in_enabled:
            self._audio_task = asyncio.create_task(self._audio_task_handler())

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._client.disconnect()
        if self._audio_task:
            self._audio_task.cancel()
            await self._audio_task

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._client.disconnect()
        if self._audio_task:
            self._audio_task.cancel()
            await self._audio_task

    async def _audio_task_handler(self):
        """处理音频输入的任务"""
        while True:
            try:
                # TODO: 实现音频处理逻辑
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in audio task: {e}")

#!env python

class Pacer:
    def __init__(self, interval):
        self.last_call_time = time.time()
        self.interval = interval

    def pace(self):
        current_time = time.time()
        elapsed_time = current_time - self.last_call_time
        if elapsed_time < self.interval:
            time.sleep(self.interval - elapsed_time)
            # print("sleep time:", (self.interval - elapsed_time)*1000)
        self.last_call_time = time.time()

    def pace_interval(self, time_interval_s):
        current_time = time.time()
        elapsed_time = current_time - self.last_call_time
        if elapsed_time < time_interval_s:
            time.sleep(time_interval_s - elapsed_time)
            # print("sleep time(ms):", (time_interval_s - elapsed_time)*1000)
        self.last_call_time = time.time()

    async def apace_interval(self, time_interval_s):
        current_time = time.time()
        elapsed_time = current_time - self.last_call_time
        # logger.info(f"elapsed_time:{elapsed_time}, time_interval_s:{time_interval_s}")
        if elapsed_time < time_interval_s:
            await asyncio.sleep(time_interval_s - elapsed_time)
        self.last_call_time = time.time()


class AGORAConnectionObserver(IRTCConnectionObserver):
    def __init__(self):
        super().__init__()

    def on_connected(self, agora_rtc_conn, conn_info, reason):
        """连接成功回调"""
        logger.info(f"Connected to channel, reason: {reason}")

    def on_disconnected(self, agora_rtc_conn, conn_info, reason):
        """连接断开回调"""
        logger.warning(f"Disconnected from channel, reason: {reason}")

    def on_connecting(self, agora_rtc_conn, conn_info, reason):
        """正在连接回调"""
        logger.info(f"Connecting to channel, reason: {reason}")

    def on_reconnecting(self, agora_rtc_conn, conn_info, reason):
        """正在重连回调"""
        logger.warning(f"Reconnecting to channel, reason: {reason}")

    def on_connection_lost(self, agora_rtc_conn, conn_info):
        """连接丢失回调"""
        logger.error("Connection lost")

    def on_user_joined(self, agora_rtc_conn, user_id):
        """用户加入频道回调"""
        logger.info(f"User {user_id} joined channel")

    def on_user_left(self, agora_rtc_conn, user_id, reason):
        """用户离开频道回调"""
        logger.info(f"User {user_id} left channel, reason: {reason}")

    def on_error(self, agora_rtc_conn, error_code, error_msg):
        """错误回调"""
        logger.error(f"Error occurred: code={error_code}, msg={error_msg}")

    def on_token_privilege_will_expire(self, agora_rtc_conn, token):
        """Token 即将过期回调"""
        logger.warning(f"Token will expire soon: {token}")

    def on_network_type_changed(self, agora_rtc_conn, network_type):
        """网络类型变化回调"""
        logger.info(f"Network type changed to: {network_type}")

class AgoraOutputTransport(BaseOutputTransport):
    def __init__(self, client: AgoraTransportClient, params: AgoraParams, **kwargs):
        super().__init__(params, **kwargs)
        self._client = client

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self._client.connect()

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._client.disconnect()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._client.disconnect()

    async def send_message(self, frame: TransportMessageFrame | TransportMessageUrgentFrame):
        participant_id = None
        if isinstance(frame, (AgoraTransportMessageFrame, AgoraTransportMessageUrgentFrame)):
            participant_id = frame.participant_id
        await self._client.send_message(frame.message, participant_id)

class AgoraTransport(BaseTransport):
    def __init__(
        self,
        token: str,
        params: AgoraParams = AgoraParams(),
        input_name: str | None = None,
        output_name: str | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ):
        # 确保有一个有效的事件循环
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

        super().__init__(input_name=input_name, output_name=output_name, loop=loop)

        callbacks = AgoraCallbacks(
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
            on_error=self._on_error,
            on_participant_joined=self._on_participant_joined,
            on_participant_left=self._on_participant_left,
            on_first_participant_joined=self._on_first_participant_joined,
            on_message_received=self._on_message_received,
            on_audio_started=self._on_audio_started,
            on_audio_stopped=self._on_audio_stopped,
        )

        self._params = params
        # 初始化客户端，确保传入事件循环
        self._client = AgoraTransportClient(
            room_id=params.room_id,
            app_id=params.app_id,
            token=token,
            params=params,
            callbacks=callbacks,
            loop=self._loop  # 这里使用已确保有效的事件循环
        )
        self._input: Optional[AgoraInputTransport] = None
        self._output: Optional[AgoraOutputTransport] = None

        # 注册事件处理器
        self._register_event_handler("on_connected")
        self._register_event_handler("on_disconnected")
        self._register_event_handler("on_error")
        self._register_event_handler("on_participant_joined")
        self._register_event_handler("on_participant_left")
        self._register_event_handler("on_first_participant_joined")
        self._register_event_handler("on_message_received")
        self._register_event_handler("on_audio_started")
        self._register_event_handler("on_audio_stopped")

    def input(self) -> AgoraInputTransport:
        if not self._input:
            self._input = AgoraInputTransport(self._client, self._params, name=self._input_name)
        return self._input

    def output(self) -> AgoraOutputTransport:
        if not self._output:
            self._output = AgoraOutputTransport(self._client, self._params, name=self._output_name)
        return self._output

    @property
    def participant_id(self) -> str:
        return self._client.participant_id

    # 事件处理方法
    async def _on_connected(self):
        await self._call_event_handler("on_connected")

    async def _on_disconnected(self):
        await self._call_event_handler("on_disconnected")

    async def _on_error(self, error: str):
        await self._call_event_handler("on_error", error)

    async def _on_participant_joined(self, participant_id: str):
        await self._call_event_handler("on_participant_joined", participant_id)

    async def _on_participant_left(self, participant_id: str):
        await self._call_event_handler("on_participant_left", participant_id)

    async def _on_first_participant_joined(self, participant_id: str):
        await self._call_event_handler("on_first_participant_joined", participant_id)

    async def _on_message_received(self, message: Any, sender: str):
        await self._call_event_handler("on_message_received", message, sender)

    async def _on_audio_started(self, participant_id: str):
        await self._call_event_handler("on_audio_started", participant_id)

    async def _on_audio_stopped(self, participant_id: str):
        await self._call_event_handler("on_audio_stopped", participant_id)