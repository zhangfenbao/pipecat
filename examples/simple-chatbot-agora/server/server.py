import argparse
import os
import subprocess
from contextlib import asynccontextmanager
from typing import Dict, Any
from datetime import datetime, timedelta

import aiohttp
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

# from agora.rtc.agora_token import RtcTokenBuilder, Role_Publisher

# Load environment variables
load_dotenv(override=True)

# Track bot processes
bot_procs = {}
MAX_BOTS_PER_ROOM = 1


def cleanup():
    """清理所有bot进程"""
    for entry in bot_procs.values():
        proc = entry[0]
        proc.terminate()
        proc.wait()


def get_bot_file():
    """获取要使用的bot实现文件"""
    bot_implementation = os.getenv("BOT_IMPLEMENTATION", "openai").lower().strip()
    if not bot_implementation:
        bot_implementation = "openai"
    if bot_implementation not in ["openai", "gemini"]:
        raise ValueError(f"Invalid BOT_IMPLEMENTATION: {bot_implementation}")
    return f"bot-{bot_implementation}"


async def generate_agora_token(channel_name: str, uid: int) -> str:
    """生成Agora Token"""
    app_id = os.getenv("AGORA_APP_ID")
    app_certificate = os.getenv("AGORA_APP_CERTIFICATE")

    if not app_id or not app_certificate:
        raise HTTPException(
            status_code=500,
            detail="Missing Agora credentials in environment"
        )

    # Token expires in 1 hour
    expiration_time = int((datetime.now() + timedelta(hours=1)).timestamp())

    # token = RtcTokenBuilder.buildTokenWithUid(
    #     app_id,
    #     app_certificate,
    #     channel_name,
    #     uid,
    #     Role_Publisher,
    #     expiration_time
    # )

    return "token"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan manager for startup and shutdown"""
    yield
    cleanup()


# Initialize FastAPI app
app = FastAPI(lifespan=lifespan)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def start_agent(request: Request):
    """Browser access endpoint - creates room and starts bot"""
    try:
        # Generate a random room name if not provided
        room_id = os.urandom(8).hex()
        uid = 0  # Bot's UID
        token = await generate_agora_token(room_id, uid)

        # Check existing bots in room
        num_bots = sum(1 for proc in bot_procs.values()
                       if proc[1] == room_id and proc[0].poll() is None)

        if num_bots >= MAX_BOTS_PER_ROOM:
            raise HTTPException(
                status_code=500,
                detail=f"Max bot limit reached for room: {room_id}"
            )

        # Start bot process
        bot_file = get_bot_file()
        try:
            proc = subprocess.Popen(
                [
                    f"python3 -m {bot_file}",
                    f"-r {room_id}",
                    f"-u {uid}",
                    f"-t {token}"
                ],
                shell=True,
                bufsize=1,
                cwd=os.path.dirname(os.path.abspath(__file__)),
            )
            bot_procs[proc.pid] = (proc, room_id)
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to start bot: {e}"
            )

        # Return Agora room URL
        return JSONResponse({
            "room_id": room_id,
            "token": token,
            "uid": str(uid)
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/connect")
async def rtvi_connect(request: Request) -> Dict[Any, Any]:
    """RTVI client connection endpoint"""
    try:
        room_id = os.urandom(8).hex()
        uid = 0
        token = await generate_agora_token(room_id, uid)

        # Start the bot process
        bot_file = get_bot_file()
        try:
            proc = subprocess.Popen(
                [
                    f"python3 -m {bot_file}",
                    f"-r {room_id}",
                    f"-u {uid}",
                    f"-t {token}"
                ],
                shell=True,
                bufsize=1,
                cwd=os.path.dirname(os.path.abspath(__file__)),
            )
            bot_procs[proc.pid] = (proc, room_id)
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to start bot: {e}"
            )

        return {
            "room_id": room_id,
            "token": token,
            "uid": str(uid)
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/status/{pid}")
def get_status(pid: int):
    """获取特定bot进程的状态"""
    proc = bot_procs.get(pid)
    if not proc:
        raise HTTPException(
            status_code=404,
            detail=f"Bot process {pid} not found"
        )

    status = "running" if proc[0].poll() is None else "finished"
    return JSONResponse({
        "bot_id": pid,
        "status": status,
        "room_id": proc[1]
    })


if __name__ == "__main__":
    import uvicorn

    # 服务器配置
    default_host = os.getenv("HOST", "0.0.0.0")
    default_port = int(os.getenv("FAST_API_PORT", "7860"))

    parser = argparse.ArgumentParser(description="Agora Bot FastAPI server")
    parser.add_argument("--host", type=str, default=default_host)
    parser.add_argument("--port", type=int, default=default_port)
    parser.add_argument("--reload", action="store_true")

    args = parser.parse_args()

    # 启动服务器
    uvicorn.run(
        "server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )