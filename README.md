SOCIAL VIDEO DOWNLOADER - PRODUCTION-READY RENDER BUILD
========================================================

This version keeps the original Flask + yt-dlp + FFmpeg architecture but fixes the main deployment problems found in the uploaded archive.

WHAT WAS FIXED
--------------
1. Removed invalid Markdown ```html fences from index.html.
2. Fixed the download link to use /download/<job_id>/<filename>.
3. Replaced the malformed requirements.txt entry `image` with imageio-ffmpeg.
4. Added yt-dlp[default], including yt-dlp-ejs for current YouTube support.
5. Added Deno installation for yt-dlp's JavaScript challenge solving.
6. Added SQLite job storage so status survives across Gunicorn threads/workers within the same Render instance.
7. Configured Gunicorn as one worker with multiple threads because download jobs and the SQLite/file store are instance-local.
8. Added Render healthCheckPath and explicit Python 3.13.5.
9. Added stronger URL/host validation instead of substring matching.
10. Added request-size limits, download concurrency limits, FFmpeg timeout, retries, and safer error handling.
11. Prevented the status API from returning the original URL.
12. Added automatic cleanup of expired files/jobs.
13. Added startup recovery so interrupted jobs do not remain stuck forever.

RENDER DEPLOYMENT
-----------------
1. Push the contents of this folder to GitHub. Do not push the original .git folder from the uploaded archive.
2. In Render, create a Web Service from the repository.
3. Render will read render.yaml if the Blueprint/service is configured from it.
4. Build command: bash build.sh
5. Start command: bash start.sh
6. Health check: /health

IMPORTANT ABOUT STORAGE
-----------------------
Render's normal service filesystem is ephemeral. The application stores temporary media under data/downloads and removes it after the configured expiry period. A service restart/deploy can remove these files. For durable downloads, add object storage (for example S3-compatible storage) instead of local files.

IMPORTANT ABOUT FREE HOSTING
-----------------------------
This is still a lightweight downloader service. It is not a multi-node job queue. Keep MAX_CONCURRENT_DOWNLOADS low. For real traffic, move jobs to Redis/Celery/RQ or another external queue and store completed media in object storage.

YOUTUBE / YT-DLP
----------------
yt-dlp changes as the source platforms change. The build installs the current yt-dlp default dependencies and Deno runtime required for modern YouTube challenge solving. Public URLs only; private/login-only content is not supported.

LOCAL WINDOWS
-------------
Python 3.13 is recommended.

py -3.13 -m venv venv
venv\Scripts\activate
python -m pip install -r requirements.txt
python app.py

Open http://127.0.0.1:5000

RESPONSIBLE USE
---------------
Only download media you are authorized to download and follow applicable copyright, privacy, and platform terms.
