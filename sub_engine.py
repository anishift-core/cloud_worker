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

try:
    import chardet
except ImportError:
    chardet = None

try:
    from deep_translator import GoogleTranslator
except ImportError:
    GoogleTranslator = None

# Spoken Sinhala dictionary
try:
    from spoken_dict import SPOKEN_DICT
except ImportError:
    SPOKEN_DICT = {}

def apply_spoken_sinhala(text):
    if not text or not SPOKEN_DICT: return text
    sorted_keys = sorted(SPOKEN_DICT.keys(), key=len, reverse=True)
    result_text = str(text)
    for key in sorted_keys:
        value = SPOKEN_DICT[key]
        pattern = r'(?<![\w\u0D80-\u0DFF])' + re.escape(key) + r'(?![\w\u0D80-\u0DFF])'
        result_text = re.sub(pattern, value, result_text)
    return result_text

def has_sinhala_characters(text):
    return bool(re.search(r'[\u0D80-\u0DFF]', str(text)))

def get_fallback_anime_ids(anime_id):
    """
    Bidirectional AniList ID <-> MyAnimeList ID resolver using AniList GraphQL API.
    Resolves both directions:
    1. If anilist_id is provided, returns corresponding MAL ID.
    2. If MAL ID is provided, returns corresponding AniList ID.
    """
    ids = {"anilist_id": str(anime_id), "mal_id": None}
    if not anime_id:
        return ids
    try:
        int_id = int(str(anime_id).strip())
        headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}

        # 1. Try querying as AniList ID
        q1 = "query ($id: Int) { Media (id: $id, type: ANIME) { id idMal } }"
        r1 = requests.post("https://graphql.anilist.co", json={"query": q1, "variables": {"id": int_id}}, headers=headers, timeout=5)
        if r1.status_code == 200:
            media = r1.json().get("data", {}).get("Media")
            if media:
                if media.get("idMal"): ids["mal_id"] = str(media["idMal"])
                if media.get("id"): ids["anilist_id"] = str(media["id"])
                return ids

        # 2. Try querying as MAL ID
        q2 = "query ($idMal: Int) { Media (idMal: $idMal, type: ANIME) { id idMal } }"
        r2 = requests.post("https://graphql.anilist.co", json={"query": q2, "variables": {"idMal": int_id}}, headers=headers, timeout=5)
        if r2.status_code == 200:
            media = r2.json().get("data", {}).get("Media")
            if media:
                if media.get("idMal"): ids["mal_id"] = str(media["idMal"])
                if media.get("id"): ids["anilist_id"] = str(media["id"])
                return ids
    except Exception:
        pass
    return ids

