"""База данных: пользователи, чаты, участники, история сообщений, оценки, инциденты."""

import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import BigInteger, Integer, String, Text, delete, func, inspect, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from safechat import config

logger = logging.getLogger(__name__)

# Версия структуры базы. Меняется — значит нужна миграция существующих данных.
SCHEMA_VERSION = "2"
LEGACY_TABLES = ("incidents", "messages", "members", "chats")  # схема первой версии, без реальных данных


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class SchemaInfo(Base):
    __tablename__ = "schema_info"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[str] = mapped_column(String(64))


class User(Base):
    """Человек, который общался с ботом в личке или подключал чат."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(64))
    started: Mapped[bool] = mapped_column(default=False)  # нажимал «Запустить» — бот может ему писать
    state: Mapped[str | None] = mapped_column(String(64))  # какой ввод ждём в диалоге, например "purpose:<chat_id>"
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Chat(Base):
    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    active: Mapped[bool] = mapped_column(default=True)  # бот состоит в группе и наблюдает
    mediator_id: Mapped[int | None] = mapped_column(BigInteger)
    mediator_notified: Mapped[bool] = mapped_column(default=False)  # медиатор получил сообщение о подключении
    # Паспорт чата
    kind: Mapped[str | None] = mapped_column(String(32))
    purpose: Mapped[str] = mapped_column(Text, default="")
    norms: Mapped[str] = mapped_column(Text, default="")
    sensitivity: Mapped[str] = mapped_column(String(8), default="medium")
    # Служебное
    last_analyzed_id: Mapped[int] = mapped_column(Integer, default=0)  # последнее проанализированное сообщение
    connected_at: Mapped[datetime] = mapped_column(default=utcnow)
    removed_at: Mapped[datetime | None]


class Member(Base):
    __tablename__ = "members"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str] = mapped_column(Text, default="")  # сведения от медиатора
    message_count: Mapped[int] = mapped_column(default=0)
    incident_count: Mapped[int] = mapped_column(default=0)
    first_seen: Mapped[datetime] = mapped_column(default=utcnow)


class ChatMessage(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)  # для ссылок на сообщение и правок
    user_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str] = mapped_column(String(255))
    reply_to_name: Mapped[str | None] = mapped_column(String(255))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Screening(Base):
    """Каждая оценка напряжения — основа для «климата» чата в статистике."""
    __tablename__ = "screenings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    tension: Mapped[float]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    level: Mapped[float]
    status: Mapped[str] = mapped_column(String(16), default="new")
    participant_ids: Mapped[str] = mapped_column(Text, default="")  # user_id через запятую
    summary: Mapped[str] = mapped_column(Text, default="")
    dialog: Mapped[str] = mapped_column(Text, default="")  # переписка на момент инцидента
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    @property
    def participants(self) -> list[int]:
        return [int(x) for x in self.participant_ids.split(",") if x]


CHAT_ID_TABLES = (Member, ChatMessage, Screening, Incident)


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
        tables = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
        if "schema_info" not in tables:
            legacy = [t for t in LEGACY_TABLES if t in tables]
            if legacy:
                logger.warning("Удаляю таблицы старой схемы: %s", ", ".join(legacy))
                for table in legacy:
                    await conn.execute(text(f'DROP TABLE "{table}"'))
            await conn.run_sync(Base.metadata.create_all)
            await conn.execute(SchemaInfo.__table__.insert().values(key="version", value=SCHEMA_VERSION))
        else:
            version = await conn.scalar(select(SchemaInfo.value).where(SchemaInfo.key == "version"))
            if version != SCHEMA_VERSION:
                raise RuntimeError(f"Схема базы v{version}, а код ждёт v{SCHEMA_VERSION}: нужна миграция данных")
            await conn.run_sync(Base.metadata.create_all)  # досоздаёт новые таблицы, если появились

        if engine.dialect.name == "postgresql":
            # Supabase открывает таблицы через свой REST API. Включённый RLS без политик
            # закрывает к ним доступ снаружи; бот — владелец таблиц, на него RLS не действует.
            for table in Base.metadata.sorted_tables:
                await conn.execute(text(f'ALTER TABLE "{table.name}" ENABLE ROW LEVEL SECURITY'))


# ---------- Пользователи ----------

async def upsert_user(user_id: int, name: str, username: str | None, started: bool = False) -> User:
    async with Session() as s, s.begin():
        user = await s.get(User, user_id)
        if user is None:
            user = User(id=user_id, name=name, username=username, started=started)
            s.add(user)
        else:
            user.name, user.username = name, username
            user.started = user.started or started
    return user


async def get_user(user_id: int) -> User | None:
    async with Session() as s:
        return await s.get(User, user_id)


async def set_user_state(user_id: int, state: str | None) -> None:
    async with Session() as s, s.begin():
        await s.execute(update(User).where(User.id == user_id).values(state=state))


# ---------- Чаты ----------

async def get_chat(chat_id: int) -> Chat | None:
    async with Session() as s:
        return await s.get(Chat, chat_id)


async def connect_chat(chat_id: int, title: str, mediator_id: int) -> Chat:
    """Подключает чат (или подключает заново) с указанным медиатором."""
    async with Session() as s, s.begin():
        chat = await s.get(Chat, chat_id)
        if chat is None:
            chat = Chat(id=chat_id, title=title, sensitivity="medium", purpose="", norms="", last_analyzed_id=0)
            s.add(chat)
        chat.title, chat.active, chat.removed_at = title, True, None
        if chat.mediator_id != mediator_id:
            chat.mediator_id, chat.mediator_notified = mediator_id, False
        chat.connected_at = utcnow()
    return chat


async def disconnect_chat(chat_id: int) -> Chat | None:
    async with Session() as s, s.begin():
        chat = await s.get(Chat, chat_id)
        if chat and chat.active:
            chat.active, chat.removed_at = False, utcnow()
            return chat
    return None


async def update_chat(chat_id: int, **fields) -> None:
    async with Session() as s, s.begin():
        await s.execute(update(Chat).where(Chat.id == chat_id).values(**fields))


async def mediated_chats(user_id: int) -> list[Chat]:
    async with Session() as s:
        rows = await s.scalars(
            select(Chat).where(Chat.mediator_id == user_id, Chat.active.is_(True)).order_by(Chat.connected_at)
        )
        return list(rows.all())


async def unnotified_chats(user_id: int) -> list[Chat]:
    async with Session() as s:
        rows = await s.scalars(select(Chat).where(
            Chat.mediator_id == user_id, Chat.active.is_(True), Chat.mediator_notified.is_(False)))
        return list(rows.all())


async def migrate_chat(old_id: int, new_id: int) -> None:
    """Группа стала супергруппой: у неё новый ID, переносим все данные."""
    async with Session() as s, s.begin():
        if await s.get(Chat, old_id) is None:
            return
        stray = await s.get(Chat, new_id)
        if stray is not None:
            await s.delete(stray)
            await s.flush()
        await s.execute(update(Chat).where(Chat.id == old_id).values(id=new_id))
        for model in CHAT_ID_TABLES:
            await s.execute(update(model).where(model.chat_id == old_id).values(chat_id=new_id))
    logger.info("Чат %s перенесён на новый ID %s", old_id, new_id)


async def chat_counters(chat_id: int) -> tuple[int, int]:
    """(сколько сообщений проанализировано всего, конфликтов за 30 дней)."""
    async with Session() as s:
        messages = await s.scalar(select(func.coalesce(func.sum(Member.message_count), 0))
                                  .where(Member.chat_id == chat_id))
        incidents = await s.scalar(select(func.count(Incident.id)).where(
            Incident.chat_id == chat_id, Incident.created_at >= utcnow() - timedelta(days=30)))
        return int(messages or 0), int(incidents or 0)


# ---------- Участники и сообщения ----------

async def touch_member(chat_id: int, user_id: int, name: str, username: str | None) -> None:
    """Запоминает участника (например, когда он вошёл в чат)."""
    async with Session() as s, s.begin():
        member = await s.get(Member, (chat_id, user_id))
        if member is None:
            s.add(Member(chat_id=chat_id, user_id=user_id, name=name, username=username, notes="",
                         message_count=0, incident_count=0))
        else:
            member.name, member.username = name, username


async def save_message(chat_id: int, chat_title: str, tg_message_id: int, user_id: int, name: str,
                       username: str | None, reply_to_name: str | None, text: str) -> ChatMessage | None:
    """Сохраняет сообщение подключённого чата. Для неподключённых чатов возвращает None."""
    async with Session() as s, s.begin():
        chat = await s.get(Chat, chat_id)
        if chat is None or not chat.active:
            return None
        if chat_title and chat.title != chat_title:
            chat.title = chat_title  # группу переименовали

        member = await s.get(Member, (chat_id, user_id))
        if member is None:
            member = Member(chat_id=chat_id, user_id=user_id, name=name, username=username, notes="",
                            message_count=0, incident_count=0)
            s.add(member)
        member.name, member.username = name, username
        member.message_count += 1

        msg = ChatMessage(chat_id=chat_id, tg_message_id=tg_message_id, user_id=user_id, name=name,
                          reply_to_name=reply_to_name, text=text)
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
    return msg


async def update_message_text(chat_id: int, tg_message_id: int, text: str) -> None:
    async with Session() as s, s.begin():
        await s.execute(update(ChatMessage).where(
            ChatMessage.chat_id == chat_id, ChatMessage.tg_message_id == tg_message_id).values(text=text))


async def recent_messages(chat_id: int, limit: int) -> list[ChatMessage]:
    async with Session() as s:
        rows = await s.scalars(
            select(ChatMessage).where(ChatMessage.chat_id == chat_id).order_by(ChatMessage.id.desc()).limit(limit)
        )
        return list(reversed(rows.all()))


async def chats_with_unanalyzed() -> list[int]:
    """Чаты, где есть сообщения новее последнего анализа (например, бот заснул посреди работы)."""
    async with Session() as s:
        rows = await s.scalars(
            select(Chat.id).where(Chat.active.is_(True)).where(
                select(ChatMessage.id).where(ChatMessage.chat_id == Chat.id,
                                             ChatMessage.id > Chat.last_analyzed_id).exists())
        )
        return list(rows.all())


async def add_screening(chat_id: int, tension: float, last_message_id: int) -> None:
    async with Session() as s, s.begin():
        s.add(Screening(chat_id=chat_id, tension=tension))
        await s.execute(update(Chat).where(Chat.id == chat_id).values(last_analyzed_id=last_message_id))


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


# ---------- Инциденты ----------

async def create_incident(chat_id: int, level: float, participant_ids: list[int], summary: str, dialog: str) -> Incident:
    async with Session() as s, s.begin():
        incident = Incident(chat_id=chat_id, level=level, status="new",
                            participant_ids=",".join(map(str, participant_ids)), summary=summary, dialog=dialog)
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
