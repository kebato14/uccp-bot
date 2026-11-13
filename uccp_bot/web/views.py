"""Разметка страниц панели. Обычный HTML без сборки и фреймворков."""
from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Tuple

from ..db.models import CostStatus, OrgStatus, Priority, Status, User
from ..utils import esc, fmt_dt, fmt_money

CSS = """
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
     color:#1c2024;background:#f4f6f8}
a{color:#2563eb;text-decoration:none}
a:hover{text-decoration:underline}
header{background:#1e293b;color:#fff;padding:12px 20px;display:flex;flex-wrap:wrap;
       gap:16px;align-items:center;position:sticky;top:0;z-index:5}
header .brand{font-weight:700;font-size:17px;margin-right:8px}
header nav{display:flex;gap:14px;flex-wrap:wrap}
header nav a{color:#cbd5e1;font-size:14px}
header nav a.active{color:#fff;font-weight:600}
header .me{margin-left:auto;font-size:13px;color:#94a3b8;text-align:right}
main{max-width:1180px;margin:0 auto;padding:20px}
h1{font-size:21px;margin:0 0 16px}
h2{font-size:16px;margin:24px 0 10px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;
       margin-bottom:20px}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px}
.card .n{font-size:26px;font-weight:700}
.card .l{font-size:13px;color:#64748b;margin-top:2px}
.card.warn .n{color:#dc2626}
.card.ok .n{color:#16a34a}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e2e8f0;
      border-radius:10px;overflow:hidden}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid #eef2f6;font-size:14px;
      vertical-align:top}
th{background:#f8fafc;font-size:12px;text-transform:uppercase;letter-spacing:.04em;
   color:#64748b}
tr:last-child td{border-bottom:none}
tr:hover td{background:#fafcff}
.filters{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px;
         margin-bottom:16px;display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end}
.filters label{font-size:12px;color:#64748b;display:block;margin-bottom:3px}
select,input{padding:7px 9px;border:1px solid #cbd5e1;border-radius:7px;font-size:14px;
             background:#fff;min-width:150px}
button{padding:8px 16px;border:0;border-radius:7px;background:#2563eb;color:#fff;
       font-size:14px;cursor:pointer}
button:hover{background:#1d4ed8}
.btn-light{background:#e2e8f0;color:#1e293b}
.tag{display:inline-block;padding:2px 8px;border-radius:20px;font-size:12px;
     background:#eef2f6;color:#475569;white-space:nowrap}
.tag.red{background:#fee2e2;color:#b91c1c}
.tag.green{background:#dcfce7;color:#15803d}
.tag.amber{background:#fef3c7;color:#b45309}
.tag.blue{background:#dbeafe;color:#1d4ed8}
.muted{color:#64748b;font-size:13px}
.pager{margin-top:14px;display:flex;gap:8px;align-items:center}
.row{display:grid;grid-template-columns:200px 1fr;gap:6px 16px;background:#fff;
     border:1px solid #e2e8f0;border-radius:10px;padding:16px}
.row dt{color:#64748b;font-size:13px}
.row dd{margin:0}
.timeline{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:14px}
.timeline .ev{padding:8px 0;border-bottom:1px solid #f1f5f9;font-size:14px}
.timeline .ev:last-child{border-bottom:none}
.login{max-width:380px;margin:12vh auto;background:#fff;border:1px solid #e2e8f0;
       border-radius:12px;padding:26px}
.login input{width:100%;font-size:22px;letter-spacing:6px;text-align:center}
.login button{width:100%;margin-top:12px;padding:11px}
.err{background:#fee2e2;color:#b91c1c;padding:9px 12px;border-radius:8px;margin-bottom:12px;
     font-size:14px}
.note{background:#eff6ff;color:#1e40af;padding:10px 12px;border-radius:8px;font-size:13px}
@media(max-width:700px){.row{grid-template-columns:1fr}
  table{display:block;overflow-x:auto}main{padding:12px}}
"""

