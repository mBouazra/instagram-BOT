"""
Publie chaque jour tous les Reels de @myeleganza_ en story, dans le même ordre.

- Une seule fournée par jour (heure réglable dans config.json) : les stories de la veille
  expirent au moment où les nouvelles arrivent, donc tes vidéos sont toujours visibles,
  jamais en double.
- Ordre fixe : du Reel le plus ancien au plus récent (ou l'inverse via config.json).
- Chaque Reel est converti UNE fois au format story (1080x1920) et hébergé par GitHub Pages
  (branche gh-pages). Chaque jour, le bot ne fait plus que publier ces vidéos dans l'ordre.

Variables d'environnement : IG_TOKEN (secret GitHub), IG_USER_ID.
Options : FORCE=1 ignore l'heure et le "déjà posté aujourd'hui", DRY_RUN=1 ne publie rien,
DIAG=1 teste une seule vidéo sans publier.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

API = "https://graph.instagram.com"
ROOT = Path(__file__).parent
STATE_FILE = ROOT / "last_posted.txt"
SITE = ROOT / "site"  # branche gh-pages (vidéos converties, servies par GitHub Pages)
_owner, _, _repo = os.environ.get("GITHUB_REPOSITORY", "mBouazra/instagram-BOT").partition("/")
PAGES_URL = os.environ.get("PAGES_URL") or f"https://{_owner.lower()}.github.io/{_repo}"
WORK = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "stories"
TZ = ZoneInfo("Europe/Paris")
GAP_HOURS = 23  # attendre que la fournée de la veille ait (presque) expiré : pas de doublon

TOKEN = os.environ["IG_TOKEN"]
USER_ID = os.environ.get("IG_USER_ID", "me")
FORCE = os.environ.get("FORCE") == "1"
DRY_RUN = os.environ.get("DRY_RUN") == "1"
DIAG = os.environ.get("DIAG") == "1"
CHECK = os.environ.get("CHECK") == "1"
CONFIG = json.loads((ROOT / "config.json").read_text())


# ---------------------------------------------------------------- API Instagram
def call(method, path, **params):
    """Appel Graph API avec reprise automatique sur limite de débit / erreur temporaire."""
    params["access_token"] = TOKEN
    data = urllib.parse.urlencode(params).encode()
    url = f"{API}/{path}"
    for attempt in range(6):
        req = (urllib.request.Request(f"{url}?{data.decode()}") if method == "GET"
               else urllib.request.Request(url, data=data, method="POST"))
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            try:
                err = json.loads(body).get("error", {})
            except ValueError:
                err = {}
            code = err.get("code")
            transient = e.code >= 500 or code in (1, 2, 4, 17, 32, 613) or err.get("is_transient")
            if transient and attempt < 5:
                wait = 30 * (2 ** attempt)
                print(f"  ⏳ limite/erreur temporaire ({code}), nouvel essai dans {wait}s")
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {e.code}: {err.get('message', body)}") from None
        except urllib.error.URLError as e:
            if attempt < 5:
                time.sleep(15)
                continue
            raise RuntimeError(f"réseau: {e}") from None


def list_reels():
    reels, params = [], {"fields": "id,media_product_type,media_url,timestamp", "limit": 100}
    while True:
        page = call("GET", f"{USER_ID}/media", **params)
        reels += [m for m in page.get("data", []) if m.get("media_product_type") == "REELS"]
        nxt = page.get("paging", {}).get("next")
        if not nxt:
            break
        params["after"] = urllib.parse.parse_qs(urllib.parse.urlparse(nxt).query)["after"][0]
    reels.sort(key=lambda m: m["timestamp"], reverse=CONFIG.get("order") == "newest_first")
    return reels


def wait_container(cid, timeout=300):
    info, deadline = {}, time.time() + timeout
    while time.time() < deadline:
        info = call("GET", cid, fields="status_code,status")
        if info.get("status_code") in ("FINISHED", "ERROR", "EXPIRED"):
            break
        time.sleep(5)
    return info


# ---------------------------------------------------------------- préparation vidéo
def download(url, dest):
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:
                f.write(r.read())
            return dest
        except Exception:
            if attempt == 2:
                raise
            time.sleep(5)


def reencode(src, dest):
    """Format story Instagram : 1080x1920, H.264, AAC 48 kHz, 30 i/s, 59 s max."""
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-t", "59",
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos,"
               "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,fps=30,format=yuv420p",
        "-c:v", "libx264", "-profile:v", "high", "-preset", "fast", "-crf", "20",
        "-maxrate", "8M", "-bufsize", "16M",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", str(dest)], check=True)
    return dest


def video_file(reel):
    return SITE / "v" / f"{reel['id']}.mp4"


def video_url(reel):
    return f"{PAGES_URL}/v/{reel['id']}.mp4"


def convert(item):
    """Télécharge et convertit un Reel dans le dossier du site. Renvoie une erreur ou None."""
    idx, reel = item
    source = CONFIG.get("video_urls", {}).get(reel["id"]) or reel.get("media_url")
    if not source:
        return "pas de fichier fourni par Instagram → ajoute-le dans video_urls"
    try:
        raw = download(source, WORK / f"{idx}_raw.mp4")
        reencode(raw, video_file(reel))
        print(f"  🎬 #{idx} convertie")
        return None
    except Exception as e:
        video_file(reel).unlink(missing_ok=True)
        print(f"  ⚠️  #{idx} conversion échouée : {e}")
        return str(e)


def is_online(url):
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except Exception:
        return False


def publish_site(new_reels):
    """Envoie les nouvelles vidéos sur GitHub Pages et attend qu'elles soient en ligne."""
    git = ["git", "-C", str(SITE)]
    subprocess.run(git + ["add", "v"], check=True)
    subprocess.run(git + ["-c", "user.name=story-bot", "-c",
                          "user.email=story-bot@users.noreply.github.com",
                          "commit", "-q", "-m", f"{len(new_reels)} vidéo(s) convertie(s)"], check=True)
    subprocess.run(git + ["push", "-q"], check=True)
    print(f"🌐 {len(new_reels)} vidéo(s) envoyée(s) sur GitHub Pages, mise en ligne…")
    deadline = time.time() + 900
    pending = list(new_reels)
    while pending and time.time() < deadline:
        time.sleep(20)
        pending = [r for r in pending if not is_online(video_url(r))]
    if pending:
        print(f"  ⚠️  {len(pending)} vidéo(s) pas encore en ligne après 15 min")


