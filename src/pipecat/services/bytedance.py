#
# Copyright (c) 2024, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import base64
import copy
import gzip
import json
import uuid
from typing import Any, AsyncGenerator, Dict, List, Mapping, Optional, Tuple

from loguru import logger
from pydantic import BaseModel
import websockets

from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    LLMFullResponseEndFrame,
    StartFrame,
    StartInterruptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.ai_services import WordTTSService, STTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

# ByteDance WebSocket 相关常量
CLIENT_FULL_REQUEST = 0b0001
CLIENT_AUDIO_ONLY_REQUEST = 0b0010
SERVER_FULL_RESPONSE = 0b1001  # 9
SERVER_ACK = 0b1011           # 11
SERVER_ERROR_RESPONSE = 0b1111 # 15

# Message Type Specific Flags
NO_SEQUENCE = 0b0000  # no check sequence
POS_SEQUENCE = 0b0001
NEG_SEQUENCE = 0b0010
NEG_SEQUENCE_1 = 0b0011

# Message Serialization
NO_SERIALIZATION = 0b0000
JSON = 0b0001
THRIFT = 0b0011
CUSTOM_TYPE = 0b1111

# Message Compression
NO_COMPRESSION = 0b0000
GZIP = 0b0001
CUSTOM_COMPRESSION = 0b1111

# 默认的 WebSocket 请求头
DEFAULT_HEADER = bytearray(b'\x11\x10\x11\x00')

# 音频请求头
AUDIO_DEFAULT_HEADER = bytearray(b'\x11\x20\x11\x00')

def generate_audio_default_header() -> bytearray:
    """生成音频请求的默认头部"""
    return AUDIO_DEFAULT_HEADER

