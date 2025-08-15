# config.py
import os

class Config:
    BOT_TOKEN: str
    ADMIN_USER_ID: int
    DATABASE_URL: str
    APP_SECRET: str
    WEBAPP_URL: str

def get_config() -> Config:
    c = Config()
    c.BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    c.ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
    c.DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///pvp.sqlite3")
    c.APP_SECRET = os.getenv("APP_SECRET", "change_me")
    c.WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:5000")
    return c
