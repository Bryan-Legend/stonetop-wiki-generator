"""
Sheets: the hand-authored fill-in pages, written in the corpus format.

``pages/<slug>.txt`` is a marker file like the ones under ``extracted/`` —
``TAG<tab>payload`` per line, bold and italic as ``<b>``/``<i>`` — using the
book markers for prose (``P``, ``H2``, ``H3``, ``B``, ``HR``) and the sheet
markers of ``text.py`` for the widgets: spinboxes, write-in boxes, check
lists with fixed ids, mark tracks, tables. A sheet written this way is a
list of short lines, which is what lets it be translated the way a book
page is (``generator/translate.py``): a translation is the same file with
the text sub-fields in another language, and every key, id and default is
a sub-field the translator never touches.

Ids and keys come from the **English** lines. ``render_sheet`` takes the
lines to show and, for a translation, ``id_lines`` — the English sheet,
aligned line for line — so a German heading keeps its English id and a
box ticked on the English page is ticked on the German one.

Inline links are written ``[[slug#fragment|label]]``; the renderer makes
the wiki link and the label is ordinary text.
"""

from __future__ import annotations

import html
import re

from .text import (
    M_B,
    M_CK,
    M_CKW,
    M_CKX,
    M_COL,
    M_DIV,
    M_DMG,
    M_ENDDIV,
    M_ENDLIST,
    M_ENDSHEET,
    M_ENDTABLE,
    M_EXTRACT,
    M_FIELD,
    M_FIELDS,
    M_GLOSS,
    M_H2,
    M_H3,
    M_HPMOUNT,
    M_HR,
    M_INV,
    M_INVW,
    M_ITEM,
    M_LI,
    M_LIST,
    M_NAMEHEAD,
    M_NOTE,
    M_NOTES,
    M_PLACE,
    M_ROW,
    M_SEP,
    M_SHEET,
    M_STAT,
    M_STATBLOCK,
    M_STATLINE,
    M_TABLE,
    M_TRACK,
    M_TYPE,
    _defmt,
    slugify_id,
)

_LINK_RE = re.compile(r"\[\[([a-z0-9-]+)(?:#([a-z0-9-]+))?\|([^\]]*)\]\]")


def _split(line: str, marker: str) -> list[str]:
    return line[len(marker):].split(M_SEP)


def _f(parts: list[str], i: int, default: str = "") -> str:
    return parts[i].strip() if i < len(parts) else default


def _attr(s: str) -> str:
    """Attribute text: no inline formatting, escaped."""
    return html.escape(_defmt(s).strip())


class SheetError(ValueError):
    pass


