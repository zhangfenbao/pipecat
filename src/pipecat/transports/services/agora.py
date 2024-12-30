import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List, Optional
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
# from agora import AgoraClient

@dataclass
class AgoraTransportMessageFrame(TransportMessageFrame):
    participant_id: str | None = None

@dataclass
class AgoraTransportMessageUrgentFrame(TransportMessageUrgentFrame):
    participant_id: str | None = None

class AgoraParams(TransportParams):
    api_url: str = "https://api.agora.io/v1"  # 示例 API URL
    api_key: str = ""
    room_id: str = ""

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
        room_id: str,
        token: str,
        params: AgoraParams,
        callbacks: AgoraCallbacks,
        loop: asyncio.AbstractEventLoop,
    ):
        self._room_id = room_id
        self._token = token
        self._params = params
        self._callbacks = callbacks
        self._loop = loop
        self._participant_id = ""
        self._connected = False
        self._connect_counter = 0
        
    @property
    def participant_id(self) -> str:
        return self._participant_id
        
    async def connect(self):
        """连接到 Agora 房间"""
        if self._connected:
            self._connect_counter += 1
            return
            
        try:
            logger.info(f"Connecting to room {self._room_id}")
            # TODO: 实现实际的连接逻辑
            self._connected = True
            self._connect_counter += 1
            await self._callbacks.on_connected()
        except Exception as e:
            logger.error(f"Error connecting to room {self._room_id}: {e}")
            await self._callbacks.on_error(str(e))
            raise

    async def disconnect(self):
        """断开与 Agora 房间的连接"""
        self._connect_counter -= 1
        
        if not self._connected or self._connect_counter > 0:
            return
            
        try:
            logger.info(f"Disconnecting from room {self._room_id}")
            # TODO: 实现实际的断开连接逻辑
            self._connected = False
            await self._callbacks.on_disconnected()
        except Exception as e:
            logger.error(f"Error disconnecting from room {self._room_id}: {e}")
            await self._callbacks.on_error(str(e))

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

    async def send_audio(self, audio_data: bytes):
        """发送音频数据"""
        if not self._connected:
            return
            
        try:
            # TODO: 实现实际的音频发送逻辑
            logger.debug("Sending audio data")
        except Exception as e:
            logger.error(f"Error sending audio: {e}")
            await self._callbacks.on_error(str(e))

    def get_participants(self) -> List[str]:
        """获取当前房间的参与者列表"""
        # TODO: 实现获取参与者列表的逻辑
        return []

    async def cleanup(self):
        """清理资源"""
        await self.disconnect()

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
        room_id: str,
        token: str,
        params: AgoraParams = AgoraParams(),
        input_name: str | None = None,
        output_name: str | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ):
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
        self._client = AgoraTransportClient(room_id, token, params, callbacks, self._loop)
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