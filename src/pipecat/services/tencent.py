#
# Copyright (c) 2024, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

import asyncio
import hmac
import hashlib
import base64
import json
import time
import uuid
import urllib.parse
from typing import AsyncGenerator
import websocket
import threading

from loguru import logger

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
)
from pipecat.services.ai_services import SegmentedSTTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601


class TencentSTTService(SegmentedSTTService):
    def __init__(
        self,
        *,
        app_id: str,
        secret_id: str,
        secret_key: str,
        engine_model_type: str = "16k_zh",
        voice_format: int = 1,  # 1: pcm
        need_vad: int = 0,      # 关闭VAD，避免句子被截断
        filter_dirty: int = 1,
        filter_modal: int = 1,
        filter_punc: int = 1,
        convert_num_mode: int = 1,
        word_info: int = 1,
        **kwargs,
    ):
        super().__init__(**kwargs)
        
        self._app_id = app_id
        self._secret_id = secret_id
        self._secret_key = secret_key
        self._engine_model_type = engine_model_type
        self._voice_format = voice_format
        self._need_vad = need_vad
        self._filter_dirty = filter_dirty
        self._filter_modal = filter_modal
        self._filter_punc = filter_punc
        self._convert_num_mode = convert_num_mode
        self._word_info = word_info
        
        self._ws = None
        self._wst = None
        self._voice_id = ""
        self._status = 0  # 0: NOTOPEN, 1: STARTED, 2: OPENED
        self._last_audio_time = 0
        self._reconnect_task = None

    def can_generate_metrics(self) -> bool:
        return True

    def _format_sign_string(self, param):
        signstr = "asr.cloud.tencent.com/asr/v2/"
        for t in param:
            if 'appid' in t:
                signstr += str(t[1])
                break
        signstr += "?"
        for x in param:
            tmp = x
            if 'appid' in x:
                continue
            for t in tmp:
                signstr += str(t)
                signstr += "="
            signstr = signstr[:-1]
            signstr += "&"
        signstr = signstr[:-1]
        return signstr

    def _create_query_string(self, param):
        signstr = "wss://asr.cloud.tencent.com/asr/v2/"
        for t in param:
            if 'appid' in t:
                signstr += str(t[1])
                break
        signstr += "?"
        for x in param:
            tmp = x
            if 'appid' in x:
                continue
            for t in tmp:
                signstr += str(t)
                signstr += "="
            signstr = signstr[:-1]
            signstr += "&"
        signstr = signstr[:-1]
        return signstr

    def _sign(self, signstr, secret_key):
        hmacstr = hmac.new(secret_key.encode('utf-8'),
                          signstr.encode('utf-8'), hashlib.sha1).digest()
        s = base64.b64encode(hmacstr)
        s = s.decode('utf-8')
        return s

    def _create_query_arr(self):
        query_arr = {
            'appid': self._app_id,
            'sub_service_type': 1,
            'engine_model_type': self._engine_model_type,
            'voice_format': self._voice_format,
            'needvad': self._need_vad,
            'filter_dirty': self._filter_dirty,
            'filter_modal': self._filter_modal,
            'filter_punc': self._filter_punc,
            'convert_num_mode': self._convert_num_mode,
            'word_info': self._word_info,
            'secretid': self._secret_id,
            'timestamp': str(int(time.time())),
            'expired': int(time.time()) + 24 * 60 * 60,
            'nonce': str(int(time.time())),
            'voice_id': self._voice_id or str(uuid.uuid1()),
        }
        return query_arr

    async def _keep_alive(self):
        """保持连接活跃的任务"""
        while self._status == 2:  # OPENED
            current_time = time.time()
            if current_time - self._last_audio_time > 10:  # 如果超过10秒没有音频
                logger.debug("Sending keep-alive packet")
                try:
                    # 使用 send 而不是 send_binary
                    await asyncio.to_thread(self._ws.sock.send, b'\x00' * 640, websocket.ABNF.OPCODE_BINARY)
                    self._last_audio_time = current_time
                except Exception as e:
                    logger.error(f"Error sending keep-alive: {e}")
                    await self._reconnect()
                    break
            await asyncio.sleep(1)

    async def _reconnect(self):
        """重新连接服务"""
        logger.info("Attempting to reconnect...")
        try:
            await self._disconnect()
            await asyncio.sleep(1)  # 等待一秒后重连
            await self._connect()
            # 等待连接完全建立
            retry_count = 0
            while self._status != 2 and retry_count < 5:
                await asyncio.sleep(1)
                retry_count += 1
            if self._status != 2:
                logger.error("Failed to establish connection after reconnect")
        except Exception as e:
            logger.error(f"Reconnection failed: {e}")

    async def _connect(self):
        logger.debug("Connecting to Tencent Cloud ASR")
        
        query_arr = self._create_query_arr()
        query = sorted(query_arr.items(), key=lambda d: d[0])
        signstr = self._format_sign_string(query)
        signature = self._sign(signstr, self._secret_key)
        signature = urllib.parse.quote(signature)
        url = self._create_query_string(query) + "&signature=" + signature
        
        logger.debug(f"Connecting to URL: {url}")

        self._ws = websocket.WebSocketApp(
            url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close
        )
        
        self._wst = threading.Thread(target=self._ws.run_forever)
        self._wst.daemon = True
        self._wst.start()
        self._status = 1  # STARTED
        self._last_audio_time = time.time()

        # 等待连接建立
        timeout = 30  # 30秒超时
        start_time = time.time()
        while not hasattr(self._ws, 'sock') or not self._ws.sock:
            if time.time() - start_time > timeout:
                logger.error("Timeout waiting for WebSocket connection")
                break
            await asyncio.sleep(0.1)
            
        if hasattr(self._ws, 'sock') and self._ws.sock:
            logger.debug("WebSocket connection established successfully")
        else:
            logger.error("Failed to establish WebSocket connection")

    async def _disconnect(self):
        if self._status == 2:  # OPENED
            try:
                # 发送结束标记
                end_msg = {
                    "type": "end",
                    "voice_id": self._voice_id
                }
                await asyncio.to_thread(
                    self._ws.sock.send,
                    json.dumps(end_msg),
                    websocket.ABNF.OPCODE_TEXT
                )
                logger.debug("Sent end message successfully")
                # 等待一小段时间，确保服务器处理结束消息
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.error(f"Error sending end message: {e}")
        
        if self._ws:
            try:
                if self._wst and self._wst.is_alive():
                    self._ws.close()
                    self._wst.join(timeout=2)
            except Exception as e:
                logger.error(f"Error closing WebSocket: {e}")
        self._status = 0  # NOTOPEN
        self._ws = None
        self._wst = None

    def _on_open(self, ws):
        self._status = 2  # OPENED
        logger.debug("Connected to Tencent Cloud ASR")
        # 启动保活任务
        asyncio.run_coroutine_threadsafe(
            self._keep_alive(),
            self.get_event_loop()
        )

    def _on_message(self, ws, message):
        try:
            logger.debug(f"Received message: {message}")
            response = json.loads(message)
            
            if response['code'] != 0:
                logger.error(f"Recognition failed: {response['message']}")
                if "超时" in response['message']:
                    asyncio.run_coroutine_threadsafe(
                        self._reconnect(),
                        self.get_event_loop()
                    )
                return

            logger.debug(f"Processing message: final={response.get('final')}")
            
            if "result" in response:
                result = response["result"]
                # 检查是否是最终结果
                is_final = result.get("slice_type") == 2
                text = result.get("voice_text_str", "")
                
                if is_final:
                    logger.info(f"Final transcription: {text}")
                    frame = TranscriptionFrame(
                        text=text,
                        user_id="",
                        timestamp=time_now_iso8601(),
                        language=Language.ZH_CN
                    )
                    asyncio.run_coroutine_threadsafe(
                        self.push_frame(frame), 
                        self.get_event_loop()
                    )
                    asyncio.run_coroutine_threadsafe(
                        self.stop_ttfb_metrics(), 
                        self.get_event_loop()
                    )
                    asyncio.run_coroutine_threadsafe(
                        self.stop_processing_metrics(), 
                        self.get_event_loop()
                    )
                else:
                    logger.debug(f"Interim transcription: {text}")
                    frame = InterimTranscriptionFrame(
                        text=text,
                        user_id="",
                        timestamp=time_now_iso8601(),
                        language=Language.ZH_CN
                    )
                    asyncio.run_coroutine_threadsafe(
                        self.push_frame(frame), 
                        self.get_event_loop()
                    )
                    asyncio.run_coroutine_threadsafe(
                        self.stop_ttfb_metrics(), 
                        self.get_event_loop()
                    )
        except Exception as e:
            logger.error(f"Error processing message: {e}")
            logger.error(f"Raw message: {message}")

    def _on_error(self, ws, error):
        if self._status != 3:  # Not FINAL
            logger.error(f"Websocket error: {error}")
            logger.error(f"Current status: {self._status}")
            self._status = 4  # ERROR

    def _on_close(self, ws, close_status_code=None, close_msg=None):
        self._status = 5  # CLOSED
        logger.debug(f"Connection closed. Status code: {close_status_code}, Message: {close_msg}")

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self._connect()

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._disconnect()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._disconnect()

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        """Transcribes given audio using Tencent Cloud ASR"""
        if not self._ws or self._status != 2:
            # 尝试重连
            await self._reconnect()
            if not self._ws or self._status != 2:
                logger.error(f"{self} error: WebSocket connection not available")
                yield ErrorFrame("WebSocket connection not available")
                return

        await self.start_processing_metrics()
        await self.start_ttfb_metrics()

        try:
            self._last_audio_time = time.time()
            logger.debug(f"Sending audio data, length: {len(audio)} bytes")
            await asyncio.to_thread(self._ws.sock.send, audio, websocket.ABNF.OPCODE_BINARY)
            logger.debug("Audio data sent successfully")
        except Exception as e:
            logger.error(f"Error sending audio: {e}")
            await self._reconnect()
            yield ErrorFrame(f"Failed to send audio: {str(e)}")
            return

        yield None 