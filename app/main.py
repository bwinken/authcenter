"""Auth Center - FastAPI application entry point."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app.database import sqlite_engine
from app.auth.routes import router as auth_router, init_templates


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: initialize SQLite tables
    async with sqlite_engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_accounts (
                staff_id      VARCHAR(50)  PRIMARY KEY,
                password_hash VARCHAR(255) NOT NULL,
                created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
                updated_at    DATETIME     DEFAULT CURRENT_TIMESTAMP
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS app_access_rules (
                id            INTEGER      PRIMARY KEY AUTOINCREMENT,
                app_id        VARCHAR(100) NOT NULL UNIQUE,
                allowed_depts TEXT         DEFAULT '[]',
                min_level     INTEGER      DEFAULT 1
            )
        """))
    yield
    # Shutdown: dispose engines
    await sqlite_engine.dispose()


app = FastAPI(
    title="Auth Center",
    description="Central SSO authentication service for internal AI applications.",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS - allow registered app origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict to known app origins in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Jinja2 templates
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))
init_templates(templates)

# Routes
app.include_router(auth_router)
