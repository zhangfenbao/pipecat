import asyncio
import base64
import hashlib
import hmac
import json
import ssl
import time
from datetime import datetime
from typing import AsyncGenerator
from urllib.parse import urlencode
from wsgiref.handlers import format_date_time
from time import mktime

import websocket
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
from pipecat.services.ai_services import STTService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601

try:
    import websocket
except ModuleNotFoundError as e:
    logger.error(f"Exception: {e}")
    logger.error(
        "In order to use Iflytek, you need to `pip install websocket-client`. Also, set `IFLYTEK_APP_ID`, `IFLYTEK_API_KEY`, and `IFLYTEK_API_SECRET` environment variables."
    )
    raise Exception(f"Missing module: {e}")

class IflytekSTTService(STTService):
    def __init__(
        self,
        *,
        app_id: str,
        api_key: str,
        api_secret: str,
        domain: str = "iat",
        language: Language = Language.ZH,
        accent: str = "mandarin",
        vad_eos: int = 10000,
        **kwargs,
    ):
        super().__init__(**kwargs)
        
        self._app_id = app_id
        self._api_key = api_key
        self._api_secret = api_secret
        
        # 业务参数设置
        self._settings = {
            "domain": domain,
            "language": self._convert_language(language),
            "accent": accent,
            "vinfo": 1,
            "vad_eos": vad_eos,
            "dwa": "wpgs",  # 开启动态修正功能
            "pd": "game",   # 设置场景
            "ptt": 0        # 设置标点符号
        }
        
        self._ws = None
        self._current_transcript = ""
        self._is_final = False
        self._buffer = b""  # 用于累积音频数据

    def _convert_language(self, language: Language) -> str:
        """Convert pipecat Language to Iflytek language code"""
        language_map = {
            Language.ZH: "zh_cn",
            Language.EN: "en_us",
            # 可以根据需要添加更多语言映射
        }
        return language_map.get(language, "zh_cn")

    def can_generate_metrics(self) -> bool:
        return True

    async def set_language(self, language: Language):
        logger.info(f"Switching STT language to: [{language}]")
        self._settings["language"] = self._convert_language(language)
        await self._disconnect()
        await self._connect()

    def _create_url(self):
        url = 'wss://ws-api.xfyun.cn/v2/iat'
        now = datetime.now()
        date = format_date_time(mktime(now.timetuple()))

        signature_origin = "host: " + "ws-api.xfyun.cn" + "\n"
        signature_origin += "date: " + date + "\n"
        signature_origin += "GET " + "/v2/iat " + "HTTP/1.1"

        signature_sha = hmac.new(
            self._api_secret.encode('utf-8'),
            signature_origin.encode('utf-8'),
            digestmod=hashlib.sha256
        ).digest()
        signature_sha = base64.b64encode(signature_sha).decode(encoding='utf-8')

        authorization_origin = f'api_key="{self._api_key}", algorithm="hmac-sha256", headers="host date request-line", signature="{signature_sha}"'
        authorization = base64.b64encode(authorization_origin.encode('utf-8')).decode(encoding='utf-8')

        v = {
            "authorization": authorization,
            "date": date,
            "host": "ws-api.xfyun.cn"
        }
        return url + '?' + urlencode(v)

    async def _connect(self):
        if self._ws is not None:
            return

        url = self._create_url()
        self._ws = websocket.WebSocket(sslopt={"cert_reqs": ssl.CERT_NONE})
        self._ws.connect(url)
        # 设置接收超时时间为5秒
        self._ws.settimeout(5)
        logger.debug("Connected to Iflytek")

    async def _disconnect(self):
        if self._ws:
            self._ws.close()
            self._ws = None
            logger.debug("Disconnected from Iflytek")

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self._connect()

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._disconnect()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._disconnect()

    async def _handle_response(self, message: str):
        try:
            logger.debug(f"Raw response: {message}")
            code = json.loads(message)["code"]
            sid = json.loads(message)["sid"]
            
            if code != 0:
                error_msg = json.loads(message)["message"]
                logger.error(f"Iflytek error: {error_msg}")
                await self.push_frame(ErrorFrame(f"Iflytek error: {error_msg}"))
                return

            data = json.loads(message)["data"]
            result = ""
            if "result" in data:
                ws = data["result"]["ws"]
                for i in ws:
                    for w in i["cw"]:
                        result += w["w"]
                
                if result.strip():
                    is_final = data.get("status", 0) == 2
                    frame_cls = TranscriptionFrame if is_final else InterimTranscriptionFrame
                    logger.info(f"Got transcript: [{result}] (final: {is_final})")
                    
                    frame = frame_cls(
                        result,
                        "",
                        time_now_iso8601(),
                        Language(self._settings["language"])
                    )
                    logger.debug(f"Pushing frame: {frame}")
                    await self.push_frame(frame)

        except Exception as e:
            logger.exception(f"Error handling Iflytek response: {e}")
            await self.push_frame(ErrorFrame(f"Error handling response: {str(e)}"))

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        try:
            # 累积音频数据
            self._buffer += audio
            
            # 如果数据太少，等待更多数据
            if len(self._buffer) < 8000:  # 参考demo使用8000字节作为帧大小
                logger.debug(f"Buffering audio data: {len(self._buffer)}/8000 bytes")
                yield None
                return

            if not self._ws or not self._ws.connected:
                await self._connect()

            await self.start_ttfb_metrics()
            await self.start_processing_metrics()

            logger.debug(f"Processing audio data: {len(self._buffer)} bytes")
            
            try:
                # 发送第一帧
                first_frame = {
                    "common": {"app_id": self._app_id},
                    "business": self._settings,
                    "data": {
                        "status": 0,
                        "format": "audio/L16;rate=16000",
                        "audio": str(base64.b64encode(self._buffer[:8000]), 'utf-8'),
                        "encoding": "raw"
                    }
                }
                
                logger.debug(f"Sending first frame with settings: {self._settings}")
                self._ws.send(json.dumps(first_frame))
                logger.debug("Sent first frame")
                
                # 分块发送剩余音频数据
                frame_size = 8000
                for i in range(frame_size, len(self._buffer), frame_size):
                    chunk = self._buffer[i:i + frame_size]
                    if not chunk:
                        break
                        
                    audio_frame = {
                        "data": {
                            "status": 1,
                            "format": "audio/L16;rate=16000",
                            "audio": str(base64.b64encode(chunk), 'utf-8'),
                            "encoding": "raw"
                        }
                    }
                    self._ws.send(json.dumps(audio_frame))
                    logger.debug(f"Sent audio chunk {(i-frame_size)//frame_size + 1}")
                    
                    # 接收响应
                    try:
                        response = self._ws.recv()
                        if response:
                            logger.debug("Got response for audio chunk")
                            await self._handle_response(response)
                    except websocket.WebSocketTimeoutException:
                        logger.debug("No response for this chunk")
                
                # 发送结束帧
                end_frame = {
                    "data": {
                        "status": 2,
                        "format": "audio/L16;rate=16000",
                        "audio": "",
                        "encoding": "raw"
                    }
                }
                self._ws.send(json.dumps(end_frame))
                logger.debug("Sent end frame")
                
                # 接收最终响应
                retries = 3
                while retries > 0:
                    try:
                        response = self._ws.recv()
                        if response:
                            logger.debug("Got final response")
                            await self._handle_response(response)
                            
                            data = json.loads(response)
                            if data.get("code") == 0 and data.get("data", {}).get("status") == 2:
                                logger.debug("Got end of speech")
                                await self.stop_processing_metrics()
                                break
                    except websocket.WebSocketTimeoutException:
                        logger.warning(f"Response timeout, retries left: {retries}")
                        retries -= 1
                    except websocket.WebSocketConnectionClosedException:
                        logger.warning("WebSocket connection closed")
                        break
                    except Exception as e:
                        logger.error(f"Error receiving response: {e}")
                        break

                # 清空缓冲区
                self._buffer = b""
                await self.stop_ttfb_metrics()

            except websocket.WebSocketConnectionClosedException:
                logger.warning("WebSocket connection closed, attempting reconnect...")
                await self._disconnect()
                await self._connect()
                # 不清空缓冲区，下次继续处理
                yield None
                return

            yield None

        except Exception as e:
            logger.exception(f"Error in run_stt: {e}")
            yield ErrorFrame(f"Error in speech-to-text: {str(e)}")
            await self._disconnect()
            # 发生错误时清空缓冲区
            self._buffer = b""