class ByteDanceTTSService(WordTTSService):
    class InputParams(BaseModel):
        voice_type: str = "zh_male_M392_conversation_wvae_bigtts"
        encoding: str = "pcm"  # PCM 格式
        speed_ratio: float = 1.0
        volume_ratio: float = 1.0
        pitch_ratio: float = 1.0
        language: Optional[Language] = Language.ZH

    def __init__(
        self,
        *,
        app_id: str,
        access_token: str,
        cluster: str = "volcano_tts",
        host: str = "openspeech.bytedance.com",
        params: InputParams = InputParams(),
        **kwargs,
    ):
        super().__init__(
            aggregate_sentences=True,
            push_text_frames=False,
            push_stop_frames=True,
            stop_frame_timeout_s=2.0,
            sample_rate=16000,  # ByteDance TTS 默认采样率
            **kwargs,
        )

        self._app_id = app_id
        self._access_token = access_token
        self._cluster = cluster
        self._host = host
        self._settings = {
            "voice_type": params.voice_type,
            "encoding": params.encoding,
            "speed_ratio": params.speed_ratio,
            "volume_ratio": params.volume_ratio,
            "pitch_ratio": params.pitch_ratio,
        }

        # WebSocket 连接
        self._websocket = None
        self._started = False
        self._cumulative_time = 0

    def can_generate_metrics(self) -> bool:
        return True

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self._connect()

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._disconnect()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._disconnect()

    async def push_frame(self, frame: Frame, direction: FrameDirection = FrameDirection.DOWNSTREAM):
        await super().push_frame(frame, direction)
        if isinstance(frame, (TTSStoppedFrame, StartInterruptionFrame)):
            self._started = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TTSSpeakFrame):
            await self.pause_processing_frames()
        elif isinstance(frame, LLMFullResponseEndFrame) and self._started:
            await self.pause_processing_frames()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            await self.resume_processing_frames()

    async def _connect(self):
        try:
            logger.debug("Connecting to ByteDance TTS")
            url = f"wss://{self._host}/api/v1/tts/ws_binary"
            headers = {"Authorization": f"Bearer; {self._access_token}"}
            self._websocket = await websockets.connect(url, extra_headers=headers, ping_interval=None)
        except Exception as e:
            logger.error(f"{self} initialization error: {e}")
            self._websocket = None

    async def _disconnect(self):
        try:
            await self.stop_all_metrics()

            if self._websocket:
                logger.debug("Disconnecting from ByteDance TTS")
                await self._websocket.close()
                self._websocket = None

            self._started = False
        except Exception as e:
            logger.error(f"{self} error closing websocket: {e}")

    def _create_request_json(self, text: str) -> Dict[str, Any]:
        return {
            "app": {
                "appid": self._app_id,
                "token": self._access_token,
                "cluster": self._cluster
            },
            "user": {
                "uid": str(uuid.uuid4())
            },
            "audio": {
                "voice_type": self._settings["voice_type"],
                "encoding": self._settings["encoding"],
                "speed_ratio": self._settings["speed_ratio"],
                "volume_ratio": self._settings["volume_ratio"],
                "pitch_ratio": self._settings["pitch_ratio"],
            },
            "request": {
                "reqid": str(uuid.uuid4()),
                "text": text,
                "text_type": "plain",
                "operation": "submit"
            }
        }

    def _create_request_bytes(self, request_json: Dict[str, Any]) -> bytearray:
        payload_bytes = str.encode(json.dumps(request_json))
        payload_bytes = gzip.compress(payload_bytes)
        full_request = bytearray(DEFAULT_HEADER)
        full_request.extend(len(payload_bytes).to_bytes(4, 'big'))
        full_request.extend(payload_bytes)
        return full_request

    async def _handle_response(self, response: bytes) -> Optional[bytes]:
        message_type = response[1] >> 4
        message_type_specific_flags = response[1] & 0x0f
        header_size = response[0] & 0x0f
        payload = response[header_size * 4:]

        if message_type == 0xb:  # audio-only server response
            if message_type_specific_flags == 0:
                return None
            sequence_number = int.from_bytes(payload[:4], "big", signed=True)
            payload_size = int.from_bytes(payload[4:8], "big", signed=False)
            audio_data = payload[8:]
            
            return audio_data if sequence_number != 0 else None
        elif message_type == 0xf:  # error message
            code = int.from_bytes(payload[:4], "big", signed=False)
            msg_size = int.from_bytes(payload[4:8], "big", signed=False)
            error_msg = payload[8:]
            error_msg = gzip.decompress(error_msg)
            error_msg = str(error_msg, "utf-8")
            raise Exception(f"ByteDance TTS error: {error_msg} (code: {code})")
        return None

    async def run_tts(self, text: str) -> AsyncGenerator[Frame, None]:
        logger.debug(f"Generating TTS: [{text}]")

        try:
            if not self._websocket:
                await self._connect()

            try:
                if not self._started:
                    await self.start_ttfb_metrics()
                    yield TTSStartedFrame()
                    self._started = True
                    self._cumulative_time = 0

                request_json = self._create_request_json(text)
                request_bytes = self._create_request_bytes(request_json)
                await self._websocket.send(request_bytes)
                await self.start_tts_usage_metrics(text)

                while True:
                    response = await self._websocket.recv()
                    audio_data = await self._handle_response(response)
                    if audio_data:
                        await self.stop_ttfb_metrics()
                        frame = TTSAudioRawFrame(audio_data, self._settings.get("sample_rate", 16000), 1)
                        yield frame

            except Exception as e:
                logger.error(f"{self} error sending message: {e}")
                yield TTSStoppedFrame()
                await self._disconnect()
                await self._connect()
                return

            yield None

        except Exception as e:
            logger.error(f"{self} exception: {e}")

