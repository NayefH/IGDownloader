"""Local Instagram photo archive application."""
import re
import base64
import unicodedata
import shutil
import threading
import time
import uuid
import zipfile
from pathlib import Path

import instaloader
from flask import Flask, jsonify, render_template, request, send_file
from instagram_auth import AuthenticationExpired, InvalidSession, InstagramAuth, close_loader, new_loader
from instagram_errors import explain
from requests.exceptions import RequestException
from werkzeug.exceptions import HTTPException

app = Flask(__name__)
app.config.update(MAX_CONTENT_LENGTH=4096, TRUSTED_HOSTS=["127.0.0.1", "localhost"])
ROOT = Path(__file__).resolve().parent / "downloads"
ROOT.mkdir(exist_ok=True)
jobs = {}
lock = threading.RLock()
auth = InstagramAuth()


@app.errorhandler(HTTPException)
def http_error(error):
    if request.path.startswith('/api/'):
        messages = {400: "Invalid request.", 404: "Not found.",
                    413: "Input too long."}
        return jsonify(error=messages.get(error.code, "Request could not be processed.")), error.code
    return error


@app.before_request
def protect_local_requests():
    if request.method == "POST":
        if request.headers.get("Origin") and request.headers["Origin"] != request.host_url.rstrip("/"):
            return jsonify(error="Origin not allowed."), 403
        if not request.is_json:
            return jsonify(error="JSON required."), 415


@app.after_request
def private_responses(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@app.route("/api/auth", methods=["GET", "POST"])
def authentication():
    if not auth.lock.acquire(blocking=False):
        return jsonify(error="Login in progress. Please wait."), 409
    def failure(message, status):
        return jsonify(error=message, **auth.state()), status

    try:
        if request.method == "GET":
            return jsonify(auth.state())
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(error="Invalid request."), 400
        action = data.get("action", "login")
        if action == "logout":
            return jsonify(auth.logout())
        if action == "browser_login":
            username = data.get("username")
            if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username.strip().removeprefix("@")):
                return failure("Enter your own Instagram username.", 400)
            return jsonify(auth.begin_browser(username.strip().removeprefix("@").lower()))
        if auth.browser_cancel is not None:
            return failure("Browser login in progress. Complete or cancel it.", 409)
        if action == "two_factor":
            code = data.get("code", "")
            if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6,8}", code.strip()):
                return jsonify(error="Enter a valid username or verification code."), 400
            return jsonify(auth.two_factor(code.strip()))
        if action != "login":
            return jsonify(error="Unknown action."), 400
        username, password = data.get("username"), data.get("password")
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username.strip().removeprefix("@")):
            return jsonify(error="Enter your Instagram username."), 400
        if not isinstance(password, str) or not password or len(password) > 1024:
            return jsonify(error="Enter your Instagram password."), 400
        return jsonify(auth.login(username.strip().removeprefix("@"), password))
    except instaloader.BadCredentialsException:
        return failure("Incorrect username, password or verification code.", 401)
    except (instaloader.LoginException, instaloader.ConnectionException):
        return failure("Instagram blocked login. Check confirmations in Instagram and retry later.", 401)
    except (AuthenticationExpired, InvalidSession) as error:
        return failure(str(error), 400)
    except RequestException:
        return failure("Cannot reach Instagram. Check your connection.", 502)
    except Exception:
        # Never log upstream exceptions: they may include authentication details.
        return failure("Login failed. Try again.", 502)
    finally:
        auth.lock.release()


def update(job_id, **values):
    with lock:
        jobs[job_id].update(values)


def profile_preview(loader, profile, username):
    preview = {"username": username}
    try:
        url = profile.profile_pic_url
        if not isinstance(url, str):
            return preview
        response = loader.context.get_raw(url)
        try:
            mime = response.headers.get("Content-Type", "").split(";")[0].lower()
            if mime not in ("image/jpeg", "image/png", "image/webp"):
                return preview
            chunks = bytearray()
            for chunk in response.iter_content(16384):
                chunks.extend(chunk)
                if len(chunks) > 1024 * 1024:
                    return preview
            preview["avatar"] = f"data:{mime};base64," + base64.b64encode(chunks).decode("ascii")
        finally:
            response.close()
    except Exception:
        pass  # An unavailable avatar must not prevent media downloads.
    return preview