def ensure_videos(indexed):
    """Convertit les Reels pas encore hébergés. Renvoie {reel_id: erreur}."""
    todo = [(i, r) for i, r in indexed if not video_file(r).exists()]
    errors = {}
    if todo:
        (SITE / "v").mkdir(parents=True, exist_ok=True)
        print(f"🎬 {len(todo)} vidéo(s) à convertir (une seule fois)…")
        with ThreadPoolExecutor(max_workers=4) as pool:
            for (i, r), err in zip(todo, pool.map(convert, todo)):
                if err:
                    errors[r["id"]] = err
        new = [r for _, r in todo if r["id"] not in errors]
        if new:
            publish_site(new)
    return errors


def still_on_account(reel_id):
    """Vérifie juste avant de publier que le Reel est toujours sur le compte (pas supprimé)."""
    try:
        call("GET", reel_id, fields="id")
        return True
    except RuntimeError as e:
        msg = str(e).lower()
        if "does not exist" in msg or "unsupported get request" in msg or "cannot be loaded" in msg:
            return False
        return True  # autre erreur : dans le doute on publie


def create_container(item):
    idx, reel = item
    try:
        return call("POST", f"{USER_ID}/media", media_type="STORIES",
                    video_url=video_url(reel))["id"], None
    except Exception as e:
        return None, str(e)


