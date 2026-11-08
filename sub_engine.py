"""
Clean MegaPlay Universal Subtitle Engine
----------------------------------------
Fetches English subtitles from MegaPlay (Server 3),
Translates into Spoken Sinhala using high-speed multi-threaded translation,
Uploads Sinhala WebVTT to GitHub Releases (DDL),
Saves direct link in Firestore under 'stream_subtitles.sinhala',
and signals job completion to RTDB.
(Contains NO ad cleaners, spam filters, or useless regexes).
"""

import os
import re
import sys
import time
import json
import base64
import requests
import concurrent.futures

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    import chardet
except ImportError:
    chardet = None

try:
    from deep_translator import GoogleTranslator
except ImportError:
    GoogleTranslator = None

# Spoken Sinhala dictionary & grammar rules
try:
    from spoken_dict import SPOKEN_DICT, SPOKEN_REGEX_RULES
except ImportError:
    try:
        from spoken_dict import SPOKEN_DICT
    except ImportError:
        SPOKEN_DICT = {}
    SPOKEN_REGEX_RULES = []

_SORTED_SPOKEN_PATTERNS = [
    (re.compile(r'(?<![\w\u0D80-\u0DFF\u200D])' + re.escape(k) + r'(?![\w\u0D80-\u0DFF\u200D])'), SPOKEN_DICT[k])
    for k in sorted(SPOKEN_DICT.keys(), key=len, reverse=True)
] if SPOKEN_DICT else []

_COMPILED_SPOKEN_REGEX = [
    (re.compile(pat) if isinstance(pat, str) else pat, repl)
    for pat, repl in (SPOKEN_REGEX_RULES or [])
]

def apply_spoken_sinhala(text):
    if not text:
        return text
    result_text = str(text)
    # 1. Run longest-first phrase and word dictionary replacements first
    for pat, value in _SORTED_SPOKEN_PATTERNS:
        result_text = pat.sub(value, result_text)
    # 2. Run structural regex rules to catch any remaining literary suffixes (-නු ඇත/තියෙනවා, -ිය පුළුවන්, etc.)
    for pat, repl in _COMPILED_SPOKEN_REGEX:
        result_text = pat.sub(repl, result_text)
    return result_text.strip()

def has_sinhala_characters(text):
    return bool(re.search(r'[\u0D80-\u0DFF]', str(text)))

