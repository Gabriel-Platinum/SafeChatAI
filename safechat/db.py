"""База данных: чаты, участники, история сообщений, инциденты."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import BigInteger, Integer, String, Text, delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from safechat import config


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Chat(Base):
    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    mediator_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Member(Base):
    __tablename__ = "members"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str] = mapped_column(Text, default="")  # сведения от медиатора
    message_count: Mapped[int] = mapped_column(default=0)
    incident_count: Mapped[int] = mapped_column(default=0)


class ChatMessage(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(255))
    reply_to_name: Mapped[str | None] = mapped_column(String(255))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    level: Mapped[float]
    participant_ids: Mapped[str] = mapped_column(Text, default="")  # user_id через запятую
    summary: Mapped[str] = mapped_column(Text, default="")
    dialog: Mapped[str] = mapped_column(Text, default="")  # переписка на момент инцидента
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    @property
    def participants(self) -> list[int]:
        return [int(x) for x in self.participant_ids.split(",") if x]


def _engine_options() -> dict:
    if not config.DATABASE_URL.startswith("postgresql+asyncpg"):
        return {}
    # Пулер Supabase (Supavisor/PgBouncer) в режиме transaction не поддерживает
    # подготовленные запросы asyncpg — отключаем их кэш и делаем имена уникальными.
    return {
        "pool_size": 3,
        "max_overflow": 2,
        "connect_args": {"statement_cache_size": 0,
                         "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__"},
    }


engine = create_async_engine(config.DATABASE_URL, pool_pre_ping=True, **_engine_options())
Session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if engine.dialect.name == "postgresql":
            # Supabase открывает таблицы через свой REST API. Включённый RLS без политик
            # закрывает к ним доступ снаружи; бот — владелец таблиц, на него RLS не действует.
            for table in Base.metadata.sorted_tables:
                await conn.execute(text(f'ALTER TABLE "{table.name}" ENABLE ROW LEVEL SECURITY'))


async def save_message(chat_id: int, chat_title: str, user_id: int, name: str, username: str | None,
                       reply_to_name: str | None, text: str) -> None:
    async with Session() as s, s.begin():
        chat = await s.get(Chat, chat_id)
        if chat is None:
            s.add(Chat(id=chat_id, title=chat_title))
        else:
            chat.title = chat_title

        member = await s.get(Member, (chat_id, user_id))
        if member is None:
            member = Member(chat_id=chat_id, user_id=user_id, name=name, username=username, notes="",
                            message_count=0, incident_count=0)
            s.add(member)
        member.name, member.username = name, username
        member.message_count += 1

        msg = ChatMessage(chat_id=chat_id, user_id=user_id, name=name, reply_to_name=reply_to_name, text=text)
        s.add(msg)
        await s.flush()

        # Храним только последние HISTORY_LIMIT сообщений чата; чистим время от времени.
        if msg.id % 50 == 0:
            cutoff = await s.scalar(
                select(ChatMessage.id).where(ChatMessage.chat_id == chat_id)
                .order_by(ChatMessage.id.desc()).offset(config.HISTORY_LIMIT).limit(1)
            )
            if cutoff:
                await s.execute(delete(ChatMessage).where(ChatMessage.chat_id == chat_id, ChatMessage.id <= cutoff))


async def recent_messages(chat_id: int, limit: int) -> list[ChatMessage]:
    async with Session() as s:
        rows = await s.scalars(
            select(ChatMessage).where(ChatMessage.chat_id == chat_id).order_by(ChatMessage.id.desc()).limit(limit)
        )
        return list(reversed(rows.all()))


async def get_chat(chat_id: int) -> Chat | None:
    async with Session() as s:
        return await s.get(Chat, chat_id)


async def set_mediator(chat_id: int, chat_title: str, user_id: int) -> None:
    async with Session() as s, s.begin():
        chat = await s.get(Chat, chat_id)
        if chat is None:
            s.add(Chat(id=chat_id, title=chat_title, mediator_id=user_id))
        else:
            chat.title, chat.mediator_id = chat_title, user_id


async def mediated_chats(user_id: int) -> list[Chat]:
    async with Session() as s:
        return list((await s.scalars(select(Chat).where(Chat.mediator_id == user_id))).all())


async def get_members(chat_id: int, user_ids: set[int] | None = None) -> list[Member]:
    async with Session() as s:
        query = select(Member).where(Member.chat_id == chat_id)
        if user_ids is not None:
            query = query.where(Member.user_id.in_(user_ids))
        return list((await s.scalars(query)).all())


async def add_note(chat_id: int, user_id: int, name: str, username: str | None, note: str) -> None:
    async with Session() as s, s.begin():
        member = await s.get(Member, (chat_id, user_id))
        if member is None:
            member = Member(chat_id=chat_id, user_id=user_id, name=name, username=username, notes="",
                            message_count=0, incident_count=0)
            s.add(member)
        member.notes = f"{member.notes}\n{note}".strip()


async def find_member_by_username(chat_ids: list[int], username: str) -> list[Member]:
    async with Session() as s:
        rows = await s.scalars(
            select(Member).where(Member.chat_id.in_(chat_ids), Member.username.ilike(username.lstrip("@")))
        )
        return list(rows.all())


async def create_incident(chat_id: int, level: float, participant_ids: list[int], summary: str, dialog: str) -> Incident:
    async with Session() as s, s.begin():
        incident = Incident(chat_id=chat_id, level=level, participant_ids=",".join(map(str, participant_ids)),
                            summary=summary, dialog=dialog)
        s.add(incident)
        for uid in participant_ids:
            member = await s.get(Member, (chat_id, uid))
            if member:
                member.incident_count += 1
    return incident


async def get_incident(incident_id: int) -> Incident | None:
    async with Session() as s:
        return await s.get(Incident, incident_id)


async def last_incident(chat_ids: list[int]) -> Incident | None:
    async with Session() as s:
        return await s.scalar(
            select(Incident).where(Incident.chat_id.in_(chat_ids)).order_by(Incident.id.desc()).limit(1)
        )


async def incidents_since(chat_id: int, days: int) -> list[Incident]:
    async with Session() as s:
        rows = await s.scalars(
            select(Incident).where(Incident.chat_id == chat_id, Incident.created_at >= utcnow() - timedelta(days=days))
        )
        return list(rows.all())
