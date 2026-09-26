import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request, send_from_directory
import imageio_ffmpeg
import yt_dlp


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024  # JSON request only; media is fetched server-side.

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DOWNLOAD_DIR = DATA_DIR / "downloads"
TEMP_DIR = DATA_DIR / "temp"
DB_PATH = DATA_DIR / "jobs.sqlite3"

for directory in (DATA_DIR, DOWNLOAD_DIR, TEMP_DIR):
    directory.mkdir(parents=True, exist_ok=True)

FILE_EXPIRY_SECONDS = int(os.environ.get("FILE_EXPIRY_SECONDS", 1800))
JOB_EXPIRY_SECONDS = int(os.environ.get("JOB_EXPIRY_SECONDS", 3600))
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", 2))
MAX_URL_LENGTH = 2048
DOWNLOAD_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_DOWNLOADS)

FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()

PLATFORM_HOSTS = {
    "youtube": {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com"},
    "instagram": {"instagram.com", "www.instagram.com"},
    "facebook": {"facebook.com", "www.facebook.com", "m.facebook.com", "fb.watch", "www.fb.watch", "fb.com", "www.fb.com"},
}


def db_connect():
    conn = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    conn = db_connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                media_type TEXT NOT NULL,
                quality TEXT NOT NULL,
                platform TEXT NOT NULL,
                status TEXT NOT NULL,
                progress REAL NOT NULL DEFAULT 0,
                title TEXT NOT NULL DEFAULT '',
                filename TEXT NOT NULL DEFAULT '',
                download_url TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',
                size INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at)")
        # A process restart invalidates active in-memory worker threads. Do not leave jobs stuck forever.
        now = time.time()
        conn.execute("""
            UPDATE jobs
            SET status='Failed', error='Server restarted before the download completed.', updated_at=?
            WHERE status IN ('Starting...', 'Downloading...', 'Download complete. Processing...', 'Converting audio to MP3...', 'Creating MP4...')
        """, (now,))
        conn.commit()
    finally:
        conn.close()


init_db()


def job_row(job_id):
    conn = db_connect()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_job(job_id, **fields):
    if not fields:
        return
    fields["updated_at"] = time.time()
    columns = list(fields.keys())
    values = [fields[c] for c in columns]
    assignments = ", ".join(f"{c}=?" for c in columns)
    conn = db_connect()
    try:
        conn.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*values, job_id))
        conn.commit()
    finally:
        conn.close()


def detect_platform(url):
    try:
        parsed = urlparse(url.strip())
        if parsed.scheme.lower() not in {"http", "https"}:
            return "unknown"
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return "unknown"

    for platform, hosts in PLATFORM_HOSTS.items():
        if host in hosts or any(host.endswith("." + h) for h in hosts if h not in {"youtu.be", "fb.watch", "fb.com"}):
            return platform
    return "unknown"


def validate_public_url(url):
    if not isinstance(url, str):
        return False, "Invalid URL."
    url = url.strip()
    if not url:
        return False, "Please enter a URL."
    if len(url) > MAX_URL_LENGTH:
        return False, "URL is too long."
    if detect_platform(url) == "unknown":
        return False, "Unsupported URL. Please use a public YouTube, Instagram, or Facebook URL."
    return True, ""


def clean_filename(name):
    name = str(name or "download")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name or "download")[:150]


def unique_filename(filename):
    base, extension = os.path.splitext(filename)
    candidate = filename
    counter = 1
    while (DOWNLOAD_DIR / candidate).exists():
        candidate = f"{base}_{counter}{extension}"
        counter += 1
    return candidate


def video_format(quality):
    return {
        "best": "bestvideo+bestaudio/best",
        "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/bestvideo+bestaudio/best",
        "720": "bestvideo[height<=720]+bestaudio/best[height<=720]/bestvideo+bestaudio/best",
        "480": "bestvideo[height<=480]+bestaudio/best[height<=480]/bestvideo+bestaudio/best",
        "360": "bestvideo[height<=360]+bestaudio/best[height<=360]/bestvideo+bestaudio/best",
    }.get(quality, "bestvideo+bestaudio/best")


def progress_hook(job_id):
    last = {"progress": -1, "status": ""}

    def hook(data):
        row = job_row(job_id)
        if not row:
            return
        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes", 0) or 0
            progress = min(round(downloaded * 100 / total, 1), 95) if total else row["progress"]
            text = "Downloading..."
        elif status == "finished":
            progress, text = 95, "Download complete. Processing..."
        else:
            return
        if progress != last["progress"] or text != last["status"]:
            update_job(job_id, progress=progress, status=text)
            last.update(progress=progress, status=text)
    return hook


def run_ffmpeg(args):
    command = [FFMPEG_PATH, "-y", "-hide_banner", "-loglevel", "error", *args]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=900)
    if result.returncode != 0:
        message = (result.stderr or "FFmpeg failed").strip()
        raise RuntimeError(message[-2000:])
    return result


def find_job_file(job_temp_dir, job_id):
    if not job_temp_dir.is_dir():
        return None
    candidates = []
    for path in job_temp_dir.iterdir():
        if not path.is_file() or not path.name.startswith(job_id):
            continue
        if path.suffix in {".part", ".ytdl", ".temp"}:
            continue
        candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def yt_options(output_template, media_type, job_id, quality):
    options = {
        "outtmpl": output_template,
        "noplaylist": True,
        "ffmpeg_location": FFMPEG_PATH,
        "progress_hooks": [progress_hook(job_id)],
        "quiet": True,
        "no_warnings": False,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 1,
        "overwrites": True,
        "restrictfilenames": False,
        # Full YouTube support needs yt-dlp-ejs plus a supported JS runtime.
        "remote_components": ["ejs:github"],
    }
    if media_type == "audio":
        options.update({"format": "bestaudio/best", "writethumbnail": False, "embedthumbnail": False})
    else:
        options.update({"format": video_format(quality), "merge_output_format": "mp4"})
    return options


