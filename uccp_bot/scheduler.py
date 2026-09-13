"""Фоновые задачи: контроль просрочки (п.25) и автоматические сводки (п.40)."""
from __future__ import annotations

import datetime as dt
import logging
import os

from aiogram import Bot
from aiogram.types import FSInputFile
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import selectinload

from .config import config
from .db.models import Request, RoleCode, Status, User
from .services import backup as backup_service, excel, flow, gsheets, notify, reports
from .services.cards import REQUEST_LOAD_OPTIONS
from .utils import esc, fmt_dt, local_from_utc, now_local, utcnow

log = logging.getLogger(__name__)


async def check_overdue(session_maker: async_sessionmaker, bot: Bot) -> None:
    async with session_maker() as session:
        rows = (
            await session.scalars(
                select(Request)
                .where(
                    Request.status.in_(Status.OPEN),
                    Request.due_at.is_not(None),
                    Request.due_at < utcnow(),
                    Request.is_overdue.is_(False),
                )
                .options(*REQUEST_LOAD_OPTIONS)
            )
        ).all()
        for request in rows:
            try:
                await flow.mark_overdue(session, bot, request)
            except Exception as exc:  # не роняем планировщик из-за одной заявки
                log.exception("Ошибка при обработке просрочки %s: %s", request.number, exc)
        if rows:
            await session.commit()
            log.info("Отмечено просроченных заявок: %s", len(rows))


async def check_org_overdue(session_maker: async_sessionmaker, bot: Bot) -> None:
    """Просроченные организационные заявки УЦЦП."""
    from .db.models import OrgRequest, OrgStatus
    from .services import org

    async with session_maker() as session:
        rows = (
            await session.scalars(
                select(OrgRequest)
                .where(
                    OrgRequest.status.in_(OrgStatus.OPEN),
                    OrgRequest.due_at.is_not(None),
                    OrgRequest.due_at < utcnow(),
                    OrgRequest.is_overdue.is_(False),
                )
                .options(*org.ORG_LOAD_OPTIONS)
            )
        ).all()
        for request in rows:
            request.is_overdue = True
            request.overdue_notified_at = utcnow()
            await org.log(session, request, "overdue",
                          details=f"Срок: {fmt_dt(request.due_at)}")
            text = (
                f"🔴 <b>Организационная заявка просрочена</b>\n"
                f"{esc(request.number)} · {esc(request.object_name)}\n"
                f"Срок был: {fmt_dt(request.due_at)}\n\n"
                f"📝 {esc(request.description)}"
            )
            await notify.send_to_user(bot, request.assignee, text)
            await notify.send_to_user(bot, request.author, text)
            await notify.notify_admins(session, bot, text)
        if rows:
            await session.commit()
            log.info("Просроченных организационных заявок: %s", len(rows))


async def _recipients(session) -> list:
    return list(
        (
            await session.scalars(
                select(User).where(
                    User.role_code.in_((RoleCode.ADMIN, RoleCode.OPS_DIRECTOR)),
                    User.is_active.is_(True),
                    User.tg_id.is_not(None),
                )
            )
        ).all()
    )


async def morning_digest(session_maker: async_sessionmaker, bot: Bot) -> None:
    async with session_maker() as session:
        open_rows = (
            await session.scalars(
                select(Request)
                .where(Request.status.in_(Status.OPEN))
                .options(*REQUEST_LOAD_OPTIONS)
                .order_by(Request.due_at)
            )
        ).all()
        today = now_local().date()
        overdue = [r for r in open_rows if r.is_overdue]
        awaiting = [r for r in open_rows if r.status == Status.AWAITING_ASSIGNMENT]
        due_today = [
            r for r in open_rows
            if r.due_at and not r.is_overdue and local_from_utc(r.due_at).date() == today
        ]

        lines = [
            f"🌅 <b>Утренняя сводка УЦЦП</b> · {today.strftime('%d.%m.%Y')}",
            "",
            f"Всего в работе: <b>{len(open_rows)}</b>",
            f"Просрочено: <b>{len(overdue)}</b>",
            f"Ожидают назначения: <b>{len(awaiting)}</b>",
            f"Сегодня по сроку: <b>{len(due_today)}</b>",
        ]
        for title, rows in (("🔴 Просроченные", overdue), ("⏳ Без исполнителя", awaiting)):
            if rows:
                lines.append("")
                lines.append(f"<b>{title}</b>")
                for r in rows[:10]:
                    lines.append(
                        f"• {esc(r.number)} · {esc(r.outlet.name)} · {esc(r.category.name)} "
                        f"(срок {fmt_dt(r.due_at)})"
                    )

        text = "\n".join(lines)
        for user in await _recipients(session):
            await notify.send_to_user(bot, user, text)


async def evening_digest(session_maker: async_sessionmaker, bot: Bot) -> None:
    async with session_maker() as session:
        today = now_local().date()
        filters = reports.ReportFilters(
            date_from=today, date_to=today, period_title="за сегодня"
        )
        data = await reports.build_report(session, filters)
        text = "🌇 " + reports.render_report(data, filters)
        for user in await _recipients(session):
            await notify.send_to_user(bot, user, text)


