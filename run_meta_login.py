import asyncio
from app.services.video import meta_bot

if __name__ == '__main__':
    asyncio.run(meta_bot.setup_meta_login())
