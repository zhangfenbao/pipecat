import asyncio
import gc
import json
import uuid
from asyncio import Event
from dataclasses import dataclass
import datetime
from typing import Any, Awaitable, Callable, List, Optional
import time

from pipecat.clocks.base_clock import BaseClock
from pydantic import BaseModel
from loguru import logger
import numpy as np
import os
source_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
filename, _ = os.path.splitext(os.path.basename(__file__))
log_folder = os.path.join(source_dir, 'logs', filename, datetime.datetime.now().strftime("%Y_%m_%d_%H_%M_%S"))
os.makedirs(log_folder, exist_ok=True)

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    StartFrame,
    TransportMessageFrame,
    TransportMessageUrgentFrame, TranscriptionFrame, InterimTranscriptionFrame,
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
    ChannelProfileType, PcmAudioFrame, ExternalVideoFrame, AudioFrame, VideoFrame,SenderOptions, TCcMode, VideoCodecType, AudioSubscriptionOptions)
from agora.rtc.agora_service import AgoraService
from agora.rtc.rtc_connection_observer import IRTCConnectionObserver
from agora.rtc.audio_pcm_data_sender import AudioPcmDataSender
from agora.rtc.audio_frame_observer import IAudioFrameObserver
from agora.rtc.video_frame_observer import IVideoFrameObserver
from agora.rtc.video_frame_sender import VideoFrameSender
from agora.rtc.video_encoded_frame_observer import IVideoEncodedFrameObserver


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
    video_enabled: bool = False  # 添加视频使能控制
    transcription_enabled: bool = False


class AgoraCallbacks(BaseModel):
    # 基础事件回调
    on_connected: Callable[[], Awaitable[None]]
    on_disconnected: Callable[[], Awaitable[None]]
    on_error: Callable[[str], Awaitable[None]]

    # 参与者相关事件
    on_user_joined: Callable[[str], Awaitable[None]]
    on_user_left: Callable[[str], Awaitable[None]]
    # on_first_participant_joined: Callable[[str], Awaitable[None]]
    #
    # # 消息事件
    # on_message_received: Callable[[Any, str], Awaitable[None]]
    #
    # # 音频事件
    # on_audio_started: Callable[[str], Awaitable[None]]
    # on_audio_stopped: Callable[[str], Awaitable[None]]
    # on_stream_message: Callable[[str, int, str, int], Awaitable[None]]  # uid, stream_id, message, length

class TranscriptionStateFrame(Frame):
    """Represents the state of transcription"""
    def __init__(self, instance_id: str, model: str):
        super().__init__()
        self.instance_id = instance_id
        self.model = model


