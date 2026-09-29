"""Read-only terminal presentation for the sniper's existing status feed."""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import logging
import math
import re
import sys
import threading
import time
from urllib.parse import urlsplit


def clean_text(value: object) -> str:
    """Keep provider/log text literal; hide URL credentials and query tokens."""
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value))
    text = " ".join("".join(c for c in text if c.isprintable() or c.isspace()).split())

    def redact(match: re.Match) -> str:
        try:
            url = urlsplit(match.group())
            return f"{url.scheme}://{url.hostname or 'endpoint'}/..."
        except ValueError:
            return "<endpoint>"

    return re.sub(r"https?://\S+", redact, text)


def seconds_since(value: object, now: datetime) -> float | None:
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (now - parsed).total_seconds()
    except (ValueError, TypeError):
        return None


def duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}"


def profit_cell(value: object, stale: bool = False):
    from rich.text import Text

    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return Text("--", style="dim")
    if amount and abs(amount) < Decimal("0.0001"):
        label = "+<0.0001" if amount > 0 else "-<0.0001"
    else:
        label = f"{amount:+.4f}" if amount else "0.0000"
    return Text(label, style="dim" if stale else "green" if amount > 0 else "red" if amount < 0 else "dim")


class EventHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.events = deque(maxlen=12)
        self.guard = threading.Lock()

    def emit(self, record):
        message = record.getMessage()
        # Quotes and their cooldowns already have persistent table rows.
        if message.split("|", 1)[0].strip() in {"CHECK", "RESULT", "PAUSE", "ROUTE"}:
            return
        stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        with self.guard:
            self.events.append((stamp, record.levelno, clean_text(message)))

    def latest(self):
        with self.guard:
            return list(self.events)[-2:]


