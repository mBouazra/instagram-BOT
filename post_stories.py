"""
Publie chaque jour tous les Reels de @myeleganza_ en story, dans le même ordre.

- Une seule fournée par jour (heure réglable dans config.json) : les stories de la veille
  expirent au moment où les nouvelles arrivent, donc tes 59 vidéos sont toujours visibles,
  jamais en double.
- Ordre fixe : du Reel le plus ancien au plus récent (ou l'inverse via config.json).
- Toutes les vidéos sont d'abord envoyées à Instagram, puis publiées une par une dans l'ordre.

Variables d'environnement : IG_TOKEN (secret GitHub), IG_USER_ID.
Options : FORCE=1 pour ignorer l'heure et le "déjà posté aujourd'hui", DRY_RUN=1 pour tester sans publier.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

API = "https://graph.instagram.com"
ROOT = Path(__file__).parent
STATE_FILE = ROOT / "last_posted.txt"
TZ = ZoneInfo("Europe/Paris")

TOKEN = os.environ["IG_TOKEN"]
USER_ID = os.environ.get("IG_USER_ID", "me")
FORCE = os.environ.get("FORCE") == "1"
DRY_RUN = os.environ.get("DRY_RUN") == "1"
CONFIG = json.loads((ROOT / "config.json").read_text())


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
            raise RuntimeError(f"{method} {path} → HTTP {e.code}: {err.get('message', body)}") from None
        except urllib.error.URLError as e:
            if attempt < 5:
                time.sleep(15)
                continue
            raise RuntimeError(f"{method} {path} → réseau: {e}") from None


def list_reels():
    reels, path, params = [], f"{USER_ID}/media", {
        "fields": "id,media_product_type,media_type,media_url,timestamp", "limit": 100}
    while path:
        page = call("GET", path, **params)
        reels += [m for m in page.get("data", []) if m.get("media_product_type") == "REELS"]
        nxt = page.get("paging", {}).get("next")
        if not nxt:
            break
        q = urllib.parse.parse_qs(urllib.parse.urlparse(nxt).query)
        path = f"{USER_ID}/media"
        params = {"fields": params["fields"], "limit": 100, "after": q["after"][0]}
    reels.sort(key=lambda m: m["timestamp"], reverse=CONFIG.get("order") == "newest_first")
    return reels


def diagnose():
    """Envoie UNE vidéo à Instagram sans la publier et affiche la réponse complète."""
    reel = next(r for r in list_reels() if r.get("media_url"))
    url = reel["media_url"]
    print(f"🔍 Test sur le Reel {reel['id']}")
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"  Fichier : HTTP {r.status}, {r.headers.get('Content-Type')}, "
                  f"{int(r.headers.get('Content-Length', 0)) / 1e6:.1f} Mo")
    except Exception as e:
        print(f"  Fichier inaccessible depuis GitHub : {e}")
    c = call("POST", f"{USER_ID}/media", media_type="STORIES", video_url=url)
    print(f"  Conteneur créé : {c}")
    for _ in range(60):
        info = call("GET", c["id"], fields="status_code,status")
        print(f"  Statut : {info}")
        if info.get("status_code") in ("FINISHED", "ERROR", "EXPIRED"):
            break
        time.sleep(5)
    print("ℹ️  Rien n'a été publié.")


def main():
    now = datetime.now(TZ)
    today = now.date().isoformat()
    last = STATE_FILE.read_text().strip() if STATE_FILE.exists() else ""

    if os.environ.get("DIAG") == "1":
        diagnose()
        return

    if not FORCE:
        if CONFIG.get("paused"):
            print("⏸  En pause (config.json → paused: true). Rien à faire.")
            return
        if last == today:
            print(f"✅ Déjà publié aujourd'hui ({today}). Rien à faire.")
            return
        if now.hour < CONFIG["post_hour"]:
            print(f"🕗 Il est {now:%H:%M} à Paris, publication prévue à {CONFIG['post_hour']}h.")
            return

    skip = set(CONFIG.get("skip", []))
    urls = CONFIG.get("video_urls", {})
    reels = [r for r in list_reels() if r["id"] not in skip]
    print(f"📦 {len(reels)} Reels à publier (ordre : {CONFIG.get('order', 'oldest_first')})")

    # 1) Envoyer toutes les vidéos à Instagram (elles se traitent en parallèle de leur côté)
    queue = []
    for i, r in enumerate(reels, 1):
        video = urls.get(r["id"]) or r.get("media_url")
        if not video:
            print(f"  ⚠️  #{i} {r['id']} : pas de fichier fourni par Instagram → ajoute-le dans video_urls")
            continue
        if DRY_RUN:
            queue.append((i, r["id"], None))
            continue
        try:
            c = call("POST", f"{USER_ID}/media", media_type="STORIES", video_url=video)
            queue.append((i, r["id"], c["id"]))
        except RuntimeError as e:
            print(f"  ❌ #{i} {r['id']} : envoi refusé — {e}")

    if DRY_RUN:
        print(f"🧪 DRY_RUN : {len(queue)} stories seraient publiées. Rien n'a été posté.")
        return

    # 2) Publier dans l'ordre, en attendant que chaque vidéo soit prête
    posted, failed = 0, len(reels) - len(queue)
    for i, reel_id, cid in queue:
        status, detail, deadline = "", "", time.time() + 300
        while time.time() < deadline:
            info = call("GET", cid, fields="status_code,status")
            status, detail = info.get("status_code", ""), info.get("status", "")
            if status in ("FINISHED", "ERROR", "EXPIRED"):
                break
            time.sleep(5)
        if status != "FINISHED":
            print(f"  ❌ #{i} {reel_id} : traitement Instagram {status or 'trop long'} — {detail}")
            failed += 1
            continue
        try:
            call("POST", f"{USER_ID}/media_publish", creation_id=cid)
        except RuntimeError as e:
            print(f"  ❌ #{i} {reel_id} : publication refusée — {e}")
            failed += 1
            continue
        if posted == 0:  # dès la 1re story publiée, on note la date pour ne jamais doubler
            STATE_FILE.write_text(today + "\n")
        posted += 1
        print(f"  ✅ #{i} {reel_id} publiée")

    print(f"\n🎉 {posted} stories publiées, {failed} en échec.")
    if failed:
        sys.exit(1)  # le job passe en rouge → GitHub t'envoie un e-mail


if __name__ == "__main__":
    main()
