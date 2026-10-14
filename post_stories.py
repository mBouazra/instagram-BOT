"""
Publie chaque jour tous les Reels de @myeleganza_ en story, dans le même ordre.

- Une seule fournée par jour (heure réglable dans config.json) : les stories de la veille
  expirent au moment où les nouvelles arrivent, donc tes vidéos sont toujours visibles,
  jamais en double.
- Ordre fixe : du Reel le plus ancien au plus récent (ou l'inverse via config.json).
- Pour chaque Reel : téléchargement → conversion au format story (1080x1920) →
  lien public temporaire (1 h) → envoi à Instagram. Puis publication dans l'ordre.

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
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

API = "https://graph.instagram.com"
ROOT = Path(__file__).parent
STATE_FILE = ROOT / "last_posted.txt"
CACHE_FILE = ROOT / "video_cache.json"  # liens des vidéos déjà converties
WORK = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "stories"
TZ = ZoneInfo("Europe/Paris")

TOKEN = os.environ["IG_TOKEN"]
USER_ID = os.environ.get("IG_USER_ID", "me")
FORCE = os.environ.get("FORCE") == "1"
DRY_RUN = os.environ.get("DRY_RUN") == "1"
DIAG = os.environ.get("DIAG") == "1"
CHECK = os.environ.get("CHECK") == "1"
CONFIG = json.loads((ROOT / "config.json").read_text())
CACHE = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}


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


def upload(path, endpoint, extra):
    out = ""
    for attempt in range(3):
        out = subprocess.run(
            ["curl", "-sS", "--max-time", "180", "-F", "reqtype=fileupload", *extra,
             "-F", f"fileToUpload=@{path}", endpoint],
            capture_output=True, text=True).stdout.strip()
        if out.startswith("https://"):
            return out
        time.sleep(10 * (attempt + 1))
    return None


def public_link(path):
    """Héberge la vidéo convertie. Renvoie (lien, permanent ?)."""
    link = upload(path, "https://catbox.moe/user/api.php", [])  # lien permanent, réutilisé chaque jour
    if link:
        return link, True
    link = upload(path, "https://litterbox.catbox.moe/resources/internals/api.php",
                  ["-F", "time=72h"])  # secours : lien temporaire
    if link:
        return link, False
    raise RuntimeError("hébergement indisponible")


def build_link(idx, reel):
    """Télécharge, convertit et héberge la vidéo d'un Reel (fait une seule fois par Reel)."""
    source = CONFIG.get("video_urls", {}).get(reel["id"]) or reel.get("media_url")
    if not source:
        raise RuntimeError("pas de fichier fourni par Instagram → ajoute-le dans video_urls")
    raw = download(source, WORK / f"{idx}_raw.mp4")
    story = reencode(raw, WORK / f"{idx}_story.mp4")
    link, permanent = public_link(story)
    if permanent:
        CACHE[reel["id"]] = link
    return link


def prepare(idx, reel, fresh=False):
    """Crée le conteneur story (avec la vidéo déjà prête si elle existe). Renvoie son ID."""
    link = None if fresh else CACHE.get(reel["id"])
    if not link:
        link = build_link(idx, reel)
    return call("POST", f"{USER_ID}/media", media_type="STORIES", video_url=link)["id"]


def safe_prepare(item, fresh=False):
    idx, reel = item
    try:
        cid = prepare(idx, reel, fresh)
        print(f"  🎬 #{idx} prête")
        return idx, reel, cid, None
    except Exception as e:
        print(f"  ⚠️  #{idx} préparation échouée : {e}")
        return idx, reel, None, str(e)


def save_cache():
    CACHE_FILE.write_text(json.dumps(CACHE, indent=1, sort_keys=True) + "\n")


# ---------------------------------------------------------------- programme
def main():
    WORK.mkdir(parents=True, exist_ok=True)
    now = datetime.now(TZ)
    today = now.date().isoformat()
    last = STATE_FILE.read_text().strip() if STATE_FILE.exists() else ""

    if DIAG:
        reel = next(r for r in list_reels() if r.get("media_url"))
        print(f"🔍 Test sur le Reel {reel['id']} (rien ne sera publié)")
        cid = prepare(0, reel)
        print(f"  → {wait_container(cid)}")
        return

    if not FORCE:
        reason = ""
        if CONFIG.get("paused"):
            reason = "⏸  En pause (config.json → paused: true). Rien à faire."
        elif last == today:
            reason = f"✅ Déjà publié aujourd'hui ({today}). Rien à faire."
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

    # 1) Préparer toutes les vidéos (4 en parallèle), en gardant l'ordre.
    #    Les vidéos déjà converties les jours précédents sont réutilisées directement.
    print(f"🎬 Préparation ({sum(r['id'] in CACHE for r in reels)} vidéos déjà prêtes)…")
    with ThreadPoolExecutor(max_workers=4) as pool:
        prepared = list(pool.map(safe_prepare, enumerate(reels, 1)))
    save_cache()

    # 2) Publier dans l'ordre
    posted, failed = 0, 0
    for idx, reel, cid, err in prepared:
        reel_id = reel["id"]
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
            # lien en cache peut-être mort : on reconvertit la vidéo et on réessaie une fois
            print(f"  🔁 #{idx} refusée ({info.get('status_code')}), nouvelle conversion…")
            CACHE.pop(reel_id, None)
            _, _, cid, err = safe_prepare((idx, reel), fresh=True)
            save_cache()
            info = {"status_code": "ERROR", "status": err} if err else wait_container(cid)
        if info.get("status_code") != "FINISHED":
            print(f"  ❌ #{idx} {reel_id} : traitement Instagram {info.get('status_code')} "
                  f"{info.get('status', '')}")
            failed += 1
            continue
        try:
            call("POST", f"{USER_ID}/media_publish", creation_id=cid)
        except RuntimeError as e:
            print(f"  ❌ #{idx} {reel_id} : publication refusée — {e}")
            failed += 1
            continue
        if posted == 0:  # dès la 1re story publiée, on note la date pour ne jamais doubler
            STATE_FILE.write_text(today + "\n")
        posted += 1
        print(f"  ✅ #{idx} {reel_id} publiée")

    print(f"\n🎉 {posted} stories publiées, {failed} en échec.")
    if failed:
        sys.exit(1)  # le job passe en rouge → GitHub t'envoie un e-mail


if __name__ == "__main__":
    main()