def render_dashboard(snapshot: dict, *, width: int, height: int, elapsed: float = 0, events=(), now=None):
    """Build a height-bounded frame. Small terminals rotate pages every 8s."""
    from rich import box
    from rich.console import Group
    from rich.table import Table
    from rich.text import Text

    now = now or datetime.now(timezone.utc)
    session, summary = snapshot.get("session", {}), snapshot.get("summary", {})
    routes = list(snapshot.get("routes", {}).values())
    live = session.get("mode") == "live"
    header = Text(" ARBBOT  ", style="bold cyan")
    header.append("LIVE" if live else "DRY RUN", style="bold white on red" if live else "bold black on cyan")
    header.append(f"  {clean_text(session.get('status', 'running')).upper()}  |  {len(routes)} routes  |  {now.astimezone():%H:%M:%S}")
    uptime = seconds_since(session.get("started_at"), now)
    if uptime is not None:
        header.append(f"  |  up {duration(uptime)}", style="dim")
    counts = Text(
        f" Checks {summary.get('checks', 0)}   Ready {summary.get('ready', 0)}   "
        f"Confirmed {summary.get('confirmed', 0)}   Pending {summary.get('submitted', 0)}   "
        f"No trade {summary.get('no_trade', 0)}   Paused {summary.get('paused', 0)}   Errors {summary.get('errors', 0)}",
        style="dim", overflow="ellipsis", no_wrap=True,
    )
    side_by_side = width >= 136 and len({r['chain'] for r in routes}) > 1
    rows_per_page = max(1, height - 14)
    groups = (
        [[r for r in routes if r['chain'] == chain] for chain in dict.fromkeys(r['chain'] for r in routes)]
        if side_by_side else [routes]
    )
    pages = max(1, max((math.ceil(len(group) / rows_per_page) for group in groups), default=1))
    page = int(elapsed // 8) % pages

    def route_table(group, table_width):
        visible = group[page * rows_per_page:(page + 1) * rows_per_page]
        title = group[0]['chain'].upper() if side_by_side and group else "ALL ROUTES"
        table = Table(title=title, title_style="bold cyan", box=box.SIMPLE_HEAD, expand=True,
                      padding=(0, 1), show_edge=False, collapse_padding=True)
        if not side_by_side:
            table.add_column("Chain", width=3, no_wrap=True)
        table.add_column("Cycle*", min_width=10, no_wrap=True)
        table.add_column("First", width=6, no_wrap=True)
        table.add_column("Gross", justify="right", min_width=8, no_wrap=True)
        show_net = table_width >= 64
        if show_net:
            table.add_column("Net", justify="right", min_width=8, no_wrap=True)
        table.add_column("Status", min_width=10, no_wrap=True)
        table.add_column("Age", justify="right", width=5, no_wrap=True)
        styles = {"CHECKING": "cyan", "READY": "bold green", "CONFIRMED": "bold green",
                  "PAUSED": "yellow", "ERROR": "bold red", "FAILED": "bold red", "SUBMITTED": "bold magenta"}
        for row in visible:
            state = str(row.get("state", "WAITING"))
            checking = state == "CHECKING"
            status = "SCANNING" if checking else state
            remaining = seconds_since(row.get("cooldown_until"), now)
            if remaining is not None and remaining < 0 and not checking:
                status = f"PAUSE {duration(math.ceil(-remaining))}"
            age = seconds_since(row.get("checked_at"), now)
            dex = str(row.get("dex_name", "MetaMatcha"))
            first = ("Matcha" if dex == "MetaMatcha" else dex) if row['swap_order'] == 'dex-first' else "Stable"
            cells = [] if side_by_side else [Text("ETH" if row['chain'] == 'ethereum' else "SOL", style="dim")]
            cells.extend([Text(str(row['pair'])), Text(first, style="cyan" if first != "Stable" else "magenta"),
                          profit_cell(row.get('gross_profit'), checking)])
            if show_net:
                cells.append(profit_cell(row.get('net_profit'), checking))
            cells.extend([Text(status, style=styles.get(state, "dim")), Text(duration(age) if age is not None else "--", style="dim")])
            table.add_row(*cells)
        return table

    if side_by_side:
        tables = Table.grid(expand=True, padding=(0, 2))
        for _ in groups:
            tables.add_column(ratio=1)
        tables.add_row(*(route_table(group, (width - 4) // 2) for group in groups))
    else:
        tables = route_table(routes, width)
    legend = Text(" *USDC/USDG = USDC > USDG > USDC. First = first venue; then the other venue.", style="dim", no_wrap=True, overflow="ellipsis")
    units = Text(" Gross/net in the cycle's first token; dim numbers = previous quote while scanning; -- = unavailable.", style="dim", no_wrap=True, overflow="ellipsis")
    recent = snapshot.get("recent_results", [])
    notes = []
    # Rotate detailed results too, so clipped table states still have context.
    if recent:
        result = recent[int(elapsed // 5) % min(6, len(recent))]
        notes.append(Text(f" Result: {result['chain'].upper()} {result['pair']} "
                          f"({'DEX first' if result['swap_order'] == 'dex-first' else 'Stable first'}) | "
                          f"{clean_text(result.get('detail', ''))}", no_wrap=True, overflow="ellipsis"))
    else:
        notes.append(Text(" Waiting for first results...", style="dim"))
    for stamp, level, message in list(events)[-2:]:
        notes.append(Text(f" {stamp} {clean_text(message)}", style="red" if level >= logging.ERROR else "yellow" if level >= logging.WARNING else "dim", no_wrap=True, overflow="ellipsis"))
    footer = Text(f" Refresh 0.5s  |  {'All routes visible' if pages == 1 else f'Page {page + 1}/{pages} - rotates every 8s; enlarge window for more rows'}  |  Ctrl+C stop", style="dim", no_wrap=True, overflow="ellipsis")
    log_hint = Text(" Full details: logs/crosschain-sniper.log  |  --display log for scrolling output", style="dim", no_wrap=True, overflow="ellipsis")
    return Group(header, counts, tables, legend, units, *notes, footer, log_hint)


@contextmanager
def terminal_dashboard(feed, logger, mode="auto"):
    """Keep rendering isolated from execution, and restore handlers on exit."""
    if mode == "log" or (mode == "auto" and not sys.stdout.isatty()):
        yield
        return
    try:
        from rich.console import Console
        from rich.live import Live
    except ImportError:
        logger.warning("Table display needs rich: install requirements.txt; using scrolling logs.")
        yield
        return

    console = Console()
    if console.is_dumb_terminal:
        yield
        return
    events = EventHandler()
    loggers = [logger, logging.getLogger("matcha.cookies"), logging.getLogger("matcha.bridge")]
    removed = []
    for target in loggers:
        for handler in list(target.handlers):
            if type(handler) is logging.StreamHandler and handler.stream in (sys.stdout, sys.stderr):
                target.removeHandler(handler)
                removed.append((target, handler))
        target.addHandler(events)
    started = time.monotonic()

    def frame():
        try:
            return render_dashboard(feed.snapshot(), width=console.width, height=console.height,
                                    elapsed=time.monotonic() - started, events=events.latest())
        except Exception:
            from rich.text import Text
            return Text("Terminal display unavailable; full results remain in logs/crosschain-sniper.log", style="yellow")

    try:
        with Live(get_renderable=frame, console=console, refresh_per_second=2,
                  vertical_overflow="crop"):
            yield
    finally:
        for target in loggers:
            target.removeHandler(events)
        for target, handler in removed:
            target.addHandler(handler)