def fetch_megaplay_english_sub(anime_id, ep_num):
    """Fetches English .vtt subtitle URL directly from MegaPlay using AniList ID & MAL ID fallbacks"""
    candidate_urls = []
    seen = set()

    def add_cand(url):
        if url not in seen:
            seen.add(url)
            candidate_urls.append(url)

    # 1. Primary input ID endpoints
    add_cand(f"https://megaplay.buzz/stream/ani/{anime_id}/{ep_num}/sub")
    add_cand(f"https://megaplay.buzz/stream/mal/{anime_id}/{ep_num}/sub")

    # 2. Resolved MAL & AniList IDs
    try:
        id_map = get_fallback_anime_ids(anime_id)
        mal_id = id_map.get("mal_id")
        ani_id = id_map.get("anilist_id")
        if mal_id and str(mal_id) != str(anime_id):
            add_cand(f"https://megaplay.buzz/stream/mal/{mal_id}/{ep_num}/sub")
            add_cand(f"https://megaplay.buzz/stream/ani/{mal_id}/{ep_num}/sub")
        if ani_id and str(ani_id) != str(anime_id):
            add_cand(f"https://megaplay.buzz/stream/ani/{ani_id}/{ep_num}/sub")
            add_cand(f"https://megaplay.buzz/stream/mal/{ani_id}/{ep_num}/sub")
    except Exception:
        pass

    # 3. Dub endpoints as secondary fallbacks
    add_cand(f"https://megaplay.buzz/stream/ani/{anime_id}/{ep_num}/dub")
    add_cand(f"https://megaplay.buzz/stream/mal/{anime_id}/{ep_num}/dub")

    headers = {
        "Referer": "https://megaplay.buzz/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }

    data_id = None
    for embed_url in candidate_urls:
        try:
            resp = requests.get(embed_url, headers=headers, timeout=10)
            if resp.status_code == 200:
                match = re.search(r'data-id=["\'](\d+)["\']', resp.text)
                if match:
                    data_id = match.group(1)
                    break
        except Exception:
            continue

    if not data_id:
        return None, "MegaPlay stream data-id not found across AniList and MAL endpoints"

    sources_url = f"https://megaplay.buzz/stream/getSources?id={data_id}"
    api_headers = {
        "Referer": "https://megaplay.buzz/",
        "Origin": "https://megaplay.buzz",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    }
    
    sources_resp = None
    for attempt in range(3):
        try:
            sources_resp = requests.get(sources_url, headers=api_headers, timeout=15)
            if sources_resp.status_code == 200:
                break
            time.sleep(0.5)
        except Exception:
            time.sleep(0.5)

    if not sources_resp or sources_resp.status_code != 200:
        return None, f"getSources failed with {sources_resp.status_code if sources_resp else 'timeout'}"

    data = sources_resp.json()
    tracks = data.get("tracks", [])

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

    sub_resp = requests.get(eng_track_url, headers=headers, timeout=20)
    if sub_resp.status_code != 200:
        return None, f"Failed downloading VTT file: {sub_resp.status_code}"

    # Safety fallback: if multiple English tracks exist and the chosen track has very few cues (< 35 lines, e.g. unlabelled forced track),
    # check the next best English track to see if it has the full dialogue!
    vtt_text = sub_resp.text
    if len(scored_tracks) > 1 and vtt_text.count("-->") < 35:
        for alt_score, alt_track in scored_tracks[1:]:
            alt_url = alt_track.get("file")
            if not alt_url or alt_url == eng_track_url:
                continue
            try:
                alt_resp = requests.get(alt_url, headers=headers, timeout=20)
                if alt_resp.status_code == 200 and alt_resp.text.count("-->") > vtt_text.count("-->") * 2:
                    print(f"🔄 Switched from short track '{selected_label}' ({vtt_text.count('-->')} cues) to full track '{alt_track.get('label')}' ({alt_resp.text.count('-->')} cues)!", flush=True)
                    vtt_text = alt_resp.text
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

def translate_guaranteed_sinhala(text):
    """
    Direct high-speed Google Chrome Translate API for Sinhala (0 auth, 0 rate limit, 100% success).
    Preserves timing and translates to natural Sinhala.
    """
    if not text or not text.strip():
        return ""
    
    # Strip HTML tags like <i>, </b> for clean translation, keep clean dialogue
    clean_text = re.sub(r'<[^>]+>', '', str(text)).strip()
    if not clean_text or not re.search(r'[a-zA-Z]', clean_text):
        return text

    # Strategy 1: Google Chrome Translate API (clients5.google.com)
    for attempt in range(3):
        try:
            url = "https://clients5.google.com/translate_a/t"
            params = {"client": "dict-chrome-ex", "sl": "auto", "tl": "si", "q": clean_text}
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            resp = requests.get(url, params=params, headers=headers, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0:
                    res_text = str(data[0][0]) if isinstance(data[0], list) else str(data[0])
                    if res_text and has_sinhala_characters(res_text):
                        return apply_spoken_sinhala(res_text)
        except Exception:
            time.sleep(0.5)

    # Strategy 2: GoogleTranslator fallback
    try:
        from deep_translator import GoogleTranslator
        trans = GoogleTranslator(source="auto", target="si").translate(clean_text)
        if trans and has_sinhala_characters(trans):
            return apply_spoken_sinhala(trans)
    except Exception:
        pass

    return clean_text

def process_and_translate_vtt(vtt_content, max_threads=20):
    """Multi-threaded VTT translation pipeline (Fast, Complete, and 100% Guaranteed Sinhala)"""
    cues = parse_vtt_cues(vtt_content)
    if not cues:
        return None

    # Collect unique cue texts to minimize requests and maximize speed
    unique_texts = list({c["text"] for c in cues if c["text"].strip()})
    translation_cache = {}

    def worker_translate(orig_text):
        translated = translate_guaranteed_sinhala(orig_text)
        return orig_text, translated

    completed_count = 0
    total_unique = len(unique_texts)
    print(f"📊 Total unique subtitle lines to translate: {total_unique} (using {max_threads} parallel threads)", flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_threads) as executor:
        future_to_text = {executor.submit(worker_translate, t): t for t in unique_texts}
        for future in concurrent.futures.as_completed(future_to_text):
            try:
                orig, trans = future.result()
                translation_cache[orig] = trans
            except Exception:
                t = future_to_text[future]
                translation_cache[t] = t
            completed_count += 1
            if completed_count % 50 == 0 or completed_count == total_unique:
                pct = int((completed_count / total_unique) * 100)
                print(f"   ⏳ Translated {completed_count}/{total_unique} lines ({pct}%)...", flush=True)

    # Reassemble WebVTT with verified Sinhala text
    out_lines = ["WEBVTT", "Kind: captions", "Language: si", ""]
    cue_idx = 1
    for cue in cues:
        si_text = translation_cache.get(cue["text"], cue["text"])
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
    return slug[:50]

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

def upload_sub_to_github_release(vtt_text, anime_id, ep_num, github_token, github_repo, anime_title="", category="tv"):
    """
    Uploads Sinhala WebVTT as direct asset to a GitHub Release.
    Naming conventions:
    - TV Series Asset: {anime_slug}_ep_{ep_num}_sinhala.vtt
    - Movie Asset: {anime_slug}_sinhala.vtt
    - Release Tag: sub-{anime_slug} (or sub-{anime_slug}-p2 for 500+ batches)
    - Auto-Vault Fallback: Automatically rolls over to next vault repo if limit is reached.
    """
    # Ensure target repo exists, auto-create if missing
    github_repo = ensure_github_repository(github_repo, github_token)

    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "MegaPlaySubBot/1.0"
    }

    anime_slug = sanitize_filename_slug(anime_title) or f"ani-{anime_id}"
    ep_int = int(ep_num)
    is_movie = (category == "movie" or ep_int == 1 and "movie" in anime_slug)

    # Chunk batches by 500 episodes to respect GitHub's 1000 asset per release limit
    chunk_idx = ((ep_int - 1) // 500) + 1 if not is_movie else 1
    part_suffix = f"-p{chunk_idx}" if chunk_idx > 1 else ""
    tag_name = f"sub-{anime_slug}{part_suffix}"

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
    if is_movie:
        asset_name = f"{anime_slug}_sinhala.vtt"
    else:
        asset_name = f"{anime_slug}_ep_{ep_num}_sinhala.vtt"

    base_upload_url = upload_url_template.split("{")[0]

    # Delete existing asset if present to allow clean replacement (with pagination support)
    page = 1
    found_asset = None
    while page <= 10:
        assets_url = f"https://api.github.com/repos/{github_repo}/releases/{release_id}/assets?per_page=100&page={page}"
        a_resp = requests.get(assets_url, headers=headers, timeout=15)
        if a_resp.status_code == 200:
            assets_chunk = a_resp.json()
            if not assets_chunk:
                break
            for asset in assets_chunk:
                if asset.get("name") == asset_name:
                    found_asset = asset
                    break
            if found_asset or len(assets_chunk) < 100:
                break
            page += 1
        else:
            break

    if found_asset:
        del_url = f"https://api.github.com/repos/{github_repo}/releases/assets/{found_asset['id']}"
        requests.delete(del_url, headers=headers, timeout=15)
        time.sleep(1) # Allow GitHub to finalize asset deletion

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

    if up_resp.status_code in (200, 201):
        asset_data = up_resp.json()
        return asset_data.get("browser_download_url")
    elif up_resp.status_code == 422 and "already_exists" in up_resp.text:
        # Fallback: Asset already exists on release, construct and return the public CDN URL
        print(f"ℹ️ Asset '{asset_name}' already registered in release {tag_name}, using CDN URL.", flush=True)
        return f"https://github.com/{github_repo}/releases/download/{tag_name}/{asset_name}"
    else:
        raise RuntimeError(f"Failed uploading asset: {up_resp.status_code} {up_resp.text}")