def adjust_vtt_timing(vtt_text, shift_sec):
    """
    Shifts all WebVTT timestamp cues by shift_sec seconds (positive or negative).
    Supports 00:00:00.000 and 00:00.000 cue formats.
    """
    if not shift_sec or abs(float(shift_sec)) < 0.001:
        return vtt_text

    shift = float(shift_sec)

    def shift_match(match):
        h = int(match.group(1)) if match.group(1) is not None else 0
        m = int(match.group(2))
        s = int(match.group(3))
        ms = int(match.group(4))
        total_sec = h * 3600 + m * 60 + s + ms / 1000.0
        new_sec = max(0.0, total_sec + shift)
        new_h = int(new_sec // 3600)
        rem = new_sec % 3600
        new_m = int(rem // 60)
        rem = rem % 60
        new_s = int(rem)
        new_ms = int(round((rem - new_s) * 1000))
        if new_ms >= 1000:
            new_s += 1
            new_ms -= 1000
        if match.group(1) is not None or new_h > 0:
            return f"{new_h:02d}:{new_m:02d}:{new_s:02d}.{new_ms:03d}"
        else:
            return f"{new_m:02d}:{new_s:02d}.{new_ms:03d}"

    time_pat = re.compile(r'(?:(\d{2,}):)?(\d{2}):(\d{2})\.(\d{3})')

    def replace_line(line):
        if '-->' in line:
            parts = line.split('-->')
            left = time_pat.sub(shift_match, parts[0])
            right = time_pat.sub(shift_match, parts[1])
            return left + '-->' + right
        return line

    lines = vtt_text.splitlines()
    return '\n'.join(replace_line(l) for l in lines)

def convert_ass_to_vtt(text):
    """Converts Advanced SubStation Alpha (.ass / .ssa) subtitle text to standard WebVTT format."""
    if not text:
        return ""
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    events_found = False
    format_indices = {}
    cues = []
    seen_cues = set()
    
    # Non-dialogue styles to exclude (Karaoke OP/ED songs, Romaji lyrics, Typesetting/Vector drawings)
    skip_style_keywords = (
        'op', 'ed', 'romaji', 'karaoke', 'kfx', 'lyrics', 'song',
        'insert', 'ending', 'opening', 'credit', 'logo', 'effect'
    )
    
    for line in lines:
        line_clean = line.strip()
        if not line_clean:
            continue
        if line_clean.lower() == '[events]':
            events_found = True
            continue
        if line_clean.startswith('[') and events_found:
            break
        if events_found and line_clean.lower().startswith('format:'):
            parts = [p.strip().lower() for p in line_clean[7:].split(',')]
            format_indices = {name: idx for idx, name in enumerate(parts)}
            continue
        if events_found and line_clean.lower().startswith('dialogue:'):
            raw_data = line_clean[9:].split(',', len(format_indices) - 1) if format_indices else line_clean[9:].split(',', 9)
            start_idx = format_indices.get('start', 1)
            end_idx = format_indices.get('end', 2)
            style_idx = format_indices.get('style', 3)
            effect_idx = format_indices.get('effect', 8)
            text_idx = format_indices.get('text', len(raw_data) - 1)
            
            if len(raw_data) > max(start_idx, end_idx, text_idx):
                start = raw_data[start_idx].strip()
                end = raw_data[end_idx].strip()
                style_val = raw_data[style_idx].strip().lower() if len(raw_data) > style_idx else ""
                effect_val = raw_data[effect_idx].strip().lower() if len(raw_data) > effect_idx else ""
                dialogue = raw_data[text_idx]
                
                # Skip OP/ED karaoke, Romaji song styles, and ASS vector drawings ({\p1}..{\p0})
                if any(kw in style_val for kw in skip_style_keywords) or any(kw in effect_val for kw in ('karaoke', 'fx', 'template')):
                    continue
                if re.search(r'\{\\[^}]*\\p[1-9]', dialogue):
                    continue
                
                # Strip ASS formatting tags {\an8}, {\b1}, {\c&H...&}, etc.
                dialogue = re.sub(r'\{[^\}]*\}', '', dialogue)
                dialogue = dialogue.replace('\\N', '\n').replace('\\n', '\n').replace('\\h', ' ')
                dialogue = dialogue.strip()
                
                if not dialogue:
                    continue
                
                # Format start & end timestamp into WebVTT hh:mm:ss.mmm
                def fmt_time(t):
                    pts = t.split(':')
                    if len(pts) == 3:
                        h = pts[0].zfill(2)
                        m = pts[1].zfill(2)
                        sec_pts = pts[2].split('.')
                        s = sec_pts[0].zfill(2)
                        ms = sec_pts[1].ljust(3, '0')[:3] if len(sec_pts) > 1 else '000'
                        return f"{h}:{m}:{s}.{ms}"
                    return t
                
                t_start = fmt_time(start)
                t_end = fmt_time(end)
                if t_start == t_end:
                    continue
                
                cue_key = (t_start, t_end, dialogue)
                if cue_key in seen_cues:
                    continue
                seen_cues.add(cue_key)
                cues.append(f"{t_start} --> {t_end}\n{dialogue}")
                
    return "WEBVTT\n\n" + "\n\n".join(cues)

def convert_srt_to_vtt(text):
    """Normalizes SRT / ASS format text to standard WebVTT format"""
    if not text:
        return ""
    normalized = text.replace('\r\n', '\n').replace('\r', '\n').strip()
    if normalized.startswith("WEBVTT"):
        return normalized
    # Detect ASS / SSA subtitles
    if "[Script Info]" in normalized or "[Events]" in normalized or "Dialogue:" in normalized:
        return convert_ass_to_vtt(normalized)
    # Replace comma in timestamps with dot (00:01:23,456 -> 00:01:23.456)
    normalized = re.sub(r'(\d{2}:\d{2}:\d{2}),(\d{3})', r'\1.\2', normalized)
    return "WEBVTT\n\n" + normalized

def extract_episode_number_from_filename(filename):
    """
    Intelligently extracts the episode number from subtitle filenames.
    Examples:
    - 'Bleach - 01 [1080p].vtt' -> 1
    - '[SubsPlease] One Piece - 1089 (1080p).srt' -> 1089
    - '(B-A)Hajime_no_Ippo_-_Round_71_(14F59B95).srt' -> 71
    - '(B-A)Hajime_no_Ippo_-_Round_61v2_(BD534F9B).srt' -> 61
    - 'Demon Slayer S03E05.srt' -> 5
    - 'Attack on Titan Episode 12 Sinhala.vtt' -> 12
    - 'ep_03_sinhala.vtt' -> 3
    - '01.vtt' -> 1
    """
    if not filename:
        return None

    base = re.sub(r'\.[a-zA-Z0-9]+$', '', str(filename))
    clean = re.sub(r'\[[^\]]*\]|\([^\)]*\)', ' ', base).strip()

    # 1. Season and Episode pattern: S01E05 or S2E12
    m = re.search(r'[sS]\d+[\s._-]*[eE](\d+)', clean)
    if m:
        return int(m.group(1))

    # 2. Explicit keywords: ep, episode, round, act, chapter
    m = re.search(r'(?:ep|ep\.|episode|round|act|chapter)[\s._-]*(\d+)', clean, re.IGNORECASE)
    if m:
        return int(m.group(1))

    # 3. Hyphen / underscore separated number (with optional v2/v3 suffix)
    m = re.search(r'[-_]\s*(\d+)(?:[vV]\d+)?\s*(?:$|[-_])', clean)
    if m:
        return int(m.group(1))

    # 4. Fallback: find all numbers, excluding standard resolutions, codecs, audio channels, and years
    clean_no_tech = re.sub(
        r'\b(?:1080p?|720p?|480p?|360p?|2160p?|4k|8k|[xh]\.?26[45]|hevc|av1|10bit|8bit|aac2[\s._-]*0|ddp?5[\s._-]*1|[57][\s._-]*1|2[\s._-]*0|blu-?ray|web-?dl|webrip|bdrip|dvdrip|hdr10?)\b',
        ' ',
        clean,
        flags=re.IGNORECASE
    )
    nums = re.findall(r'\b\d+\b', clean_no_tech)
    if nums:
        filtered = [int(n) for n in nums if int(n) not in (1080, 720, 480, 360, 2160, 264, 265) and not (1950 <= int(n) <= 2035 and len(n) == 4)]
        if filtered:
            return filtered[-1]

    return None

def _parse_title_features(title):
    if not title:
        return {"base": set(), "season": None, "part": None, "is_final": False, "is_special": False}
    t = str(title).lower()
    t = re.sub(r'\b(?:season|part|cour)\s+iii\b', lambda m: m.group(0).rsplit('iii', 1)[0] + '3', t)
    t = re.sub(r'\b(?:season|part|cour)\s+ii\b', lambda m: m.group(0).rsplit('ii', 1)[0] + '2', t)
    t = re.sub(r'\b(?:season|part|cour)\s+iv\b', lambda m: m.group(0).rsplit('iv', 1)[0] + '4', t)

    is_final = bool(re.search(r'\b(final\s+season|the\s+final|kanketsu[\s-]*hen)\b', t))
    is_special = bool(re.search(r'\b(ova|oad|ona|special|movie|recap|chronicle)\b', t))

    season = None
    m_s = re.search(r'\bseason\s*(\d+)\b|\b(\d+)(?:st|nd|rd|th)\s*season\b|\bs(\d+)\b', t)
    if m_s:
        season = int(next(g for g in m_s.groups() if g is not None))

    part = None
    m_p = re.search(r'\b(?:part|cour)\s*(\d+)\b|\b(\d+)(?:st|nd|rd|th)\s*(?:part|cour)\b', t)
    if m_p:
        part = int(next(g for g in m_p.groups() if g is not None))

    clean = re.sub(
        r'\b(season\s*\d+|\d+(?:st|nd|rd|th)\s*season|s\d+|(?:part|cour)\s*\d+|\d+(?:st|nd|rd|th)\s*(?:part|cour)|the\s+final\s+season|final\s+season|kanketsu[\s-]*hen|the|no|of|on|in|to|a|an|and|wa|ni|ga|wo|o)\b',
        ' ',
        t
    )
    words = {w for w in re.findall(r'[a-z]{2,}', clean) if w not in {'tv', 'part', 'season', 'cour', 'final'}}
    return {
        "base": words,
        "season": season,
        "part": part,
        "is_final": is_final,
        "is_special": is_special
    }

def _score_anikoto_candidate(cand_en, cand_jp, cand_year, target_titles, target_year=None):
    cand_feats = [_parse_title_features(cand_en), _parse_title_features(cand_jp)]
    target_feats = [_parse_title_features(t) for t in (target_titles or []) if t]
    if not target_feats:
        return -1000

    t_base = set()
    for tf in target_feats:
        t_base |= tf["base"]
    t_season = next((tf["season"] for tf in target_feats if tf["season"] is not None), None)
    t_part = next((tf["part"] for tf in target_feats if tf["part"] is not None), None)
    t_final = any(tf["is_final"] for tf in target_feats)
    t_special = any(tf["is_special"] for tf in target_feats)

    c_base = set()
    for cf in cand_feats:
        c_base |= cf["base"]
    c_season = next((cf["season"] for cf in cand_feats if cf["season"] is not None), None)
    c_part = next((cf["part"] for cf in cand_feats if cf["part"] is not None), None)
    c_final = any(cf["is_final"] for cf in cand_feats)
    c_special = any(cf["is_special"] for cf in cand_feats)

    if t_base and not (t_base & c_base):
        return -1000
    if t_special != c_special:
        return -1000
    if t_final != c_final:
        return -1000

    eff_t_season = t_season if t_season is not None else (None if t_final else 1)
    eff_c_season = c_season if c_season is not None else (None if c_final else 1)
    if eff_t_season != eff_c_season:
        return -1000

    eff_t_part = t_part if t_part is not None else 1
    eff_c_part = c_part if c_part is not None else 1
    if eff_t_part != eff_c_part:
        return -1000

    score = len(t_base & c_base) * 20
    if target_year and cand_year:
        try:
            ty, cy = int(target_year), int(cand_year)
            if ty == cy:
                score += 50
            elif abs(ty - cy) > 1:
                score -= 40
        except Exception:
            pass
    return score

def get_fallback_anime_ids(anime_id):
    """
    Bidirectional AniList ID <-> MyAnimeList ID resolver using AniList GraphQL API,
    including TV PREQUEL chain metadata for split-cour / Part 2 episode offset resolution.
    """
    ids = {
        "anilist_id": str(anime_id) if anime_id else None,
        "mal_id": None,
        "title": None,
        "titles": [],
        "season_year": None,
        "episodes": None,
        "prequels": []
    }
    if not anime_id:
        return ids
    try:
        int_id = int(str(anime_id).strip())
        headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
        gql_fields = """
          id
          idMal
          seasonYear
          episodes
          title { english romaji userPreferred }
          relations {
            edges {
              relationType(version: 2)
              node {
                id
                idMal
                episodes
                seasonYear
                format
                type
                title { english romaji userPreferred }
                relations {
                  edges {
                    relationType(version: 2)
                    node {
                      id
                      idMal
                      episodes
                      seasonYear
                      format
                      type
                      title { english romaji userPreferred }
                    }
                  }
                }
              }
            }
          }
        """

        media = None
        q1 = f"query ($id: Int) {{ Media (id: $id, type: ANIME) {{ {gql_fields} }} }}"
        r1 = requests.post("https://graphql.anilist.co", json={"query": q1, "variables": {"id": int_id}}, headers=headers, timeout=6)
        if r1.status_code == 200:
            media = r1.json().get("data", {}).get("Media")

        if not media:
            q2 = f"query ($idMal: Int) {{ Media (idMal: $idMal, type: ANIME) {{ {gql_fields} }} }}"
            r2 = requests.post("https://graphql.anilist.co", json={"query": q2, "variables": {"idMal": int_id}}, headers=headers, timeout=6)
            if r2.status_code == 200:
                media = r2.json().get("data", {}).get("Media")

        if media:
            if media.get("idMal"):
                ids["mal_id"] = str(media["idMal"])
            if media.get("id"):
                ids["anilist_id"] = str(media["id"])
            ids["season_year"] = media.get("seasonYear")
            ids["episodes"] = media.get("episodes")
            t_obj = media.get("title") or {}
            t_list = [t_obj.get("english"), t_obj.get("romaji"), t_obj.get("userPreferred")]
            ids["titles"] = [t.strip() for t in t_list if t and str(t).strip()]
            if ids["titles"]:
                ids["title"] = ids["titles"][0]

            # Extract TV PREQUEL chain (up to 2 levels for Part 2 / Part 3 split cours, including across intermediate Recap/Special entries)
            edges = (media.get("relations") or {}).get("edges") or []
            for edge in edges:
                if edge.get("relationType") == "PREQUEL":
                    pnode = edge.get("node") or {}
                    p_is_tv = (pnode.get("type") == "ANIME" and pnode.get("format") == "TV" and (pnode.get("episodes") or 0) > 0)
                    p_eps = int(pnode["episodes"]) if p_is_tv else 0
                    if p_is_tv:
                        pt_obj = pnode.get("title") or {}
                        pt_list = [pt_obj.get("english"), pt_obj.get("romaji"), pt_obj.get("userPreferred")]
                        p_titles = [t.strip() for t in pt_list if t and str(t).strip()]
                        ids["prequels"].append({
                            "anilist_id": str(pnode["id"]) if pnode.get("id") else None,
                            "mal_id": str(pnode["idMal"]) if pnode.get("idMal") else None,
                            "episodes_offset": p_eps,
                            "season_year": pnode.get("seasonYear"),
                            "title": p_titles[0] if p_titles else None,
                            "titles": p_titles
                        })
                    # Check 2nd-level prequel (e.g. Part 3 -> Part 2 -> Part 1, or Part 2 -> Recap Special -> Part 1)
                    sub_edges = (pnode.get("relations") or {}).get("edges") or []
                    for sedge in sub_edges:
                        if sedge.get("relationType") == "PREQUEL":
                            spnode = sedge.get("node") or {}
                            if spnode.get("type") == "ANIME" and spnode.get("format") == "TV" and (spnode.get("episodes") or 0) > 0:
                                spt_obj = spnode.get("title") or {}
                                spt_list = [spt_obj.get("english"), spt_obj.get("romaji"), spt_obj.get("userPreferred")]
                                sp_titles = [t.strip() for t in spt_list if t and str(t).strip()]
                                sp_eps = int(spnode["episodes"])
                                ids["prequels"].append({
                                    "anilist_id": str(spnode["id"]) if spnode.get("id") else None,
                                    "mal_id": str(spnode["idMal"]) if spnode.get("idMal") else None,
                                    "episodes_offset": p_eps + sp_eps,
                                    "season_year": spnode.get("seasonYear"),
                                    "title": sp_titles[0] if sp_titles else None,
                                    "titles": sp_titles
                                })
    except Exception:
        pass

    # Fallback / Enrich via api.ani.zip (provides TVDB episode mappings & base season title even if AniList GraphQL is rate-limited)
    try:
        az_id = ids.get("anilist_id") or anime_id
        az_res = requests.get(f"https://api.ani.zip/mappings?anilist_id={az_id}", headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
        if az_res.status_code == 200:
            az_data = az_res.json()
            az_mal = (az_data.get("mappings") or {}).get("mal_id")
            if az_mal and not ids.get("mal_id"):
                ids["mal_id"] = str(az_mal)
            az_titles_obj = az_data.get("titles") or {}
            raw_en = az_titles_obj.get("en") or az_titles_obj.get("x-jat") or ""
            clean_base = re.sub(r'\s*\(\d{4}\)\s*$', '', str(raw_en)).strip()
            if clean_base:
                ids["base_season_title"] = clean_base
                if not ids.get("title"):
                    ids["title"] = clean_base
                if clean_base not in ids["titles"]:
                    ids["titles"].append(clean_base)
            ids["anizip_episodes"] = az_data.get("episodes") or {}
    except Exception:
        pass

    return ids

def _parse_anikoto_search_items(html):
    """Parses <a class='item' ...> blocks from Anikoto search HTML into structured candidate dicts."""
    items = []
    if not html:
        return items
    for m in re.finditer(r'<a\b[^>]*class=[\'"][^\'"]*\bitem\b[^\'"]*[\'"][^>]*>([\s\S]*?)</a>', html, re.IGNORECASE):
        full_tag = m.group(0)
        inner = m.group(1)
        m_href = re.search(r'href=[\'"](https?://anikoto\.cz/watch/[^/\'"]+|/watch/[^/\'"]+)[\'"]', full_tag, re.IGNORECASE)
        if not m_href:
            continue
        href = m_href.group(1)
        if not href.startswith("http"):
            href = "https://anikoto.cz" + href
        m_name = re.search(r'<div[^>]*class=[\'"][^\'"]*\bname\b[^\'"]*[\'"][^>]*>([^<]*)</div>', inner, re.IGNORECASE)
        en_title = m_name.group(1).strip() if m_name else ""
        m_jp = re.search(r'data-jp=[\'"]([^\'"]*)[\'"]', inner, re.IGNORECASE)
        jp_title = m_jp.group(1).strip() if m_jp else ""
        m_yr = re.search(r'<span[^>]*class=[\'"][^\'"]*\bdot\b[^\'"]*[\'"][^>]*>\s*(19\d\d|20\d\d)\s*</span>', inner, re.IGNORECASE)
        year = int(m_yr.group(1)) if m_yr else None
        items.append({
            "href": href,
            "en_title": en_title,
            "jp_title": jp_title,
            "year": year
        })
    return items

def resolve_megaplay_via_anikoto(title, ep_num, anime_id=None, id_map=None):
    """
    Directly resolves episode stream using Anikoto Search and https://anikotoapi.site/series/{series_id}.
    Strictly validates title, season, part, and MAL/AniList IDs so wrong seasons (e.g. S4 instead of S3 Part 2) are never picked,
    and automatically resolves split-cour Part 2 episodes combined under Prequel series entries.
    """
    if not id_map and anime_id:
        id_map = get_fallback_anime_ids(anime_id)
    id_map = id_map or {}

    target_titles = list(id_map.get("titles") or [])
    if title and str(title).strip() and str(title).strip() not in target_titles:
        target_titles.insert(0, str(title).strip())
    if not target_titles:
        return None

    try:
        s = requests.Session()
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        s.headers.update({"User-Agent": ua})

        def search_anikoto_candidates(kw):
            cands = []
            seen_hrefs = set()
            search_url = f"https://anikoto.cz/ajax/anime/search?keyword={requests.utils.quote(str(kw))}"
            r = s.get(search_url, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=10)
            if r.status_code == 200:
                html = r.json().get("result", {}).get("html", "")
                for item in _parse_anikoto_search_items(html):
                    if item["href"] not in seen_hrefs:
                        seen_hrefs.add(item["href"])
                        cands.append(item)
            if not cands:
                s_url = f"https://anikoto.cz/search?keyword={requests.utils.quote(str(kw))}"
                s_resp = s.get(s_url, timeout=10)
                if s_resp.status_code == 200:
                    for item in _parse_anikoto_search_items(s_resp.text):
                        if item["href"] not in seen_hrefs:
                            seen_hrefs.add(item["href"])
                            cands.append(item)
            return cands

        def try_resolve_from_watch_link(watch_link, target_ep_num, expected_ani_id=None, expected_mal_id=None):
            wr = s.get(watch_link, timeout=10)
            dids = re.findall(r'data-id=[\'"](\d+)[\'"]', wr.text)
            if not dids:
                return None
            series_id = dids[0]

            api_url = f"https://anikotoapi.site/series/{series_id}"
            api_res = s.get(api_url, timeout=10)
            if api_res.status_code == 200:
                api_json = api_res.json().get("data", {})
                anime_meta = api_json.get("anime") or {}
                series_mal = str(anime_meta.get("mal_id") or "").strip()
                series_ani = str(anime_meta.get("ani_id") or "").strip()

                # Strict ID verification: if series has a known MAL or AniList ID and it conflicts with expected IDs, reject it!
                if expected_mal_id and series_mal and series_mal != str(expected_mal_id):
                    return None
                if expected_ani_id and series_ani and series_ani != str(expected_ani_id):
                    return None

                episodes = api_json.get("episodes", [])
                target_ep = next((e for e in episodes if int(e.get("number", 0)) == int(target_ep_num)), None)
                if target_ep:
                    embed_id = target_ep.get("episode_embed_id")
                    embed_url = (target_ep.get("embed_url") or {}).get("sub") or f"https://megaplay.buzz/stream/s-2/{embed_id}/sub"
                    mr = s.get(embed_url, headers={"Referer": "https://megaplay.buzz/"}, timeout=10)
                    m_ids = re.findall(r'data-id=[\'"](\d+)[\'"]', mr.text)
                    if m_ids:
                        print(f"🎬 [AnikotoAPI] Successfully resolved working MegaPlay stream data-id: {m_ids[0]} via Series {series_id} (EP {target_ep_num})", flush=True)
                        return m_ids[0]
            return None

        # 1. Try strict direct match for current anime at ep_num
        all_cands = search_anikoto_candidates(target_titles[0])
        scored = []
        for c in all_cands:
            sc = _score_anikoto_candidate(c["en_title"], c["jp_title"], c["year"], target_titles, id_map.get("season_year"))
            if sc > 0:
                scored.append((sc, c))
        scored.sort(key=lambda x: x[0], reverse=True)

        for sc, c in scored:
            did = try_resolve_from_watch_link(
                c["href"],
                ep_num,
                expected_ani_id=id_map.get("anilist_id"),
                expected_mal_id=id_map.get("mal_id")
            )
            if did:
                return did

        # 2. Try Prequel Continuation match on Anikoto (e.g. Season 3 Part 2 -> Season 3 with ep = prequel_eps + ep_num)
        for preq in (id_map.get("prequels") or []):
            preq_titles = preq.get("titles") or ([preq["title"]] if preq.get("title") else [])
            if not preq_titles:
                continue
            combined_ep = int(preq.get("episodes_offset", 0)) + int(ep_num)
            p_cands = search_anikoto_candidates(preq_titles[0])
            # Also include any already-fetched candidates
            seen_h = {pc["href"] for pc in p_cands}
            for ac in all_cands:
                if ac["href"] not in seen_h:
                    p_cands.append(ac)

            p_scored = []
            for c in p_cands:
                sc = _score_anikoto_candidate(c["en_title"], c["jp_title"], c["year"], preq_titles, preq.get("season_year"))
                if sc > 0:
                    p_scored.append((sc, c))
            p_scored.sort(key=lambda x: x[0], reverse=True)

            for sc, c in p_scored:
                did = try_resolve_from_watch_link(
                    c["href"],
                    combined_ep,
                    expected_ani_id=preq.get("anilist_id"),
                    expected_mal_id=preq.get("mal_id")
                )
                if did:
                    print(f"🎬 [Anikoto Prequel Continuation] Resolved '{target_titles[0]}' EP {ep_num} via Prequel '{preq_titles[0]}' EP {combined_ep} -> data-id {did}", flush=True)
                    return did

        # 3. Split-Cour / Base-Season Continuation match via ani.zip TVDB mapping & embed_id jump detection
        # IMPORTANT: Only run Split-Cour fallback when the target explicitly has a Part / Cour suffix
        # or verified ani.zip episode mappings! Never strip "Season 2" down to "Season 1" and grab a random Episode 12+N!
        has_explicit_part = any(
            bool(re.search(r'\b(?:part|cour)\s*\d+\b|\b\d+(?:st|nd|rd|th)\s*(?:part|cour)\b', str(tt), re.IGNORECASE))
            for tt in target_titles
        )
        base_titles = []
        if id_map.get("base_season_title") and (has_explicit_part or id_map.get("anizip_episodes")):
            base_titles.append(id_map["base_season_title"])
        for tt in target_titles:
            stripped = re.sub(r'\b(?:part|cour)\s*\d+\b', '', str(tt), flags=re.IGNORECASE)
            stripped = re.sub(r'\b\d+(?:st|nd|rd|th)\s*(?:part|cour)\b', '', stripped, flags=re.IGNORECASE)
            stripped = re.sub(r'\s*\(\d{4}\)\s*', ' ', stripped)
            stripped = re.sub(r'[:\-–—\s]+$', '', stripped)
            stripped = re.sub(r'\s+', ' ', stripped).strip()
            if stripped and stripped.lower() != str(tt).strip().lower() and stripped not in base_titles:
                base_titles.append(stripped)

        if base_titles:
            base_cands = list(all_cands)
            seen_bh = {bc["href"] for bc in base_cands}
            for ec in search_anikoto_candidates(base_titles[0]):
                if ec["href"] not in seen_bh:
                    seen_bh.add(ec["href"])
                    base_cands.append(ec)

            base_scored = []
            for c in base_cands:
                sc = _score_anikoto_candidate(c["en_title"], c["jp_title"], c["year"], base_titles, None)
                if sc > 0:
                    base_scored.append((sc, c))
            base_scored.sort(key=lambda x: x[0], reverse=True)

            az_ep_info = (id_map.get("anizip_episodes") or {}).get(str(ep_num)) or {}
            az_ep_title = str((az_ep_info.get("title") or "").strip() if isinstance(az_ep_info.get("title"), str) else ((az_ep_info.get("title") or {}).get("en") or "")).strip().lower()
            az_season_ep_num = az_ep_info.get("episodeNumber")

            for sc, c in base_scored:
                wr = s.get(c["href"], timeout=10)
                dids = re.findall(r'data-id=[\'"](\d+)[\'"]', wr.text)
                if not dids:
                    continue
                series_id = dids[0]
                api_res = s.get(f"https://anikotoapi.site/series/{series_id}", timeout=10)
                if api_res.status_code != 200:
                    continue
                api_json = api_res.json().get("data", {})
                episodes = api_json.get("episodes") or []
                if not episodes:
                    continue

                matched_ep = None
                if az_ep_title:
                    matched_ep = next((e for e in episodes if str(e.get("title") or "").strip().lower() == az_ep_title), None)
                if not matched_ep and az_season_ep_num and int(az_season_ep_num) > int(ep_num):
                    matched_ep = next((e for e in episodes if int(e.get("number", 0)) == int(az_season_ep_num)), None)
                if not matched_ep and has_explicit_part and len(episodes) >= 12 + int(ep_num):
                    target_comb_num = 12 + int(ep_num)
                    matched_ep = next((e for e in episodes if int(e.get("number", 0)) == target_comb_num), None)

                if matched_ep:
                    embed_id = matched_ep.get("episode_embed_id")
                    embed_url = (matched_ep.get("embed_url") or {}).get("sub") or f"https://megaplay.buzz/stream/s-2/{embed_id}/sub"
                    mr = s.get(embed_url, headers={"Referer": "https://megaplay.buzz/"}, timeout=10)
                    m_ids = re.findall(r'data-id=[\'"](\d+)[\'"]', mr.text)
                    if m_ids:
                        print(f"🎬 [Anikoto Base-Season Split-Cour] Resolved '{target_titles[0]}' EP {ep_num} -> Series {series_id} EP {matched_ep.get('number')} (data-id: {m_ids[0]})", flush=True)
                        return m_ids[0]

    except Exception as e:
        print(f"⚠️ [Anikoto Fallback] Error: {e}", flush=True)
    return None

def fetch_megaplay_english_sub(anime_id, ep_num, title=None):
    """Fetches English .vtt subtitle URL directly from MegaPlay using AniList ID, MAL ID, Prequel Continuation, and Anikoto fallbacks"""
    candidate_urls = []
    seen = set()

    def add_cand(url):
        if url not in seen:
            seen.add(url)
            candidate_urls.append(url)

    # 1. Primary input ID endpoints
    add_cand(f"https://megaplay.buzz/stream/ani/{anime_id}/{ep_num}/sub")
    add_cand(f"https://megaplay.buzz/stream/mal/{anime_id}/{ep_num}/sub")

    # 2. Resolved MAL & AniList IDs + Prequel metadata
    id_map = {}
    try:
        id_map = get_fallback_anime_ids(anime_id)
        mal_id = id_map.get("mal_id")
        ani_id = id_map.get("anilist_id")
        if not title and id_map.get("title"):
            title = id_map.get("title")
        if mal_id and str(mal_id) != str(anime_id):
            add_cand(f"https://megaplay.buzz/stream/mal/{mal_id}/{ep_num}/sub")
            add_cand(f"https://megaplay.buzz/stream/ani/{mal_id}/{ep_num}/sub")
        if ani_id and str(ani_id) != str(anime_id):
            add_cand(f"https://megaplay.buzz/stream/ani/{ani_id}/{ep_num}/sub")
            add_cand(f"https://megaplay.buzz/stream/mal/{ani_id}/{ep_num}/sub")
    except Exception:
        pass

    # 3. Dub endpoints as secondary fallbacks for direct IDs
    add_cand(f"https://megaplay.buzz/stream/ani/{anime_id}/{ep_num}/dub")
    add_cand(f"https://megaplay.buzz/stream/mal/{anime_id}/{ep_num}/dub")

    # 4. Split-Cour / Part 2 Prequel Continuation endpoints on MegaPlay (e.g. AOT S3 Part 2 Ep 1..10 = AOT S3 Ep 13..22)
    for preq in (id_map.get("prequels") or []):
        comb_ep = int(preq.get("episodes_offset", 0)) + int(ep_num)
        p_ani = preq.get("anilist_id")
        p_mal = preq.get("mal_id")
        if p_mal:
            add_cand(f"https://megaplay.buzz/stream/mal/{p_mal}/{comb_ep}/sub")
        if p_ani:
            add_cand(f"https://megaplay.buzz/stream/ani/{p_ani}/{comb_ep}/sub")
        if p_mal:
            add_cand(f"https://megaplay.buzz/stream/mal/{p_mal}/{comb_ep}/dub")
        if p_ani:
            add_cand(f"https://megaplay.buzz/stream/ani/{p_ani}/{comb_ep}/dub")

    headers = {
        "Referer": "https://megaplay.buzz/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }

    def verify_get_sources(did):
        """Helper to test if a MegaPlay data-id actually produces valid sources"""
        if not did:
            return None
        sources_url = f"https://megaplay.buzz/stream/getSources?id={did}"
        api_headers = {
            "Referer": "https://megaplay.buzz/",
            "Origin": "https://megaplay.buzz",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        }
        for _ in range(2):
            try:
                resp = requests.get(sources_url, headers=api_headers, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("tracks") or data.get("sources"):
                        return data
                time.sleep(0.3)
            except Exception:
                time.sleep(0.3)
        return None

    sources_data = None
    data_id = None

    # Step 1: Try candidate URLs (Direct AniList & MAL, then Prequel Continuation AniList & MAL)
    for embed_url in candidate_urls:
        try:
            resp = requests.get(embed_url, headers=headers, timeout=10)
            if resp.status_code == 200:
                match = re.search(r'data-id=["\'](\d+)["\']', resp.text)
                if match:
                    candidate_did = match.group(1)
                    # Verify sources work! If MegaPlay has broken/404 data-id, do NOT stop here!
                    verified_data = verify_get_sources(candidate_did)
                    if verified_data:
                        data_id = candidate_did
                        sources_data = verified_data
                        print(f"✅ Resolved MegaPlay data-id {data_id} via {embed_url}", flush=True)
                        break
                    else:
                        print(f"⚠️ Embed {embed_url} returned data-id {candidate_did}, but getSources failed/404. Falling back...", flush=True)
        except Exception:
            continue

    # Step 2: Verified Anikoto Fallback if AniList/MAL failed or their getSources returned 404/broken
    if not sources_data and (title or id_map.get("titles")):
        print(f"🔄 Triggering Verified Anikoto Fallback for '{title or anime_id}' EP {ep_num}...", flush=True)
        anikoto_did = resolve_megaplay_via_anikoto(title, ep_num, anime_id=anime_id, id_map=id_map)
        if anikoto_did:
            verified_data = verify_get_sources(anikoto_did)
            if verified_data:
                data_id = anikoto_did
                sources_data = verified_data

    if not data_id or not sources_data:
        return None, "MegaPlay stream data-id not found or getSources returned 404 across AniList, MAL, and Anikoto"

    tracks = sources_data.get("tracks", [])

    # Filter only subtitle/caption tracks (exclude "thumbnails" tracks)
    sub_tracks = [
        t for t in tracks
        if t.get("file") and (t.get("kind") or "captions").lower() in ("captions", "subtitles", "")
        and "thumbnail" not in (t.get("kind") or "").lower()
        and "sprite" not in (t.get("file") or "").lower()
    ]

    def score_english_track(t):
        label = (t.get("label") or "").strip().lower()
        file_url = (t.get("file") or "").strip().lower()
        is_default = bool(t.get("default"))

        is_eng = ("english" in label) or (label in ("en", "eng")) or ("eng" in file_url)
        if not is_eng:
            return -100  # Not an English track

        # Penalize Forced / Signs & Songs tracks heavily (they only contain sign/title translations, not full dialogue!)
        is_forced = any(w in label for w in ("forced", "sign", "song", "caption")) or "forced" in file_url
        if is_forced:
            return 10

        # Exact "English" (full original subtitle track) gets highest priority!
        if label in ("english", "en", "eng", "english (us)", "english [full]", "english (full)"):
            return 100 + (5 if is_default else 0)

        # Secondary full English tracks: English (SDH), English (CC)
        if "sdh" in label or "cc" in label:
            return 85 + (5 if is_default else 0)

        # English (Dubtitle)
        if "dubtitle" in label or "dub" in label:
            return 75 + (5 if is_default else 0)

        # Any other non-forced English track
        return 70 + (5 if is_default else 0)

    scored_tracks = []
    for t in sub_tracks:
        s = score_english_track(t)
        if s > 0:
            scored_tracks.append((s, t))

    scored_tracks.sort(key=lambda x: x[0], reverse=True)

    eng_track_url = None
    selected_label = None
    if scored_tracks:
        best_track = scored_tracks[0][1]
        eng_track_url = best_track.get("file")
        selected_label = best_track.get("label") or "English"
        print(f"🎯 Selected English Subtitle Track: '{selected_label}' (Score: {scored_tracks[0][0]})", flush=True)
    elif sub_tracks:
        eng_track_url = sub_tracks[0].get("file")
        selected_label = sub_tracks[0].get("label") or "Fallback Track"
        print(f"⚠️ No explicit English label found, falling back to first subtitle track: '{selected_label}'", flush=True)

    if not eng_track_url:
        return None, "No subtitle track available on MegaPlay"

    def download_vtt_with_mirror_failover(target_url):
        """
        Downloads a MegaPlay .vtt subtitle with automatic CDN domain healing.
        Polls https://megaplay.buzz/lib/check_domain.json to discover MegaPlay's live
        healthy fallback mirror (e.g. xdw5v.qeltrix.top) and bypasses dead hosts like 4gmkc.mysubsplay.com.
        """
        import urllib.parse
        mirrors = ["https://xdw5v.qeltrix.top", "https://fetch.nexabloom.top"]
        failed_hosts = {"4gmkc.mysubsplay.com"}

        try:
            cd_res = requests.get(
                "https://megaplay.buzz/lib/check_domain.json",
                headers={"Referer": "https://megaplay.buzz/", "User-Agent": headers["User-Agent"]},
                timeout=5
            )
            if cd_res.status_code == 200:
                cd_data = cd_res.json()
                fb = (cd_data.get("fallback") or "").strip().rstrip("/")
                if fb:
                    fb_origin = fb if fb.startswith("http") else f"https://{fb}"
                    if fb_origin not in mirrors:
                        mirrors.insert(0, fb_origin)
                for fh in (cd_data.get("failed") or []):
                    if fh:
                        failed_hosts.add(str(fh).strip().lower())
        except Exception:
            pass

        candidate_sub_urls = []
        try:
            parsed = urllib.parse.urlparse(target_url)
            host = (parsed.hostname or "").lower()
            path_and_query = parsed.path + (f"?{parsed.query}" if parsed.query else "")

            # If original host is known dead, prioritize healthy mirrors first
            if host in failed_hosts and parsed.path.startswith("/anime/"):
                for m in mirrors:
                    u = f"{m}{path_and_query}"
                    if u not in candidate_sub_urls:
                        candidate_sub_urls.append(u)
                candidate_sub_urls.append(target_url)
            else:
                candidate_sub_urls.append(target_url)
                if parsed.path.startswith("/anime/"):
                    for m in mirrors:
                        u = f"{m}{path_and_query}"
                        if u not in candidate_sub_urls:
                            candidate_sub_urls.append(u)
        except Exception:
            candidate_sub_urls = [target_url]

        last_status = "Connection Error"
        for cand_u in candidate_sub_urls:
            try:
                r = requests.get(cand_u, headers=headers, timeout=15)
                last_status = r.status_code
                if r.status_code == 200 and ("WEBVTT" in r.text or "-->" in r.text):
                    if cand_u != target_url:
                        print(f"🛡️ [Auto-Heal] Downloaded subtitle via healthy mirror: {cand_u}", flush=True)
                    return r.text, None
            except Exception as ex:
                last_status = str(ex)
        return None, f"Failed downloading VTT file across all mirrors (last status: {last_status})"

    vtt_text, dl_err = download_vtt_with_mirror_failover(eng_track_url)
    if not vtt_text:
        return None, dl_err

    # Safety fallback: if multiple English tracks exist and the chosen track has very few cues (< 35 lines, e.g. unlabelled forced track),
    # check the next best English track to see if it has the full dialogue!
    if len(scored_tracks) > 1 and vtt_text.count("-->") < 35:
        for alt_score, alt_track in scored_tracks[1:]:
            alt_url = alt_track.get("file")
            if not alt_url or alt_url == eng_track_url:
                continue
            try:
                alt_text, _ = download_vtt_with_mirror_failover(alt_url)
                if alt_text and alt_text.count("-->") > vtt_text.count("-->") * 2:
                    print(f"🔄 Switched from short track '{selected_label}' ({vtt_text.count('-->')} cues) to full track '{alt_track.get('label')}' ({alt_text.count('-->')} cues)!", flush=True)
                    vtt_text = alt_text
                    break
            except Exception:
                pass

    return vtt_text, None

def parse_vtt_cues(vtt_text):
    """Extracts (timecode, text) cues from WebVTT"""
    lines = vtt_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues = []
    current_time = None
    current_lines = []

    for line in lines:
        if "-->" in line:
            if current_time and current_lines:
                cues.append({"time": current_time, "text": " ".join(current_lines)})
            current_time = line
            current_lines = []
        elif current_time:
            if line.strip() == "":
                cues.append({"time": current_time, "text": " ".join(current_lines)})
                current_time = None
                current_lines = []
            else:
                current_lines.append(line.strip())

    if current_time and current_lines:
        cues.append({"time": current_time, "text": " ".join(current_lines)})

    return cues

ANIME_ENGLISH_GLOSSARY = [
    (re.compile(r'\bInter[\s-]*High\b', re.IGNORECASE), 'Inter-High tournament'),
    (re.compile(r'\bNationals\b', re.IGNORECASE), 'National tournament'),
    (re.compile(r'\bPrelims\b', re.IGNORECASE), 'Preliminary rounds'),
    (re.compile(r'\bhigh\s+school\s+student(s?)\b', re.IGNORECASE), r'High-School student\1'),
    (re.compile(r'\bhigh\s+school\b', re.IGNORECASE), 'High-School'),
    (re.compile(r'\bmiddle\s+school\b', re.IGNORECASE), 'Middle-School'),
    (re.compile(r'\bhomeroom\b', re.IGNORECASE), 'class period'),
    (re.compile(r'\bcram\s+school\b', re.IGNORECASE), 'tuition class'),
    (re.compile(r'\bstudent\s+council\b', re.IGNORECASE), 'Student Council'),
    (re.compile(r'\bclass\s+rep\b', re.IGNORECASE), 'class monitor'),
    (re.compile(r'\bI\s+see\s*\.\b', re.IGNORECASE), 'I understand.'),
    (re.compile(r'^I\s+see[!?.]*$', re.IGNORECASE), 'Oh, I understand.'),
    (re.compile(r'\bWhat\'?s\s+the\s+matter\s*\?', re.IGNORECASE), 'What is the problem?'),
    (re.compile(r'\bIt\s+can\'?t\s+be\s+helped\b', re.IGNORECASE), 'Nothing can be done about it'),
    (re.compile(r'\bcan\'?t\s+be\s+helped\b', re.IGNORECASE), 'cannot be avoided'),
    (re.compile(r'\bDon\'?t\s+play\s+dumb\b', re.IGNORECASE), 'Do not pretend you do not know'),
    (re.compile(r'\bGive\s+me\s+a\s+break\b', re.IGNORECASE), 'Stop joking around'),
    (re.compile(r'\bNo\s+way\s*([!?.])', re.IGNORECASE), r'That is impossible\1'),
    (re.compile(r'\bAre\s+you\s+kidding\s+me\b', re.IGNORECASE), 'Are you joking with me'),
    (re.compile(r'\bhang\s+up\b', re.IGNORECASE), 'end the phone call'),
    (re.compile(r'\bhung\s+up\b', re.IGNORECASE), 'ended the phone call'),
]

def _preprocess_anime_english(text):
    res = str(text)
    for pat, repl in ANIME_ENGLISH_GLOSSARY:
        res = pat.sub(repl, res)
    return res

def _is_primarily_ascii_english(text):
    letters = re.findall(r'[^\W\d_]', str(text), re.UNICODE)
    if not letters:
        return True
    ascii_letters = [ch for ch in letters if 'a' <= ch.lower() <= 'z']
    return (len(ascii_letters) / len(letters)) >= 0.85

def translate_guaranteed_sinhala(text):
    """
    High-speed universal multi-language translation pipeline:
    1. If text is already in Sinhala -> return with Spoken Sinhala dictionary.
    2. If text is already English -> translate directly (sl='en', tl='si') with anime glossary normalization.
    3. Any non-English input language (Japanese, Russian, French, Spanish, German, Chinese, Arabic, etc.)
       is detected and translated to English first (sl='auto', tl='en') for maximum quality, then to Sinhala.
    4. Spoken Sinhala dictionary is applied for authentic, natural phrasing.
    """
    if not text or not str(text).strip():
        return ""
    
    # Strip HTML/formatting tags like <i>, </b>, etc.
    clean_text = re.sub(r'<[^>]+>', '', str(text)).strip()
    # If text contains no letters across any language (e.g. only numbers, punctuation, timestamps, music symbols)
    if not clean_text or not re.search(r'[^\W\d_]', clean_text, re.UNICODE):
        return text

    if has_sinhala_characters(clean_text):
        return apply_spoken_sinhala(clean_text)

    url = "https://clients5.google.com/translate_a/t"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    en_text = _preprocess_anime_english(clean_text)
    detected_lang = "en"

    # Step 1: Only run auto -> en if text contains non-ASCII / non-English characters
    if not _is_primarily_ascii_english(clean_text):
        for _ in range(2):
            try:
                resp = requests.get(url, params={"client": "dict-chrome-ex", "sl": "auto", "tl": "en", "q": clean_text}, headers=headers, timeout=6)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        first = data[0]
                        if isinstance(first, list):
                            en_text = _preprocess_anime_english(str(first[0]))
                            if len(first) > 1 and first[1]:
                                detected_lang = str(first[1]).lower()
                        else:
                            en_text = _preprocess_anime_english(str(first))
                        break
            except Exception:
                time.sleep(0.3)

    if detected_lang == "si" or has_sinhala_characters(en_text):
        return apply_spoken_sinhala(en_text)

    # Step 2: Translate English text to Sinhala (en -> si)
    for _ in range(3):
        try:
            resp = requests.get(url, params={"client": "dict-chrome-ex", "sl": "en", "tl": "si", "q": en_text}, headers=headers, timeout=6)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    si_text = str(data[0][0]) if isinstance(data[0], list) else str(data[0])
                    if si_text and has_sinhala_characters(si_text):
                        return apply_spoken_sinhala(si_text)
        except Exception:
            time.sleep(0.3)

    # Strategy 3: GoogleTranslator fallback (deep_translator)
    try:
        from deep_translator import GoogleTranslator
        trans = GoogleTranslator(source="auto", target="si").translate(en_text or clean_text)
        if trans and has_sinhala_characters(trans):
            return apply_spoken_sinhala(trans)
    except Exception:
        pass

    return clean_text

def _translate_contextual_group(texts_list):
    """
    Translates a group of consecutive subtitle cues that form a single split sentence
    together in one request so Google Translate has full sentence context, then splits back!
    """
    if not texts_list:
        return []
    if len(texts_list) == 1:
        return [translate_guaranteed_sinhala(texts_list[0])]

    cleaned = [_preprocess_anime_english(re.sub(r'<[^>]+>', '', str(t)).strip()) for t in texts_list]
    joined_query = "\n".join(cleaned)
    url = "https://clients5.google.com/translate_a/t"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    for _ in range(3):
        try:
            resp = requests.get(url, params={"client": "dict-chrome-ex", "sl": "en", "tl": "si", "q": joined_query}, headers=headers, timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    si_block = str(data[0][0]) if isinstance(data[0], list) else str(data[0])
                    parts = [p.strip() for p in si_block.split("\n") if p.strip()]
                    if len(parts) == len(texts_list) and all(has_sinhala_characters(p) for p in parts):
                        return [apply_spoken_sinhala(p) for p in parts]
        except Exception:
            time.sleep(0.3)

    return [translate_guaranteed_sinhala(t) for t in texts_list]

def process_and_translate_vtt(vtt_content, max_threads=20):
    """Context-aware multi-threaded VTT translation pipeline (Fast, Complete, and 100% Guaranteed Sinhala)"""
    cues = parse_vtt_cues(vtt_content)
    if not cues:
        return None

    # Group consecutive cues that belong to a split sentence (e.g. cue ends with ',' or '...' or lowercase word,
    # or next cue starts with lowercase/conjunction) up to 3 cues per group for natural sentence context!
    cue_groups = []
    current_group = []
    for idx, c in enumerate(cues):
        txt = re.sub(r'<[^>]+>', '', c["text"]).strip()
        if not txt:
            if current_group:
                cue_groups.append(current_group)
                current_group = []
            continue

        current_group.append(idx)
        is_continuation = (
            txt.endswith((",", "...", "—", "-"))
            or (txt[-1].isalnum())
        )
        next_starts_cont = False
        if idx + 1 < len(cues):
            nxt_txt = re.sub(r'<[^>]+>', '', cues[idx + 1]["text"]).strip()
            if nxt_txt and (nxt_txt[0].islower() or nxt_txt.startswith(("...", "and ", "but ", "or ", "so ", "because ", "since "))):
                next_starts_cont = True

        if len(current_group) >= 3 or not (is_continuation or next_starts_cont):
            cue_groups.append(current_group)
            current_group = []

    if current_group:
        cue_groups.append(current_group)

    translated_by_idx = {}
    completed_groups = 0
    total_groups = len(cue_groups)
    print(f"📊 Translating {len(cues)} subtitle cues across {total_groups} contextual groups (using {max_threads} parallel threads)", flush=True)

    def worker_group(grp_indices):
        grp_texts = [cues[i]["text"] for i in grp_indices]
        trans_list = _translate_contextual_group(grp_texts)
        return grp_indices, trans_list

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_threads) as executor:
        future_to_grp = {executor.submit(worker_group, g): g for g in cue_groups}
        for future in concurrent.futures.as_completed(future_to_grp):
            grp = future_to_grp[future]
            try:
                g_idx, trans_list = future.result()
                for i, tr in zip(g_idx, trans_list):
                    translated_by_idx[i] = tr
            except Exception:
                for i in grp:
                    translated_by_idx[i] = cues[i]["text"]
            completed_groups += 1
            if completed_groups % 40 == 0 or completed_groups == total_groups:
                pct = int((completed_groups / max(1, total_groups)) * 100)
                print(f"   ⏳ Translated {completed_groups}/{total_groups} context groups ({pct}%)...", flush=True)

    # Reassemble WebVTT with verified Sinhala text
    out_lines = ["WEBVTT", "Kind: captions", "Language: si", ""]
    cue_idx = 1
    for i, cue in enumerate(cues):
        si_text = translated_by_idx.get(i, cue["text"])
        out_lines.append(str(cue_idx))
        out_lines.append(cue["time"])
        out_lines.append(si_text)
        out_lines.append("")
        cue_idx += 1

    return "\n".join(out_lines)

def sanitize_filename_slug(name):
    """Sanitizes anime title for clean GitHub Release tags and filenames"""
    if not name:
        return ""
    # Replace non-alphanumeric with hyphen
    slug = re.sub(r'[^a-zA-Z0-9]+', '-', str(name).strip()).strip('-').lower()
    return slug[:50].strip('-')

def ensure_github_repository(github_repo, github_token):
    """Ensures GitHub repository exists. If not, automatically creates it under user account."""
    if not github_repo or "/" not in github_repo:
        return github_repo

    owner, repo_name = github_repo.split("/", 1)
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "MegaPlaySubBot/1.0"
    }

    check_url = f"https://api.github.com/repos/{github_repo}"
    resp = requests.get(check_url, headers=headers, timeout=10)
    if resp.status_code == 200:
        return github_repo

    # If repo doesn't exist (HTTP 404), create it automatically
    if resp.status_code == 404:
        print(f"📦 Repository '{github_repo}' not found. Automatically creating it...", flush=True)
        create_url = "https://api.github.com/user/repos"
        c_resp = requests.post(create_url, headers=headers, json={
            "name": repo_name,
            "description": "AniShift Subtitle Vault & Cloud Storage",
            "private": False,
            "auto_init": True
        }, timeout=15)
        if c_resp.status_code in (200, 201):
            print(f"✅ Automatically created repository: {github_repo}", flush=True)
            time.sleep(2) # Give GitHub a moment to provision
    # Ensure repo is not empty (GitHub rejects Releases on repos with 0 commits / no default branch)
    commit_check = requests.get(f"https://api.github.com/repos/{github_repo}/commits", headers=headers, timeout=10)
    if commit_check.status_code == 409 or (commit_check.status_code == 404 and resp.status_code == 200):
        print(f"🌱 Initializing empty repository '{github_repo}' with initial commit...", flush=True)
        readme_payload = {
            "message": "Initial commit - AniShift Sub Vault",
            "content": "IyBBbmlTaGlmdCBTdWJ0aXRsZSBWYXVsdA==", # Base64 of "# AniShift Subtitle Vault"
            "branch": "main"
        }
        requests.put(f"https://api.github.com/repos/{github_repo}/contents/README.md", headers=headers, json=readme_payload, timeout=15)
        time.sleep(2)

    return github_repo

def upload_sub_to_github_release(vtt_text, anime_id, ep_num, github_token, github_repo, anime_title="", category="tv", existing_sub_url=None):
    """
    Uploads Sinhala WebVTT as direct asset to a GitHub Release.
    Naming conventions:
    - TV Series Asset: {anime_slug}_ep_{ep_num}_sinhala.vtt
    - Movie Asset: {anime_slug}_sinhala.vtt
    - Release Tag: sub-{anime_slug} (or sub-{anime_slug}-p2 for 500+ batches)
    - Auto-Vault Fallback: Automatically rolls over to next vault repo if limit is reached.
    """
    explicit_tag = None
    explicit_asset_name = None
    if existing_sub_url and "github.com/" in str(existing_sub_url) and "/releases/download/" in str(existing_sub_url):
        try:
            clean_existing = str(existing_sub_url).split("?")[0].strip()
            m_gh = re.search(r'github\.com/([^/]+/[^/]+)/releases/download/([^/]+)/([^/?#]+)', clean_existing)
            if m_gh:
                github_repo = m_gh.group(1)
                explicit_tag = m_gh.group(2)
                explicit_asset_name = m_gh.group(3)
        except Exception:
            pass

    # Ensure target repo exists, auto-create if missing
    github_repo = ensure_github_repository(github_repo, github_token)

    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "MegaPlaySubBot/1.0"
    }

    anime_slug = sanitize_filename_slug(anime_title) or f"ani-{anime_id}"
    ep_int = int(ep_num)
    is_movie = (category == "movie" or (ep_int == 1 and "movie" in anime_slug))

    # Chunk batches by 500 episodes to respect GitHub's 1000 asset per release limit
    chunk_idx = ((ep_int - 1) // 500) + 1 if not is_movie else 1
    part_suffix = f"-p{chunk_idx}" if chunk_idx > 1 else ""
    raw_tag = explicit_tag or f"sub-{anime_slug}{part_suffix}"
    tag_name = re.sub(r'[^a-zA-Z0-9._-]+', '-', str(raw_tag)).strip('-.') or f"sub-ani-{anime_id}"

    rel_url = f"https://api.github.com/repos/{github_repo}/releases/tags/{tag_name}"
    resp = requests.get(rel_url, headers=headers, timeout=15)

    release_id = None
    upload_url_template = None

    if resp.status_code == 200:
        rel_data = resp.json()
        release_id = rel_data["id"]
        upload_url_template = rel_data["upload_url"]
    else:
        # Create Release with clean descriptive anime name
        create_url = f"https://api.github.com/repos/{github_repo}/releases"
        release_display_name = f"{anime_title or anime_slug.capitalize()} Sinhala Subtitles"
        if chunk_idx > 1:
            release_display_name += f" (Part {chunk_idx})"

        release_payload = {
            "tag_name": tag_name,
            "target_commitish": "main",
            "name": release_display_name,
            "body": f"Automated WebVTT Sinhala subtitles for {anime_title or anime_id} (Batch {chunk_idx})",
            "draft": False,
            "prerelease": False
        }

        c_resp = requests.post(create_url, headers=headers, json=release_payload, timeout=15)

        # Self-healing: If tag already exists (422 already_exists) or commitish failed
        if c_resp.status_code == 422:
            resp_txt = c_resp.text
            if "already_exists" in resp_txt:
                print(f"🔄 Release or tag '{tag_name}' already exists in '{github_repo}'. Retrieving existing release...", flush=True)
                # First try fetching via tag again (might have been created concurrently or as a git tag)
                time.sleep(1)
                retry_rel = requests.get(rel_url, headers=headers, timeout=15)
                if retry_rel.status_code == 200:
                    c_resp = retry_rel
                else:
                    # Search through releases list by tag_name
                    list_rel = requests.get(f"https://api.github.com/repos/{github_repo}/releases?per_page=100", headers=headers, timeout=15)
                    if list_rel.status_code == 200:
                        for r_item in list_rel.json():
                            if r_item.get("tag_name") == tag_name:
                                c_resp = requests.Response()
                                c_resp.status_code = 200
                                c_resp._content = json.dumps(r_item).encode('utf-8')
                                break
            else:
                print(f"🌱 GitHub Release validation failed on '{github_repo}'. Checking/committing README.md...", flush=True)
                readme_payload = {
                    "message": "Initial commit - AniShift Sub Vault",
                    "content": "IyBBbmlTaGlmdCBTdWJ0aXRsZSBWYXVsdA==", # Base64 for "# AniShift Subtitle Vault"
                    "branch": "main"
                }
                requests.put(f"https://api.github.com/repos/{github_repo}/contents/README.md", headers=headers, json=readme_payload, timeout=15)
                time.sleep(2)
                c_resp = requests.post(create_url, headers=headers, json=release_payload, timeout=15)

        if c_resp.status_code in (200, 201):
            rel_data = c_resp.json()
            release_id = rel_data["id"]
            upload_url_template = rel_data["upload_url"]
        else:
            raise RuntimeError(f"Failed creating GitHub release: {c_resp.status_code} {c_resp.text}")

    # Asset naming:
    # Series: {anime_slug}_ep_{ep_num}_sinhala.vtt
    # Movie: {anime_slug}_sinhala.vtt (no ep number)
    if explicit_asset_name:
        asset_name = explicit_asset_name
    elif is_movie:
        asset_name = f"{anime_slug}_sinhala.vtt"
    else:
        asset_name = f"{anime_slug}_ep_{ep_num}_sinhala.vtt"

    base_upload_url = upload_url_template.split("{")[0]

    def delete_matching_asset():
        page = 1
        found = None
        while page <= 10:
            assets_url = f"https://api.github.com/repos/{github_repo}/releases/{release_id}/assets?per_page=100&page={page}"
            a_resp = requests.get(assets_url, headers=headers, timeout=15)
            if a_resp.status_code == 200:
                assets_chunk = a_resp.json()
                if not assets_chunk:
                    break
                for asset in assets_chunk:
                    if asset.get("name") == asset_name:
                        found = asset
                        break
                if found or len(assets_chunk) < 100:
                    break
                page += 1
            else:
                break
        if found:
            del_url = f"https://api.github.com/repos/{github_repo}/releases/assets/{found['id']}"
            requests.delete(del_url, headers=headers, timeout=15)
            time.sleep(1.2)
            return True
        return False

    # Delete existing asset if present to allow clean replacement
    was_replaced = delete_matching_asset()

    upload_headers = {
        "Authorization": f"Bearer {github_token}",
        "Content-Type": "text/vtt; charset=utf-8",
        "User-Agent": "MegaPlaySubBot/1.0"
    }

    up_resp = requests.post(
        f"{base_upload_url}?name={asset_name}",
        headers=upload_headers,
        data=vtt_text.encode("utf-8"),
        timeout=30
    )

    if up_resp.status_code == 422 and "already_exists" in up_resp.text:
        delete_matching_asset()
        time.sleep(1.5)
        up_resp = requests.post(
            f"{base_upload_url}?name={asset_name}",
            headers=upload_headers,
            data=vtt_text.encode("utf-8"),
            timeout=30
        )

    if up_resp.status_code in (200, 201):
        asset_data = up_resp.json()
        dl_url = asset_data.get("browser_download_url")
        if was_replaced or existing_sub_url:
            return f"{dl_url}?v={int(time.time())}"
        return dl_url
    elif up_resp.status_code == 422 and "already_exists" in up_resp.text:
        print(f"ℹ️ Asset '{asset_name}' already registered in release {tag_name}, using CDN URL.", flush=True)
        return f"https://github.com/{github_repo}/releases/download/{tag_name}/{asset_name}?v={int(time.time())}"
    else:
        raise RuntimeError(f"Failed uploading asset: {up_resp.status_code} {up_resp.text}")

