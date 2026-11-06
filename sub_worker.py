"""
GitHub Actions Cloud Subtitle Worker
------------------------------------
Executes inside GitHub Actions runners:
1. Receives job payload (anime_id, ep_num, doc_id, category, episode_path).
2. Fetches MegaPlay English subtitle directly.
3. Translates to Sinhala using megaplay_sub_engine with Spoken Sinhala dictionary.
4. Uploads .vtt to GitHub Releases to get a permanent DDL link.
5. Saves the subtitle link in Firestore: stream_subtitles.sinhala = "<github_vtt_url>".
6. Emits completion signal to RTDB ('megaplay_sub_jobs/completed/{job_id}') for Manager bot.
"""

import os
import sys
import json
import time
import requests
import firebase_admin
from firebase_admin import credentials, firestore, db as rtdb
from dotenv import load_dotenv

from sub_engine import (
    fetch_megaplay_english_sub,
    process_and_translate_vtt,
    upload_sub_to_github_release,
    adjust_vtt_timing,
    convert_srt_to_vtt,
    convert_ass_to_vtt,
    extract_episode_number_from_filename,
    has_sinhala_characters
)

load_dotenv()

FIREBASE_DB_URL = os.getenv("FIREBASE_DB_URL", "https://anishift-core-default-rtdb.firebaseio.com/")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPO = os.getenv("GITHUB_REPO", "").strip()

backup_db = None

def init_firebase():
    global backup_db
    # 1. Primary DB (anishift-core)
    key_path = "anishift-core.json"
    if not os.path.exists(key_path):
        key_path = "serviceAccountKey.json"
    if not os.path.exists(key_path):
        fb_json = os.getenv("FIREBASE_JSON")
        if fb_json:
            with open(key_path, "w", encoding="utf-8") as f:
                f.write(fb_json)
        else:
            raise RuntimeError("Missing anishift-core.json / serviceAccountKey.json / FIREBASE_JSON")

    if not firebase_admin._apps:
        cred = credentials.Certificate(key_path)
        firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
    
    primary_client = firestore.client()

    # 2. Backup DB (anisora-core) if available
    backup_key_path = "anisora-core.json"
    backup_json = os.getenv("BACKUP_FIREBASE_JSON")
    if not os.path.exists(backup_key_path) and backup_json:
        try:
            with open(backup_key_path, "w", encoding="utf-8") as f:
                f.write(backup_json)
        except Exception: pass

    if os.path.exists(backup_key_path):
        try:
            b_cred = credentials.Certificate(backup_key_path)
            firebase_admin.initialize_app(b_cred, name="backup_app")
            backup_db = firestore.client(app=firebase_admin.get_app("backup_app"))
            print("✅ Backup Firestore connected (anisora-core)", flush=True)
        except Exception as e:
            print(f"⚠️ Backup DB init warning: {e}", flush=True)

    return primary_client

