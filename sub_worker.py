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
    upload_sub_to_github_release
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

    print(f"🎬 [CloudSubWorker] Starting Job {job_id}: Anime {anime_id} EP {ep_num} ({episode_path})", flush=True)

    db = init_firebase()

    # 1. Fetch English Subtitle from MegaPlay
    print(f"📥 Fetching English Subtitle from MegaPlay...", flush=True)
    eng_vtt, err = fetch_megaplay_english_sub(anime_id, ep_num)
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

    # 2. Translate to Sinhala
    print(f"🌐 Translating English WebVTT to Spoken Sinhala...", flush=True)
    sinhala_vtt = process_and_translate_vtt(eng_vtt, max_threads=15)
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