# ---------------------------------------------------------------- programme
def main():
    WORK.mkdir(parents=True, exist_ok=True)
    now = datetime.now(TZ)
    today = now.date().isoformat()
    last = STATE_FILE.read_text().strip() if STATE_FILE.exists() else ""
    try:  # date + heure de la dernière fournée (ancien format : date seule)
        last_dt = datetime.fromisoformat(last if "T" in last else last + "T14:00")
        last_dt = last_dt if last_dt.tzinfo else last_dt.replace(tzinfo=TZ)
    except ValueError:
        last_dt = None
    ready_at = last_dt + timedelta(hours=GAP_HOURS) if last_dt else now

    if DIAG:
        reel = next(r for r in list_reels() if r.get("media_url"))
        print(f"🔍 Test sur le Reel {reel['id']} (rien ne sera publié)")
        err = ensure_videos([(0, reel)]).get(reel["id"])
        cid, err = (None, err) if err else create_container((0, reel))
        info = {"status_code": "ERROR", "status": err} if err else wait_container(cid)
        print(f"  → {info}")
        sys.exit(0 if info.get("status_code") == "FINISHED" else 1)

    if not FORCE:
        reason = ""
        if CONFIG.get("paused"):
            reason = "⏸  En pause (config.json → paused: true). Rien à faire."
        elif last_dt and last_dt.date() == now.date():
            reason = f"✅ Déjà publié aujourd'hui ({last_dt:%H:%M}). Rien à faire."
        elif now < ready_at:
            reason = (f"⏳ Les stories d'hier ({last_dt:%d/%m %H:%M}) sont encore en ligne, "
                      f"prochaine fournée après {ready_at:%H:%M}.")
        elif now.hour < CONFIG["post_hour"]:
            reason = f"🕗 Il est {now:%H:%M} à Paris, publication prévue à {CONFIG['post_hour']}h."
        if CHECK:  # vérification rapide avant d'installer ffmpeg
            with open(os.environ["GITHUB_OUTPUT"], "a") as f:
                f.write(f"run={'false' if reason else 'true'}\n")
        if reason:
            print(reason)
            return
    elif CHECK:
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write("run=true\n")
    if CHECK:
        print("▶️  Publication à lancer.")
        return

    skip = set(CONFIG.get("skip", []))
    reels = [r for r in list_reels() if r["id"] not in skip]
    print(f"📦 {len(reels)} Reels à publier (ordre : {CONFIG.get('order', 'oldest_first')})")

    if DRY_RUN:
        missing = [r["id"] for r in reels
                   if not (CONFIG.get("video_urls", {}).get(r["id"]) or r.get("media_url"))]
        print(f"🧪 DRY_RUN : {len(reels) - len(missing)} stories seraient publiées, "
              f"{len(missing)} sans fichier : {missing}")
        return

    # 1) Convertir les nouveaux Reels (une seule fois), puis créer les stories chez Instagram
    indexed = list(enumerate(reels, 1))
    errors = ensure_videos(indexed)
    ready = [(i, r) for i, r in indexed if r["id"] not in errors]
    print(f"📤 Envoi de {len(ready)} stories à Instagram…")
    with ThreadPoolExecutor(max_workers=4) as pool:
        containers = dict(zip([r["id"] for _, r in ready], pool.map(create_container, ready)))

    # 2) Publier dans l'ordre
    posted, failed = 0, 0
    for idx, reel in indexed:
        reel_id = reel["id"]
        err = errors.get(reel_id)
        cid = None
        if not err:
            cid, err = containers[reel_id]
        if err:
            if "pas de fichier" in err:  # Reel avec musique sous licence : simple avertissement
                print(f"  ⚠️  #{idx} {reel_id} ignorée : {err}")
            else:
                print(f"  ❌ #{idx} {reel_id} : {err}")
                failed += 1
            continue
        try:
            info = wait_container(cid)
        except RuntimeError as e:
            info = {"status_code": "ERROR", "status": str(e)}
        if info.get("status_code") != "FINISHED":
            print(f"  ❌ #{idx} {reel_id} : traitement Instagram {info.get('status_code')} "
                  f"{info.get('status', '')}")
            failed += 1
            continue
        if not still_on_account(reel_id):
            print(f"  🗑️  #{idx} {reel_id} : Reel supprimé du compte (article vendu ?), story non publiée")
            continue
        try:
            call("POST", f"{USER_ID}/media_publish", creation_id=cid)
        except RuntimeError as e:
            print(f"  ❌ #{idx} {reel_id} : publication refusée — {e}")
            failed += 1
            continue
        if posted == 0:  # dès la 1re story publiée, on note la date pour ne jamais doubler
            STATE_FILE.write_text(datetime.now(TZ).isoformat(timespec="minutes") + "\n")
        posted += 1
        print(f"  ✅ #{idx} {reel_id} publiée")

    print(f"\n🎉 {posted} stories publiées, {failed} en échec.")
    if failed:
        sys.exit(1)  # le job passe en rouge → GitHub t'envoie un e-mail


if __name__ == "__main__":
    main()