@app.post("/api/profile")
def find_profile():
    data = request.get_json(silent=True)
    username = data.get("username", "") if isinstance(data, dict) else ""
    if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username.strip().removeprefix("@")):
        return jsonify(error="Enter a valid Instagram username."), 400
    username = username.strip().removeprefix("@")
    loader = None
    try:
        snapshot = auth.snapshot()
        loader = new_loader()
        if snapshot:
            loader.load_session(*snapshot)
        profile = instaloader.Profile.from_username(loader.context, username)
        return jsonify(profile_preview(loader, profile, username))
    except BlockingIOError:
        return jsonify(error="Wait for login to finish."), 409
    except Exception as error:
        return jsonify(error=explain(error)[1]), 502
    finally:
        close_loader(loader)


class DownloadCancelled(Exception):
    pass


def highlight_folder(title, used_names):
    name = unicodedata.normalize("NFC", title or "")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', '_', name).strip().rstrip('. ')
    name = name[:80].rstrip('. ') or "Untitled"
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]", name.split('.')[0], re.I):
        name = '_' + name
    candidate = name
    index = 2
    while candidate.casefold() in used_names:
        candidate = f"{name} ({index})"
        index += 1
    used_names.add(candidate.casefold())
    return candidate


def check_cancelled(job_id):
    with lock:
        if jobs[job_id].get("cancel_requested"):
            raise DownloadCancelled()


def archive_photos(job_id, username, session_snapshot=None, media_type="photos", sources=None):
    sources = sources or ["posts"]
    directory = ROOT / job_id
    loader = None
    phase = 'profile'
    try:
        check_cancelled(job_id)
        directory.mkdir()
        loader = new_loader()
        if session_snapshot is not None:
            login, cookies = session_snapshot
            loader.load_session(login, cookies)
        update(job_id, message="Loading profile...")
        profile = instaloader.Profile.from_username(loader.context, username)
        check_cancelled(job_id)
        update(job_id, profile=profile_preview(loader, profile, username))
        if any(source in sources for source in ("stories", "highlights")) and session_snapshot is None:
            raise ValueError("Sign in to download stories and highlights.")
        own_profile = (loader.context.username or "").casefold() == username.casefold()
        if profile.is_private and not own_profile and not profile.followed_by_viewer:
            raise ValueError("Private profile. Sign in with an account that has access.")
        update(job_id, total=profile.mediacount if sources == ["posts"] else 0, message="Downloading media...")
        phase = 'posts'
        with zipfile.ZipFile(directory / "photos.zip", "w", compression=zipfile.ZIP_STORED) as archive:
            def content():
                if "posts" in sources:
                    for post in profile.get_posts():
                        check_cancelled(job_id)
                        yield "posts", post
                if "stories" in sources:
                    update(job_id, message="Downloading current story...")
                    for story in loader.get_stories(userids=[profile.userid]):
                        check_cancelled(job_id)
                        for item in story.get_items():
                            check_cancelled(job_id)
                            yield "stories", item
                if "highlights" in sources:
                    update(job_id, message="Downloading highlights...")
                    used_names = set()
                    for highlight in loader.get_highlights(profile):
                        check_cancelled(job_id)
                        folder = highlight_folder(highlight.title, used_names)
                        for item in highlight.get_items():
                            check_cancelled(job_id)
                            yield f"highlights/{folder}", item

            for source, post in content():
                check_cancelled(job_id)
                if source == "posts" and post.typename == "GraphSidecar":
                    items = enumerate(post.get_sidecar_nodes(), 1)
                else:
                    items = [(1, post)]
                for index, item in items:
                    check_cancelled(job_id)
                    is_video = item.is_video
                    if (is_video and media_type == "photos") or (not is_video and media_type == "videos"):
                        continue
                    url = item.video_url if is_video else (item.display_url if source == "posts" and post.typename == "GraphSidecar" else item.url)
                    if not url:
                        raise ValueError("Media URL unavailable. Try again later.")
                    phase = 'video' if is_video else 'image'
                    identifier = post.shortcode if source == "posts" else str(post.mediaid)
                    stem = directory / f"{source.split('/')[0]}_{post.date_utc:%Y-%m-%d}_{identifier}_{index:02d}"
                    loader.download_pic(str(stem), url, post.date_utc)
                    check_cancelled(job_id)
                    files = list(directory.glob(stem.name + ".*"))
                    if not files:
                        raise ValueError("Could not save media. Try again.")
                    for photo in files:
                        media_folder = "Videos" if is_video else "Photos"
                        archive.write(photo, f"{username}/{source}/{media_folder}/{photo.name}")
                        photo.unlink()
                        with lock:
                            key = "videos" if is_video else "images"
                            jobs[job_id][key] = jobs[job_id].get(key, 0) + 1
                phase = 'posts'
                with lock:
                    jobs[job_id]["processed"] += 1
        with lock:
            count = jobs[job_id]["images"] + jobs[job_id].get("videos", 0)
        if not count:
            raise ValueError("No matching media available.")
        with lock:
            check_cancelled(job_id)
            update(job_id, status="done", message=f"{count} files ready.")
    except DownloadCancelled:
        shutil.rmtree(directory, ignore_errors=True)
        update(job_id, status="cancelled", message="Download cancelled.")
    except Exception as error:
        if jobs[job_id].get("cancel_requested"):
            shutil.rmtree(directory, ignore_errors=True)
            update(job_id, status="cancelled", message="Download cancelled.")
            return
        if isinstance(error, OSError) and not isinstance(error, RequestException):
            error_code = 'storage_error'
            message = "Could not save files. Check disk space and folder permissions."
        elif isinstance(error, ValueError):
            error_code = 'no_photos' if not (jobs[job_id]['images'] + jobs[job_id].get('videos', 0)) else 'incomplete_download'
            message = str(error)
        else:
            error_code, message = explain(error)
        shutil.rmtree(directory, ignore_errors=True)
        update(job_id, status="error", message=message, error_code=error_code, phase=phase)
    finally:
        close_loader(loader)
        update(job_id, finished=time.time())


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/jobs")
def create_job():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify(error="Enter a valid username or verification code."), 400
    username = data.get("username", "")
    if not isinstance(username, str):
        return jsonify(error="Enter a valid username or verification code."), 400
    username = username.strip().removeprefix("@")
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username):
        return jsonify(error="Enter a username, without a profile URL."), 400
    media_type = data.get("media_type", "photos")
    if not isinstance(media_type, str) or media_type not in ("photos", "videos", "both"):
        return jsonify(error="Select photos, videos or both."), 400
    sources = data.get("sources", ["posts"])
    if (not isinstance(sources, list) or not sources or
            any(not isinstance(source, str) or source not in ("posts", "stories", "highlights") for source in sources)):
        return jsonify(error="Select posts, current story or highlights."), 400
    sources = list(dict.fromkeys(sources))
    try:
        session_snapshot = auth.snapshot()
    except BlockingIOError:
        return jsonify(error="Wait for login to finish."), 409
    if any(source in sources for source in ("stories", "highlights")) and session_snapshot is None:
        return jsonify(error="Sign in to download stories and highlights."), 401
    with lock:
        for key, job in list(jobs.items()):
            if job.get("finished", float("inf")) < time.time() - 3600:
                shutil.rmtree(ROOT / key, ignore_errors=True)
                del jobs[key]
        if any(job["status"] == "running" for job in jobs.values()):
            return jsonify(error="A download is already running."), 409
        job_id = uuid.uuid4().hex
        jobs[job_id] = dict(username=username, status="running", media_type=media_type, sources=sources, images=0, videos=0, processed=0, total=0, message="Preparing download...")
    try:
        threading.Thread(target=archive_photos, args=(job_id, username, session_snapshot, media_type, sources), daemon=True).start()
    except Exception:
        update(job_id, status="error", message="Could not start download.", finished=time.time())
        return jsonify(error="Could not start download. Try again."), 503
    return jsonify(id=job_id), 202


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    with lock:
        job = jobs.get(job_id)
        return (jsonify(dict(job)), 200) if job else (jsonify(error="Download not found."), 404)