class AgoraTransportClient:
    def __init__(
            self,
            app_id: str,
            room_id: str,
            token: str,
            params: AgoraParams,
            callbacks: AgoraCallbacks,
            # loop: None,
            loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        self._loop = loop or asyncio.get_event_loop()  # 确保有事件循环
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

        # 添加帧观察器相关的属性
        self._audio_frame_observer = None
        self._video_frame_observer = None
        self._video_encoded_observer = None

        # 添加用于存储数据流ID的属性
        self._stream_ids = set()
        self._transcription_started = False

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

            # 初始化帧观察器
            self._init_frame_observers()

            logger.debug(f"Agora initialization succeeded")
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
                auto_subscribe_audio=1,
                client_role_type=ClientRoleType.CLIENT_ROLE_BROADCASTER,
                channel_profile=ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING,
                audio_subs_options=AudioSubscriptionOptions(
                    pcm_data_only=0,
                    bytes_per_sample=2,
                    number_of_channels=1,
                    sample_rate_hz=16000
                ),
            )
            # 2. 创建 RTC 连接
            self._connection = self._agora_service.create_rtc_connection(conn_config)
            if not self._connection:
                raise Exception("create connection failed")

            # 3. 注册连接观察者并建立连接
            self._observer = AGORAConnectionObserver(callbacks=self._callbacks, loop=self._loop)
            self._connection.register_observer(self._observer)

            logger.info(f"Connecting to Agora room {self._room_id} with uid {self._participant_id}")

            ret = self._connection.connect(
                token=self._token,
                chan_id=self._room_id,
                user_id=self._participant_id
            )

            if ret < 0:
                raise Exception(f"connect failed: {ret}")

            # 4. 创建和发布媒体轨道
            await self._create_and_publish_tracks()
            logger.info("Successfully connected to Agora channel")
            self._connected = True
            if self._callbacks and getattr(self._callbacks, 'on_connected', None):
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
            # 注销帧观察器
            if self._local_user:
                if self._audio_frame_observer:
                    self._local_user.unregister_audio_frame_observer()

            if self._connection:
                self._connection.unregister_observer()
                self._connection.disconnect()
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
            logger.debug("PCM 数据发送器创建成功")  # 添加此日志

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

    def _init_frame_observers(self):
        """初始化帧观察器"""
        try:
            # 创建帧观察器实例
            self._audio_frame_observer = SampleAudioFrameObserver(loop=self._loop)
            self._video_frame_observer = SampleVideoFrameObserver()
            self._video_encoded_observer = SampleVideoEncodedFrameObserver()

            logger.info("帧观察器初始化成功")
        except Exception as e:
            logger.error(f"初始化帧观察器时出错: {e}")
            raise

    async def _create_and_publish_tracks(self):
        """创建并发布媒体轨道"""
        try:
            # 1.创建音频轨道（PCM）- 这些创建方法可能不是异步的
            self._audio_track_pcm = self._agora_service.create_custom_audio_track_pcm(self._pcm_data_sender)
            if not self._audio_track_pcm:
                logger.error("create audio track pcm failed")
                return

            logger.debug("PCM 音频轨道创建成功")  # 添加此日志

            # 创建音频轨道（已编码）
            self._audio_track_encoded = self._agora_service.create_custom_audio_track_encoded(self._audio_sender, 1)
            if not self._audio_track_encoded:
                logger.error("create audio track encoded failed")
                return

            # 只有在启用视频时才创建视频轨道
            if getattr(self._params, 'video_enabled', False):
                if self._video_sender:
                    self._video_track_frame = self._agora_service.create_custom_video_track_frame(self._video_sender)
                    if not self._video_track_frame:
                        logger.error("create video track frame failed")
                        return

                if self._video_encoded_sender:
                    _sender_options = SenderOptions(
                        cc_mode=TCcMode.CC_ENABLED,
                        codec_type=VideoCodecType.VIDEO_CODEC_H264,
                        target_bitrate=640)
                    # 确保不设置 cc_mode
                    self._video_track_encoded = self._agora_service.create_custom_video_track_encoded(
                        self._video_encoded_sender, _sender_options)
                    if not self._video_track_encoded:
                        logger.error("create video track encoded failed")
                        return

            # 启用音频轨道 - set_enabled 可能不是异步的
            if self._audio_track_pcm:
                self._audio_track_pcm.set_enabled(1)
                if self._local_user:
                    ret = self._local_user.publish_audio(self._audio_track_pcm)
                    logger.info(f"发布音频轨道结果: {ret}")
                    if ret < 0:
                        logger.error(f"发布音频轨道失败: {ret}")

            # 如果视频轨道存在且视频功能启用，则启用视频轨道
            if self._video_track_frame and getattr(self._params, 'video_enabled', False):
                self._video_track_frame.set_enabled(1)

            # 获取本地用户并发布轨道
            self._local_user = self._connection.get_local_user()
            if self._local_user:
                # 设置音频采集参数
                ret = self._local_user.set_playback_audio_frame_parameters(
                    sample_rate_hz=16000,  # 采样率
                    channels=1,  # 单声道
                    mode=0,  # RAW PCM mode
                    samples_per_call=1024  # 每次回调的采样数
                )
                logger.info(f"Set audio parameters result: {ret}")
                if self._audio_track_pcm:
                    self._local_user.publish_audio(self._audio_track_pcm)
                # 设置音频参数
                self._local_user.set_playback_audio_frame_before_mixing_parameters(1, 16000)
                # 注册观察器
                if self._audio_frame_observer:
                    ret = self._local_user.register_audio_frame_observer(
                        observer=self._audio_frame_observer,
                        enable_vad=0,
                        vad_configure=None
                    )
                    if ret < 0:
                        logger.error("register_audio_frame_observer failed")
                        return
                # 注册视频观察器（如果启用）
                if getattr(self._params, 'video_enabled', False):
                    if self._video_frame_observer:
                        self._local_user.register_video_frame_observer(self._video_frame_observer)
                    if self._video_encoded_observer:
                        self._local_user.register_video_encoded_frame_observer(self._video_encoded_observer)
                # 触发音频开始回调
                if self._callbacks and getattr(self._callbacks, 'on_audio_started', None):
                    await self._callbacks.on_audio_started(self._participant_id)
                logger.info("Successfully registered all observers and set parameters")
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
    async def push_yuv_data_from_file(self, width, height, fps, video_sender: VideoFrameSender, video_file_path,
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
        """发送消息实现
        Args:
            message: 要发送的消息内容
            participant_id: 目标参与者ID，如果为None则广播
        """
        if not self._connected:
            return

        try:
            # 如果消息流ID还未创建，先创建一个
            if not hasattr(self, '_stream_id'):
                # 创建数据流，设置为不可靠模式（与示例保持一致）
                self._stream_id = self._connection.create_data_stream(False, False)
                logger.info(f"Created data stream with ID: {self._stream_id}")

            # 准备消息内容
            if isinstance(message, str):
                msg_str = message
            else:
                msg_str = json.dumps(message)

            # 发送消息
            ret = self._connection.send_stream_message(self._stream_id, msg_str)
            if ret < 0:
                logger.error(f"Send stream message failed with code: {ret}")
            else:
                logger.info(f"Sent message successfully: {msg_str}")
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            if self._callbacks:
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

    async def _setup_message_handlers(self):
        """设置消息相关的事件处理器"""
        self._connection.register_observer({
            'onStreamMessage': self._on_stream_message
        })

    def _on_stream_message(self, connection, uid: int, stream_id: int, data: str, length: int):
        """处理收到的数据流消息
        Args:
            connection: RTCConnection实例
            uid: 发送消息的用户ID
            stream_id: 数据流ID
            data: 消息内容
            length: 消息长度
        """
        try:
            logger.debug(f"Received stream message from uid {uid}, stream_id {stream_id}, length {length}")
            logger.debug(f"Message content: {data}")

            # 如果有回调处理器，触发消息回调
            if self._callbacks:
                asyncio.create_task(self._callbacks.on_message(str(uid), data))

        except Exception as e:
            logger.error(f"Error processing stream message: {e}")
            if self._callbacks:
                asyncio.create_task(self._callbacks.on_error(str(e)))

    async def cleanup(self):
        """清理资源"""
        try:
            logger.info("开始清理 Agora 资源...")

            # 先禁用所有轨道
            if self._audio_track_pcm:
                self._audio_track_pcm.set_enabled(0)
                self._audio_track_pcm = None

            # 断开连接
            if self._connection:
                self._connection.disconnect()
                self._connection = None

            # 清理发送器
            self._pcm_data_sender = None
            self._audio_sender = None
            self._video_sender = None

            # 最后释放服务
            if self._agora_service:
                self._agora_service.release()
                self._agora_service = None

            # 强制垃圾回收
            gc.collect()

            logger.info("Agora 资源清理完成")
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")

    async def read_next_audio_frame(self) -> Optional[InputAudioRawFrame]:
        try:
            # 创建一个缓冲区来累积音频数据
            if not hasattr(self, '_audio_buffer'):
                self._audio_buffer = bytearray()

            # 计算所需的最小字节数
            sample_rate = self._params.audio_in_sample_rate or 16000
            min_bytes_needed = int(sample_rate * 0.02) * 2  # 20ms的数据量

            # 循环直到收集到足够的数据
            while len(self._audio_buffer) < 32000:
                audio_data = await self._audio_frame_observer.get_next_frame()
                # logger.debug(f"read_next_audio_frame.audio_data size: {len(audio_data) if audio_data else None}")

                if audio_data:
                    self._audio_buffer.extend(audio_data)
                else:
                    # 如果没有新数据，短暂等待后继续
                    await asyncio.sleep(0.01)
                    continue

            # 此时已经确保有足够的数据
            frame_data = bytes(self._audio_buffer[:min_bytes_needed])
            self._audio_buffer = self._audio_buffer[min_bytes_needed:]

            frame = InputAudioRawFrame(
                audio=frame_data,
                sample_rate=sample_rate,
                num_channels=self._params.audio_in_channels or 1
            )

            # 验证帧参数
            if frame.sample_rate <= 0:
                logger.error(f"Invalid sample rate: {frame.sample_rate}")
                return None
            if frame.num_channels <= 0:
                logger.error(f"Invalid channel count: {frame.num_channels}")
                return None

            # logger.debug(f"Created frame with {len(frame_data)} bytes of audio data")
            return frame

        except Exception as e:
            logger.error(f"Error reading audio frame: {e}")
            logger.exception("Detailed error information:")
            return None

    async def start_transcription(self):
        """Start the transcription service"""
        if not self._transcription_started:
            self._transcription_started = True
            instance_id = str(uuid.uuid4())
            frame = TranscriptionStateFrame(
                instance_id=instance_id,
                model="rtvi-transcription"  # 或其他模型名称
            )
            logger.info(f"Transcription started: instanceId={instance_id}")
            return frame
        return None


class AgoraInputTransport(BaseInputTransport):
    def __init__(self, client: AgoraTransportClient, params: AgoraParams, **kwargs):
        super().__init__(params, **kwargs)
        self._client = client
        self._audio_buffer = bytearray()  # 添加音频缓冲区
        self._min_buffer_size = 32000  # 设置最小缓冲区大小（约2秒的音频）
        self._processing = False
        self._current_participant_id = None

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self._client.connect()
        if self._params.audio_in_enabled or self._params.vad_enabled:
            self._processing = True
            self._audio_task = self.get_event_loop().create_task(self._audio_in_task_handler())

    async def stop(self, frame: EndFrame):
        """Stop the transport and audio processing."""
        self._processing = False
        if self._audio_task:
            self._audio_task.cancel()
            try:
                await self._audio_task
            except asyncio.CancelledError:
                pass
        await self._client.disconnect()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._client.disconnect()
        if self._audio_task:
            self._audio_task.cancel()
            await self._audio_task

    #
    # Audio in
    #
    async def _audio_in_task_handler(self):
        # logger.info("Audio In task handler started")
        while self._processing:
            try:
                # logger.info("Audio In task handler Begin")
                frame = await self._client.read_next_audio_frame()
                if frame and frame.audio:
                    # 确保音频数据是bytes类型
                    if isinstance(frame.audio, bytearray):
                        frame.audio = bytes(frame.audio)
                    await self.push_audio_frame(frame)
                else:
                    logger.warning(f"Audio In task handler failed no bytes: {frame.audio}")
            except asyncio.CancelledError:
                logger.error("Audio In task handler Cancelled Error")
                break
            except Exception as e:
                logger.error(f"Error in audio task handler: {e}", exc_info=True)
                await asyncio.sleep(0.1)  # 添加错误重试间隔

    async def push_transcription_frame(self, frame: TranscriptionFrame | InterimTranscriptionFrame):
        logger.debug(f"Push transcription frame: {frame}")
        await self.push_frame(frame)

class AudioFrameObserverForTranscription(IAudioFrameObserver):
    def __init__(self, queue: asyncio.Queue):
        super().__init__()
        self._queue = queue
        logger.info("AudioFrameObserverForTranscription initialized")

    def on_playback_audio_frame_before_mixing(self, agora_local_user, channelId, uid,
                                              audio_frame: AudioFrame,
                                              vad_result_state: int,
                                              vad_result_bytearray: bytearray):
        """处理混音前的音频帧"""
        try:
            if audio_frame and audio_frame.buffer:
                logger.debug(f"Received audio frame: channels={audio_frame.channels}, "
                             f"sample_rate={audio_frame.samples_per_sec}, "
                             f"buffer_size={len(audio_frame.buffer)}")
                # 直接将音频帧添加到队列中，避免使用事件循环
                self._queue.put_nowait(audio_frame)
                logger.debug("Successfully queued audio frame")
            else:
                logger.warning("Received invalid audio frame")
        except Exception as e:
            logger.error(f"Error processing audio frame: {e}")
        return True

    def on_record_audio_frame(self, agora_local_user, channelId, frame):
        logger.debug(f"on_record_audio_frame called for channel {channelId}")
        return 0

    def on_mixed_audio_frame(self, agora_local_user, channelId, frame):
        logger.debug(f"on_mixed_audio_frame called for channel {channelId}")
        return 0

    def on_playback_audio_frame(self, agora_local_user, channelId, frame):
        logger.debug(f"on_playback_audio_frame called for channel {channelId}")
        return 0

class SystemClock(BaseClock):
    """具体的时钟实现"""
    def __init__(self):
        self._start_time = None

    def get_time(self) -> float:
        """获取当前时间戳"""
        if self._start_time is None:
            return 0.0
        return time.time() - self._start_time

    def start(self):
        """开始计时"""
        self._start_time = time.time()


def apply_audio_gain(audio_frame, gain):
    """对音频帧数据进行增益处理"""
    try:
        # 将bytearray转换为numpy数组
        samples = np.frombuffer(audio_frame.buffer, dtype=np.int16)

        # 应用增益
        samples = samples.astype(np.float32) * gain

        # 裁剪到16位整数范围
        samples = np.clip(samples, -32768, 32767)

        # 转回16位整数
        samples = samples.astype(np.int16)

        # 创建新的buffer
        new_buffer = bytearray(samples.tobytes())

        # 创建新的AudioFrame对象，复制所有属性
        new_frame = AudioFrame(
            type=audio_frame.type,
            samples_per_channel=audio_frame.samples_per_channel,
            bytes_per_sample=audio_frame.bytes_per_sample,
            channels=audio_frame.channels,
            samples_per_sec=audio_frame.samples_per_sec,
            buffer=new_buffer,
            render_time_ms=audio_frame.render_time_ms,
            avsync_type=audio_frame.avsync_type,
            far_field_flag=audio_frame.far_field_flag,
            rms=audio_frame.rms,
            voice_prob=audio_frame.voice_prob,
            music_prob=audio_frame.music_prob,
            pitch=audio_frame.pitch
        )

        return new_frame
    except Exception as e:
        logger.error(f"音频增益处理失败: {e}")
        return audio_frame


class SampleAudioFrameObserver(IAudioFrameObserver):
    def __init__(self, save_to_disk=True,loop=None):
        super().__init__()
        self.save_to_disk = save_to_disk
        self._audio_frame_queue = asyncio.Queue()
        self._loop = loop  # 保存事件循环的引用

    async def get_next_frame(self) -> Optional[bytes]:
        try:
            return await self._audio_frame_queue.get()
        except asyncio.QueueEmpty:
            return None

    def on_record_audio_frame(self, agora_local_user, channelId, frame):
        logger.info(f"on_record_audio_frame")
        return 0

    def on_playback_audio_frame(self, agora_local_user, channelId, frame):
        # logger.info(f"on_playback_audio_frame")
        return 0

    def on_ear_monitoring_audio_frame(self, agora_local_user, frame):
        logger.info(f"on_ear_monitoring_audio_frame")
        return 0

    def on_playback_audio_frame_before_mixing(self, agora_local_user, channel_id, uid, audio_frame: AudioFrame,
                                              vad_result_state: int, vad_result_bytearray: bytearray):
        """处理混音前的音频帧"""
        try:
            # 1. Save to disk if enabled
            if self.save_to_disk:
                file_path = os.path.join(log_folder, f"{channel_id}_{uid}.pcm")
                # logger.info(f"Saving audio frame to {file_path}, length={len(audio_frame.buffer)}")
                with open(file_path, "ab") as f:
                    f.write(audio_frame.buffer)
            # 2. Push to queue for pipeline processing
            if self._loop and audio_frame.buffer:
                # Use run_coroutine_threadsafe since we're in a different thread
                asyncio.run_coroutine_threadsafe(
                    self._audio_frame_queue.put(audio_frame.buffer),
                    self._loop
                )
                # logger.debug(f"Queued audio frame for processing, size: {len(audio_frame.buffer)}")
            return True
        except Exception as e:
            logger.error(f"Error in audio frame processing: {e}")
            return False


class SampleVideoFrameObserver(IVideoFrameObserver):
    def on_frame(self,
                 channel_id,
                 remote_uid,
                 frame: VideoFrame) -> int:
        """处理原始YUV格式的视频帧"""
        file_path = os.path.join("re", channel_id + "_" + remote_uid + ".yuv")

        # 计算Y、U、V分量的大小
        y_size = frame.y_stride * frame.height
        uv_size = (frame.u_stride * frame.height // 2)

        with open(file_path, "ab") as f:
            f.write(frame.y_buffer[:y_size])
            f.write(frame.u_buffer[:uv_size])
            f.write(frame.v_buffer[:uv_size])
        return 1


class SampleVideoEncodedFrameObserver(IVideoEncodedFrameObserver):
    def on_encoded_video_frame(self,
                               uid,
                               image_buffer,
                               length,
                               video_encoded_frame_info) -> int:
        """处理编码后的视频帧"""
        file_path = os.path.join("received_audio", str(uid) + ".h264")

        with open(file_path, "ab") as f:
            f.write(image_buffer[:length])
        return 1


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
    def __init__(self, callbacks, loop):
        super().__init__()
        self._callbacks = callbacks  # 添加对回调的引用
        self._loop = loop  # 添加事件循环的引用

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
        logger.info(f"Agora SDK 触发 on_user_joined: User {user_id}")

        async def handle_user_joined():
            if self._callbacks:
                # 先启动转写服务
                if hasattr(self._callbacks, '_client'):
                    frame = await self._callbacks._client.start_transcription()
                    if frame:
                        # 这里可以触发转写开始的事件
                        logger.info(f"Transcription started: {frame.instance_id}")

                # 然后调用原有的用户加入回调
                await self._callbacks.on_user_joined(user_id)

        asyncio.run_coroutine_threadsafe(
            handle_user_joined(),
            self._loop
        )

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
        self._tts_service = None  # 将在 process_frame 中设置
        self._is_stopping = False  # 添加状态标记

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self._client.connect()

    async def stop(self, frame: EndFrame):
        if self._is_stopping:  # 避免重复停止
            return
        self._is_stopping = True
        logger.info("停止输出传输...")
        try:
            await super().stop(frame)
            # 等待当前音频处理完成
            await asyncio.sleep(0.1)
            # 断开前确保所有队列都已清空
            await asyncio.sleep(0.2)  # 额外等待以确保所有操作完成
            await self._client.disconnect()
        except Exception as e:
            logger.error(f"停止传输时发生错误: {e}")
        finally:
            self._is_stopping = False

    async def cancel(self, frame: CancelFrame):
        if self._is_stopping:  # 避免重复停止
            return
        self._is_stopping = True
        logger.info("取消输出传输...")
        try:
            await super().cancel(frame)
            await self._client.disconnect()
        except Exception as e:
            logger.error(f"取消传输时发生错误: {e}")
        finally:
            self._is_stopping = False

    async def send_message(self, frame: TransportMessageFrame | TransportMessageUrgentFrame):
        participant_id = None
        if isinstance(frame, (AgoraTransportMessageFrame, AgoraTransportMessageUrgentFrame)):
            participant_id = frame.participant_id
        await self._client.send_message(frame.message, participant_id)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        logger.info(f"Processing frame ZZZ: {frame}")
        # 添加对 TTSAudioRawFrame 的处理
        if isinstance(frame, OutputAudioRawFrame):
            try:
                # 1. 先验证输入数据
                if not frame.audio:
                    logger.error("输入音频数据为空")
                    return

                # 2. 创建和填充 PCM 音频帧
                pcm_frame = PcmAudioFrame()
                pcm_frame.data = bytearray(frame.audio)
                pcm_frame.samples_per_channel = frame.num_frames
                pcm_frame.bytes_per_sample = 2  # 16-bit PCM
                pcm_frame.number_of_channels = frame.num_channels
                pcm_frame.sample_rate = frame.sample_rate
                pcm_frame.timestamp = 0

                # 3. 验证 PCM 帧数据
                if (len(pcm_frame.data) == 0 or
                        pcm_frame.samples_per_channel <= 0 or
                        pcm_frame.sample_rate <= 0):
                    logger.error("PCM帧参数无效")
                    return

                # 4. 检查发送器
                if not self._client._pcm_data_sender:
                    logger.error("PCM数据发送器未初始化")
                    return

                # 5. 发送数据并处理返回值
                ret = self._client._pcm_data_sender.send_audio_pcm_data(pcm_frame)
                logger.info(f"PCM data sent: {ret}")

                if ret is None or ret >= 0:
                    # 计算音频播放时长并等待
                    delay = frame.num_frames / frame.sample_rate
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"发送音频数据失败: {ret}")

            except Exception as e:
                logger.error(f"处理音频帧时出错: {e}", exc_info=True)
                logger.error(
                    f"音频帧信息: num_frames={frame.num_frames}, "
                    f"sample_rate={frame.sample_rate}, "
                    f"num_channels={frame.num_channels}, "
                    f"audio_size={len(frame.audio) if frame.audio else None}"
                )

            # 6. 继续处理其他帧
            await super().push_frame(frame, direction)

    async def write_raw_audio_frames(self, frames: bytes):
        """实现父类的方法，但我们在 process_frame 中直接处理了"""
        pass

class AgoraTransport(BaseTransport):
    def __init__(
            self,
            token: str,
            params: AgoraParams = AgoraParams(),
            input_name: str | None = None,
            output_name: str | None = None,
            # loop: asyncio.AbstractEventLoop | None = None,
    ):
        # # 确保有一个有效的事件循环
        # if loop is None:
        #     try:
        #         loop = asyncio.get_running_loop()
        #     except RuntimeError:
        #         loop = asyncio.new_event_loop()
        #         asyncio.set_event_loop(loop)

        # super().__init__(input_name=input_name, output_name=output_name, loop=loop)
        # 调用父类初始化，但不传入 loop 参数
        super().__init__(input_name=input_name, output_name=output_name)
        # 获取当前事件循环
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
        self._output: Optional[AgoraOutputTransport] = None
        self._transcription_callback = None

        callbacks = AgoraCallbacks(
            on_connected=self._on_connected,
            on_disconnected=self._on_disconnected,
            on_error=self._on_error,
            on_user_joined=self._on_user_joined_with_transcription,
            on_user_left=self._on_user_left,
            # on_first_participant_joined=self._on_first_participant_joined,
            # on_message_received=self._on_message_received,
            # on_audio_started=self._on_audio_started,
            # on_audio_stopped=self._on_audio_stopped,
        )

        self._params = params
        # 初始化客户端，确保传入事件循环
        self._client = AgoraTransportClient(
            room_id=params.room_id,
            app_id=params.app_id,
            token=token,
            params=params,
            callbacks=callbacks,
            loop=self._loop
        )
        self._input: Optional[AgoraInputTransport] = None
        self._output: Optional[AgoraOutputTransport] = None

        # 注册事件处理器
        self._register_event_handler("on_connected")
        self._register_event_handler("on_disconnected")
        self._register_event_handler("on_error")
        self._register_event_handler("on_user_joined")
        self._register_event_handler("on_user_left")
        # self._register_event_handler("on_message_received")
        # self._register_event_handler("on_audio_started")
        # self._register_event_handler("on_audio_stopped")

    def input(self) -> AgoraInputTransport:
        if not self._input:
            self._input = AgoraInputTransport(self._client, self._params, name=self._input_name)
        return self._input

    def output(self) -> AgoraOutputTransport:
        if not self._output:
            self._output = AgoraOutputTransport(self._client, self._params, name=self._output_name)
        return self._output

    def set_tts_service(self, tts_service):
        """设置 TTS 服务"""
        if self._output:
            self._output._tts_service = tts_service

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

    async def _on_user_joined(self, participant_id: str):
        logger.info(f"AgoraTransport 触发 on_user_joined 回调: {participant_id}")
        await self._call_event_handler("on_user_joined", participant_id)

    async def _on_user_left(self, participant_id: str):
        await self._call_event_handler("on_user_left", participant_id)

    async def _on_user_joined_with_transcription(self, participant_id: str):
        """处理用户加入和转写启动"""
        logger.info(f"User joined with transcription: {participant_id}")
        # 先启动转写
        frame = await self._client.start_transcription()
        if frame:
            # 如果有转写回调，触发它
            if self._transcription_callback:
                await self._transcription_callback(frame)

        # 触发原有的用户加入事件
        await self._on_user_joined(participant_id)