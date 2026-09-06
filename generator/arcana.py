"""
Marker lines → arcana card HTML (minor and major), built on ``structure``.
"""

from __future__ import annotations

import html
import re

from .structure import (
    AnchorRegistry,
    book_icon_img_html,
    dice_button,
    linkify_pages,
    render_check_list,
    render_mark_track,
    render_stat_block,
)
from .text import (
    M_FACE,
    BOLD_PREFIX_RE,
    DICE_RE,
    HP_LINE_RE,
    MARKER_RE,
    M_B,
    M_B2,
    M_C,
    M_E,
    M_H3,
    M_ICON,
    M_MARK,
    M_Q,
    _ARCANA_TAGS,
    _defmt,
    _is_all_caps_label,
    _is_pure_arcana_tag_line,
    looks_like_tag_line,
    normalize_section_key,
    slugify_id,
    strip_markers,
    titlecase_label,
    titlecase_name,
)


def _split_discovery_and_tags(line: str) -> tuple[str, str]:
    """'An old scroll case, fragile' → ('An old scroll case', 'fragile')."""
    L = line.strip().lstrip(",").strip()
    if not L:
        return "", ""
    # Whole line is tags
    if _is_pure_arcana_tag_line(L):
        parts = [p.strip() for p in re.split(r"[,;]", L) if p.strip()]
        return "", ", ".join(parts)
    # Trailing tags after comma
    tag_alt = "|".join(
        re.escape(t) for t in sorted(_ARCANA_TAGS, key=len, reverse=True)
    )
    m = re.match(
        r"^(.+?)[,\s]+((?:(?:" + tag_alt + r")[\s,]*)+)$",
        L,
        re.I,
    )
    if m:
        return m.group(1).strip(" ,"), re.sub(r"\s+", " ", m.group(2).strip(" ,"))
    # Space-separated trailing tags only (no following prose):
    # "A giant's dormitory magical"
    m2 = re.match(
        r"^(.+?)\s+(" + tag_alt + r"(?:\s*,\s*(?:" + tag_alt + r"))*)\s*$",
        L,
        re.I,
    )
    if m2 and len(m2.group(1).split()) <= 8:
        disc = m2.group(1).strip(" ,")
        # Don't strip tags off a sentence that merely ends with a tag word by chance
        if not disc.endswith((".", "!", "?", "…")):
            return disc, re.sub(r"\s+", " ", m2.group(2).strip(" ,"))
    return L, ""


def _peel_discovery_tag_prose(line: str) -> tuple[str, str, str]:
    """
    Split a mashed discovery line into (title, tags, prose).
    'A giant\\'s dormitory magical In a ruin…' →
    (\"A giant's dormitory\", 'magical', 'In a ruin…')
    """
    L = line.strip()
    if not L:
        return "", "", ""
    tag_alt = "|".join(
        re.escape(t) for t in sorted(_ARCANA_TAGS, key=len, reverse=True)
    )
    m = re.match(
        r"^(.{{2,50}}?)\s+({tags}(?:\s*,\s*(?:{tags}))*)\s+([A-Z].+)$".format(
            tags=tag_alt
        ),
        L,
        re.I,
    )
    if m and len(m.group(1).split()) <= 6:
        disc = m.group(1).strip(" ,")
        if not disc.endswith((".", "!", "?", "…")):
            return (
                disc,
                re.sub(r"\s+", " ", m.group(2).strip(" ,")),
                m.group(3).strip(),
            )
    disc, tags = _split_discovery_and_tags(L)
    return disc, tags, ""


def _is_unlock_intro(line: str) -> bool:
    low = line.lower()
    if re.search(
        r"(you can learn|you can unlock|to unlock|to learn|the manual reveals|"
        r"there is magic here|the pictograms|the notes reveal|to unlock the|"
        r"need one of the following|need one of these|you must\b|"
        r"but to learn|but you must|but need|but…|but\.\.\.)",
        low,
    ):
        return True
    if "but" in low and (
        "learn" in low or "unlock" in low or "must" in low or "secret" in low
    ):
        return True
    return False


def _is_unlock_item(line: str) -> bool:
    L = line.strip()
    if not L:
        return False
    if L.startswith(("…", "...", "•", "·")):
        return True
    # Bare requirement after "need one of the following"
    if len(L) < 160 and L[0:1].isupper() and not re.match(r"^When you\b", L, re.I):
        if re.match(
            r"^(A |An |The |Some |Risk |Spend |Get |Acquire |Dig |"
            r"Translate |Decipher |Study |Meditate |First )",
            L,
        ):
            return True
    return False


def _is_unlock_divider(line: str) -> bool:
    L = line.strip()
    low = L.lower()
    if re.match(r"^or\.+$", low) or low in {"or…", "or...", "or"}:
        return True
    if re.match(r"^and then\b", low):
        return True
    if re.match(r"^and either\b", low):
        return True
    if re.match(r"^either\b", low) and len(L) < 40:
        return True
    return False


def _is_tag_token(tok: str) -> bool:
    t = tok.lower().strip(",+;")
    if t in _ARCANA_TAGS:
        return True
    if re.match(r"^\+?\d+$", t):
        return True
    if t in {"piercing", "damage", "armor", "uses", "weight"}:
        return True
    # "+1" already; "1d4" not a tag
    return False


