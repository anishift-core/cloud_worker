# 🎬 AniShift Subtitle Bot & Cloud Worker System

A high-performance subtitle automation engine for AniShift.
Automatically extracts MegaPlay English subtitles, accurately translates them to **natural spoken Sinhala (කටවහර)** using Google Neural Translation + Spoken Dictionary, uploads `.vtt` tracks to permanent GitHub Releases, and links them directly to episode documents.

---

## 🏗️ Architecture

```mermaid
graph TD
    A[Manager Bot: jobs/*.json] -->|Read Pending Jobs| B[Manager: sub_manager.py]
    B -->|Check completed signals| C[Firebase Realtime Database]
    B -->|Trigger Job via GITHUB_TOKEN| D[GitHub Actions: sub_worker.yml]
    D -->|1. Fetch English Sub| E[MegaPlay CDN / API]
    D -->|2. Translate to Sinhala| F[Spoken Sinhala Engine: sub_engine.py]
    D -->|3. Upload .vtt| G[GitHub Vault Repo Releases]
    D -->|4. Save Sub Link| H[Firestore: stream.sinhala_sub & status: uploaded]
    D -->|5. Success Signal| C
```

---
