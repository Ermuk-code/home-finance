import asyncio
import logging
import uvicorn
from fastapi import FastAPI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Домашний сервис контроля денежных средств")


@app.get("/health")
async def health_check():
    return {"status": "ok"}


async def main():
    logger.info("Запуск сервиса...")
    config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())