def _strip_power_line_tags(line: str) -> tuple[str, str]:
    """
    'The Broom's Lullaby magical, area, near' →
    (\"The Broom's Lullaby\", 'magical, area, near')
    """
    L = line.strip().lstrip(",").strip()
    if not L:
        return "", ""
    # Leading comma-tags only
    if L.startswith(",") or (looks_like_tag_line(L) and not HP_LINE_RE.search(L)):
        return "", L.lstrip(", ").strip()
    # Try split discovery/tags helper (works for trailing comma-tags)
    name, tags = _split_discovery_and_tags(L)
    if tags:
        return name, tags
    # Space-separated trailing tags: "Name magical, area, near" or "Name magical area near"
    # Normalize commas to spaces for token walk
    rough = re.sub(r"[,;]+", " ", L)
    words = rough.split()
    tag_i = len(words)
    while tag_i > 1 and _is_tag_token(words[tag_i - 1]):
        tag_i -= 1
    if tag_i < len(words) and tag_i >= 1:
        # Rebuild tags from original trailing portion when possible
        name = " ".join(words[:tag_i])
        tag_str = ", ".join(words[tag_i:])
        return name, tag_str
    return L, ""


def _line_matches_power_title(line: str, power_norm: str) -> bool:
    if not power_norm or not line:
        return False
    name, _tags = _strip_power_line_tags(line)
    ln = normalize_section_key(name)
    if not ln:
        return False
    if ln == power_norm:
        return True
    # Allow title without leading "The "
    if power_norm.startswith("the ") and ln == power_norm[4:]:
        return True
    if ln.startswith("the ") and power_norm == ln[4:]:
        return True
    return False


def _find_power_title_index(lines: list[str], article_title: str) -> int | None:
    """Index where the power name (back of card) begins."""
    power = (article_title or "").strip()
    power_norm = normalize_section_key(power)
    if power_norm:
        for i, line in enumerate(lines):
            if re.match(r"^When you\b", line, re.I):
                continue
            if line.strip().startswith(("…", "...", "•", "·")):
                continue
            if _is_unlock_intro(line) and not _line_matches_power_title(line, power_norm):
                continue
            # Title alone, or title + tags on one line
            if _line_matches_power_title(line, power_norm):
                # Prefer later occurrence (back of card) over mention in unlock prose
                # Only accept if line is short-ish or starts with the title
                name, _ = _strip_power_line_tags(line)
                if len(line) <= len(power) + 40 or line.lower().startswith(
                    name[:12].lower()
                ):
                    # Skip if this is long unlock prose containing the name
                    if len(line) > len(power) + 50 and not re.match(
                        r"^" + re.escape(name), line, re.I
                    ):
                        continue
                    return i
            if i + 1 < len(lines) and len(lines[i + 1]) < 48:
                joined = line + " " + lines[i + 1]
                if _line_matches_power_title(joined, power_norm):
                    return i
            # Multi-line title fragments
            name, _ = _strip_power_line_tags(line)
            ln = normalize_section_key(name)
            if (
                ln
                and len(ln) >= 4
                and power_norm.startswith(ln + " ")
                and i + 1 < len(lines)
                and not re.match(r"^When you\b", lines[i + 1], re.I)
                and len(lines[i + 1]) < 40
            ):
                jn = normalize_section_key(name + " " + lines[i + 1])
                if jn == power_norm:
                    return i

    # Fallback: line immediately before first post-unlock "When you"
    for i, line in enumerate(lines):
        if not re.match(r"^When you\b", line, re.I) or i == 0:
            continue
        for j in range(i - 1, max(-1, i - 5), -1):
            L = lines[j].strip()
            if not L or L.startswith(("…", "...")):
                continue
            if _is_unlock_intro(L):
                continue
            if _is_unlock_item(L) and L.startswith(("…", "...")):
                continue
            name, _ = _strip_power_line_tags(L)
            if len(name.split()) > 8:
                continue
            ln = normalize_section_key(name)
            if power_norm and (
                ln == power_norm
                or power_norm.startswith(ln)
                or ln in power_norm
            ):
                return j
            if not power_norm and len(name.split()) <= 5:
                return j
        # No title line — power starts at this When you
        return i
    return None