def render_sheet(
    lines: list[str],
    slug: str,
    link_fn,
    *,
    extract_fn=None,
    id_lines: list[str] | None = None,
) -> str:
    """Body HTML for a sheet.

    ``link_fn`` turns payload text into HTML (sentinels to tags, dice to
    buttons, text escaped) — ``linkify_pages`` bound to the page.
    ``extract_fn(from, to, blocks)`` supplies the HTML an ``EXTRACT`` line
    splices in. ``id_lines`` are the lines ids are made from (the English
    sheet when ``lines`` is a translation); they default to ``lines``.
    """
    ids = id_lines if id_lines is not None else lines
    if len(ids) != len(lines):
        raise SheetError(
            f"{slug}: sheet has {len(lines)} lines, its English {len(ids)}"
        )

    out: list[str] = []
    used_ids: set[str] = set()
    n = len(lines)
    i = 0

    def rich(text: str) -> str:
        """Payload text to HTML, with ``[[slug#frag|label]]`` links."""
        pieces: list[str] = []
        pos = 0
        for m in _LINK_RE.finditer(text):
            pieces.append(link_fn(text[pos:m.start()]))
            tslug, frag, label = m.group(1), m.group(2), m.group(3)
            href = f"{tslug}.html" + (f"#{frag}" if frag else "")
            if tslug == slug:
                href = f"#{frag}" if frag else f"{tslug}.html"
            fragment = f' data-fragment="{frag}"' if frag else ""
            pieces.append(
                f'<a class="wiki-link" href="{href}" data-slug="{tslug}"'
                f"{fragment}>{link_fn(label)}</a>"
            )
            pos = m.end()
        pieces.append(link_fn(text[pos:]))
        return "".join(pieces)

    def key(k: str) -> str:
        return html.escape(f"{slug}:{k}")

    def heading_id(text_for_id: str) -> str:
        base = slugify_id(_defmt(text_for_id).strip()) or "section"
        hid = base
        k = 2
        while hid in used_ids:
            hid = f"{base}-{k}"
            k += 1
        used_ids.add(hid)
        return hid

    def field_input(
        k: str, *, placeholder: str = "", aria: str = "", default: str = "",
        cls: str = "wiki-field",
    ) -> str:
        ph = f' placeholder="{_attr(placeholder)}"' if placeholder else ""
        lab = f' aria-label="{_attr(aria)}"' if aria else ""
        dflt = (
            f' data-default="{_attr(default)}" value="{_attr(default)}"'
            if default
            else ""
        )
        return (
            f'<input type="text" class="{cls}" data-field-key="{key(k)}"'
            f'{dflt}{ph}{lab} autocomplete="off">'
        )

    def checkbox(cid: str, *, aria: str = "") -> str:
        lab = f' aria-label="{_attr(aria)}"' if aria else ""
        return (
            f'<input type="checkbox" class="wiki-check" id="{html.escape(cid)}" '
            f'data-check-id="{html.escape(cid)}"{lab}>'
        )

    def spin(
        k: str, label: str, default: str, lo: str, hi: str, aria: str = ""
    ) -> str:
        sign = ' data-spin-sign="1"' if default[:1] in "+-" else ""
        return (
            f'<span class="pb-spin"><input type="text" class="wiki-field pb-track-box" '
            f'data-field-key="{key(k)}" data-default="{_attr(default)}" '
            f'value="{_attr(default)}" aria-label="{_attr(aria or label)}" '
            f'autocomplete="off" inputmode="numeric" role="spinbutton" '
            f'data-spin-min="{_attr(lo)}" data-spin-max="{_attr(hi)}"{sign} '
            f'aria-valuemin="{_attr(lo)}" aria-valuemax="{_attr(hi)}">'
            f'<span class="pb-spin-btns" aria-hidden="true">'
            f'<button type="button" class="pb-spin-step" data-step="1" '
            f'tabindex="-1">▴</button>'
            f'<button type="button" class="pb-spin-step" data-step="-1" '
            f'tabindex="-1">▾</button></span></span>'
        )

    # Runs of like lines that share one wrapper.
    open_run: str | None = None  # "stats" | "bullets" | None

    def close_run() -> None:
        nonlocal open_run
        if open_run == "stats":
            out.append("</div>")
        elif open_run == "bullets":
            out.append("</ul>")
        open_run = None

    def open_stats(count: int) -> None:
        nonlocal open_run
        if open_run != "stats":
            close_run()
            cls = "fs-stats fs-stats-6" if count >= 5 else "fs-stats"
            out.append(f'<div class="{cls}">')
            open_run = "stats"

    def run_length(start: int, markers: tuple[str, ...]) -> int:
        k = start
        while k < n and lines[k].startswith(markers):
            k += 1
        return k - start

    # LIST … ENDLIST state
    list_open = False
    table: dict | None = None

    while i < n:
        line = lines[i]
        idl = ids[i]

        if line.startswith(M_B):
            if open_run != "bullets":
                close_run()
                out.append('<ul class="bullets">')
                open_run = "bullets"
            out.append(f"<li>{rich(line[len(M_B):])}</li>")
            i += 1
            continue
        if line.startswith((M_STAT, M_DMG)):
            open_stats(run_length(i, (M_STAT, M_DMG)))
            p = _split(line, M_STAT if line.startswith(M_STAT) else M_DMG)
            if line.startswith(M_STAT):
                k, label, default, lo, hi = (
                    _f(p, 0), _f(p, 1), _f(p, 2), _f(p, 3), _f(p, 4)
                )
                gloss, aria = _f(p, 5), _f(p, 6)
                g = f'<span class="fs-muted">{rich(gloss)}</span>' if gloss else ""
                out.append(
                    f'<div class="pb-track">{spin(k, label, default, lo, hi, aria)}'
                    f'<span class="pb-track-name">{rich(label)}</span>{g}</div>'
                )
            else:
                k, label, default = _f(p, 0), _f(p, 1), _f(p, 2)
                title = _f(p, 3, "Roll damage")
                out.append(
                    f'<div class="pb-track">'
                    + field_input(
                        k, aria=label, default=default,
                        cls="wiki-field pb-track-box",
                    )
                    + f'<button type="button" class="pb-track-name pb-roll" '
                    f'data-roll-damage="{_attr(default)}" title="{_attr(title)}">'
                    f"{rich(label)}</button></div>"
                )
            i += 1
            continue
        close_run()

        if line.startswith(M_SHEET):
            p = _split(line, M_SHEET)
            kind, hp, label = _f(p, 0), _f(p, 1), _f(p, 2)
            extra = f' data-hp-key="{_attr(hp)}"' if hp else ""
            if label:
                extra += f' data-label="{_attr(label)}"'
            out.append(f'<div class="follower-sheet" data-sheet="{_attr(kind)}"{extra}>')
        elif line == M_ENDSHEET or line == M_ENDDIV:
            out.append("</div>")
        elif line.startswith(M_DIV):
            p = _split(line, M_DIV)
            cls, did = _f(p, 0), _f(p, 1)
            idattr = f' id="{html.escape(did)}"' if did else ""
            out.append(f'<div class="{_attr(cls)}"{idattr}>')
        elif line.startswith(M_H2) or line.startswith(M_H3):
            mk = M_H2 if line.startswith(M_H2) else M_H3
            tag = "h2" if mk == M_H2 else "h3"
            p = _split(line, mk)
            text, gloss = _f(p, 0), _f(p, 1)
            idp = _split(idl, mk)
            hid = heading_id(_f(idp, 2) or idp[0])
            g = f' <span class="fs-muted">{rich(gloss)}</span>' if gloss else ""
            out.append(f'<{tag} id="{html.escape(hid)}">{rich(text)}{g}</{tag}>')
        elif line.startswith(M_NOTE):
            out.append(f'<p class="fs-muted">{rich(line[len(M_NOTE):])}</p>')
        elif line.startswith(M_GLOSS):
            out.append(f'<p class="fs-gloss">{rich(line[len(M_GLOSS):])}</p>')
        elif line.startswith(M_STATLINE):
            out.append(f'<p class="stat-line">{rich(line[len(M_STATLINE):])}</p>')
        elif line == M_HR:
            out.append("<hr>")
        elif line == M_HPMOUNT:
            out.append('<div class="fs-hp-mount"></div>')
        elif line.startswith(M_FIELD):
            p = _split(line, M_FIELD)
            k, label, ph = _f(p, 0), _f(p, 1), _f(p, 2)
            lab = f'<span class="fs-label">{rich(label)}</span>' if label else ""
            out.append(
                f'<div class="fs-field">{lab}'
                + field_input(k, placeholder=ph, aria=ph or label)
                + "</div>"
            )
        elif line.startswith(M_FIELDS):
            p = _split(line, M_FIELDS)
            cls, label, prefix, count, ph = (
                _f(p, 0), _f(p, 1), _f(p, 2), _f(p, 3, "1"), _f(p, 4)
            )
            ltag = "ol" if cls == "fs-moves" else "ul"
            items = "".join(
                f"<li>{field_input(f'{prefix}{k}', placeholder=ph, aria=ph)}</li>"
                for k in range(int(count))
            )
            out.append(
                f'<div class="fs-field {_attr(cls)}">'
                f'<span class="fs-label">{rich(label)}</span>'
                f"<{ltag}>{items}</{ltag}></div>"
            )
        elif line.startswith(M_NOTES):
            p = _split(line, M_NOTES)
            k, label, rows = _f(p, 0), _f(p, 1), _f(p, 2, "3")
            aria = _f(p, 3) or label or "Notes"
            lab = f'<span class="fs-label">{rich(label)}</span>' if label else ""
            out.append(
                f'<div class="fs-field fs-notes">{lab}'
                f'<textarea class="wiki-field" data-field-key="{key(k)}" '
                f'rows="{_attr(rows)}" aria-label="{_attr(aria)}"></textarea></div>'
            )
        elif line.startswith(M_TRACK):
            p = _split(line, M_TRACK)
            tid, label, steps, word, cls = (
                _f(p, 0), _f(p, 1), _f(p, 2, "3"), _f(p, 3, "Mark"), _f(p, 4)
            )
            lab = f'<span class="track-label">{rich(label)}</span>' if label else ""
            marks = "".join(
                f'<label class="track-step" for="{html.escape(tid)}-{k}" '
                f'title="{_attr(word)} {k + 1}">'
                + checkbox(f"{tid}-{k}", aria=f"{_defmt(word)} {k + 1}")
                + "</label>"
                for k in range(int(steps))
            )
            extra = f" {_attr(cls)}" if cls else ""
            out.append(
                f'<div class="arcana-track{extra}" data-check-list="{html.escape(tid)}">'
                f'{lab}<span class="track-steps">{marks}</span></div>'
            )
        elif line.startswith(M_LIST):
            p = _split(line, M_LIST)
            cls, lid = _f(p, 0), _f(p, 1)
            classes = " ".join(c for c in ("check-list", cls) if c)
            if cls in ("fs-items", "fs-places", "fs-inventory"):
                classes = cls
            idattr = f' data-check-list="{html.escape(lid)}"' if lid else ""
            out.append(f'<ul class="{_attr(classes)}"{idattr}>')
            list_open = True
        elif line == M_ENDLIST:
            out.append("</ul>")
            list_open = False
        elif line.startswith(M_CK):
            p = _split(line, M_CK)
            cid, text = _f(p, 0), _f(p, 1)
            out.append(
                f'<li class="check-item"><label for="{html.escape(cid)}">'
                f"{checkbox(cid)} <span>{rich(text)}</span></label></li>"
            )
        elif line.startswith(M_CKX):
            text = line[len(M_CKX):]
            out.append(
                '<li class="check-item"><label><input type="checkbox" checked '
                f'disabled aria-label="Starts ticked"> <span>{rich(text)}</span>'
                "</label></li>"
            )
        elif line.startswith(M_CKW):
            p = _split(line, M_CKW)
            cid, k, ph = _f(p, 0), _f(p, 1), _f(p, 2)
            out.append(
                f'<li class="check-item"><label for="{html.escape(cid)}">'
                f"{checkbox(cid)}</label>"
                + field_input(k, placeholder=ph, aria=ph)
                + "</li>"
            )
        elif line.startswith(M_INV):
            p = _split(line, M_INV)
            iid, slots, text = _f(p, 0), _f(p, 1, "1"), _f(p, 2)
            boxes = "".join(
                checkbox(f"{iid}-{k}", aria="Slot") for k in range(int(slots))
            )
            out.append(
                f'<li class="inv-item"><span class="inv-slots">{boxes}</span>'
                f'<label for="{html.escape(iid)}-0"><span>{rich(text)}</span></label></li>'
            )
        elif line.startswith(M_INVW):
            p = _split(line, M_INVW)
            iid, k, ph, aria = _f(p, 0), _f(p, 1), _f(p, 2), _f(p, 3)
            out.append(
                f'<li class="inv-item"><span class="inv-slots">'
                f'{checkbox(f"{iid}-0", aria="Slot")}</span>'
                + field_input(k, placeholder=ph, aria=aria or ph)
                + "</li>"
            )
        elif line.startswith(M_ITEM):
            p = _split(line, M_ITEM)
            k, default, aria = _f(p, 0), _f(p, 1), _f(p, 2, "Entry")
            out.append(
                "<li>"
                + field_input(
                    k, default=default, placeholder="" if default else "…",
                    aria=aria,
                )
                + "</li>"
            )
        elif line.startswith(M_PLACE):
            p = _split(line, M_PLACE)
            letter, text, k = _f(p, 0), _f(p, 1), _f(p, 2)
            body = rich(text) if text else field_input(k)
            out.append(
                f'<li><span class="fs-badge">{_attr(letter)}</span>{body}</li>'
            )
        elif line.startswith(M_LI):
            out.append(f"<li>{rich(line[len(M_LI):])}</li>")
        elif line.startswith(M_TYPE):
            p = _split(line, M_TYPE)
            cid, name, k, examples = _f(p, 0), _f(p, 1), _f(p, 2), _f(p, 3)
            ex = ""
            if examples or k:
                inner = rich(examples)
                if k:
                    inner += (
                        (" " if examples else "")
                        + field_input(k, placeholder="…", aria="…")
                    )
                ex = f' <span class="fs-muted">({inner})</span>'
            out.append(
                f'<div class="fs-type-head"><label for="{html.escape(cid)}">'
                f"{checkbox(cid)} <strong>{rich(name)}</strong></label>{ex}</div>"
            )
        elif line.startswith(M_STATBLOCK):
            p = _split(line, M_STATBLOCK)
            name, tags = _f(p, 0), _f(p, 1)
            stats = "<br>".join(rich(x) for x in p[2:] if x.strip())
            hid = heading_id(_split(idl, M_STATBLOCK)[0])
            tag_html = f'<p class="stat-tags">{rich(tags)}</p>' if tags else ""
            out.append(
                f'<div class="stat-block follower" id="{html.escape(hid)}">'
                f'<h3 class="stat-name">{rich(name)}</h3>{tag_html}'
                f'<p class="stat-stats">{stats}</p></div>'
            )
        elif line.startswith(M_TABLE):
            p = _split(line, M_TABLE)
            table = {"prefix": _f(p, 0), "rows": int(_f(p, 1, "10")), "cols": [], "rows_set": []}
        elif line.startswith(M_COL):
            if table is None:
                raise SheetError(f"{slug}: COL outside a TABLE")
            p = _split(line, M_COL)
            table["cols"].append((_f(p, 0), _f(p, 1)))
        elif line.startswith(M_ROW):
            if table is None:
                raise SheetError(f"{slug}: ROW outside a TABLE")
            table["rows_set"].append(_split(line, M_ROW))
        elif line == M_ENDTABLE:
            if table is None:
                raise SheetError(f"{slug}: ENDTABLE without a TABLE")
            head = "".join(f"<th>{rich(h)}</th>" for _k, h in table["cols"])
            rows = []
            for r in range(table["rows"]):
                preset = table["rows_set"][r] if r < len(table["rows_set"]) else []
                cells = "".join(
                    "<td>"
                    + field_input(
                        f"{table['prefix']}{r}-{ck}", aria=_defmt(h),
                        default=_f(preset, c),
                    )
                    + "</td>"
                    for c, (ck, h) in enumerate(table["cols"])
                )
                rows.append(f"<tr>{cells}</tr>")
            out.append(
                '<div class="fs-table-wrap"><table class="fs-table">'
                f"<thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody>"
                "</table></div>"
            )
            table = None
        elif line.startswith(M_NAMEHEAD):
            p = _split(line, M_NAMEHEAD)
            cid, k, ph, aria_c, aria_n = (
                _f(p, 0), _f(p, 1), _f(p, 2), _f(p, 3), _f(p, 4)
            )
            out.append(
                f'<h3 class="si-title">{checkbox(cid, aria=aria_c)} '
                + field_input(k, placeholder=ph, aria=aria_n or ph, cls="wiki-field si-name")
                + "</h3>"
            )
        elif line.startswith(M_EXTRACT):
            p = _split(line, M_EXTRACT)
            if extract_fn is None:
                raise SheetError(f"{slug}: EXTRACT needs the book's extraction")
            out.append(extract_fn(_f(p, 0), _f(p, 1), _f(p, 2)))
        elif line.startswith("\x02"):
            raise SheetError(f"{slug}: line {i + 1}: marker not allowed on a sheet: {line[:16]!r}")
        else:
            out.append(f"<p>{rich(line)}</p>")
        i += 1
    close_run()
    if list_open:
        raise SheetError(f"{slug}: LIST never closed")
    return "\n".join(out)


def sheet_excerpt(lines: list[str]) -> str:
    """The first real paragraph of a sheet."""
    for line in lines:
        if line.startswith("\x02"):
            continue
        text = _LINK_RE.sub(lambda m: m.group(3), _defmt(line)).strip()
        if len(text) >= 40:
            return text if len(text) <= 320 else text[:319].rsplit(" ", 1)[0] + "…"
    return ""


__all__ = ["render_sheet", "sheet_excerpt", "SheetError"]
