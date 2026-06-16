# Kodak Monitor — Modifications du 16 juin 2026 (v2.0.0 → v2.1.0)

## 1. Correctifs

### Popup invisible au démarrage
Le popup de statut se lançait hors écran : sa position sauvegardée (`popup_x`/`popup_y`
dans `kodak_monitor_config.json`) ne correspondait plus à la configuration actuelle des
écrans (changement de moniteur entre-temps). Réinitialisée à `-1`/`-1` pour revenir au
positionnement automatique (bas-droite de l'écran principal).

### Lancement avec le mauvais Python
Le SDK Kodak (`chcusb.dll`, `KA6900.dll`) est en 32-bit. Si l'app est lancée avec
`python kodak_monitor.py` sans préciser l'interpréteur, Windows peut résoudre vers un
Python 64-bit installé en parallèle et l'app refuse de démarrer. Toujours utiliser :
```
C:\Users\arthu\AppData\Local\Programs\Python\Python312-32\python.exe kodak_monitor.py
```

### Build .exe cassé
`build_exe.bat` appelait `make_version_info.py`, qui avait disparu du dossier (seul son
`.pyc` compilé restait dans `__pycache__`). Le script a été recréé — il génère
`version_info.txt` à partir de `APP_VERSION` défini dans `kodak_monitor.py`. Le build a
été testé de bout en bout (`dist\KodakMonitor.exe` généré avec succès).

## 2. Nouvelle fonctionnalité : Écran de veille (verrouillage PIN)

Contexte : les PC sont exposés au public dans une galerie et les opérateurs ne sont pas
toujours présents. Un nouvel onglet **"Écran de veille"** a été ajouté au panneau de
réglages (icône ⚙ du popup → protégé par le PIN existant) :

- **Verrouillage automatique** après un délai d'inactivité configurable (en minutes).
- **PIN dédié**, différent du PIN qui protège l'accès aux paramètres — modifiable dans
  le même onglet (champ "PIN de déverrouillage", laisser vide pour conserver l'actuel).
  Valeur par défaut si aucun PIN n'est défini : `1234`.
- **Clavier numérique tactile intégré** (gros boutons 0-9, Effacer, ⌫) — aucun clavier
  physique ni clavier virtuel Windows requis.
- **Bouton "▶ TEST"** pour déclencher le verrouillage immédiatement (sans attendre le
  délai d'inactivité) et vérifier le PIN/le rendu.
- **Multi-écrans** : l'écran de verrouillage est dupliqué à l'identique sur chaque
  moniteur physique détecté (et non étiré sur une seule fenêtre virtuelle) ; la saisie
  du PIN sur un écran est recopiée en direct sur les autres. Le déverrouillage depuis
  n'importe quel écran ferme tous les verrouillages en même temps.
- **Barre des tâches masquée** automatiquement pendant le verrouillage (principale +
  écrans secondaires), restaurée au déverrouillage ou si l'app se ferme (filet de
  sécurité via `atexit`).
- La fenêtre de verrouillage se réaffirme au premier plan toutes les 250 ms et reprend
  le focus si elle le perd.

Fichiers/clés de config ajoutés (`kodak_monitor_config.json`) :
```json
"screensaver_enabled": false,
"screensaver_timeout_minutes": 5,
"screensaver_pin_hash": ""
```

### Limite connue
Cette protection est gérée au niveau de l'application, pas du système. Un balayage
tactile insistant depuis le bord de l'écran (geste "Edge UI" de Windows) peut encore,
dans de rares cas, faire apparaître brièvement une UI Windows par-dessus avant que la
fenêtre de verrouillage ne reprenne le dessus. Pour une étanchéité totale, il faudrait
un vrai mode kiosque (compte Windows dédié + remplacement du shell) — solution plus
lourde à mettre en place, volontairement non retenue pour l'instant.

## 3. Renforcement Windows (gestes tactiles de bord)

Pour limiter l'apparition du menu/de la barre Windows lors d'un balayage depuis le bord
de l'écran par-dessus l'écran de veille, les clés de registre suivantes ont été
appliquées sur la machine de test (à reproduire sur chaque PC de la galerie) :

```
HKCU\SOFTWARE\Policies\Microsoft\Windows\EdgeUI       AllowEdgeSwipe   (DWORD) = 0
HKLM\SOFTWARE\Policies\Microsoft\Windows\EdgeUI       AllowEdgeSwipe   (DWORD) = 0
HKCU\Software\Microsoft\Windows\CurrentVersion\ImmersiveShell\EdgeUI
                                                        DisableTLcorner (DWORD) = 1
                                                        DisableTRcorner (DWORD) = 1
```
Un redémarrage de `explorer.exe` (ou une déconnexion/reconnexion) est nécessaire pour
que ces clés prennent effet.

## 4. Version

`APP_VERSION` passé de `2.0.0` à `2.1.0` dans `kodak_monitor.py`, `version_info.txt`
régénéré en conséquence (utilisé par PyInstaller pour les infos de version de l'exe).

## 5. Fichiers modifiés/créés aujourd'hui

- `kodak_monitor.py` — onglet Écran de veille, verrouillage multi-écrans, version 2.1.0
- `kodak_monitor_config.json` — `popup_x`/`popup_y` réinitialisés, nouvelles clés veille
- `make_version_info.py` — recréé (manquant)
- `version_info.txt` — régénéré en 2.1.0.0
- `CHANGELOG_2026-06-16.md` — ce document