@app.post("/api/jobs/<job_id>/cancel")
def cancel_job(job_id):
    with lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify(error="Download not found."), 404
        if job["status"] == "running":
            job.update(cancel_requested=True, message="Cancelling download...")
        return jsonify(dict(job))


@app.get("/api/jobs/<job_id>/download")
def download(job_id):
    with lock:
        job = jobs.get(job_id)
        if not job or job["status"] != "done":
            return jsonify(error="ZIP not ready yet."), 404
        username = job["username"]
        suffix = {"photos": "photos", "videos": "videos", "both": "media"}[job.get("media_type", "photos")]
        try:
            return send_file(ROOT / job_id / "photos.zip", as_attachment=True, download_name=f"{username}_{suffix}.zip", max_age=0)
        except FileNotFoundError:
            return jsonify(error="ZIP expired. Start a new download."), 410


if __name__ == "__main__":
    import argparse
    import webbrowser
    from waitress import create_server

    parser = argparse.ArgumentParser(description="Local Instagram downloader")
    parser.add_argument("--open-browser", action="store_true", help="Open the app after startup")
    parser.add_argument("--port", type=int, default=5000, help="Local port (default: 5000)")
    args = parser.parse_args()
    server = create_server(app, host="127.0.0.1", port=args.port, threads=4)
    url = f"http://127.0.0.1:{server.effective_port}"
    print(f"Instagram Downloader: {url}", flush=True)
    if args.open_browser:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    server.run()