def download_worker(job_id, url, media_type, quality):
    job_temp_dir = TEMP_DIR / job_id
    job_temp_dir.mkdir(parents=True, exist_ok=True)
    temp_base = job_temp_dir / job_id
    final_file = None

    with DOWNLOAD_SEMAPHORE:
        try:
            platform = detect_platform(url)
            update_job(job_id, platform=platform, status="Downloading...", progress=1)
            output_template = str(temp_base) + ".%(ext)s"
            options = yt_options(output_template, media_type, job_id, quality)

            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=True)

            title = info.get("title") or ("Downloaded audio" if media_type == "audio" else "Downloaded video")
            media_id = clean_filename(info.get("id") or job_id)
            safe_title = clean_filename(title)
            input_file = find_job_file(job_temp_dir, job_id)
            if not input_file:
                raise RuntimeError("Downloaded media file could not be found on the server.")

            extension = ".mp3" if media_type == "audio" else ".mp4"
            final_filename = unique_filename(f"{safe_title}_{media_id}{extension}")
            final_file = DOWNLOAD_DIR / final_filename

            if media_type == "audio":
                update_job(job_id, status="Converting audio to MP3...", progress=97)
                run_ffmpeg(["-i", str(input_file), "-vn", "-c:a", "libmp3lame", "-b:a", "192k", str(final_file)])
            else:
                update_job(job_id, status="Creating MP4...", progress=97)
                run_ffmpeg(["-i", str(input_file), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(final_file)])

            if not final_file.is_file() or final_file.stat().st_size <= 0:
                raise RuntimeError("Final output file was not created correctly.")

            size = final_file.stat().st_size
            update_job(
                job_id,
                progress=100,
                status="Completed",
                title=title,
                filename=final_filename,
                download_url=f"/download/{job_id}/{final_filename}",
                size=size,
            )
        except Exception as exc:
            message = str(exc).strip() or "Download failed."
            if final_file and final_file.exists():
                try:
                    final_file.unlink()
                except OSError:
                    pass
            update_job(job_id, status="Failed", progress=0, error=message[-3000:])
        finally:
            shutil.rmtree(job_temp_dir, ignore_errors=True)


def cleanup_old_files():
    now = time.time()
    for path in DOWNLOAD_DIR.iterdir():
        if path.is_file():
            try:
                if now - path.stat().st_mtime > FILE_EXPIRY_SECONDS:
                    path.unlink()
            except OSError:
                pass
    conn = db_connect()
    try:
        cutoff = now - JOB_EXPIRY_SECONDS
        conn.execute("DELETE FROM jobs WHERE created_at < ?", (cutoff,))
        conn.commit()
    finally:
        conn.close()


def cleanup_loop():
    while True:
        time.sleep(300)
        cleanup_old_files()


threading.Thread(target=cleanup_loop, daemon=True, name="cleanup").start()


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/download")
def start_download():
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    media_type = data.get("type", "video")
    quality = data.get("quality", "best")

    ok, error = validate_public_url(url)
    if not ok:
        return jsonify({"error": error}), 400
    if media_type not in {"video", "audio"}:
        return jsonify({"error": "Invalid download type."}), 400
    if quality not in {"best", "1080", "720", "480", "360"}:
        quality = "best"

    job_id = uuid.uuid4().hex
    platform = detect_platform(url)
    now = time.time()
    conn = db_connect()
    try:
        conn.execute(
            "INSERT INTO jobs (id,url,media_type,quality,platform,status,progress,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (job_id, url, media_type, quality, platform, "Starting...", 0, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    threading.Thread(target=download_worker, args=(job_id, url, media_type, quality), daemon=True, name=f"download-{job_id[:8]}").start()
    return jsonify({"job_id": job_id, "platform": platform}), 202


@app.get("/api/status/<job_id>")
def download_status(job_id):
    job = job_row(job_id)
    if not job:
        return jsonify({"error": "Download job not found or expired."}), 404
    # Do not return the original URL back to the browser.
    job.pop("url", None)
    return jsonify(job)


@app.get("/download/<job_id>/<path:filename>")
def download_file(job_id, filename):
    job = job_row(job_id)
    if not job or job.get("status") != "Completed":
        return jsonify({"error": "Download job not found or not completed."}), 404
    if job.get("filename") != filename:
        return jsonify({"error": "Invalid download file."}), 404
    if Path(filename).name != filename:
        return jsonify({"error": "Invalid filename."}), 400
    filepath = DOWNLOAD_DIR / filename
    if not filepath.is_file():
        return jsonify({"error": "File has expired. Please download it again."}), 404
    mimetype = "audio/mpeg" if filename.lower().endswith(".mp3") else "video/mp4"
    return send_from_directory(str(DOWNLOAD_DIR), filename, as_attachment=True, download_name=filename, mimetype=mimetype, max_age=0)


@app.get("/health")
def health():
    try:
        conn = db_connect()
        conn.execute("SELECT 1").fetchone()
        conn.close()
        return jsonify({"status": "ok", "service": "social-video-downloader"}), 200
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 503


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"error": "Request is too large."}), 413


@app.errorhandler(500)
def server_error(_error):
    return jsonify({"error": "Internal server error. Check the Render logs for details."}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