def _merge_orphan_bullets(lines: list[str]) -> list[str]:
    """'•' / 'Consume…' → '• Consume…'; join mid-bullet wraps."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        L = lines[i].strip()
        if L in {"•", "·", "-", "–"} and i + 1 < len(lines):
            nxt = lines[i + 1].strip().lstrip("•· ").strip()
            if nxt:
                # Cost/HP/etc. are stats, not bullet bodies
                if re.match(r"^(Cost|HP|Armor|Damage|Instinct|Loyalty)\b", nxt, re.I):
                    out.append(nxt)
                    i += 2
                    continue
                # Continue previous fragment ending with 'and' / 'or'
                if out and re.search(
                    r"\b(and|or|to|the|a|an|with|,)\s*$", out[-1].rstrip(), re.I
                ):
                    out[-1] = out[-1].rstrip() + " " + nxt
                else:
                    out.append("• " + nxt)
                i += 2
                continue
        if L.startswith("•") and len(L.lstrip("•· ").strip()) == 0:
            i += 1
            continue
        # Bullet line that continues previous incomplete bullet/prose
        if L.startswith(("•", "·")):
            body = L.lstrip("•· ").strip()
            if out and re.search(
                r"\b(and|or|to|the|a|an|with|,)\s*$", out[-1].rstrip(), re.I
            ):
                out[-1] = out[-1].rstrip() + " " + body
                i += 1
                continue
            out.append("• " + body)
            i += 1
            continue
        # Mid-wrap without bullet marker
        if (
            out
            and (
                out[-1].startswith("•")
                or re.search(r"\b(and|or)\s*$", out[-1].rstrip(), re.I)
            )
            and (
                re.search(r"\b(and|or|to|the|a|an|with|,)\s*$", out[-1].rstrip(), re.I)
                or out[-1].count("(") > out[-1].count(")")
            )
            and L
            and not re.match(r"^When you\b", L, re.I)
            and not HP_LINE_RE.search(L)
            and not re.match(r"^(Cost|HP|Armor|Damage|Instinct)\b", L, re.I)
        ):
            out[-1] = out[-1].rstrip() + " " + L
            i += 1
            continue
        out.append(lines[i])
        i += 1
    return out


def _is_track_line(line: str) -> bool:
    """Blaze: 4nil, 1d4, 1d6… or similar stepped tracks."""
    return bool(
        re.match(
            r"^[A-Za-z][A-Za-z\s]{0,20}:\s*.*\b(nil|\d*d\d+)\b",
            line.strip(),
            re.I,
        )
    )


def _render_track_line(line: str, link_fn) -> str:
    """Render 'Blaze: 4nil, 1d4, 1d6, 1d8, 1d10' as a stepped track."""
    m = re.match(r"^([^:]+):\s*(.+)$", line.strip())
    if not m:
        return f'<p class="arcana-track">{link_fn(line)}</p>'
    label, rest = m.group(1).strip(), m.group(2).strip()
    steps = [s.strip() for s in re.split(r"[,/|]", rest) if s.strip()]
    cells = []
    for s in steps:
        # dice in step
        cell = html.escape(s)
        cell = DICE_RE.sub(
            lambda mm: dice_button(mm.group(1)), cell
        )
        cells.append(f'<span class="track-step">{cell}</span>')
    return (
        f'<div class="arcana-track">'
        f'<span class="track-label">{html.escape(label)}</span>'
        f'<span class="track-steps">{"".join(cells)}</span>'
        f"</div>"
    )


def _looks_like_follower_name(line: str) -> bool:
    L = line.strip()
    if not L or len(L) > 50 or re.match(r"^When you\b", L, re.I):
        return False
    if HP_LINE_RE.search(L) or looks_like_tag_line(L):
        return False
    words = L.split()
    if len(words) > 6:
        return False
    # Title Case or Proper Name
    return L[0:1].isupper() and not L.endswith(".")


def _dedupe_arcana_title(s: str) -> str:
    """Collapse a doubled card title ("A folktale folktale" -> "A folktale")."""
    s = re.sub(r"\s+", " ", s).strip()
    words = s.split()
    n = len(words)
    for k in range(1, n // 2 + 1):
        tail = words[n - k:]
        if words[n - 2 * k: n - k] == tail:
            # drop the repeated trailing phrase
            return " ".join(words[: n - k])
    return s


def _skip_arcana_power_heading(
    lines: list[str], start: int, power_norm: str
) -> int:
    """
    Advance past the power-name heading on a minor-arcana back face.

    The title is already shown in the face header; consuming it avoids
    duplicates. Wrapped titles often arrive as consecutive H3 lines
    (``Aalz Galt's`` + ``Sudden Sinkhole``).
    """
    i = start
    n = len(lines)
    while i < n:
        raw = lines[i]
        d = _defmt(strip_markers(raw)).strip()
        if raw.startswith("\x02TH") or not d or d.lower() in ("front", "back"):
            i += 1
            continue
        break

    bits: list[str] = []
    while i < n:
        raw = lines[i]
        d = _defmt(strip_markers(raw)).strip()
        if not d:
            i += 1
            continue
        is_h3 = raw.startswith("\x02H3")
        is_plain_title = (
            not raw.startswith("\x02")
            and not re.match(r"^(When you|When |During|If )\b", d, re.I)
            and len(d) <= 48
            and not d.endswith((".", "!", "?", "…"))
        )
        if not (is_h3 or is_plain_title):
            break
        cand = _dedupe_arcana_title(d)
        cnorm = normalize_section_key(cand)
        if not cnorm:
            break
        trial = normalize_section_key(" ".join(bits + [cand]))
        # Accept if trial equals or is a prefix of the power name
        if (
            trial == power_norm
            or power_norm.startswith(trial + " ")
            or power_norm == trial
        ):
            bits.append(cand)
            i += 1
            if trial == power_norm:
                break
            continue
        if cnorm == power_norm:
            i += 1
            break
        break
    return i


_ARCANA_TAG_WORD = re.compile(
    r"^(magical|fragile|immobile|beautiful|terrifying|close|reach|near|far|"
    r"hand|worn|warm|crude|slow|applied|thrown|messy|forceful|area|"
    r"dangerous|loud|ap|reload|cumbersome|awkward|indestructible|implanted|"
    r"large|two-handed)\b[\s,]*",
    re.I,
)


def _arcana_strip_tag_seps(text: str) -> str:
    """Drop leading inventory diamonds, commas, spaces, and fmt-only separators.

    PDF tag lines often bold the commas between italic tags:
    ``◇◇ \\x04,\\x05 \\x06magical\\x07\\x04,\\x05 \\x06beautiful\\x07``.
    Those bold commas must not stop the peel.
    """
    t = text or ""
    while t:
        # Plain diamonds / commas / whitespace
        m = re.match(r"^[◇\s,]+", t)
        if m:
            t = t[m.end() :]
            continue
        # Bold- or italic-wrapped separator (comma, diamond, space, or empty)
        m = re.match(r"^[\x04\x06]([◇\s,]*)[\x05\x07]", t)
        if m:
            t = t[m.end() :]
            continue
        break
    return t


def _arcana_tags_prose(text: str):
    """Peel a leading tag run (italic words, +N damage, etc.) off a line.

    Returns (tags_string, remaining_prose). The prose keeps its formatting.
    Handles bold commas and inventory diamonds between italic tag words.
    """
    t = _arcana_strip_tag_seps(text)
    tags: list[str] = []
    while t:
        t = _arcana_strip_tag_seps(t)
        if not t:
            break
        # Nested bold+italic tag: \x04\x06word\x07\x05
        m = re.match(r"^\x04\x06([^\x06\x07]*)\x07\x05", t)
        if m:
            bit = _defmt(m.group(1)).strip(" ,")
            if bit:
                tags.append(bit)
            t = t[m.end() :]
            continue
        m = re.match(r"^\x06([^\x06\x07]*)\x07", t)
        if m:
            bit = _defmt(m.group(1)).strip(" ,")
            if bit:
                tags.append(bit)
            t = t[m.end() :]
            continue
        m = re.match(r"^\+?\d+\s*(?:damage|piercing|armor|uses)\b", t)
        if m:
            tags.append(m.group(0).strip())
            t = t[m.end() :]
            continue
        m = _ARCANA_TAG_WORD.match(t)
        if m:
            tags.append(m.group(1))
            t = t[m.end() :]
            continue
        break
    tag_str = ", ".join(x for x in tags if x)
    return tag_str, t.strip()


def _arcana_strip_running(line: str) -> str:
    """Drop 'front'/'back'/page-number running-header junk from a card line."""
    d = _defmt(strip_markers(line))
    d = re.sub(r"\bfront\s*\d*\b", "", d, flags=re.I)
    d = re.sub(r"\bback\s*\d*\b", "", d, flags=re.I)
    return re.sub(r"\s+", " ", d).strip()


def structure_minor_arcana_html(
    lines: list[str],
    article_title: str,
    lookup: dict[int, dict],
    articles: list[dict],
    current_slug: str | None,
    section_index: dict | None = None,
    *,
    lookups: dict[str, dict[int, dict]] | None = None,
    section_indexes: dict[str, dict] | None = None,
    current_book: str | None = None,
) -> tuple[str, list[dict]]:
    """Layout a minor arcanum from rich marker lines (front + back faces)."""
    anchors = AnchorRegistry()
    link_kw = {
        "lookups": lookups,
        "section_indexes": section_indexes,
        "current_book": current_book,
    }

    def link(text: str) -> str:
        return linkify_pages(text, lookup, current_slug, section_index, **link_kw)

    def content(raw: str) -> str:
        return raw[len("\x02H3 "):] if raw.startswith("\x02H3 ") else _arcana_content(raw)

    power = (article_title or "Minor Arcanum").strip()
    power_norm = normalize_section_key(power)

    # Drop stray running-header-only lines
    lines = [l for l in lines if l.startswith("\x02") or _arcana_strip_running(l)]

    # Face split at the power-name H3 (the second card title)
    h3_idx = [i for i, l in enumerate(lines) if l.startswith("\x02H3")]
    split = None
    for i in h3_idx:
        nm = normalize_section_key(
            _dedupe_arcana_title(_defmt(strip_markers(lines[i])))
        )
        if nm == power_norm and i > 0:
            split = i
            break
    if split is None and len(h3_idx) >= 2:
        split = h3_idx[1]
    if split is None:
        # No clear back face; treat everything as front
        split = len(lines)

    front = lines[:split]
    back = lines[split:]

    def render_move(trigger: str, fl: list[str], j: int, n: int):
        body = [trigger]
        picks: list[str] = []
        seen_pick = False
        while j < n:
            r = fl[j]
            l = _defmt(strip_markers(r)).strip()
            if not l:
                j += 1
                continue
            if r.startswith(("\x02H", M_FACE, "\x02TH", M_MARK, M_ICON)):
                break  # an icon opens a follower's stat block
            if re.match(r"^(When you|When |During this battle|If )\b", l, re.I) and not r.startswith((M_B, M_B2)):
                break
            if r.startswith((M_B, M_B2)):
                picks.append(_arcana_content(r))
                seen_pick = True
                j += 1
                continue
            if seen_pick:
                picks.append(_arcana_content(r))
            else:
                body.append(_arcana_content(r))
            j += 1
        block = f'<div class="arcana-move"><p>{link(" ".join(body))}</p>'
        if picks:
            block += '<ul class="arcana-picks">'
            for p in picks:
                block += f"<li>{link(p)}</li>"
            block += "</ul>"
        block += "</div>"
        return block, j

    # ---- Front face: discovery + unlock ----
    fparts: list[str] = []
    disc_name = ""
    disc_tags = ""
    i = 0
    n = len(front)
    # Discovery name: the first run of consecutive H3 title lines. A long title
    # wraps onto a second H3 line (e.g. "Runes around" + "a ruined hall"), so
    # join them instead of leaving the tail as a stray paragraph.
    while i < n and not front[i].startswith("\x02H3"):
        i += 1
    title_parts: list[str] = []
    while i < n and front[i].startswith("\x02H3"):
        title_parts.append(_defmt(strip_markers(front[i])))
        i += 1
    disc_name = _dedupe_arcana_title(" ".join(title_parts))
    # tags + description + unlock
    desc_paras: list[str] = []
    unlock_intro = ""
    unlock_items: list[str] = []
    unlock_dividers: dict[int, str] = {}
    in_unlock = False
    while i < n:
        raw = front[i]
        i += 1
        d = _defmt(strip_markers(raw)).strip()
        if not d:
            continue
        if raw.startswith("\x02TH"):
            continue
        if raw.startswith((M_C, M_E)):
            in_unlock = True
            item = _arcana_content(raw)
            item = re.sub(r"^[\s…\.]+", "", item)
            unlock_items.append(item)
            continue
        if in_unlock:
            # "or:" / "and then:" dividers between requirement groups
            if len(d) <= 24 and re.match(r"^(or|and(?: then)?|either)\b", d, re.I):
                unlock_dividers[len(unlock_items)] = d.rstrip(":.") + ":"
                continue
            unlock_items[-1] = unlock_items[-1] + " " + _arcana_content(raw) if unlock_items else _arcana_content(raw)
            continue
        # tags on the first content line
        if not desc_paras and not disc_tags:
            tg, prose = _arcana_tags_prose(_arcana_content(raw))
            if tg:
                disc_tags = tg
            if prose:
                desc_paras.append(prose)
            continue
        desc_paras.append(_arcana_content(raw))
    # The last description paragraph before the checklist is the unlock intro
    if unlock_items and desc_paras:
        dl_last = _defmt(desc_paras[-1])
        if re.search(r"(you (?:can|either|must|need)|following|:|…|\.\.\.)\s*$", dl_last, re.I) or "you " in dl_last.lower():
            unlock_intro = desc_paras.pop()

    if disc_name:
        did = anchors.add(disc_name)
        fparts.append(
            _arcana_title_line(disc_name, "Front", tag="h3", hid=did)
        )
    else:
        fparts.append(_arcana_title_line(power, "Front", tag="h3"))
    if disc_tags:
        fparts.append(f'<p class="arcana-tags">{link(disc_tags)}</p>')
    for p in desc_paras:
        fparts.append(f"<p>{link(p)}</p>")
    if unlock_items:
        fparts.append('<div class="arcana-unlock">')
        if unlock_intro:
            fparts.append(
                f'<p class="arcana-unlock-intro">{link(unlock_intro)}</p>'
            )
        # One arcanum per page, so the id needs no arcanum prefix — the
        # storage key already opens with the page's slug.
        lid = "unlock"
        # split into groups by dividers
        groups: list[tuple[str, list[str]]] = []
        cur_hdr = ""
        cur: list[str] = []
        for idx, it in enumerate(unlock_items):
            if idx in unlock_dividers:
                groups.append((cur_hdr, cur))
                cur_hdr = unlock_dividers[idx]
                cur = []
            cur.append(it)
        groups.append((cur_hdr, cur))
        gi = 0
        for hdr, items in groups:
            if hdr:
                fparts.append(f'<p class="si-requires">{html.escape(hdr)}</p>')
            fparts.append(render_check_list(items, link, f"{lid}-{gi}"))
            gi += 1
        fparts.append("</div>")

    # ---- Back face: power moves ----
    bparts: list[str] = []
    power_tags = ""
    n = len(back)
    # Skip power-name heading (may be one H3 or wrapped H3s); shown in header
    i = _skip_arcana_power_heading(back, 0, power_norm)
    first_prose = True
    while i < n:
        raw = back[i]
        d = _defmt(strip_markers(raw)).strip()
        if not d or raw.startswith("\x02TH") or d.lower() in ("front", "back"):
            i += 1
            continue
        if raw.startswith(M_ICON):
            # A bound spirit's stat block; a stray icon is never printed as its path
            fb = _arcana_follower_block(
                back, i, lookup, current_slug, section_index, anchors, link_kw,
                "follower",
            )
            if fb:
                bparts.append(fb[0])
                i = fb[1]
            else:
                i += 1
            continue
        if raw.startswith(M_MARK):
            nm = int(raw[len(M_MARK):] or "0")
            bparts.append(
                render_mark_track(nm, "uses", label="Uses")
            )
            i += 1
            continue
        # use / level track lines: "Blaze: ◇ 1d4, …" or "hours: ◇◇◇"
        if "◇" in d and re.match(r"^[A-Za-z][\w\s]{0,18}:\s*◇", d):
            bparts.append(f'<p class="arcana-uses">{link(_arcana_content(raw))}</p>')
            i += 1
            continue
        named = _arcana_named_move(_arcana_content(raw))
        if named and not re.match(r"^When", named[0], re.I):
            name, mtags, trig = named
            name = titlecase_label(name)
            hid = anchors.add(name)
            label = html.escape(name)
            if mtags:
                label += f' <span class="arcana-sub-tags">({html.escape(mtags)})</span>'
            bparts.append(f'<h3 id="{html.escape(hid)}" class="arcana-sub">{label}</h3>')
            i += 1
            if trig:
                block, i = render_move(trig, back, i, n)
                bparts.append(block)
            continue
        if re.match(r"^(When you|When |During this battle|If )\b", d, re.I):
            block, i = render_move(_arcana_content(raw), back, i + 1, n)
            bparts.append(block)
            continue
        # power tags line / prose
        if first_prose:
            tg, prose = _arcana_tags_prose(_arcana_content(raw))
            if tg and not prose:
                power_tags = tg
                first_prose = False
                i += 1
                continue
            first_prose = False
        bparts.append(f"<p>{link(_arcana_content(raw))}</p>")
        i += 1

    hid = anchors.add(power)
    parts = [f'<div class="arcana-card" id="{html.escape(hid)}">']
    parts.append('<div class="arcana-face arcana-front">')
    parts.extend(fparts)
    parts.append("</div>")
    if bparts:
        parts.append('<div class="arcana-face arcana-back-face">')
        parts.append(_arcana_title_line(power, "Reverse", tag="h2"))
        if power_tags:
            parts.append(f'<p class="arcana-tags">{link(power_tags)}</p>')
        parts.extend(bparts)
        parts.append("</div>")
    parts.append("</div>")
    return "\n".join(parts), anchors.sections


def _is_mark_track_line(line: str) -> int:
    """
    Detect progress mark rows like 'l l l l l' (PDF dingbats as 'l').
    Returns number of marks, or 0 if not a track.
    """
    L = line.strip()
    # only l's and spaces (and maybe o/O/□)
    if re.fullmatch(r"[lLoO□☐○●•·\s]{2,}", L):
        n = len(re.findall(r"[lLoO□☐○●•·]", L))
        if 2 <= n <= 12:
            return n
    return 0


def _arcana_content(raw: str) -> str:
    """Strip a leading \\x02 marker but keep inline formatting sentinels."""
    if raw.startswith("\x02"):
        return MARKER_RE.sub("", raw)
    return raw


# A label the card sets in bold mid-line: two stats the column crammed onto one
# line ("Special qualities fireproof … \x04Instinct\x05 to be overzealous").
_FOLLOWER_LABEL_SPLIT_RE = re.compile(
    r"\s+(?=\x04(?:Special qualit(?:y|ies)|Instinct|Cost|Damage|HP)\x05)"
)
# A trigger line ends the block: the card goes back to its own moves.
_FOLLOWER_STOP_RE = re.compile(r"^(When |Each time |If |During )", re.I)


def _arcana_follower_block(
    lines: list[str],
    i: int,
    lookup: dict,
    current_slug: str | None,
    section_index: dict | None,
    anchors: "AnchorRegistry",
    link_kw: dict,
    check_id: str,
) -> tuple[str, int] | None:
    """A follower's stat block printed on an arcanum's card.

    The card opens it with a category icon and the follower's name, then sets
    tags, HP/Armor, Damage, Instinct, Special qualities, moves and Cost exactly
    the way a monster block is set — so it is read the same way and handed to
    ``render_stat_block`` as a ``follower``. The HP box the card prints for the
    player to fill in ("HP" / "Starts at 13" / a bare number) is dropped: the
    wiki's tracker takes its place. Returns ``(html, next_index)`` or ``None``
    when the icon is not opening a block at all."""
    n = len(lines)
    raw = lines[i]
    if not raw.startswith(M_ICON):
        return None
    icon = raw[len(M_ICON):].strip()
    j = i + 1
    while j < n and not _defmt(strip_markers(lines[j])).strip():
        j += 1
    if j >= n:
        return None
    name_raw = lines[j]
    name = _defmt(strip_markers(name_raw)).strip()
    # A card ruled into columns lands its HP/Armor boxes on the name line.
    boxed = re.search(r"\s+HP\s+Armor\s*$", name)
    if not (name_raw.startswith(M_H3) or boxed):
        return None
    name = re.sub(r"\s+HP\s+Armor\s*$", "", name).strip()
    j += 1

    block: list[str] = []
    tags = ""
    while j < n:
        L = lines[j]
        d = _defmt(strip_markers(L)).strip()
        if not d:
            j += 1
            continue
        # The tag line is the first thing under the name, set wholly in italics
        if not block and not tags and re.fullmatch(r"(\x06[^\x07]*\x07[\s,()0-9]*)+", L.strip()):
            tags = d
            j += 1
            continue
        if L.startswith(M_C):
            block.append(L)
            j += 1
            continue
        if L.startswith((M_B, M_B2, M_Q, M_E)):
            block.append("• " + MARKER_RE.sub("", L).strip())
            j += 1
            continue
        if L.startswith("\x02"):
            break  # rule, heading, face, icon — the block is over
        if d.lower() in ("front", "back"):
            j += 1
            continue
        if re.match(r"^HP\s*$", d, re.I):
            # The printed HP box: "HP" / "Starts at 13 each" / "10"
            j += 1
            while j < n:
                d2 = _defmt(strip_markers(lines[j])).strip()
                if re.match(r"^(Starts at \d+.*|\d+)$", d2, re.I):
                    j += 1
                    continue
                break
            break
        if _FOLLOWER_STOP_RE.match(d) and not d.startswith("•"):
            break
        for piece in _FOLLOWER_LABEL_SPLIT_RE.split(L):
            pd = _defmt(piece).strip()
            if not pd:
                continue
            # "Max. 13 lacks organs" — the HP box's cap, set beside it
            m_max = re.search(r"\bMax\.?\s+(\d+)\b", pd)
            if m_max:
                block.append(f"HP {m_max.group(1)}")
                rest = (pd[: m_max.start()] + pd[m_max.end():]).strip(" ;,")
                if rest:
                    block.append(rest)
                continue
            block.append(piece.strip())
        j += 1

    plain = [_defmt(strip_markers(b)).lower() for b in block]
    if not any(HP_LINE_RE.search(p) or p.startswith(("damage", "instinct")) for p in plain):
        return None
    html_out = render_stat_block(
        name,
        block,
        lookup,
        current_slug,
        section_index,
        anchor_id=anchors.add(name),
        link_kw=link_kw,
        check_id=check_id,
        icon_html=book_icon_img_html(icon, rel_prefix="") if icon else "",
        variant="follower",
        tags=tags,
    )
    return html_out, j


def _arcana_title_line(
    name: str,
    face_label: str,
    *,
    tag: str = "h2",
    hid: str | None = None,
) -> str:
    """Single-line arcana header: ``Name — FRONT/REVERSE`` (sans-serif via CSS).

    If *name* is empty, only the face label is shown (major reverse side).
    """
    id_attr = f' id="{html.escape(hid)}"' if hid else ""
    name = titlecase_name(name or "")
    face = f'<span class="arcana-title-face">{html.escape(face_label)}</span>'
    if not (name or "").strip():
        return f"<{tag}{id_attr} class=\"arcana-title-line\">{face}</{tag}>"
    return (
        f"<{tag}{id_attr} class=\"arcana-title-line\">"
        f'<span class="arcana-title-name">{html.escape(name)}</span>'
        f'<span class="arcana-title-sep" aria-hidden="true"> — </span>'
        f"{face}"
        f"</{tag}>"
    )


def _arcana_named_move(text: str):
    """Parse "NAME [(tags)] [When you ...]" into (name, tags, trigger) or None."""
    m = BOLD_PREFIX_RE.match(text)
    if not m:
        return None
    name = _defmt(m.group(1)).strip()
    if not name or not _is_all_caps_label(name):
        return None
    rest = text[m.end():]
    tags = ""
    tm = re.match(r"^[\s]*\x06([^\x06\x07]*)\x07\s*", rest)
    if tm:
        cand = _defmt(tm.group(1)).strip()
        if "(" in cand or _is_pure_arcana_tag_line(cand):
            tags = cand.strip(" ()")
            rest = rest[tm.end():]
    trigger = rest.strip()
    return name, tags, trigger


def _arcana_desc_tags(text: str):
    """Split a description line into (tags, description).

    Gear tags in the PDF often interleave italic words with roman numerals
    (``, close, +1 damage, 1 piercing, messy, magical``). Peel the full run
    via ``_arcana_tags_prose`` — a single leading italic span is not enough.
    """
    return _arcana_tags_prose(text)


def structure_major_arcana_html(
    lines: list[str],
    article_title: str,
    lookup: dict[int, dict],
    articles: list[dict],
    current_slug: str | None,
    section_index: dict | None = None,
    *,
    lookups: dict[str, dict[int, dict]] | None = None,
    section_indexes: dict[str, dict] | None = None,
    current_book: str | None = None,
) -> tuple[str, list[dict]]:
    """Layout a major arcanum from rich marker lines (front/back faces)."""
    anchors = AnchorRegistry()
    link_kw = {
        "lookups": lookups,
        "section_indexes": section_indexes,
        "current_book": current_book,
    }

    def link(text: str) -> str:
        return linkify_pages(text, lookup, current_slug, section_index, **link_kw)

    power = (article_title or "Major Arcanum").strip()
    power_norm = normalize_section_key(power)

    faces: dict[str, list[str]] = {"front": [], "back": []}
    cur = "front"
    for l in lines:
        if l.startswith(M_FACE):
            cur = l[len(M_FACE):].strip() or cur
            faces.setdefault(cur, [])
            continue
        faces.setdefault(cur, []).append(l)

    def render_face(fl: list[str], face: str, front: bool):
        out: list[str] = []
        tags = ""
        desc_done = False
        cons_items: list[str] = []
        section = "moves"
        i = 0
        n = len(fl)

        def dl(s: str) -> str:
            return _defmt(strip_markers(s)).strip()

        def collect_move(trigger_text: str, j: int):
            body = [trigger_text]
            picks: list[str] = []
            seen_pick = False
            while j < n:
                r = fl[j]
                l = dl(r)
                if not l or l.lower() in ("front", "back"):
                    j += 1
                    continue
                if r.startswith(("\x02H", M_FACE, "\x02TH", M_MARK, M_ICON)):
                    break  # an icon opens a follower's stat block
                if r.startswith(M_C):
                    break
                if _arcana_named_move(_arcana_content(r)):
                    break
                if re.match(r"^(When you|During this battle)\b", l, re.I):
                    break
                if r.startswith((M_B, M_B2)):
                    picks.append(_arcana_content(r))
                    seen_pick = True
                    j += 1
                    continue
                if seen_pick:
                    picks.append(_arcana_content(r))
                else:
                    body.append(_arcana_content(r))
                j += 1
            block = f'<div class="arcana-move"><p>{link(" ".join(body))}</p>'
            if picks:
                block += '<ul class="arcana-picks">'
                for p in picks:
                    block += f"<li>{link(p)}</li>"
                block += "</ul>"
            block += "</div>"
            return block, j

        while i < n:
            raw = fl[i]
            L = dl(raw)
            if not L:
                i += 1
                continue
            if raw.startswith("\x02TH") or L.lower() in ("front", "back"):
                i += 1
                continue
            if raw.startswith(M_ICON):
                fb = _arcana_follower_block(
                    fl, i, lookup, current_slug, section_index, anchors, link_kw,
                    f"arcana-{slugify_id(power)}-{face}-follower-{i}",
                )
                if fb:
                    out.append(fb[0])
                    i = fb[1]
                else:
                    i += 1
                continue
            if raw.startswith("\x02H2") and L.lower() == "moves":
                i += 1
                continue
            if raw.startswith(("\x02H2", "\x02H3")) and re.match(
                r"^Mysteries of\b", L, re.I
            ):
                i += 1
                continue
            if raw.startswith(("\x02H2", "\x02H3")) and normalize_section_key(
                L
            ) == power_norm:
                i += 1
                continue
            if raw.startswith("\x02H2") and re.match(r"^Consequences\b", L, re.I):
                section = "consequences"
                i += 1
                continue
            if section == "consequences":
                if raw.startswith(M_C):
                    cons_items.append(_arcana_content(raw))
                i += 1
                continue
            if raw.startswith(M_MARK):
                nm = int(raw[len(M_MARK):] or "0")
                lid = f"{face}-marks"
                out.append(render_mark_track(nm, lid, label="Progress marks"))
                i += 1
                continue
            named = _arcana_named_move(_arcana_content(raw))
            if named:
                name, mtags, trigger = named
                name = titlecase_label(name)
                hid = anchors.add(name)
                label = html.escape(name)
                if mtags:
                    label += (
                        f' <span class="arcana-sub-tags">'
                        f"({html.escape(mtags)})</span>"
                    )
                out.append(
                    f'<h3 id="{html.escape(hid)}" class="arcana-sub">{label}</h3>'
                )
                i += 1
                if trigger and re.match(
                    r"^(When you|During)\b", _defmt(trigger), re.I
                ):
                    block, i = collect_move(trigger, i)
                    out.append(block)
                elif trigger:
                    out.append(f"<p>{link(trigger)}</p>")
                continue
            if re.match(r"^(When you|During this battle)\b", L, re.I):
                block, i = collect_move(_arcana_content(raw), i + 1)
                out.append(block)
                continue
            if front and not desc_done:
                t, desc = _arcana_desc_tags(_arcana_content(raw))
                if t:
                    tags = f"{tags}, {t}" if tags else t
                if desc:
                    out.append(f"<p>{link(desc)}</p>")
                    desc_done = True
                elif not t:
                    # Neither tags nor residual prose — treat as description.
                    out.append(f"<p>{link(_arcana_content(raw))}</p>")
                    desc_done = True
                # Pure tag line: keep desc_done False so a wrapped tag
                # continuation (or the real description) is handled next.
                i += 1
                continue
            out.append(f"<p>{link(_arcana_content(raw))}</p>")
            i += 1

        if cons_items:
            lid = "cons"
            out.append('<div class="arcana-unlock arcana-consequences">')
            out.append('<p class="si-requires">Consequences</p>')
            out.append(render_check_list(cons_items, link, lid))
            out.append(
                '<p class="arcana-note"><em>Mark consequences as they apply.</em></p>'
            )
            out.append("</div>")
        return "\n".join(out), tags

    front_html, tags = render_face(faces.get("front", []), "front", True)
    back_html, _ = render_face(faces.get("back", []), "back", False)

    hid = anchors.add(power)
    parts = [
        f'<div class="arcana-card arcana-major" id="{html.escape(hid)}">',
        '<div class="arcana-face arcana-front">',
        _arcana_title_line(power, "Front", tag="h2"),
    ]
    if tags:
        parts.append(f'<p class="arcana-tags">{link(tags)}</p>')
    parts.append(front_html)
    parts.append("</div>")
    if back_html.strip():
        parts.append('<div class="arcana-face arcana-back-face">')
        # Majors have no alternate reverse name — face label only
        parts.append(_arcana_title_line("", "Reverse", tag="h2"))
        parts.append(back_html)
        parts.append("</div>")
    parts.append("</div>")
    return "\n".join(parts), anchors.sections


def _arcana_excerpt(lines: list[str]) -> str:
    excerpt = ""
    for line in lines:
        if line.startswith("\x02"):
            continue
        clean = _defmt(line).strip()
        if len(clean) >= 50 and not clean.lower().startswith("when you"):
            excerpt = clean
            break
    if not excerpt and lines:
        excerpt = _defmt(strip_markers(lines[0]))
    if len(excerpt) > 320:
        excerpt = excerpt[:319].rsplit(" ", 1)[0] + "…"
    return excerpt


def minor_arcana_html(
    lines: list[str],
    article_title: str,
    lookup: dict[int, dict],
    articles: list[dict],
    current_slug: str | None,
    section_index: dict | None = None,
    *,
    lookups: dict[str, dict[int, dict]] | None = None,
    section_indexes: dict[str, dict] | None = None,
    current_book: str | None = None,
) -> tuple[str, str, list[dict]]:
    """Build HTML for a single minor arcanum card (front+back) from its
    extracted lines (``extract_minor_arcana_rich`` or the corpus).

    Returns (body_html, excerpt, sections).
    """
    body, sections = structure_minor_arcana_html(
        lines,
        article_title,
        lookup,
        articles,
        current_slug,
        section_index=section_index,
        lookups=lookups,
        section_indexes=section_indexes,
        current_book=current_book,
    )
    return body, _arcana_excerpt(lines), sections


def major_arcana_html(
    lines: list[str],
    article_title: str,
    lookup: dict[int, dict],
    articles: list[dict],
    current_slug: str | None,
    section_index: dict | None = None,
    *,
    lookups: dict[str, dict[int, dict]] | None = None,
    section_indexes: dict[str, dict] | None = None,
    current_book: str | None = None,
) -> tuple[str, str, list[dict]]:
    """Build HTML for a major arcanum (two-page card) from its extracted
    lines (``extract_major_arcana_rich`` or the corpus)."""
    body, sections = structure_major_arcana_html(
        lines,
        article_title,
        lookup,
        articles,
        current_slug,
        section_index=section_index,
        lookups=lookups,
        section_indexes=section_indexes,
        current_book=current_book,
    )
    return body, _arcana_excerpt(lines), sections
