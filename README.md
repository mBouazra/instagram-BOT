# Eleganza Story Bot

Publie chaque jour tes Reels en story, automatiquement, dans le même ordre.

## Comment ça marche
- Chaque jour à **8h (heure de Paris)**, le bot republie tous tes Reels en story, du plus ancien au plus récent.
- Les stories de la veille expirent au même moment : tes vidéos sont **toujours visibles et jamais en double**.
- Un nouveau Reel publié sur ton compte est ajouté automatiquement, à la fin de la liste.

## Installation (une seule fois, ~10 min)
1. Crée un dépôt **privé** sur GitHub (ex. `eleganza-stories`) et mets-y tous ces fichiers
   (bouton *Add file → Upload files*, en gardant le dossier `.github/workflows`).
2. Dans le dépôt : **Settings → Secrets and variables → Actions**
   - onglet **Secrets** → *New repository secret* : `IG_TOKEN` = ton token Instagram
   - onglet **Variables** → *New repository variable* : `IG_USER_ID` = `17841480522170396`
3. **Test à blanc** : onglet **Actions → Stories quotidiennes → Run workflow**, coche *Test à blanc*.
   Le journal doit afficher le nombre de stories qui seraient publiées.
4. C'est tout. Le bot tourne tout seul dès le lendemain 8h.
   Pour lancer tout de suite : *Run workflow* en cochant *Publier maintenant*.

## Réglages (fichier `config.json`, modifiable depuis GitHub, même sur téléphone)
| Réglage | Effet |
|---|---|
| `"post_hour": 8` | Heure de publication (heure de Paris) |
| `"order": "oldest_first"` | Ordre : `oldest_first` ou `newest_first` |
| `"paused": true` | Met le bot en pause (vacances, rupture de stock…) |
| `"skip": ["ID"]` | Reels à ne plus poster (produit épuisé) |
| `"video_urls": {"ID": "https://…mp4"}` | Fichier à utiliser pour un Reel qu'Instagram ne fournit pas |

## Poster une promo en premier
Poste la promo **à la main juste avant 8h** (ex. 7h50) : elle apparaît en premier, puis les 59 vidéos suivent.
Pour une promo un autre jour à une autre heure : mets `post_hour` à cette heure-là le matin même,
poste ta promo juste avant, puis remets 8 le lendemain.

## Token
Le token est renouvelé automatiquement le 1er et le 15 du mois, à condition d'ajouter un secret `GH_PAT` :
GitHub → *Settings (profil) → Developer settings → Fine-grained tokens* → accès à ce dépôt uniquement,
permission **Secrets : Read and write**. Sans lui, régénère le token sur Meta tous les ~50 jours.

## Si ça échoue
GitHub t'envoie un e-mail. Ouvre **Actions** → le dernier run : chaque vidéo en échec est listée avec la raison.
