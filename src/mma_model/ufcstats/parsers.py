"""Parse ufcstats.com HTML into structured records."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup


def _id_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    parts = path.split("/")
    return parts[-1] if parts else ""


def parse_date(text: str) -> Optional[datetime]:
    text = " ".join(text.split())
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


@dataclass
class EventRow:
    ufcstats_id: str
    name: str
    date: Optional[datetime]
    location: str
    url: str


def parse_completed_events(html: str) -> list[EventRow]:
    soup = BeautifulSoup(html, "lxml")
    rows: list[EventRow] = []
    for tr in soup.select("table.b-statistics__table-events tbody tr"):
        a = tr.select_one('a[href*="event-details"]')
        if not a or not a.get("href"):
            continue
        url = a["href"].strip()
        name = a.get_text(strip=True)
        sid = _id_from_url(url)
        if not sid:
            continue
        span = tr.select_one("span.b-statistics__date")
        dt = parse_date(span.get_text()) if span else None
        loc_td = tr.find_all("td")
        location = ""
        if len(loc_td) > 1:
            location = loc_td[1].get_text(strip=True)
        rows.append(EventRow(sid, name, dt, location, url))
    return rows


@dataclass
class EventFightRow:
    fight_id: str
    fight_url: str
    fighter_a_id: str
    fighter_a_name: str
    fighter_b_id: str
    fighter_b_name: str
    winner_id: Optional[str]
    weight_class: str
    method: str
    fight_round: Optional[int]
    time_str: Optional[str]


def parse_event_fights(html: str) -> list[EventFightRow]:
    soup = BeautifulSoup(html, "lxml")
    out: list[EventFightRow] = []
    for tr in soup.select("tr.b-fight-details__table-row.b-fight-details__table-row__hover"):
        link = tr.get("data-link") or ""
        if "fight-details" not in link:
            continue
        fight_id = _id_from_url(link)
        f_links = tr.select('a[href*="fighter-details"]')
        if len(f_links) < 2:
            continue
        fa = f_links[0]
        fb = f_links[1]
        fa_id = _id_from_url(fa["href"])
        fb_id = _id_from_url(fb["href"])
        win_flag = tr.select_one("a.b-flag_style_green")
        winner_id: Optional[str] = fa_id if win_flag else None
        wtxt = tr.select_one("td.l-page_align_left p.b-fight-details__table-text")
        wc = ""
        for p in tr.select("td.l-page_align_left p.b-fight-details__table-text"):
            t = p.get_text(" ", strip=True)
            if t and any(x in t.lower() for x in ("weight", "fly", "bantam", "feather", "light", "welter", "middle", "light heavy", "heavy", "women", "catch")):
                wc = t.replace("\n", " ").strip()
                break
        methods = [p.get_text(" ", strip=True) for p in tr.select("td.l-page_align_left p.b-fight-details__table-text")]
        method = ""
        for m in methods:
            if m and len(m) < 40 and any(
                k in m.upper() for k in ("DEC", "KO", "TKO", "SUB", "DQ", "NC", "M-DEC", "U-DEC", "S-DEC")
            ):
                method = m
                break
        rnd_el = tr.select("td.b-fight-details__table-col")[-2] if len(tr.select("td")) > 2 else None
        fight_round: Optional[int] = None
        time_str: Optional[str] = None
        cols = tr.select("td.b-fight-details__table-col")
        if len(cols) >= 2:
            rtext = cols[-2].get_text(strip=True)
            ttext = cols[-1].get_text(strip=True)
            if rtext.isdigit():
                fight_round = int(rtext)
            time_str = ttext or None
        out.append(
            EventFightRow(
                fight_id=fight_id,
                fight_url=link.strip(),
                fighter_a_id=fa_id,
                fighter_a_name=fa.get_text(strip=True),
                fighter_b_id=fb_id,
                fighter_b_name=fb.get_text(strip=True),
                winner_id=winner_id,
                weight_class=wc,
                method=method,
                fight_round=fight_round,
                time_str=time_str,
            )
        )
    return out


@dataclass
class FighterTotals:
    fighter_id: str
    name: str
    kd: int
    sig_str_landed: int
    sig_str_attempted: int
    sig_str_pct: Optional[float]
    total_str_landed: int
    total_str_attempted: int
    td_landed: int
    td_attempted: int
    td_pct: Optional[float]
    sub_att: int
    rev: int
    ctrl_seconds: int


def _parse_of_pattern(s: str) -> tuple[int, int]:
    s = s.strip()
    m = re.match(r"^(\d+)\s+of\s+(\d+)$", s)
    if not m:
        return 0, 0
    return int(m.group(1)), int(m.group(2))


def _parse_pct(s: str) -> Optional[float]:
    s = s.strip()
    if s in ("---", "", "–"):
        return None
    m = re.match(r"^(\d+)%$", s)
    if m:
        return int(m.group(1)) / 100.0
    return None


def _parse_ctrl(s: str) -> int:
    s = s.strip()
    if s in ("---", ""):
        return 0
    parts = s.split(":")
    if len(parts) == 2:
        try:
            m, sec = int(parts[0]), int(parts[1])
            return m * 60 + sec
        except ValueError:
            return 0
    return 0


def parse_fight_totals(html: str) -> list[FighterTotals]:
    soup = BeautifulSoup(html, "lxml")
    tables = soup.select("table")
    target = None
    for t in tables:
        ths = [x.get_text(strip=True) for x in t.select("thead th")]
        if ths and ths[0].lower().startswith("fighter") and "KD" in ths:
            target = t
            break
    if target is None:
        return []
    rows = target.select("tbody tr")
    if not rows:
        return []
    tr = rows[0]
    cells = tr.select("td")
    if len(cells) < 2:
        return []
    name_ps = cells[0].select("p.b-fight-details__table-text a")
    if len(name_ps) < 2:
        return []
    out: list[FighterTotals] = []
    col_vals: list[list[str]] = []
    for c in cells[1:]:
        texts = [p.get_text(strip=True) for p in c.select("p.b-fight-details__table-text")]
        col_vals.append(texts)
    n = 2
    for idx in range(2):
        nm = name_ps[idx].get_text(strip=True)
        fid = _id_from_url(name_ps[idx].get("href", ""))
        kd = int(col_vals[0][idx]) if len(col_vals[0]) > idx else 0
        sl, sa = _parse_of_pattern(col_vals[1][idx]) if len(col_vals) > 1 else (0, 0)
        sig_pct = _parse_pct(col_vals[2][idx]) if len(col_vals) > 2 else None
        tl, ta = _parse_of_pattern(col_vals[3][idx]) if len(col_vals) > 3 else (0, 0)
        tdl, tda = _parse_of_pattern(col_vals[4][idx]) if len(col_vals) > 4 else (0, 0)
        td_p = _parse_pct(col_vals[5][idx]) if len(col_vals) > 5 else None
        sub_att = int(col_vals[6][idx]) if len(col_vals) > 6 and col_vals[6][idx].isdigit() else 0
        rev = int(col_vals[7][idx]) if len(col_vals) > 7 and col_vals[7][idx].isdigit() else 0
        ctrl = _parse_ctrl(col_vals[8][idx]) if len(col_vals) > 8 else 0
        out.append(
            FighterTotals(
                fighter_id=fid,
                name=nm,
                kd=kd,
                sig_str_landed=sl,
                sig_str_attempted=sa,
                sig_str_pct=sig_pct,
                total_str_landed=tl,
                total_str_attempted=ta,
                td_landed=tdl,
                td_attempted=tda,
                td_pct=td_p,
                sub_att=sub_att,
                rev=rev,
                ctrl_seconds=ctrl,
            )
        )
    return out
