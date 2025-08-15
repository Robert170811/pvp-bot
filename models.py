# models.py
from datetime import datetime
from sqlalchemy import (
    String, Integer, BigInteger, DateTime, Boolean, ForeignKey, Enum, UniqueConstraint,
    func
)
from sqlalchemy.orm import declarative_base, Mapped, mapped_column, relationship
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import enum
import os

Base = declarative_base()

class Currency(enum.Enum):
    STARS = "stars"
    GIFTS = "gifts"

class MatchStatus(enum.Enum):
    OPEN = "open"
    LOCKED = "locked"
    RESOLVED = "resolved"
    CANCELED = "canceled"

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True, unique=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stars_balance: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    inventory_items: Mapped[list["InventoryItem"]] = relationship("InventoryItem", back_populates="user")
    bets: Mapped[list["Bet"]] = relationship("Bet", back_populates="user")

class Gift(Base):
    __tablename__ = "gifts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)  # например "ROSE"
    title: Mapped[str] = mapped_column(String(64))
    value_stars: Mapped[int] = mapped_column(Integer)  # цена подарка в звёздах (номинал)

class InventoryItem(Base):
    __tablename__ = "inventory_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    gift_id: Mapped[int] = mapped_column(ForeignKey("gifts.id"))
    qty: Mapped[int] = mapped_column(Integer, default=0)

    user: Mapped[User] = relationship("User", back_populates="inventory_items")
    gift: Mapped[Gift] = relationship("Gift")

    __table_args__ = (UniqueConstraint("user_id", "gift_id", name="uix_user_gift"),)

class Match(Base):
    __tablename__ = "matches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[MatchStatus] = mapped_column(Enum(MatchStatus), default=MatchStatus.OPEN)
    currency: Mapped[Currency] = mapped_column(Enum(Currency))  # STARS или GIFTS
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    winner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # для контроля ставок
    total_value_stars: Mapped[int] = mapped_column(Integer, default=0)  # pool в звёздах (включая gifts, конвертированные по номиналу)

    bets: Mapped[list["Bet"]] = relationship("Bet", back_populates="match", cascade="all, delete-orphan")

class Bet(Base):
    __tablename__ = "bets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    amount_stars: Mapped[int] = mapped_column(Integer, default=0)  # если звёздами
    # если подарками — храним JSON-подобно упрощённо: "ROSE:2,BOX:1"
    gifts_blob: Mapped[str | None] = mapped_column(String(512), nullable=True)
    value_stars: Mapped[int] = mapped_column(Integer, default=0)  # пересчитанное значение ставки в звёздах

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    user: Mapped[User] = relationship("User", back_populates="bets")
    match: Mapped[Match] = relationship("Match", back_populates="bets")
    __table_args__ = (UniqueConstraint("match_id", "user_id", name="uix_match_user"),)

def get_engine():
    db_url = os.getenv("DATABASE_URL", "sqlite:///pvp.sqlite3")
    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    return create_engine(db_url, echo=False, future=True, connect_args=connect_args)

engine = get_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

def init_db():
    Base.metadata.create_all(engine)
    # наполним базу базовыми подарками
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        existing = s.query(Gift).count()
        if existing == 0:
            s.add_all([
                Gift(code="ROSE", title="Роза", value_stars=5),
                Gift(code="COOKIE", title="Печенька", value_stars=10),
                Gift(code="BOX", title="Подарочная коробка", value_stars=25),
                Gift(code="STAR", title="Суперзвезда", value_stars=100),
            ])
            s.commit()