def main():
    payload_str = os.getenv("JOB_PAYLOAD")
    if not payload_str:
        print("❌ Missing JOB_PAYLOAD environment variable!", flush=True)
        sys.exit(1)

    payload = json.loads(payload_str)
    job_id = payload.get("job_id")
    anime_id = str(payload.get("anime_id"))
    ep_num = int(payload.get("ep_num", 1))
    episode_path = payload.get("episode_path")
    anime_title = payload.get("title") or payload.get("anime_title") or payload.get("name") or None

    print(f"🎬 [CloudSubWorker] Starting Job {job_id}: Anime {anime_id} EP {ep_num} ({episode_path})", flush=True)

    db = init_firebase()

    # 1. Fetch Subtitle: Either from Custom URL (if provided) or MegaPlay
    custom_sub_url = payload.get("custom_sub_url")
    delay_sec = float(payload.get("delay_sec", 0.0) or 0.0)

    raw_sub_text = None

    if custom_sub_url:
        print(f"📥 Downloading Custom Subtitle from: {custom_sub_url}...", flush=True)
        try:
            import urllib.parse
            parsed_u = urllib.parse.urlparse(custom_sub_url)
            domain = parsed_u.netloc.lower()
            base_domain = '.'.join(domain.split('.')[-2:]) if '.' in domain else domain
            req_headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": f"{parsed_u.scheme}://{base_domain}/"
            }
            resp = requests.get(custom_sub_url, timeout=45, headers=req_headers)
            
            # Auto-resolve tmpfiles.org landing page to direct download link if needed
            if resp.status_code == 200 and ("<!DOCTYPE" in resp.text[:200] or "<html" in resp.text[:200].lower()) and "tmpfiles.org" in custom_sub_url:
                import re as re_mod
                dl_match = re_mod.search(r'href=[\x27\x22](https?://[^\x27\x22]*tmpfiles\.org/dl/[^\x27\x22]+)[\x27\x22]', resp.text)
                if dl_match:
                    resolved_dl = dl_match.group(1)
                    print(f"🔗 Resolved tmpfiles landing page to direct download URL: {resolved_dl}", flush=True)
                    resp = requests.get(resolved_dl, timeout=45, headers=req_headers)

            if resp.status_code == 200:
                # Check if downloaded content is a ZIP archive
                import io, zipfile
                content_bytes = resp.content
                is_zip = False
                try:
                    zip_buf = io.BytesIO(content_bytes)
                    if zipfile.is_zipfile(zip_buf):
                        is_zip = True
                except Exception:
                    is_zip = False

                if is_zip:
                    print(f"📦 Custom URL is a ZIP archive! Extracting subtitle for Episode {ep_num}...", flush=True)
                    with zipfile.ZipFile(zip_buf, "r") as zf:
                        matching_fnames = []
                        for fname in zf.namelist():
                            if fname.endswith("/") or "__MACOSX" in fname:
                                continue
                            fl = fname.lower()
                            if fl.endswith(".vtt") or fl.endswith(".srt") or fl.endswith(".ass") or fl.endswith(".ssa") or fl.endswith(".sub"):
                                extracted_ep = extract_episode_number_from_filename(fname)
                                if extracted_ep == ep_num:
                                    matching_fnames.append(fname)

                        # Prefer English or specific language tracks if multiple exist (e.g. track6_[eng].ass vs track5_und.ass)
                        def score_zip_file(name):
                            nl = name.lower()
                            score = 0
                            if any(k in nl for k in ["eng", "english", "[en]", "_en.", "en-"]):
                                score += 100
                            if "und" not in nl:
                                score += 20
                            if nl.endswith(".ass") or nl.endswith(".ssa"):
                                score += 10
                            return score

                        matching_fnames.sort(key=score_zip_file, reverse=True)

                        if matching_fnames:
                            matched_fname = matching_fnames[0]
                            raw_txt = zf.read(matched_fname).decode("utf-8-sig", errors="replace")
                            mfl = matched_fname.lower()
                            if mfl.endswith(".ass") or mfl.endswith(".ssa") or "[events]" in raw_txt.lower():
                                found_vtt = convert_ass_to_vtt(raw_txt)
                            else:
                                found_vtt = convert_srt_to_vtt(raw_txt)
                            print(f"✅ Found best match in ZIP for Ep {ep_num}: {matched_fname} ({len(found_vtt)} bytes)", flush=True)
                            raw_sub_text = found_vtt
                        else:
                            print(f"⚠️ Episode {ep_num} not found inside ZIP archive!", flush=True)
                else:
                    # Single subtitle file download
                    decoded = content_bytes.decode("utf-8-sig", errors="replace")
                    # Sanity check: Ensure it's actual subtitle content and not an HTML error page!
                    if "<!DOCTYPE" in decoded or "<html" in decoded.lower():
                        print(f"⚠️ Downloaded custom sub URL returned an HTML webpage instead of subtitle text!", flush=True)
                    else:
                        c_lower = custom_sub_url.lower()
                        if c_lower.endswith(".ass") or c_lower.endswith(".ssa") or "[events]" in decoded.lower():
                            raw_sub_text = convert_ass_to_vtt(decoded)
                        else:
                            raw_sub_text = convert_srt_to_vtt(decoded)
                        print(f"✅ Successfully downloaded custom subtitle ({len(raw_sub_text)} bytes)", flush=True)
            else:
                print(f"❌ Failed to download custom sub: HTTP {resp.status_code}", flush=True)
        except Exception as ce:
            print(f"❌ Custom sub download exception: {ce}", flush=True)

    if not raw_sub_text:
        # Fallback to MegaPlay English sub
        print(f"📥 Fetching English Subtitle from MegaPlay...", flush=True)
        eng_vtt, err = fetch_megaplay_english_sub(anime_id, ep_num, title=anime_title)
        if err or not eng_vtt:
            print(f"⚠️ Failed fetching English sub: {err}", flush=True)
            # Notify RTDB of failure
            rtdb.reference(f"megaplay_sub_jobs/completed/{job_id}").set({
                "status": "failed",
                "error": err or "Subtitle not found",
                "anime_id": anime_id,
                "ep_num": ep_num,
                "timestamp": time.time()
            })
            sys.exit(0)
        raw_sub_text = eng_vtt

    # 2. Check if text already has Sinhala characters or needs translation
    if has_sinhala_characters(raw_sub_text):
        print(f"ℹ️ Subtitle is already in Sinhala. Skipping translation.", flush=True)
        sinhala_vtt = raw_sub_text
    else:
        print(f"🌐 Translating English WebVTT to Spoken Sinhala...", flush=True)
        sinhala_vtt = process_and_translate_vtt(raw_sub_text, max_threads=15)
        if not sinhala_vtt:
            print(f"⚠️ Translation returned empty content", flush=True)
            rtdb.reference(f"megaplay_sub_jobs/completed/{job_id}").set({
                "status": "failed",
                "error": "Translation failed",
                "anime_id": anime_id,
                "ep_num": ep_num,
                "timestamp": time.time()
            })
            sys.exit(1)

    # 2.5 Apply timing delay shift if requested
    if abs(delay_sec) > 0.001:
        print(f"⏱️ Applying timing delay adjustment of {delay_sec:+.3f}s to subtitle cues...", flush=True)
        sinhala_vtt = adjust_vtt_timing(sinhala_vtt, delay_sec)

    # 3. Upload to GitHub Releases
    anime_title = payload.get("anime_title") or payload.get("title", "")
    category = payload.get("category", "tv")
    print(f"☁️ Uploading Sinhala .vtt to GitHub Releases ({GITHUB_REPO}) [Title: {anime_title}, Category: {category}]...", flush=True)
    sub_url = upload_sub_to_github_release(
        vtt_text=sinhala_vtt,
        anime_id=anime_id,
        ep_num=ep_num,
        github_token=GITHUB_TOKEN,
        github_repo=GITHUB_REPO,
        anime_title=anime_title,
        category=category
    )
    print(f"✅ Subtitle uploaded successfully: {sub_url}", flush=True)

    # 4. Save in Firestore episode document matching our stream.sinhala_sub structure
    print(f"💾 Updating Firestore document: {episode_path}...", flush=True)
    update_payload = {
        "episode_number": ep_num,
        "status": "uploaded",
        "stream": {
            "sinhala_sub": sub_url
        }
    }
    # Primary DB with retry and fallback
    fs_updated = False
    for attempt in range(1, 4):
        try:
            ep_ref = db.document(episode_path)
            ep_ref.set(update_payload, merge=True)
            fs_updated = True
            break
        except Exception as fe:
            print(f"⚠️ Primary Firestore update attempt {attempt} failed: {fe}", flush=True)
            if "Quota exceeded" in str(fe) or "RESOURCE_EXHAUSTED" in str(fe):
                time.sleep(2 * attempt)
            else:
                break

    if not fs_updated:
        print("ℹ️ Firestore write skipped/deferred (quota limit). Manager will sync once refreshed.", flush=True)

    # Backup DB (anisora-core) if available
    if backup_db:
        try:
            b_ep_ref = backup_db.document(episode_path)
            b_ep_ref.set(update_payload, merge=True)
            print("💾 Mirrored episode update to Backup Firestore", flush=True)
        except Exception as be:
            print(f"⚠️ Backup DB update notice: {be}", flush=True)

    # 5. Emit completed signal to RTDB for Manager and save in RTDB sub status
    rtdb.reference(f"megaplay_sub_jobs/completed/{job_id}").set({
        "status": "success",
        "url": sub_url,
        "fs_updated": fs_updated,
        "anime_id": anime_id,
        "ep_num": ep_num,
        "timestamp": time.time()
    })

    print(f"🎉 Job {job_id} finished 100% successfully!", flush=True)

if __name__ == "__main__":
    main()