async def monthly_google_report(
    session_maker: async_sessionmaker, bot: Bot, year: int = 0, month: int = 0
) -> None:
    """Ежемесячный отчёт УЦЦП в Google Sheets (по умолчанию — за прошедший месяц)."""
    if not year or not month:
        year, month = gsheets.previous_month()
    label = gsheets.month_title(year, month)

    async with session_maker() as session:
        recipients = await _recipients(session)
        requests = await gsheets.month_requests(session, year, month)

        if not gsheets.credentials_available():
            text = (
                f"📄 <b>Месячный отчёт УЦЦП за {label}</b>\n"
                f"Заявок за месяц: <b>{len(requests)}</b>\n\n"
                "⚠️ Google Sheets ещё не подключён — высылаю отчёт файлом Excel.\n"
                "Чтобы включить автоматическую выгрузку в таблицу, подложите ключ "
                "сервисного аккаунта (см. README, раздел «Google Sheets»)."
            )
            path = excel.export_requests(requests, title=label) if requests else None
            for user in recipients:
                await notify.send_to_user(bot, user, text)
                if path and user.tg_id:
                    try:
                        await bot.send_document(
                            user.tg_id, FSInputFile(path), caption=f"Отчёт УЦЦП за {label}"
                        )
                    except Exception as exc:
                        log.warning("Не удалось отправить Excel: %s", exc)
            return

        try:
            result = await gsheets.export_month(session, year, month)
        except Exception as exc:
            log.exception("Ошибка выгрузки в Google Sheets: %s", exc)
            text = (
                f"❌ Не удалось выгрузить отчёт за {label} в Google Sheets.\n"
                f"Причина: <code>{esc(str(exc)[:300])}</code>\n\n"
                "Отчёт доступен в разделе 📊 Отчёты и в выгрузке Excel."
            )
            for user in recipients:
                await notify.send_to_user(bot, user, text)
            return

        text = (
            f"📊 <b>Месячный отчёт УЦЦП за {label}</b> выгружен в Google Sheets.\n\n"
            f"🔧 Ремонтные — лист <b>{esc(result.worksheet)}</b>\n"
            f"• заявок: <b>{result.rows}</b> · расходы: <b>{result.total_cost}</b> "
            f"{esc(config.currency)}\n\n"
            f"🗂 Организационные УЦЦП — лист <b>{esc(result.org_worksheet)}</b>\n"
            f"• заявок: <b>{result.org_rows}</b> · затраты: <b>{result.org_cost}</b> "
            f"{esc(config.currency)}\n\n"
            f"{esc(result.url)}"
        )
        for user in recipients:
            await notify.send_to_user(bot, user, text)



async def daily_backup(session_maker: async_sessionmaker, bot: Bot) -> None:
    """Ежедневная резервная копия базы (п. «сохранность данных»)."""
    async with session_maker() as session:
        recipients = await _recipients(session)
        try:
            result = await backup_service.run_backup()
        except Exception as exc:
            log.exception("Резервное копирование не выполнено: %s", exc)
            text = (
                "❌ <b>Резервная копия базы не создана!</b>\n"
                f"Причина: <code>{esc(str(exc)[:300])}</code>\n\n"
                "Это важно: в базе все заявки и пользователи. "
                "Проверьте место на диске и права на папку."
            )
            for user in recipients:
                await notify.send_to_user(bot, user, text)
            return

        lines = [
            "💾 <b>Резервная копия базы создана</b>",
            f"Файл: <code>{esc(os.path.basename(result.path))}</code> · "
            f"{result.size_kb} КБ",
        ]
        if result.drive_url:
            lines.append(f"☁️ Google Диск: {esc(result.drive_url)}")
        elif result.drive_error:
            lines.append(f"⚠️ На Google Диск не ушла: {esc(result.drive_error)}")
        else:
            lines.append(
                "ℹ️ Копия на Google Диск не отправлена — доступ к Google не настроен."
            )
        text = "\n".join(lines)

        for user in recipients:
            if not user.tg_id:
                continue
            await notify.send_to_user(bot, user, text)
            if config.backup_to_telegram:
                try:
                    await bot.send_document(
                        user.tg_id,
                        FSInputFile(result.path),
                        caption="Копия базы УЦЦП. Сохраните — по ней можно "
                        "полностью восстановить бота.",
                    )
                except Exception as exc:
                    log.warning("Копию не удалось отправить в Telegram: %s", exc)


def setup_scheduler(session_maker: async_sessionmaker, bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=config.timezone)
    scheduler.add_job(
        check_overdue,
        IntervalTrigger(minutes=config.overdue_check_minutes),
        args=(session_maker, bot),
        id="check_overdue",
        replace_existing=True,
    )
    scheduler.add_job(
        check_org_overdue,
        IntervalTrigger(minutes=config.overdue_check_minutes),
        args=(session_maker, bot),
        id="check_org_overdue",
        replace_existing=True,
    )
    scheduler.add_job(
        daily_backup,
        CronTrigger(hour=config.backup_hour, minute=20),
        args=(session_maker, bot),
        id="daily_backup",
        replace_existing=True,
    )
    scheduler.add_job(
        morning_digest,
        CronTrigger(hour=8, minute=30),
        args=(session_maker, bot),
        id="morning_digest",
        replace_existing=True,
    )
    scheduler.add_job(
        evening_digest,
        CronTrigger(hour=19, minute=0),
        args=(session_maker, bot),
        id="evening_digest",
        replace_existing=True,
    )
    # Месячный отчёт в Google Sheets — за прошедший месяц (п. «Отчётность УЦЦП»)
    scheduler.add_job(
        monthly_google_report,
        CronTrigger(
            day=config.monthly_report_day,
            hour=config.monthly_report_hour,
            minute=10,
        ),
        args=(session_maker, bot),
        id="monthly_google_report",
        replace_existing=True,
    )
    return scheduler