STATUS_TAG = {
    Status.NEW: "blue", Status.AWAITING_ASSIGNMENT: "amber",
    Status.ACCEPTED: "blue", Status.IN_PROGRESS: "blue",
    Status.DONE: "amber", Status.CONFIRMED: "green", Status.CLOSED: "green",
    Status.REJECTED: "red", Status.CANCELLED: "red", Status.RETURNED: "amber",
    Status.RESCHEDULE_PROPOSED: "amber",
    OrgStatus.CLARIFY: "amber", OrgStatus.ASSIGNED: "blue",
}
PRIORITY_TAG = {Priority.CRITICAL: "red", Priority.HIGH: "red",
                Priority.MEDIUM: "amber", Priority.LOW: ""}


def page(title: str, body: str, user: Optional[User] = None, active: str = "") -> str:
    nav = ""
    if user is not None:
        items = [("/", "Сводка", "home"), ("/requests", "Заявки", "requests"),
                 ("/org", "Заявки в УЦЦП", "org"), ("/reports", "Отчёты", "reports")]
        if user.can_manage_users:
            items.append(("/users", "Пользователи", "users"))
        nav = "".join(
            f'<a href="{href}" class="{"active" if key == active else ""}">{esc(label)}</a>'
            for href, label, key in items
        )
        me = (
            f'<div class="me">{esc(user.full_name)}<br>{esc(user.role_title)} · '
            f'<a href="/logout" style="color:#94a3b8">выйти</a></div>'
        )
        header = (
            f'<header><div class="brand">УЦЦП</div><nav>{nav}</nav>{me}</header>'
        )
    else:
        header = ""
    return (
        "<!doctype html><html lang=ru><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>{esc(title)} · УЦЦП</title><style>{CSS}</style></head><body>"
        f"{header}<main>{body}</main></body></html>"
    )


def login_page(error: str = "") -> str:
    err = f'<div class="err">{esc(error)}</div>' if error else ""
    return page(
        "Вход",
        f'<div class="login"><h1>Панель УЦЦП</h1>{err}'
        '<p class="muted">Откройте бота в Telegram, отправьте команду '
        "<b>/panel</b> и введите полученный код.</p>"
        '<form method="post" action="/login">'
        '<input name="code" inputmode="numeric" maxlength="6" placeholder="000000" '
        'autocomplete="off" autofocus>'
        "<button type=submit>Войти</button></form>"
        '<p class="muted" style="margin-top:14px">Код действует 5 минут '
        "и используется один раз.</p></div>",
    )


def tag(text: str, kind: str = "") -> str:
    return f'<span class="tag {kind}">{esc(text)}</span>'


def stat_card(number, label: str, kind: str = "") -> str:
    return f'<div class="card {kind}"><div class="n">{number}</div><div class="l">{esc(label)}</div></div>'


def table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows
    )
    if not body:
        body = f'<tr><td colspan="{len(headers)}" class="muted">Ничего не найдено</td></tr>'
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def select_field(name: str, label: str, options: List[Tuple[str, str]], current: str) -> str:
    opts = "".join(
        f'<option value="{esc(value)}"{" selected" if value == current else ""}>'
        f"{esc(text)}</option>"
        for value, text in options
    )
    return f'<div><label>{esc(label)}</label><select name="{name}">{opts}</select></div>'


def request_row(req) -> List[str]:
    overdue = ' <span class="tag red">просрочена</span>' if req.is_overdue else ""
    return [
        f'<a href="/requests/{req.id}"><b>{esc(req.number)}</b></a>',
        esc(fmt_dt(req.created_at)),
        esc(req.outlet.name if req.outlet else "—"),
        esc(req.category.name if req.category else "—"),
        esc((req.description or "")[:70]),
        esc(req.executor.full_name if req.executor else "не назначен"),
        tag(Priority.title(req.priority), PRIORITY_TAG.get(req.priority, "")),
        tag(Status.title(req.status), STATUS_TAG.get(req.status, "")) + overdue,
        esc(fmt_dt(req.due_at)),
        esc(fmt_money(req.total_cost) if req.total_cost else "—"),
    ]


def definition_list(pairs: Sequence[Tuple[str, str]]) -> str:
    items = "".join(
        f"<dt>{esc(k)}</dt><dd>{v}</dd>" for k, v in pairs if v not in (None, "")
    )
    return f'<dl class="row">{items}</dl>'
