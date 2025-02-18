import argparse
import os
from typing import Tuple

import aiohttp
from datetime import datetime, timedelta

# # Agora token生成相关导入
# from agora.rtc.agora_token import RtcTokenBuilder, Role_Publisher


async def generate_agora_token(app_id: str, app_certificate: str, channel_name: str, uid: int) -> str:
    """生成Agora Token"""
    # Token过期时间设置为1小时
    expiration_time = int((datetime.now() + timedelta(hours=1)).timestamp())

    # # 生成RTC Token
    # token = RtcTokenBuilder.buildTokenWithUid(
    #     app_id,
    #     app_certificate,
    #     channel_name,
    #     uid,
    #     Role_Publisher,
    #     expiration_time
    # )

    return "token"


async def configure(aiohttp_session: aiohttp.ClientSession) -> Tuple[str, str, int]:
    """配置Agora连接参数。

    Returns:
        Tuple[str, str, int]: 包含(room_id, token, uid)
    """
    parser = argparse.ArgumentParser(description="Agora AI Bot")
    parser.add_argument(
        "-r", "--room", type=str, required=False,
        help="Agora room ID to join"
    )
    parser.add_argument(
        "-u", "--uid", type=int, required=False,
        help="User ID for Agora connection"
    )
    parser.add_argument(
        "-t", "--token", type=str, required=False,
        help="Agora token (if not provided, will be generated)"
    )

    args, unknown = parser.parse_known_args()

    # 获取配置参数
    app_id = os.getenv("AGORA_APP_ID")
    app_certificate = os.getenv("AGORA_APP_CERTIFICATE")
    room_id = args.room or os.getenv("AGORA_ROOM_ID")
    uid = args.uid or int(os.getenv("AGORA_UID", "0"))
    token = args.token

    if not app_id:
        raise Exception(
            "No Agora App ID specified. Set AGORA_APP_ID in your environment."
        )

    if not room_id:
        raise Exception(
            "No Agora room specified. Use -r/--room option or set AGORA_ROOM_ID in your environment."
        )

    # 如果没有提供token，则生成一个
    if not token:
        if not app_certificate:
            raise Exception(
                "No Agora App Certificate specified. Set AGORA_APP_CERTIFICATE in your environment for token generation."
            )
        token = await generate_agora_token(app_id, app_certificate, room_id, uid)

    return room_id, token, uid