class ByteDanceSTTService(STTService):
    #这个实现有问题，只能单轮stt，不能持续
    def __init__(
        self,
        *,
        app_id: str,
        access_token: str,
        cluster: str = "volcengine_streaming_common",
        host: str = "openspeech.bytedance.com",
        max_retries: int = 3,
        retry_delay: float = 1.0,
        **kwargs,
    ):
        super().__init__(**kwargs)
        
        self._app_id = app_id
        self._access_token = access_token
        self._cluster = cluster
        self._host = host
        self._websocket = None
        self._started = False
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._lock = asyncio.Lock()
        self._sequence = 0
        self._session_id = None
        self._settings = {
            "format": "raw",  # 音频格式为 PCM
            "rate": 16000,    # 采样率
            "bits": 16,       # 位深
            "channel": 1,     # 单声道
            "codec": "raw",   # 编码格式
            "language": "zh-CN",  # 默认中文
            "nbest": 1,       # 返回结果数量
            "workflow": "audio_in,resample,partition,vad,fe,decode,itn,nlu_punctuate",
            "show_language": False,
            "show_utterances": False,
            "result_type": "full"
        }

    def can_generate_metrics(self) -> bool:
        return True

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self._connect()

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._disconnect()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._disconnect()

    async def _connect_with_retry(self):
        for attempt in range(self._max_retries):
            try:
                logger.debug("Connecting to ByteDance STT")
                url = f"wss://{self._host}/api/v2/asr"
                headers = {"Authorization": f"Bearer; {self._access_token}"}
                self._websocket = await websockets.connect(url, extra_headers=headers)
                
                # 重置会话状态
                self._sequence = 1  # 从1开始
                self._session_id = str(uuid.uuid4())
                
                # 发送初始化请求
                request = self._create_request_json()
                request_bytes = self._create_request_bytes(request)
                await self._websocket.send(request_bytes)
                
                # 接收服务器响应
                response = await self._websocket.recv()
                result = self._parse_response(response)
                
                logger.debug(f"Server response: {result}")
                
                if not result or not isinstance(result.get('payload_msg'), dict):
                    logger.error(f"Invalid response format: {result}")
                    if attempt < self._max_retries - 1:
                        await asyncio.sleep(self._retry_delay)
                        continue
                    raise Exception(f"Invalid response format from server: {result}")

                code = result.get('payload_msg', {}).get('code')
                if code != 1000:
                    error_msg = result.get('payload_msg', {}).get('message', 'Unknown error')
                    if 'concurrency' in error_msg:
                        logger.warning(f"Concurrency limit reached, retrying in {self._retry_delay} seconds...")
                        await asyncio.sleep(self._retry_delay)
                        continue
                    raise Exception(f"Failed to initialize STT: {result}")
                
                # 保存会话ID
                self._session_id = result.get('payload_msg', {}).get('session_id', self._session_id)
                self._started = True
                return True
                    
            except Exception as e:
                logger.error(f"{self} initialization error: {e}")
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(self._retry_delay)
                else:
                    await self._disconnect()
                    return False
        return False

    async def _connect(self):
        async with self._lock:
            if not self._websocket:
                return await self._connect_with_retry()
            return True

    async def _disconnect(self):
        async with self._lock:
            try:
                await self.stop_all_metrics()
                if self._websocket:
                    logger.debug("Disconnecting from ByteDance STT")
                    await self._websocket.close()
                    self._websocket = None
                self._started = False
                self._sequence = 0
                self._session_id = None
            except Exception as e:
                logger.error(f"{self} error closing websocket: {e}")

    def _create_request_json(self) -> Dict[str, Any]:
        return {
            "app": {
                "appid": self._app_id,
                "token": self._access_token,
                "cluster": self._cluster,
            },
            "user": {
                "uid": str(uuid.uuid4())
            },
            "request": {
                "reqid": str(uuid.uuid4()),
                "nbest": self._settings["nbest"],
                "workflow": self._settings["workflow"],
                "show_language": self._settings["show_language"],
                "show_utterances": self._settings["show_utterances"],
                "result_type": self._settings["result_type"],
                "sequence": self._sequence,
                "end": False  # 添加 end 标志
            },
            "audio": {
                "format": self._settings["format"],
                "rate": self._settings["rate"],
                "language": self._settings["language"],
                "bits": self._settings["bits"],
                "channel": self._settings["channel"],
                "codec": self._settings["codec"]
            }
        }

    def _create_audio_request_json(self) -> Dict[str, Any]:
        return {
            "reqid": str(uuid.uuid4()),
            "sequence": self._sequence,
            "end": False
        }

    def _create_request_bytes(self, request_json: Dict[str, Any], is_audio: bool = False) -> bytearray:
        payload_bytes = str.encode(json.dumps(request_json))
        payload_bytes = gzip.compress(payload_bytes)
        
        # 选择合适的头部
        header = AUDIO_DEFAULT_HEADER if is_audio else DEFAULT_HEADER
        full_request = bytearray(header)
        
        # 添加负载大小和负载
        full_request.extend(len(payload_bytes).to_bytes(4, 'big'))
        full_request.extend(payload_bytes)
        return full_request

    def _parse_response(self, response: bytes) -> Dict[str, Any]:
        try:
            protocol_version = response[0] >> 4
            header_size = response[0] & 0x0f
            message_type = response[1] >> 4
            message_type_specific_flags = response[1] & 0x0f
            serialization_method = response[2] >> 4
            message_compression = response[2] & 0x0f
            reserved = response[3]
            header_extensions = response[4:header_size * 4]
            payload = response[header_size * 4:]
            result = {}
            payload_msg = None

            # 打印详细的解析信息用于调试
            logger.debug(f"Parsing response - protocol_version: {protocol_version}, header_size: {header_size}")
            logger.debug(f"message_type: {message_type}, flags: {message_type_specific_flags}")
            logger.debug(f"serialization: {serialization_method}, compression: {message_compression}")
            logger.debug(f"Payload length: {len(payload)}")
            logger.debug(f"Raw payload: {payload[:20]}...")

            if message_type == SERVER_FULL_RESPONSE:  # 0x9
                payload_size = int.from_bytes(payload[:4], "big", signed=True)
                payload_msg = payload[4:]
                result['payload_size'] = payload_size
            elif message_type == SERVER_ACK:  # 0xb
                sequence_number = int.from_bytes(payload[:4], "big", signed=True)
                result['seq'] = sequence_number
                if len(payload) >= 8:
                    payload_size = int.from_bytes(payload[4:8], "big", signed=False)
                    payload_msg = payload[8:]
                    result['payload_size'] = payload_size
            elif message_type == SERVER_ERROR_RESPONSE:  # 0xf
                code = int.from_bytes(payload[:4], "big", signed=False)
                result['code'] = code
                payload_size = int.from_bytes(payload[4:8], "big", signed=False)
                payload_msg = payload[8:]
                result['payload_size'] = payload_size
            else:
                logger.warning(f"Unknown message type: {message_type}")
                return {'payload_msg': {'code': -1, 'message': f'Unknown message type: {message_type}'}}

            if payload_msg is not None:
                if message_compression == GZIP:
                    try:
                        payload_msg = gzip.decompress(payload_msg)
                    except Exception as e:
                        logger.error(f"Decompression error: {e}")
                        return {'payload_msg': {'code': -1, 'message': f'Decompression error: {str(e)}'}}

                if serialization_method == JSON:
                    try:
                        payload_msg = json.loads(str(payload_msg, "utf-8"))
                    except Exception as e:
                        logger.error(f"JSON decode error: {e}")
                        return {'payload_msg': {'code': -1, 'message': f'JSON decode error: {str(e)}'}}
                elif serialization_method != NO_SERIALIZATION:
                    try:
                        payload_msg = str(payload_msg, "utf-8")
                    except Exception as e:
                        logger.error(f"String decode error: {e}")
                        return {'payload_msg': {'code': -1, 'message': f'String decode error: {str(e)}'}}

                result['payload_msg'] = payload_msg

            logger.debug(f"Parsed result: {result}")
            return result
        except Exception as e:
            logger.error(f"Error parsing response: {e}")
            return {'payload_msg': {'code': -1, 'message': f'Error parsing response: {str(e)}'}}

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        try:
            async with self._lock:
                if not self._websocket or not self._started:
                    if not await self._connect():
                        yield ErrorFrame("Failed to connect to ByteDance STT service")
                        return

                # 音频分片大小（10KB，与demo保持一致）
                chunk_size = 10000
                offset = 0
                audio_len = len(audio)

                while offset < audio_len:
                    # 获取当前分片
                    chunk = audio[offset:offset + chunk_size]
                    is_last = (offset + chunk_size >= audio_len)
                    
                    # 压缩音频分片
                    chunk_compressed = gzip.compress(chunk)
                    
                    # 创建音频请求
                    audio_request = bytearray(generate_audio_default_header())
                    audio_request.extend(len(chunk_compressed).to_bytes(4, 'big'))
                    audio_request.extend(chunk_compressed)
                    
                    # 发送请求
                    self._sequence += 1
                    await self._websocket.send(audio_request)
                    
                    # 处理响应
                    response = await self._websocket.recv()
                    result = self._parse_response(response)
                    
                    if result.get('payload_msg'):
                        if isinstance(result['payload_msg'], dict):
                            code = result['payload_msg'].get('code')
                            if code == 1020:  # 会话过期
                                logger.warning("Session expired, reconnecting...")
                                await self._disconnect()
                                if await self._connect():
                                    # 重试当前分片
                                    continue
                                else:
                                    yield ErrorFrame("Failed to reconnect to STT service")
                                    return
                            elif code != 1000:  # 其他错误
                                error_msg = result['payload_msg'].get('message', 'Unknown error')
                                yield ErrorFrame(f"STT error: {error_msg}")
                                return
                            
                            # 处理识别结果
                            text = result['payload_msg'].get('result', [{}])[0].get('text', '')
                            if text:
                                yield TranscriptionFrame(text, "", time_now_iso8601())
                    
                    # 更新偏移量
                    offset += chunk_size

        except Exception as e:
            logger.error(f"{self} error in run_stt: {e}")
            yield ErrorFrame(f"Error in speech recognition: {str(e)}")
            await self._disconnect()  # 断开连接，下次会重新连接

    async def set_language(self, language: Language):
        logger.info(f"Switching STT language to: [{language}]")
        self._settings["language"] = language.value
        await self._disconnect()
        await self._connect()