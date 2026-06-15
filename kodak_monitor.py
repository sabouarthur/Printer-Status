#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KODAK PRINTER MONITOR + HOTFOLDER
Monitoring USB + Impression directe + GUI
Kodak 6800/6850 + 6900/6950

python kodak_monitor.py
Requiert: Python 32-bit, Pillow (pip install Pillow)
"""

import ctypes
from ctypes import POINTER, byref, c_ubyte, c_ushort, c_ulong, c_void_p, c_bool, create_string_buffer
import json, os, sys, time, struct, threading, logging
import hashlib
import hmac
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Any

MAX_USB_DEVICE = 128

# Constantes Kodak 68xx SDK pour impression 10x15 / 4x6 sans bordure.
KPINFTAG_PAPER = 0
KPINFTAG_POLISH = 20
KSIZEOF_PINF_PAPERSET = 10
KSIZEOF_PINF_POLISHSET = 2
KFORMAT_PIXEL_RGB = 3
KCOMPONENT_RGB = 3
KDEPTH_8BIT = 8
SDK_10X15_WIDTH = 1844
SDK_10X15_HEIGHT = 1240
SDK_10X15_MEDIA_CODE = 6
SDK_10X15_PRINT_TYPE = 1
SDK_10X15_POLISH = 1
# En exe PyInstaller, le dossier de travail est celui de l'exe, pas le dossier temp
if getattr(sys, 'frozen', False):
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = str(Path(__file__).parent)


def _configure_console_output():
    """Force UTF-8 on Windows console to avoid mojibake in logs."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_configure_console_output()

APP_VERSION = "2.0.0"
WINDOWS_SPOOLER_GRACE_SECONDS = 12
WINDOWS_SPOOLER_POLL_SECONDS = 0.25
KODAK_PRINTER_HINTS = ("KODAK", "6850", "6800", "6950", "6900")
WINDOWS_PRINTER_ACTIVE_STATUS_MASK = 0x200 | 0x400 | 0x4000

def _resource(filename):
    """Chemin vers un fichier ressource (compatible PyInstaller --onefile)."""
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, filename)
    return os.path.join(SCRIPT_DIR, filename)

# Logger fichier global
LOG_FILE = os.path.join(SCRIPT_DIR, "kodak_print.log")
_file_logger = logging.getLogger("kodak")
_file_logger.setLevel(logging.INFO)
try:
    from logging.handlers import RotatingFileHandler
    _fh = RotatingFileHandler(LOG_FILE, maxBytes=50*1024*1024, backupCount=5, encoding="utf-8")
    _fh.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    _file_logger.addHandler(_fh)
except (OSError, ImportError) as e:
    print(f"[LOG] Impossible d'initialiser le fichier log: {e}")

def log_print(msg):
    """Log dans le fichier ET dans la console."""
    _file_logger.info(msg)
    print(f"  📂 {msg}")


GUI_CONFIG_FILE = os.path.join(SCRIPT_DIR, "kodak_monitor_config.json")


def _flush_log_handlers():
    for handler in list(_file_logger.handlers):
        try:
            handler.flush()
        except Exception:
            pass


def _format_bytes(size):
    try:
        size = int(size)
    except Exception:
        size = 0
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.2f} Mo"
    if size >= 1024:
        return f"{size / 1024:.1f} Ko"
    return f"{size} o"


def get_log_size():
    try:
        return os.path.getsize(LOG_FILE) if os.path.exists(LOG_FILE) else 0
    except OSError:
        return 0


def read_log_tail(max_chars=200000):
    _flush_log_handlers()
    try:
        if not os.path.exists(LOG_FILE):
            return ""
        size = os.path.getsize(LOG_FILE)
        with open(LOG_FILE, "rb") as f:
            if size > max_chars:
                f.seek(max(0, size - max_chars))
                data = f.read()
                marker = b"\n"
                pos = data.find(marker)
                if pos >= 0:
                    data = data[pos + 1:]
                prefix = f"... affichage limité aux derniers {_format_bytes(max_chars)} ...\n"
            else:
                data = f.read()
                prefix = ""
        return prefix + data.decode("utf-8", errors="replace")
    except Exception as e:
        return f"Lecture du log impossible: {e}"


def clear_log_files():
    _flush_log_handlers()
    removed = 0
    errors = []
    for path in [LOG_FILE] + [f"{LOG_FILE}.{i}" for i in range(1, 6)]:
        try:
            if path == LOG_FILE:
                with open(path, "w", encoding="utf-8"):
                    pass
                removed += 1
            elif os.path.exists(path):
                os.remove(path)
                removed += 1
        except Exception as e:
            errors.append(f"{os.path.basename(path)}: {e}")
    return removed, errors


def trim_log_by_days(retention_days):
    retention_days = max(1, int(retention_days))
    _flush_log_handlers()
    if not os.path.exists(LOG_FILE):
        return 0, 0
    cutoff = datetime.now().timestamp() - retention_days * 86400
    kept = []
    removed = 0
    timestamp_re = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = timestamp_re.match(line)
                if m:
                    try:
                        ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                    except ValueError:
                        ts = None
                    if ts is not None and ts < cutoff:
                        removed += 1
                        continue
                kept.append(line)
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.writelines(kept)
    except Exception:
        return 0, 0
    return removed, len(kept)


def trim_log_by_size(max_mb):
    max_bytes = max(1, int(max_mb)) * 1024 * 1024
    _flush_log_handlers()
    if not os.path.exists(LOG_FILE):
        return 0, 0
    try:
        size = os.path.getsize(LOG_FILE)
        if size <= max_bytes:
            return size, size
        with open(LOG_FILE, "rb") as f:
            f.seek(size - max_bytes)
            data = f.read()
        pos = data.find(b"\n")
        if pos >= 0:
            data = data[pos + 1:]
        with open(LOG_FILE, "wb") as f:
            f.write(data)
        return size, len(data)
    except Exception:
        return 0, 0

CONFIG_DEFAULTS = {
    "allow_popup_close": False,
    "fullscreen_on_error": False,
    "hide_popup_when_ready": False,
    "protect_config_access": False,
    "config_access_pin_hash": "",
    "print_counter_file": "kodak_compteur.json",
    "scan_interval": 2,
    "scan_detail_interval": 60,
    "max_untrusted_identity_delta": 200,
    "hotfolder_path": "",
    "hotfolder_printer": "",
    "hotfolder_copies": 1,
    "hotfolder_action": "Supprimer apres impression",
    "sdk_print_gap_seconds": 75,
    "windows_driver_post_print_delay": 12,
    "sdk_print_notice_seconds": 30,
    "log_retention_days": 30,
    "log_max_mb": 10,
    "popup_font_size": 11,
    "popup_x": -1,
    "popup_y": -1,
}

PIN_HASH_ITERATIONS = 200_000


def _hash_config_pin(pin: str) -> str:
    pin = str(pin or "1234")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, PIN_HASH_ITERATIONS)
    return f"pbkdf2_sha256${PIN_HASH_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_config_pin(pin: str, pin_hash: str) -> bool:
    pin_hash = str(pin_hash or "")
    pin = str(pin or "")
    if pin_hash.startswith("pbkdf2_sha256$"):
        try:
            _, iterations, salt_hex, digest_hex = pin_hash.split("$", 3)
            expected = hashlib.pbkdf2_hmac(
                "sha256",
                pin.encode("utf-8"),
                bytes.fromhex(salt_hex),
                int(iterations),
            ).hex()
            return hmac.compare_digest(expected, digest_hex)
        except Exception:
            return False
    if pin_hash.startswith("sha256:"):
        expected = hashlib.sha256(pin.encode("utf-8")).hexdigest()
        return hmac.compare_digest(expected, pin_hash.split(":", 1)[1])
    return pin == "1234"


def _migrate_config_pin(data: Dict[str, Any]) -> bool:
    if "config_access_pin" not in data:
        return False
    pin = str(data.pop("config_access_pin") or "1234").strip() or "1234"
    data["config_access_pin_hash"] = _hash_config_pin(pin)
    return True

OBSOLETE_CONFIG_KEYS = {
    "scan_count_interval",
    "print_busy_grace_seconds",
    "usb_busy_activity_seconds",
    "post_print_notice_seconds",
    "spooler_activity_seconds",
    "windows_spooler_check",
    "hotfolder_format",
}


# ============================================================================
#  COMPTEUR D'IMPRESSIONS (par jour / mois / an)
# ============================================================================

def _get_counter_path():
    """Retourne le chemin du fichier compteur depuis la config."""
    try:
        cfg = load_gui_config()
        name = cfg.get("print_counter_file", "") or os.path.join(SCRIPT_DIR, "kodak_compteur.json")
    except:
        name = os.path.join(SCRIPT_DIR, "kodak_compteur.json")
    # Si c'est un chemin absolu, on le garde tel quel
    if os.path.isabs(name):
        return name
    # Sinon on le met dans SCRIPT_DIR
    return os.path.join(SCRIPT_DIR, name)


def _load_counters():
    """Charge le fichier compteur."""
    p = _get_counter_path()
    try:
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_counters(data):
    """Sauvegarde le fichier compteur."""
    p = _get_counter_path()
    try:
        # Créer le répertoire si nécessaire
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        # Écriture directe (os.replace peut échouer entre volumes)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log_print(f"⚠ Compteur: erreur sauvegarde ({p}): {e}")


def increment_print_counter(copies=1):
    """Incrémente le compteur d'impressions pour aujourd'hui."""
    now = datetime.now()
    year = str(now.year)
    month = f"{now.month:02d}"
    day = f"{now.day:02d}"

    data = _load_counters()

    # Structure: { "2026": { "total": N, "mois": { "03": { "total": N, "jours": { "26": N } } } } }
    if year not in data:
        data[year] = {"total": 0, "mois": {}}
    data[year]["total"] += copies

    if month not in data[year]["mois"]:
        data[year]["mois"][month] = {"total": 0, "jours": {}}
    data[year]["mois"][month]["total"] += copies

    if day not in data[year]["mois"][month]["jours"]:
        data[year]["mois"][month]["jours"][day] = 0
    data[year]["mois"][month]["jours"][day] += copies

    _save_counters(data)
    log_print(f"📊 Compteur: +{copies} impression(s) → {day}/{month}/{year}")


def load_gui_config() -> Dict[str, Any]:
    defaults = CONFIG_DEFAULTS.copy()
    log_print(f"[CONFIG] Lecture: {GUI_CONFIG_FILE}")
    try:
        if os.path.exists(GUI_CONFIG_FILE):
            with open(GUI_CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict):
                legacy_keys = sorted(k for k in OBSOLETE_CONFIG_KEYS if k in data)
                for key in legacy_keys:
                    data.pop(key, None)
                migrated_pin = _migrate_config_pin(data)
                defaults.update(data)
                if legacy_keys or migrated_pin:
                    cleaned = legacy_keys + (["config_access_pin"] if migrated_pin else [])
                    log_print(f"[CONFIG] Nettoyage: anciennes cles retirees: {', '.join(cleaned)}")
                    save_gui_config(defaults)
                log_print(f"[CONFIG] OK — {len(data)} clés chargées — "
                          f"pin=*** "
                          f"protect={data.get('protect_config_access','?')} "
                          f"close={data.get('allow_popup_close','?')} "
                          f"fullscreen={data.get('fullscreen_on_error','?')}")
            else:
                log_print("[CONFIG] ⚠ Le fichier JSON n'est pas un objet dict")
        else:
            log_print(f"[CONFIG] ⚠ Fichier introuvable — valeurs par défaut utilisées")
    except Exception as e:
        log_print(f"[CONFIG] ⚠ Erreur lecture: {e}")
    return defaults


def save_gui_config(cfg: Dict[str, Any]) -> None:
    try:
        data = dict(cfg)
        _migrate_config_pin(data)
        for key in OBSOLETE_CONFIG_KEYS:
            data.pop(key, None)
        with open(GUI_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log_print(f"⚠ Impossible sauvegarder config GUI: {e}")


# ============================================================================
#  CODES DE STATUT
# ============================================================================
STATUS_FR = {
    0:("OK","Imprimante prête","ok"),
    1000:("SYSTÈME","Mémoire pleine","critique"),1002:("SYSTÈME","Impossible charger DLL USB","critique"),
    1003:("SYSTÈME","Impossible charger DLL ColorMatch","critique"),1004:("SYSTÈME","Impossible ouvrir port USB","critique"),
    1005:("SYSTÈME","Accès fichier impossible","erreur"),1006:("SYSTÈME","Paramètre invalide","erreur"),
    1008:("SYSTÈME","Imprimante non trouvée","erreur"),1009:("SYSTÈME","Impossible charger KA6900IP.DLL","critique"),
    1500:("SYSTÈME","Utilisée par un autre thread","attente"),1501:("SYSTÈME","Impossible créer thread","critique"),
    2000:("CONNEXION","Non reconnue (éteinte/déconnectée)","erreur"),
    2504:("SERVICE","Erreur indéfinie","critique"),2600:("USB","Erreur communication USB","critique"),
    2100:("OCCUPATION","Impression en cours / Buffer plein","attente"),
    2101:("ACTIVITÉ","Init. CPU principal","attente"),2102:("ACTIVITÉ","Init. ruban","attente"),
    2103:("ACTIVITÉ","Chargement papier","attente"),2104:("ACTIVITÉ","Protection thermique","attente"),
    2105:("ACTIVITÉ","Panneau opérateur","attente"),2106:("ACTIVITÉ","Auto-diagnostic","attente"),
    2107:("ACTIVITÉ","Téléchargement firmware","attente"),
    2200:("IMPRESSION","Alimentation papier","attente"),2201:("IMPRESSION","Préchauffage","attente"),
    2202:("IMPRESSION","Impression Jaune / Rembobinage ruban","attente"),
    2203:("IMPRESSION","Retour papier après Jaune","attente"),
    2204:("IMPRESSION","Impression Magenta / Panneau opérateur","attente"),
    2205:("IMPRESSION","Retour papier après Magenta / Init.","attente"),
    2206:("IMPRESSION","Impression Cyan","attente"),2207:("IMPRESSION","Retour papier après Cyan","attente"),
    2208:("IMPRESSION","Couche protection (OP)","attente"),2209:("IMPRESSION","Position coupe","attente"),
    2210:("IMPRESSION","Éjection papier","attente"),2211:("IMPRESSION","Retour position repos","attente"),
    2212:("IMPRESSION","Impression terminée","ok"),2220:("IMPRESSION","Découpe (slitting)","attente"),
    2300:("INFO","Pas d'impression","ok"),2301:("PAPIER","Taille non disponible","erreur"),
    2304:("PAPIER","Papier non installé","erreur"),2305:("PAPIER","Plus de papier","erreur"),
    2306:("RUBAN","Plus de ruban","erreur"),2307:("RUBAN","Ruban épuisé","erreur"),
    2308:("RUBAN","Ruban incorrect","erreur"),2309:("CAPOT","Capot/porte ouvert(e)","erreur"),
    2310:("CAPOT","Erreur fermeture porte","erreur"),2311:("PAPIER","Papier trop enfoncé","erreur"),
    2312:("DÉCOUPE","Bac chutes plein","erreur"),2314:("RUBAN","Erreur rembobinage ruban","erreur"),
    2315:("RUBAN","Erreur détection ruban","erreur"),2316:("RUBAN","Erreur rembobinage #2","erreur"),
    2332:("SERVICE","Erreur calibration THV","critique"),2333:("SERVICE","THV non calibré","erreur"),
    2340:("MÉMOIRE","Mémoire insuffisante (panoramique)","erreur"),
    2341:("RUBAN","Ruban insuffisant (panoramique)","erreur"),
    2401:("CAPOT","Capot supérieur ouvert","erreur"),2402:("CAPOT","Capot papier ouvert","erreur"),
    2403:("RUBAN","Ruban incorrect","erreur"),2404:("PAPIER","Papier incorrect","erreur"),
    2405:("RUBAN","Ruban vide","erreur"),2408:("PAPIER","Plus de papier","erreur"),
    2412:("PAPIER","Prêt chargement papier","attente"),2413:("PAPIER","Retirer le papier","attente"),
    3001:("BOURRAGE","Erreur début transport","erreur"),3016:("BOURRAGE","Erreur transport","erreur"),
    3064:("BOURRAGE","Erreur coupe papier","erreur"),3081:("BOURRAGE","Capteur bord off au repos","erreur"),
    3091:("BOURRAGE","Capteur bord on au repos","erreur"),3095:("BOURRAGE","Papier trop avancé","erreur"),
    4001:("CONTRÔLE","Erreur EEPROM","critique"),4010:("CONTRÔLE","Erreur ASIC / Table","critique"),
    4022:("CONTRÔLE","Communication MAIN-MSP","critique"),4025:("CONTRÔLE","Alim. tête thermique","critique"),
    4101:("MÉCANIQUE","Tête/galet (origine)","critique"),4120:("MÉCANIQUE","Coupe G→D","critique"),
    4201:("CAPTEUR","Capteur tête/coupe","critique"),4301:("TEMPÉRATURE","Capteur tête","critique"),
    4305:("TEMPÉRATURE","Préchauffage/thermistance","critique"),4308:("TEMPÉRATURE","Protection thermique","critique"),
}
# Conseils affichés sous la description — un texte court et actionnable par code
CONSEILS_FR = {
    # --- Erreurs système ---
    1002: "La DLL USB est manquante ou incompatible — vérifiez le dossier 68xx/",
    1003: "La DLL ColorMatch est manquante — vérifiez le dossier 68xx/",
    1004: "Vérifiez que l'imprimante est allumée et le câble USB bien branché",
    1005: "Problème d'accès fichier — relancez le logiciel en administrateur",
    1006: "Paramètre invalide envoyé au SDK — redémarrez le logiciel",
    1008: "Imprimante non reconnue — vérifiez le câble USB et que l'imprimante est allumée",
    1009: "La DLL KA6900IP.DLL est manquante — vérifiez le dossier 6900/",
    1500: "Veuillez patienter — l'imprimante est utilisée par une autre application",
    1501: "Impossible de démarrer le thread interne — redémarrez le logiciel",
    2000: "Vérifiez que l'imprimante est allumée et le câble USB branché",
    # --- Attente / activité ---
    2100: "Veuillez patienter — impression en cours",
    2101: "Veuillez patienter — initialisation en cours",
    2102: "Veuillez patienter — chargement du ruban",
    2103: "Veuillez patienter — chargement du papier",
    2104: "Veuillez patienter — refroidissement de la tête thermique",
    2105: "Veuillez patienter — action sur le panneau opérateur",
    2106: "Veuillez patienter — auto-diagnostic en cours",
    2107: "Ne pas éteindre l'imprimante — mise à jour du firmware en cours",
    # --- Impression en cours ---
    2200: "Veuillez patienter — alimentation du papier",
    2201: "Veuillez patienter — préchauffage de la tête",
    2202: "Veuillez patienter — impression couche jaune",
    2203: "Veuillez patienter — retour papier",
    2204: "Veuillez patienter — impression couche magenta",
    2205: "Veuillez patienter — repositionnement papier",
    2206: "Veuillez patienter — impression couche cyan",
    2207: "Veuillez patienter — retour papier",
    2208: "Veuillez patienter — application couche de protection",
    2209: "Veuillez patienter — positionnement pour découpe",
    2210: "Veuillez patienter — éjection de la photo",
    2211: "Veuillez patienter — retour en position de repos",
    2220: "Veuillez patienter — découpe en cours",
    2412: "Veuillez charger le papier dans l'imprimante",
    2413: "Veuillez retirer le papier de l'imprimante",
    # --- Erreurs consommables ---
    2304: "Chargez un rouleau de papier dans l'imprimante",
    2305: "Rouleau de papier épuisé — chargez un nouveau rouleau",
    2306: "Remplacez le ruban thermique",
    2307: "Ruban épuisé — remplacez le ruban thermique",
    2308: "Le ruban ne correspond pas au papier chargé — vérifiez la compatibilité",
    2309: "Refermez le capot de l'imprimante",
    2310: "Le capot ne se ferme pas correctement — vérifiez qu'aucun objet ne bloque",
    2311: "Le papier est mal positionné — retirez et rechargez le rouleau",
    2312: "Videz le bac de découpe (chutes de papier)",
    2314: "Erreur de rembobinage — retirez et réinstallez le ruban",
    2315: "Ruban non détecté — vérifiez qu'il est bien installé",
    2316: "Erreur de rembobinage — retirez et réinstallez le ruban",
    2332: "Erreur de calibration THV — lancez l'auto-calibration depuis le menu imprimante",
    2333: "Calibration THV absente — effectuez une calibration depuis le menu imprimante",
    2340: "Mémoire insuffisante pour l'impression panoramique — réduisez la taille de l'image",
    2341: "Ruban insuffisant pour terminer le panoramique — remplacez le ruban",
    2401: "Refermez le capot supérieur",
    2402: "Refermez le capot papier",
    2403: "Le ruban ne correspond pas au papier — vérifiez la compatibilité",
    2404: "Le papier chargé ne correspond pas au format configuré",
    2405: "Ruban vide — remplacez le ruban thermique",
    2408: "Rouleau de papier épuisé — chargez un nouveau rouleau",
    2600: "Débranchez et rebranchez le câble USB, puis redémarrez l'imprimante",
}
# Conseils génériques par plage de codes
def _conseil_plage(code):
    if 3000 <= code < 4000:
        return "Un bourrage papier est détecté — ouvrez le capot et dégagez le papier coincé"
    if 4000 <= code < 4400:
        return "Erreur matérielle grave — éteignez et rallumez l'imprimante. Si le problème persiste, contactez le service technique"
    return ""

def get_status_info(code):
    conseil = CONSEILS_FR.get(code, _conseil_plage(code))
    if code in STATUS_FR:
        c,d,s=STATUS_FR[code]; return {"categorie":c,"description":d,"gravite":s,"conseil":conseil}
    if 3000<=code<4000: return {"categorie":"BOURRAGE","description":f"Bourrage (code {code})","gravite":"erreur","conseil":conseil}
    if 4000<=code<4100: return {"categorie":"CONTRÔLE","description":f"Contrôle (code {code})","gravite":"critique","conseil":conseil}
    if 4100<=code<4200: return {"categorie":"MÉCANIQUE","description":f"Mécanique (code {code})","gravite":"critique","conseil":conseil}
    if 4200<=code<4300: return {"categorie":"CAPTEUR","description":f"Capteur (code {code})","gravite":"critique","conseil":conseil}
    if 4300<=code<4400: return {"categorie":"TEMPÉRATURE","description":f"Température (code {code})","gravite":"critique","conseil":conseil}
    return {"categorie":"INCONNU","description":f"Code {code}","gravite":"erreur","conseil":conseil}

# ============================================================================
#  SDK WRAPPER (monitoring + impression)
# ============================================================================
class KodakSDK:
    def __init__(self, model, dll_dir):
        self.model=model; self._is_open=False; self._dll_dir=dll_dir
        self._dll_name="chcusb.dll" if model=="68xx" else "KA6900.dll"
        p=os.path.join(dll_dir,self._dll_name)
        if not os.path.exists(p): raise FileNotFoundError(f"{self._dll_name} introuvable dans {dll_dir}")
        # Forcer le répertoire courant au dossier DLL pour que les dépendances soient trouvées
        old_cwd = os.getcwd()
        os.chdir(dll_dir)
        os.environ["PATH"] = dll_dir + ";" + os.environ.get("PATH", "")
        try:
            if hasattr(os, 'add_dll_directory'): os.add_dll_directory(dll_dir)
        except (AttributeError, OSError) as e:
            log_print(f"[SDK] add_dll_directory non disponible: {e}")
        try:
            self._dll = ctypes.WinDLL(p)
        finally:
            os.chdir(old_cwd)
        d=self._dll
        # Prototypes monitoring
        d.chcusb_open.argtypes=[POINTER(c_ushort)]; d.chcusb_open.restype=c_bool
        d.chcusb_close.argtypes=[]; d.chcusb_close.restype=None
        d.chcusb_listupPrinter.argtypes=[POINTER(c_ubyte)]; d.chcusb_listupPrinter.restype=c_ushort
        d.chcusb_selectPrinter.argtypes=[c_ubyte,POINTER(c_ushort)]; d.chcusb_selectPrinter.restype=c_ubyte
        d.chcusb_status.argtypes=[POINTER(c_ushort)]; d.chcusb_status.restype=c_bool
        d.chcusb_statusAll.argtypes=[POINTER(c_ubyte),POINTER(c_ushort)]; d.chcusb_statusAll.restype=c_bool
        d.chcusb_getPrinterInfo.argtypes=[c_ushort,c_void_p,POINTER(c_ulong)]; d.chcusb_getPrinterInfo.restype=c_bool
        d.chcusb_blinkLED.argtypes=[POINTER(c_ushort)]; d.chcusb_blinkLED.restype=c_bool
        d.chcusb_resetPrinter.argtypes=[POINTER(c_ushort)]; d.chcusb_resetPrinter.restype=c_bool
        # Prototypes impression
        d.chcusb_setPrinterInfo.argtypes=[c_ushort,c_void_p,POINTER(c_ulong),POINTER(c_ushort)]; d.chcusb_setPrinterInfo.restype=c_bool
        if model=="68xx":
            d.chcusb_imageformat.argtypes=[c_ushort,c_ushort,c_ushort,c_ushort,c_ushort,POINTER(c_ushort)]
        else:
            d.chcusb_imageformat.argtypes=[c_ushort,c_ushort,c_ushort,c_ushort,c_ushort,c_ushort,c_ushort,POINTER(c_ushort)]
        d.chcusb_imageformat.restype=c_bool
        d.chcusb_copies.argtypes=[c_ushort,POINTER(c_ushort)]; d.chcusb_copies.restype=c_bool
        d.chcusb_startpage.argtypes=[POINTER(c_ushort),POINTER(c_ushort)]; d.chcusb_startpage.restype=c_bool
        d.chcusb_write.argtypes=[POINTER(c_ubyte),POINTER(c_ulong),POINTER(c_ushort)]; d.chcusb_write.restype=c_bool
        d.chcusb_endpage.argtypes=[POINTER(c_ushort)]; d.chcusb_endpage.restype=c_bool
        d.chcusb_setIcctable.argtypes=[c_void_p,c_void_p,c_ushort,c_void_p,c_void_p,c_void_p,c_void_p,c_void_p,c_void_p,POINTER(c_ushort)]
        d.chcusb_setIcctable.restype=c_bool
        d.chcusb_setmtf.argtypes=[c_void_p]; d.chcusb_setmtf.restype=None

    # --- Monitoring ---
    def open(self):
        r=c_ushort(0); ok=self._dll.chcusb_open(byref(r)); self._is_open=ok; return ok,r.value
    def close(self):
        if self._is_open: self._dll.chcusb_close(); self._is_open=False
    def listup(self):
        a=(c_ubyte*MAX_USB_DEVICE)()
        for i in range(MAX_USB_DEVICE): a[i]=0xFF
        n=self._dll.chcusb_listupPrinter(a); return [a[i] for i in range(n) if a[i]!=0xFF]
    def select(self, pid):
        r=c_ushort(0); self._dll.chcusb_selectPrinter(c_ubyte(pid),byref(r)); return r.value
    def get_status(self):
        r=c_ushort(0); ok=self._dll.chcusb_status(byref(r)); return ok,r.value
    def status_all(self, ids):
        a=(c_ubyte*MAX_USB_DEVICE)(); b=(c_ushort*MAX_USB_DEVICE)()
        for i in range(MAX_USB_DEVICE): a[i]=0xFF
        for i,p in enumerate(ids): a[i]=p
        self._dll.chcusb_statusAll(a,b); return {p:b[i] for i,p in enumerate(ids)}
    def info(self, tag, sz):
        buf=create_string_buffer(sz); l=c_ulong(sz)
        return buf.raw[:l.value] if self._dll.chcusb_getPrinterInfo(c_ushort(tag),buf,byref(l)) else None

    @staticmethod
    def _extract_serial_candidate(raw_bytes):
        """Extrait un candidat S/N ASCII depuis un blob binaire (6900)."""
        try:
            chunks = re.findall(rb"[ -~]{6,32}", raw_bytes or b"")
        except Exception:
            return ""
        for chunk in chunks:
            s = chunk.decode("ascii", errors="ignore").strip(" \x00")
            if not s:
                continue
            up = s.upper()
            # Ecarter les labels firmware/modele.
            if any(token in up for token in ("KODAK", "FIRMWARE", "VERSION", "VER", "6900", "6950")):
                continue
            if re.fullmatch(r"[A-Z0-9\\-]{6,24}", up):
                digits = sum(ch.isdigit() for ch in up)
                letters = sum(ch.isalpha() for ch in up)
                if digits >= 3 and letters >= 1:
                    return up
        return ""

    def firmware(self):
        if self.model=="68xx":
            d=self.info(3,16)
            if d and len(d)>=16:
                return {"main_boot":int.from_bytes(d[0:2],'little'),"main_control":int.from_bytes(d[2:4],'little'),
                        "serial":d[8:16].decode('ascii',errors='replace').strip('\x00')}
        else:
            d=self.info(3,115)
            if d and len(d)>=8:
                out = {"firmware_hex": d[:32].hex(), "firmware_fp": hashlib.sha1(d).hexdigest()[:16]}
                serial = self._extract_serial_candidate(d)
                if serial:
                    out["serial"] = serial
                return out
        return None
    def counts(self):
        d=self.info(4,20 if self.model=="68xx" else 28)
        if d and len(d)>=20:
            return {"total":int.from_bytes(d[0:4],'little'),"maintenance":int.from_bytes(d[4:8],'little'),
                    "depuis_remp":int.from_bytes(d[8:12],'little'),"coupe":int.from_bytes(d[12:16],'little'),
                    "media_restant":int.from_bytes(d[16:20],'little')}
        return None

    # --- Impression SDK 68xx / 10x15 ---
    def _check_sdk(self, ok, code, label):
        if not ok:
            info = get_status_info(int(code))
            desc = info.get("description", "unknown")
            raise RuntimeError(f"{label} failed: {int(code)} ({desc})")

    def paper_entries(self):
        if self.model != "68xx":
            return []
        raw = self.info(KPINFTAG_PAPER, 121)
        if not raw:
            return []
        count = raw[0]
        out = []
        offset = 1
        for _ in range(count):
            entry = raw[offset:offset + KSIZEOF_PINF_PAPERSET]
            if len(entry) < KSIZEOF_PINF_PAPERSET:
                break
            out.append({
                "raw": entry,
                "media_code": entry[0],
                "width": int.from_bytes(entry[1:3], "little"),
                "height": int.from_bytes(entry[3:5], "little"),
                "component": entry[5],
                "print_type": entry[6],
                "combo_second": entry[7],
            })
            offset += KSIZEOF_PINF_PAPERSET
        return out

    def _find_paper_entry(self, media_code, print_type, width, height):
        entries = self.paper_entries()
        for e in entries:
            if (e["media_code"] == media_code and e["print_type"] == print_type
                    and e["width"] == width and e["height"] == height):
                return e["raw"]
        available = ", ".join(
            f"code={e['media_code']} type={e['print_type']} size={e['width']}x{e['height']}"
            for e in entries
        )
        raise RuntimeError(
            f"Format papier SDK non annonce: code={media_code} type={print_type} "
            f"size={width}x{height}. Disponibles: {available or 'aucun'}"
        )

    def set_printer_info_raw(self, tag, data, expected_len, label):
        buf = (c_ubyte * len(data)).from_buffer_copy(data)
        length = c_ulong(expected_len)
        result = c_ushort(0)
        ok = self._dll.chcusb_setPrinterInfo(c_ushort(tag), ctypes.cast(buf, c_void_p), byref(length), byref(result))
        self._check_sdk(ok, result.value, label)

    def set_paper_10x15(self):
        entry = self._find_paper_entry(
            SDK_10X15_MEDIA_CODE, SDK_10X15_PRINT_TYPE, SDK_10X15_WIDTH, SDK_10X15_HEIGHT
        )
        self.set_printer_info_raw(KPINFTAG_PAPER, entry, KSIZEOF_PINF_PAPERSET, "chcusb_setPrinterInfo(PAPER)")

    def set_polish(self, polish=SDK_10X15_POLISH):
        data = int(polish).to_bytes(2, "little")
        self.set_printer_info_raw(KPINFTAG_POLISH, data, KSIZEOF_PINF_POLISHSET, "chcusb_setPrinterInfo(POLISH)")

    def imageformat_10x15(self):
        result = c_ushort(0)
        ok = self._dll.chcusb_imageformat(
            c_ushort(KFORMAT_PIXEL_RGB), c_ushort(KCOMPONENT_RGB), c_ushort(KDEPTH_8BIT),
            c_ushort(SDK_10X15_WIDTH), c_ushort(SDK_10X15_HEIGHT), byref(result)
        )
        self._check_sdk(ok, result.value, "chcusb_imageformat")

    def set_copies(self, copies):
        result = c_ushort(0)
        ok = self._dll.chcusb_copies(c_ushort(max(1, min(999, int(copies)))), byref(result))
        self._check_sdk(ok, result.value, "chcusb_copies")

    def startpage(self):
        page_id = c_ushort(0)
        result = c_ushort(0)
        ok = self._dll.chcusb_startpage(byref(page_id), byref(result))
        self._check_sdk(ok, result.value, "chcusb_startpage")
        return page_id.value

    def write_bytes(self, data, label="chcusb_write", chunk_size=1024 * 1024):
        for offset in range(0, len(data), chunk_size):
            chunk = data[offset:offset + chunk_size]
            arr = (c_ubyte * len(chunk)).from_buffer_copy(chunk)
            length = c_ulong(len(chunk))
            result = c_ushort(0)
            ok = self._dll.chcusb_write(arr, byref(length), byref(result))
            self._check_sdk(ok, result.value, label)

    def endpage(self):
        result = c_ushort(0)
        ok = self._dll.chcusb_endpage(byref(result))
        self._check_sdk(ok, result.value, "chcusb_endpage")

    def print_10x15_rgb(self, rgb_data, copies=1, printer_id=None):
        if self.model != "68xx":
            raise RuntimeError("L'impression SDK 10x15 est disponible uniquement sur Kodak 68xx.")
        expected = SDK_10X15_WIDTH * SDK_10X15_HEIGHT * 3
        if len(rgb_data) != expected:
            raise RuntimeError(f"Buffer RGB invalide: attendu {expected}, obtenu {len(rgb_data)}")

        pids = self.listup()
        if printer_id is None:
            if not pids:
                raise RuntimeError("Aucune imprimante 68xx detectee par le SDK")
            printer_id = pids[0]
        elif printer_id not in pids:
            raise RuntimeError(f"Imprimante USB id={printer_id} non detectee (detectees: {pids})")

        sel_code = self.select(printer_id)
        if sel_code != 0:
            raise RuntimeError(f"chcusb_selectPrinter failed: {sel_code}")
        ok, status_code = self.get_status()
        self._check_sdk(ok, status_code, "chcusb_status")
        if status_code != 0:
            info = get_status_info(status_code)
            raise RuntimeError(f"Imprimante non prete: {status_code} ({info.get('description', 'unknown')})")

        self.set_paper_10x15()
        self.set_polish(SDK_10X15_POLISH)
        self.imageformat_10x15()
        self.set_copies(copies)
        page_id = self.startpage()
        self.write_bytes(rgb_data, "chcusb_write(10x15 image)")
        self.endpage()
        return {"printer_id": int(printer_id), "page_id": int(page_id), "copies": int(copies)}


# ============================================================================
#  MONITEUR
# ============================================================================
SDKS_DEF = {"68xx":{"dll":"chcusb.dll","nom":"Kodak 6800/6850"},"6900":{"dll":"KA6900.dll","nom":"Kodak 6900/6950"}}

class Monitor:
    def __init__(self, json_path=None):
        if json_path is None:
            json_path = os.path.join(SCRIPT_DIR, "kodak_printer_status.json")
        cfg = load_gui_config()
        self.interval = max(1, cfg.get("scan_interval", 2))
        self.detail_interval = max(10, cfg.get("scan_detail_interval", 60))
        try:
            self.max_untrusted_delta = max(1, int(cfg.get("max_untrusted_identity_delta", 200)))
        except Exception:
            self.max_untrusted_delta = 200
        self.json_path=json_path; self.sdks={}; self.msgs=[]
        self._lock = threading.Lock()  # Verrou pour accès SDK exclusif
        self._last_statuses = {}  # Pour détecter les changements d'état
        self._last_scan_logs = {}
        self._scan_log_repeat_seconds = 3600
        self._last_can_print = None
        self._totals_file = os.path.join(SCRIPT_DIR, ".kodak_hw_totals.json")
        self._identity_file = os.path.join(SCRIPT_DIR, ".kodak_hw_identities.json")
        self._last_totals = self._load_last_totals()  # Persisté pour survivre aux redémarrages
        self._last_identities = self._load_last_identities()  # Liaison clé USB -> imprimante physique
        self._last_detail_time = 0  # Timestamp du dernier scan détaillé (firmware/compteurs)
        self._cached_details = {}  # Cache firmware/compteurs entre scans détaillés
        self._last_result = {}  # Dernier résultat de scan (protégé par _lock)
        self.sdk_print_notice_seconds = max(5, int(cfg.get("sdk_print_notice_seconds", 30)))
        self.sdk_print_gap_seconds = max(1, int(cfg.get("sdk_print_gap_seconds", 75)))
        self.windows_spooler_grace_seconds = max(1, int(cfg.get("windows_driver_post_print_delay", WINDOWS_SPOOLER_GRACE_SECONDS)))
        self.preferred_windows_printer = str(cfg.get("hotfolder_printer", "") or "").strip()
        self._sdk_print_active_until = 0
        self._next_sdk_print_allowed_at = 0
        self._windows_spooler_active_until = 0
        self._windows_spooler_last_printers = []
        self._windows_spooler_busy = False
        self._windows_spooler_stop = threading.Event()
        self._windows_spooler_thread = threading.Thread(
            target=self._windows_spooler_poll_loop,
            daemon=True,
            name="WindowsSpoolerPoll",
        )
        self._windows_spooler_thread.start()
        for m,info in SDKS_DEF.items():
            for d in [os.path.join(SCRIPT_DIR,m), os.path.join(SCRIPT_DIR,m.upper()), SCRIPT_DIR]:
                if os.path.isdir(d) and os.path.exists(os.path.join(d,info["dll"])):
                    try:
                        self.sdks[m]=KodakSDK(m,d); self.msgs.append(("ok",f"SDK {info['nom']} trouvé ({info['dll']})")); break
                    except Exception as e: self.msgs.append(("err",f"SDK {info['nom']}: {e}"))
            else: self.msgs.append(("skip",f"SDK {info['nom']} non trouvé"))


    def _log_scan_status(self, model, pids, statuses, now=None):
        now = time.time() if now is None else now
        codes = [statuses.get(pid, -1) for pid in pids]
        signature = (tuple(pids), tuple(codes))
        previous = self._last_scan_logs.get(model)
        should_log = (
            previous is None
            or previous.get("signature") != signature
            or (now - previous.get("at", 0)) >= self._scan_log_repeat_seconds
        )
        if should_log:
            log_print(f"[SCAN] {model.upper()} pids={pids} codes={codes}")
            self._last_scan_logs[model] = {"signature": signature, "at": now}


    def _load_last_totals(self):
        """Charge les derniers compteurs matériels depuis le disque."""
        p = self._totals_file
        try:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_last_totals(self):
        """Sauvegarde les compteurs matériels sur disque."""
        try:
            with open(self._totals_file, "w", encoding="utf-8") as f:
                json.dump(self._last_totals, f)
        except Exception as e:
            log_print(f"[Monitor] Impossible de sauvegarder les totaux: {e}")

    def _load_last_identities(self):
        """Charge les identités d'imprimantes depuis le disque."""
        p = self._identity_file
        try:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
        except Exception:
            pass
        return {}

    def _save_last_identities(self):
        """Sauvegarde les identités d'imprimantes sur disque."""
        try:
            with open(self._identity_file, "w", encoding="utf-8") as f:
                json.dump(self._last_identities, f)
        except Exception:
            pass

    @staticmethod
    def _printer_identity(printer_data):
        """
        Retourne une identité stable de l'imprimante physique.
        Priorité:
        - numéro de série (fiable)
        - empreinte firmware (fallback)
        """
        fw = printer_data.get("firmware", {}) or {}
        serial = str(fw.get("serial", "")).strip()
        if serial:
            return f"serial:{serial}"
        firmware_fp = str(fw.get("firmware_fp", "")).strip()
        if firmware_fp:
            return f"fwfp:{firmware_fp}"
        firmware_hex = str(fw.get("firmware_hex", "")).strip()
        if firmware_hex:
            return f"fwhex:{firmware_hex}"
        return None

    @staticmethod
    def _identity_is_trusted(identity):
        """Une identité est fiable si elle vient d'un vrai numéro de série."""
        return bool(identity) and str(identity).startswith("serial:")

    def _write_status_json(self, result):
        try:
            t = self.json_path + ".tmp"
            with open(t, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            os.replace(t, self.json_path)
        except Exception as e:
            log_print(f"[Monitor] Impossible de sauvegarder le statut JSON: {e}")

    def _is_target_windows_printer(self, printer_name, driver_name=""):
        name = str(printer_name or "").strip()
        driver = str(driver_name or "").strip()
        if not name:
            return False
        if self.preferred_windows_printer and name.lower() == self.preferred_windows_printer.lower():
            return True
        hay = f"{name} {driver}".upper()
        return any(token in hay for token in KODAK_PRINTER_HINTS)

    @staticmethod
    def _windows_printer_fields(printer):
        if isinstance(printer, dict):
            return {
                "name": str(printer.get("pPrinterName", "") or ""),
                "driver": str(printer.get("pDriverName", "") or ""),
                "jobs": int(printer.get("cJobs", 0) or 0),
                "status": int(printer.get("Status", 0) or 0),
            }

        # Fallback pour les anciens pywin32: tester les positions les plus courantes.
        values = list(printer) if isinstance(printer, (tuple, list)) else []
        candidates = []
        for idx in (2, 1):
            if len(values) > idx:
                candidates.append(str(values[idx] or ""))
        name = next((v for v in candidates if v and not v.startswith("\\")), candidates[0] if candidates else "")
        driver = str(values[4] if len(values) > 4 else "")
        jobs = int(values[19] if len(values) > 19 and values[19] else 0)
        status = int(values[18] if len(values) > 18 and values[18] else 0)
        return {"name": name, "driver": driver, "jobs": jobs, "status": status}

    def _get_windows_spooler_jobs(self):
        try:
            import win32print
        except Exception:
            return []

        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        try:
            printers = win32print.EnumPrinters(flags, None, 2)
        except Exception:
            return []

        active = []
        for printer in printers:
            fields = self._windows_printer_fields(printer)
            name = fields["name"]
            driver = fields["driver"]
            if not self._is_target_windows_printer(name, driver):
                continue

            if fields["jobs"] > 0 or (fields["status"] & WINDOWS_PRINTER_ACTIVE_STATUS_MASK):
                active.append({
                    "printer": str(name),
                    "jobs": max(1, fields["jobs"]),
                    "source": "printer-status",
                })
                continue

            handle = None
            try:
                handle = win32print.OpenPrinter(name)
                try:
                    jobs = win32print.EnumJobs(handle, 0, 99, 2)
                except Exception:
                    jobs = win32print.EnumJobs(handle, 0, 99, 1)
                if jobs:
                    active.append({"printer": str(name), "jobs": len(jobs), "source": "jobs"})
            except Exception:
                continue
            finally:
                if handle is not None:
                    try:
                        win32print.ClosePrinter(handle)
                    except Exception:
                        pass
        return active

    def _refresh_windows_spooler_state(self):
        now = time.time()
        active_jobs = self._get_windows_spooler_jobs()
        if active_jobs:
            self._windows_spooler_last_printers = [j.get("printer", "") for j in active_jobs if j.get("printer")]
            self._windows_spooler_active_until = now + self.windows_spooler_grace_seconds

        active = now < self._windows_spooler_active_until
        if active != self._windows_spooler_busy:
            if active:
                names = ", ".join(self._windows_spooler_last_printers) or "imprimante Kodak"
                log_print(f"ℹ [SPOOLER] Impression Windows détectée — accès SDK suspendu ({names})")
            else:
                log_print("ℹ [SPOOLER] File Windows inactive — reprise de la surveillance SDK")
            self._windows_spooler_busy = active
        return active

    def _windows_spooler_poll_loop(self):
        while not self._windows_spooler_stop.wait(WINDOWS_SPOOLER_POLL_SECONDS):
            try:
                self._refresh_windows_spooler_state()
            except Exception as e:
                log_print(f"[SPOOLER] Surveillance Windows interrompue: {e}")
                time.sleep(2)

    def is_windows_driver_printing(self):
        return self._refresh_windows_spooler_state()

    def _build_windows_spooler_result(self):
        status = get_status_info(2100).copy()
        printer_names = ", ".join(self._windows_spooler_last_printers) or "pilote Windows"
        status["categorie"] = "IMPRESSION WINDOWS"
        status["description"] = "Impression en cours via le pilote Windows"
        status["conseil"] = f"Surveillance SDK suspendue pour ne pas perturber {printer_names}"
        result = {
            "horodatage": datetime.now().isoformat(),
            "peut_imprimer": False,
            "imprimantes": [{
                "modele": "WINDOWS",
                "id": 0,
                "peut_imprimer": False,
                "statut_code": 2100,
                "statut": status,
            }],
            "erreurs_systeme": [],
        }
        self._last_result = result
        self._write_status_json(result)
        return result

    def _set_sdk_print_status(self, description, detail="", hold_seconds=None, printer_id=0):
        hold = self.sdk_print_notice_seconds if hold_seconds is None else max(1, int(hold_seconds))
        self._sdk_print_active_until = max(self._sdk_print_active_until, time.time() + hold)
        status = get_status_info(2100).copy()
        status["description"] = description
        status["conseil"] = detail
        result = {
            "horodatage": datetime.now().isoformat(),
            "peut_imprimer": False,
            "imprimantes": [{
                "modele": "68XX",
                "id": int(printer_id or 0),
                "peut_imprimer": False,
                "statut_code": 2100,
                "statut": status,
            }],
            "erreurs_systeme": [],
        }
        self._last_result = result
        self._write_status_json(result)

    def _image_to_10x15_rgb(self, image_path):
        try:
            from PIL import Image, ImageOps
        except Exception as e:
            raise RuntimeError(f"Pillow requis pour imprimer les images: {e}")

        with Image.open(image_path) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            target_ratio = SDK_10X15_WIDTH / SDK_10X15_HEIGHT
            ratio = img.width / img.height
            if ratio > target_ratio:
                new_width = int(img.height * target_ratio)
                left = (img.width - new_width) // 2
                img = img.crop((left, 0, left + new_width, img.height))
            elif ratio < target_ratio:
                new_height = int(img.width / target_ratio)
                top = (img.height - new_height) // 2
                img = img.crop((0, top, img.width, top + new_height))
            img = img.resize((SDK_10X15_WIDTH, SDK_10X15_HEIGHT), Image.Resampling.LANCZOS)
            return img.tobytes("raw", "RGB")

    def print_image_10x15_sdk(self, image_path, copies=1):
        image_path = os.path.abspath(image_path)
        if not os.path.isfile(image_path):
            raise FileNotFoundError(image_path)
        if "68xx" not in self.sdks:
            raise RuntimeError("SDK Kodak 68xx introuvable: impossible d'imprimer via le SDK")
        if self.is_windows_driver_printing():
            names = ", ".join(self._windows_spooler_last_printers) or "le pilote Windows"
            raise RuntimeError(f"Impression Windows active — attente avant accès SDK ({names})")
        copies = max(1, min(999, int(copies)))
        rgb = self._image_to_10x15_rgb(image_path)

        with self._lock:
            if self.is_windows_driver_printing():
                names = ", ".join(self._windows_spooler_last_printers) or "le pilote Windows"
                raise RuntimeError(f"Impression Windows active — attente avant accès SDK ({names})")
            wait = self._next_sdk_print_allowed_at - time.time()
            if wait > 0:
                self._set_sdk_print_status(
                    "Attente buffer imprimante",
                    f"Prochaine impression dans {int(wait)} s",
                    hold_seconds=int(wait) + 2,
                )
                end = time.time() + wait
                while time.time() < end:
                    time.sleep(min(1, end - time.time()))

            sdk = self.sdks["68xx"]
            self._set_sdk_print_status(
                "Impression SDK en cours",
                os.path.basename(image_path),
                hold_seconds=max(self.sdk_print_notice_seconds, self.sdk_print_gap_seconds),
            )
            ok, code = sdk.open()
            if not ok:
                raise RuntimeError(f"Impossible ouvrir port USB 68xx: {code}")
            try:
                result = sdk.print_10x15_rgb(rgb, copies=copies)
                pid = result.get("printer_id", 0)
                self._set_sdk_print_status(
                    "Impression SDK envoyee",
                    f"{os.path.basename(image_path)} - {copies} copie(s)",
                    hold_seconds=max(self.sdk_print_notice_seconds, self.sdk_print_gap_seconds),
                    printer_id=pid,
                )
                self._next_sdk_print_allowed_at = time.time() + self.sdk_print_gap_seconds
                log_print(f"[SDK PRINT] 10x15 envoye: {os.path.basename(image_path)} ({copies} copie(s), USB id={pid})")
                return result
            finally:
                sdk.close()

    def scan(self):
        with self._lock:
            r={"horodatage":datetime.now().isoformat(),"peut_imprimer":False,"imprimantes":[],"erreurs_systeme":[]}
            ok_any=False; printing_detected=False
            now = time.time()
            if now < self._sdk_print_active_until and self._last_result:
                return self._last_result
            if self.is_windows_driver_printing():
                return self._build_windows_spooler_result()
            need_details = (now - self._last_detail_time) >= self.detail_interval

            for m,sdk in self.sdks.items():
                ok,code=sdk.open()
                if not ok:
                    _si = get_status_info(code)
                    r["erreurs_systeme"].append({"modele":m,"code":code,"description":_si["description"],"conseil":_si.get("conseil","")})
                    continue
                try:
                    # Listup seulement au scan détaillé ou si on n'a pas encore de PIDs
                    cached_pids_key = f"_pids_{m}"
                    if need_details or not hasattr(self, cached_pids_key):
                        pids = sdk.listup()
                        if pids:
                            setattr(self, cached_pids_key, pids)
                    else:
                        pids = getattr(self, cached_pids_key, [])

                    if not pids:
                        r["erreurs_systeme"].append({"modele":m,"code":-1,"description":f"Aucune {m.upper()} détectée"})
                        continue

                    # StatusAll = rapide, pas besoin de select
                    st = sdk.status_all(pids)
                    self._log_scan_status(m, pids, st, now)
                    for pid in pids:
                        sc=st.get(pid,-1); si=get_status_info(sc); rdy=(sc==0)
                        if rdy: ok_any=True
                        if 2100 <= sc <= 2212:
                            printing_detected=True
                        pd={"modele":m.upper(),"id":pid,"peut_imprimer":rdy,"statut_code":sc,"statut":si}
                        key = f"{m.upper()}_{pid}"

                        # Firmware/compteurs seulement au scan détaillé, pas pendant impression
                        if need_details and not printing_detected:
                            sdk.select(pid)
                            try:
                                fw=sdk.firmware()
                                if fw:
                                    pd["firmware"]=fw
                                    self._cached_details[f"{key}_fw"] = fw
                            except Exception as e:
                                log_print(f"[SDK] firmware() erreur ({key}): {e}")
                            try:
                                c=sdk.counts()
                                if c:
                                    pd["compteurs"]=c
                                    self._cached_details[f"{key}_ct"] = c
                            except Exception as e:
                                log_print(f"[SDK] counts() erreur ({key}): {e}")
                        else:
                            if f"{key}_fw" in self._cached_details:
                                pd["firmware"] = self._cached_details[f"{key}_fw"]
                            if f"{key}_ct" in self._cached_details:
                                pd["compteurs"] = self._cached_details[f"{key}_ct"]
                        r["imprimantes"].append(pd)
                finally:
                    sdk.close()

            if need_details and not printing_detected:
                self._last_detail_time = now
            r["peut_imprimer"]=ok_any
            self._last_result = r

            # --- Log des changements d'état ---
            # Erreurs système
            for e in r.get("erreurs_systeme", []):
                key = f"sys_{e.get('modele','?')}"
                desc = e.get("description", "?")
                if self._last_statuses.get(key) != desc:
                    log_print(f"⚠ [{e.get('modele','?')}] {desc}")
                    self._last_statuses[key] = desc

            # État par imprimante
            for p in r.get("imprimantes", []):
                key = f"{p.get('modele','?')}_{p.get('id','?')}"
                sc = p.get("statut_code", -1)
                prev_sc = self._last_statuses.get(key)
                if prev_sc != sc:
                    si = p.get("statut", {})
                    cat = si.get("categorie", "?")
                    desc = si.get("description", "?")
                    sev = si.get("gravite", "?")
                    if sc == 0:
                        log_print(f"✅ [{key}] Imprimante prête")
                    else:
                        log_print(f"❌ [{key}] {cat} — {desc} (code {sc}, {sev})")
                    self._last_statuses[key] = sc

                # Détecter les impressions via le compteur matériel
                compteurs = p.get("compteurs", {})
                total_hw = compteurs.get("total")
                skip_increment = False

                identity = self._printer_identity(p)
                if identity:
                    prev_identity = self._last_identities.get(key)
                    if prev_identity is None:
                        # Premier rattachement identité/clé: on évite un rattrapage potentiellement faux.
                        if key in self._last_totals and total_hw is not None and self._last_totals.get(key) != total_hw:
                            skip_increment = True
                            log_print(f"ℹ [{key}] Initialisation identité {identity}: recalage compteur sans incrément.")
                    elif prev_identity != identity:
                        # La clé USB est identique mais l'imprimante physique a changé.
                        skip_increment = True
                        log_print(f"ℹ [{key}] Changement d'imprimante ({prev_identity} -> {identity}): delta ignoré.")

                    if prev_identity != identity:
                        self._last_identities[key] = identity
                        self._save_last_identities()

                if total_hw is not None:
                    prev_total = self._last_totals.get(key)
                    if (not skip_increment) and prev_total is not None and total_hw > prev_total:
                        new_prints = total_hw - prev_total
                        # Cap pour identité non fiable
                        if (not self._identity_is_trusted(identity)) and new_prints > self.max_untrusted_delta:
                            skip_increment = True
                            log_print(
                                f"ℹ [{key}] Delta important sans identité série fiable ({new_prints} > {self.max_untrusted_delta}): "
                                f"recalage sans incrément."
                            )
                        # Cap absolu même pour identité fiable (protège contre reset/corruption de .kodak_hw_totals.json)
                        elif new_prints > 10000:
                            skip_increment = True
                            log_print(
                                f"ℹ [{key}] Delta anormalement élevé ({new_prints}) même avec identité fiable — "
                                f"probable reset du fichier compteur matériel. Recalage sans incrément."
                            )
                        else:
                            log_print(f"🖨 [{key}] {new_prints} impression(s) détectée(s) (compteur: {prev_total} → {total_hw})")
                            increment_print_counter(new_prints)
                    if self._last_totals.get(key) != total_hw:
                        self._last_totals[key] = total_hw
                        self._save_last_totals()

            # Changement global peut_imprimer
            if self._last_can_print is not None and self._last_can_print != ok_any:
                if ok_any:
                    log_print("✅ Statut global: PRÊTE À IMPRIMER")
                else:
                    log_print("❌ Statut global: IMPRESSION IMPOSSIBLE")
            self._last_can_print = ok_any
            try:
                t=self.json_path+".tmp"
                with open(t,'w',encoding='utf-8') as f: json.dump(r,f,ensure_ascii=False,indent=2)
                os.replace(t,self.json_path)
            except Exception as e:
                log_print(f"[Monitor] Impossible de sauvegarder le statut JSON: {e}")
            return r





# ============================================================================
#  HOTFOLDER — Surveillance dossier + impression automatique
# ============================================================================

class HotFolder:
    """Surveille un dossier et imprime automatiquement en SDK Kodak 68xx 10x15."""

    ACTIONS = ["Supprimer apres impression", "Deplacer dans 'imprime/'", "Laisser dans le dossier"]

    def __init__(self, path, copies, action, gap_sec, monitor=None):
        self.path = path
        self.copies = max(1, int(copies))
        self.action = action
        self.gap_sec = max(1, int(gap_sec))
        self.monitor = monitor
        self._stop = threading.Event()
        self._thread = None
        self._seen = {}
        self._processed = set()
        self._last_print = 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="HotFolder")
        self._thread.start()
        log_print(f"[Hotfolder] Demarre - {self.path}")

    def stop(self):
        self._stop.set()

    def is_running(self):
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set())

    def _file_signature(self, fpath):
        st = os.stat(fpath)
        return (fpath, int(st.st_mtime_ns), int(st.st_size))

    def _run(self):
        exts = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
        while not self._stop.wait(2):
            try:
                if not self.path or not os.path.isdir(self.path):
                    continue
                now = time.time()
                try:
                    entries = sorted(os.listdir(self.path))
                except OSError:
                    continue
                for fname in entries:
                    if self._stop.is_set():
                        break
                    if os.path.splitext(fname.lower())[1] not in exts:
                        continue
                    fpath = os.path.join(self.path, fname)
                    if not os.path.isfile(fpath):
                        continue
                    try:
                        sig = self._file_signature(fpath)
                    except OSError:
                        continue
                    if sig in self._processed:
                        continue
                    seen = self._seen.get(fpath)
                    if not seen or seen.get("sig") != sig:
                        self._seen[fpath] = {"time": now, "sig": sig}
                        continue
                    if now - seen["time"] < 2:
                        continue
                    if self.monitor is not None and self.monitor.is_windows_driver_printing():
                        continue
                    if now - self._last_print < self.gap_sec:
                        continue
                    self._process(fpath, fname, sig)
                self._seen = {k: v for k, v in self._seen.items() if os.path.exists(k)}
                if len(self._processed) > 1000:
                    self._processed = set(list(self._processed)[-500:])
            except Exception as e:
                log_print(f"[Hotfolder] Erreur: {e}")
        log_print("[Hotfolder] Arrete")

    def _process(self, fpath, fname, sig):
        printed = False
        try:
            log_print(f"[Hotfolder] Impression SDK 10x15: {fname} ({self.copies} copie(s))")
            self._print_file(fpath)
            printed = True
            self._last_print = time.time()
            if "Supprimer" in self.action:
                try:
                    os.remove(fpath)
                    log_print(f"[Hotfolder] Supprime: {fname}")
                except OSError as e:
                    log_print(f"[Hotfolder] Suppression impossible ({fname}): {e}")
                    self._processed.add(sig)
            elif "Deplacer" in self.action or "D?placer" in self.action:
                done = os.path.join(self.path, "imprime")
                os.makedirs(done, exist_ok=True)
                base, ext = os.path.splitext(fname)
                dest = os.path.join(done, fname)
                if os.path.exists(dest):
                    dest = os.path.join(done, f"{base}_{int(time.time())}{ext}")
                try:
                    os.replace(fpath, dest)
                    log_print(f"[Hotfolder] Deplace: {fname} -> imprime/")
                except OSError as e:
                    log_print(f"[Hotfolder] Deplacement impossible ({fname}): {e}")
                    self._processed.add(sig)
            else:
                # Evite de re-imprimer en boucle si l'utilisateur garde le fichier.
                self._processed.add(sig)
                log_print(f"[Hotfolder] Fichier conserve: {fname}")
        except Exception as e:
            log_print(f"[Hotfolder] Erreur {fname}: {e}")
        finally:
            if printed and len(self._processed) > 1000:
                self._processed = set(list(self._processed)[-500:])
            self._seen.pop(fpath, None)

    def _print_file(self, fpath):
        if self.monitor is None:
            raise RuntimeError("Hotfolder sans moniteur SDK")
        return self.monitor.print_image_10x15_sdk(fpath, copies=self.copies)


# ============================================================================
#  GUI — MINI POPUP + FENÊTRE CONFIG
# ============================================================================

def run_gui(mon):
    import tkinter as tk
    from tkinter import ttk, filedialog

    BG="#0d1117";BC="#161b22";BD="#010409";BO="#30363d"
    TX="#e6edf3";TD="#7d8590";AM="#f0883e";GR="#3fb950"
    RD="#f85149";YL="#d29922";BL="#58a6ff";PU="#bc8cff"
    POPUP_BG="#f8fafc";POPUP_BORDER="#d7dee8";POPUP_BADGE="#eef3f8";POPUP_MUTED="#5f6f82"
    POPUP_TEXT="#172033";POPUP_BTN_HOVER="#dfe7f0"
    SV={"ok":{"fg":GR,"bg":"#0d2818"},"attente":{"fg":YL,"bg":"#2a1f00"},
        "impression":{"fg":GR,"bg":"#0d2818"},
        "erreur":{"fg":RD,"bg":"#2d0a0a"},"critique":{"fg":"#ff4040","bg":"#3d0a0a"}}

    config_win = [None]  # référence fenêtre config
    config_visible = [False]  # suspend le plein écran tant que la config est ouverte
    hotfolder = [None]  # instance HotFolder active
    gui_cfg = load_gui_config()
    try:
        trim_log_by_days(max(1, int(gui_cfg.get("log_retention_days", 30))))
        trim_log_by_size(max(1, int(gui_cfg.get("log_max_mb", 10))))
    except Exception as e:
        log_print(f"⚠ Purge log impossible: {e}")
    _fs = [max(8, min(36, gui_cfg.get("popup_font_size", 11)))]  # [font_size] mutable pour les closures
    # Appliquer les intervalles de scan lus depuis le fichier config
    mon.interval = max(1, gui_cfg.get("scan_interval", 2))
    mon.detail_interval = max(10, gui_cfg.get("scan_detail_interval", 60))

    # === MINI POPUP (toujours visible) ===
    root = tk.Tk()
    root.title("Kodak")
    try:
        root.iconbitmap(_resource("icone-imprimante.ico"))
    except Exception:
        try:
            _app_icon = tk.PhotoImage(file=_resource("icone-imprimante.png"))
            root.iconphoto(True, _app_icon)
        except Exception:
            pass
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.resizable(False, False)
    root.configure(bg=POPUP_BG)

    popup_close_allowed = tk.BooleanVar(master=root, value=bool(gui_cfg.get("allow_popup_close", False)))
    fullscreen_on_error = tk.BooleanVar(master=root, value=bool(gui_cfg.get("fullscreen_on_error", False)))
    hide_popup_when_ready = tk.BooleanVar(master=root, value=bool(gui_cfg.get("hide_popup_when_ready", False)))
    protect_config_access = tk.BooleanVar(master=root, value=bool(gui_cfg.get("protect_config_access", False)))
    config_access_pin_var = tk.StringVar(master=root, value="")
    config_pin_prompt = [None]

    popup = tk.Frame(root, bg=POPUP_BORDER, padx=1, pady=1)
    popup.pack(fill=tk.BOTH, expand=True)

    popup_inner = tk.Frame(popup, bg=POPUP_BG, padx=12, pady=8)
    popup_inner.pack(fill=tk.BOTH, expand=True)

    status_accent = tk.Frame(popup_inner, bg=TD, width=3)
    status_accent.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 11))

    status_badge = tk.Frame(popup_inner, bg=POPUP_BADGE, width=36, height=36)
    status_badge.pack(side=tk.LEFT, padx=(0, 12), anchor="center")
    status_badge.pack_propagate(False)

    status_dot = tk.Label(status_badge, text="●", font=("Segoe UI", 18), bg=POPUP_BADGE, fg=TD)
    status_dot.place(relx=0.5, rely=0.48, anchor="center")

    status_block = tk.Frame(popup_inner, bg=POPUP_BG)
    status_block.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, anchor="center")

    status_top = tk.Frame(status_block, bg=POPUP_BG)
    status_top.pack(fill=tk.X)
    status_text = tk.Label(status_top, text="...", font=("Segoe UI Semibold", 11), bg=POPUP_BG, fg=POPUP_TEXT, anchor="w", justify="left")
    status_text.pack(side=tk.LEFT, anchor="w", fill=tk.X, expand=True)
    version_lbl_popup = tk.Label(status_top, text=f"v{APP_VERSION}", font=("Segoe UI Semibold", 9), bg=POPUP_BG, fg="#64748b", anchor="e")
    version_lbl_popup.pack(side=tk.RIGHT, padx=(8, 0), anchor="e")
    status_detail = tk.Label(status_block, text="", font=("Segoe UI", 9), bg=POPUP_BG, fg=POPUP_MUTED, anchor="w", justify="left", wraplength=620)
    status_detail.pack(anchor="w", pady=(3, 0))

    def release_popup_for_config():
        """Laisse la fenêtre PIN/config passer devant en gardant le plein écran bloquant."""
        config_visible[0] = True
        try:
            root.attributes("-topmost", False)
        except tk.TclError:
            pass

    def restore_popup_after_config():
        config_visible[0] = False
        apply_popup_mode(_current_mode())

    def ask_config_pin_then_open():
        release_popup_for_config()
        if config_pin_prompt[0] and config_pin_prompt[0].winfo_exists():
            try:
                config_pin_prompt[0].deiconify()
                config_pin_prompt[0].lift()
                config_pin_prompt[0].focus_force()
            except:
                pass
            return

        pw = tk.Toplevel(root)
        config_pin_prompt[0] = pw
        pw.title("Accès protégé")
        pw.configure(bg=BG)
        pw.geometry("430x900")
        pw.resizable(False, False)
        pw.attributes("-topmost", True)

        frm = tk.Frame(pw, bg=BC, padx=18, pady=16, highlightbackground=BO, highlightthickness=1)
        frm.pack(fill=tk.BOTH, expand=True, padx=12, pady=12)

        # En-tête PIN : titre + voyant statut + version
        pin_head = tk.Frame(frm, bg=BC); pin_head.pack(fill=tk.X, pady=(0, 4))
        tk.Label(pin_head, text="Configuration protégée", font=("Segoe UI", 12, "bold"), bg=BC, fg=AM).pack(side=tk.LEFT)
        tk.Label(pin_head, text=f"Version {APP_VERSION}", font=("Segoe UI", 10, "bold"), bg=BC, fg=BL).pack(side=tk.RIGHT, anchor="e")
        with mon._lock:
            _pin_r = mon._last_result or {}
        _pin_col = GR if _pin_r.get('peut_imprimer') else (YL if not mon.sdks else RD)
        _pin_txt = "Prête" if _pin_r.get('peut_imprimer') else ("Aucun SDK" if not mon.sdks else "Non disponible")
        pin_voyant_row = tk.Frame(frm, bg=BC); pin_voyant_row.pack(fill=tk.X, pady=(0, 6))
        tk.Label(pin_voyant_row, text="●", font=("Segoe UI", 10), bg=BC, fg=_pin_col).pack(side=tk.LEFT)
        tk.Label(pin_voyant_row, text=f" Imprimante — {_pin_txt}", font=("Segoe UI", 9), bg=BC, fg=_pin_col).pack(side=tk.LEFT)
        tk.Label(frm, text="Saisir le code PIN pour ouvrir le paramétrage.", font=("Segoe UI", 10), bg=BC, fg=TX).pack(anchor="w", pady=(0, 10))

        pin_entry_var = tk.StringVar(master=pw, value="")
        pin_entry = tk.Entry(frm, textvariable=pin_entry_var, show="●", font=("Segoe UI", 28, "bold"), bg=BD, fg=TX, insertbackground=TX, relief="flat", justify="center", width=10)
        pin_entry.pack(fill=tk.X, pady=(0, 12), ipady=12)

        info_lbl = tk.Label(frm, text="", font=("Segoe UI", 11, "bold"), bg=BC, fg=RD)
        info_lbl.pack(anchor="w", pady=(0, 10))

        def close_pin_prompt():
            try:
                pw.grab_release()
            except:
                pass
            try:
                pw.destroy()
            except:
                pass
            config_pin_prompt[0] = None
            if not (config_win[0] and config_win[0].winfo_exists()):
                restore_popup_after_config()

        def validate_pin(event=None):
            if _verify_config_pin(pin_entry_var.get(), gui_cfg.get("config_access_pin_hash", "")):
                try:
                    pw.grab_release()
                except:
                    pass
                try:
                    pw.destroy()
                except:
                    pass
                config_pin_prompt[0] = None
                build_config_window()
            else:
                info_lbl.config(text="Code PIN incorrect.")
                try:
                    pw.bell()
                except:
                    pass
                try:
                    pin_entry.selection_range(0, tk.END)
                    pin_entry.icursor(tk.END)
                    pin_entry.focus_force()
                except:
                    pass

        def append_digit(digit):
            current = pin_entry_var.get()
            if len(current) < 12:
                pin_entry_var.set(current + str(digit))
            pin_entry.icursor(tk.END)
            pin_entry.focus_force()

        def backspace_digit():
            current = pin_entry_var.get()
            if current:
                pin_entry_var.set(current[:-1])
            pin_entry.icursor(tk.END)
            pin_entry.focus_force()

        def clear_digits():
            pin_entry_var.set("")
            pin_entry.icursor(tk.END)
            pin_entry.focus_force()

        keypad = tk.Frame(frm, bg=BC)
        keypad.pack(fill=tk.X, pady=(8, 10))

        keypad_layout = [
            [("1", lambda: append_digit("1")), ("2", lambda: append_digit("2")), ("3", lambda: append_digit("3"))],
            [("4", lambda: append_digit("4")), ("5", lambda: append_digit("5")), ("6", lambda: append_digit("6"))],
            [("7", lambda: append_digit("7")), ("8", lambda: append_digit("8")), ("9", lambda: append_digit("9"))],
            [("Effacer", clear_digits), ("0", lambda: append_digit("0")), ("⌫", backspace_digit)],
        ]

        for row_index, row in enumerate(keypad_layout):
            for col_index, (caption, cmd) in enumerate(row):
                is_action = caption in {"Effacer", "⌫"}
                button = tk.Button(
                    keypad,
                    text=caption,
                    command=cmd,
                    width=9 if is_action else 7,
                    height=3,
                    font=("Segoe UI", 16, "bold"),
                    bg="#1f2937" if not is_action else "#374151",
                    fg=TX,
                    activebackground="#4b5563",
                    activeforeground=TX,
                    relief="flat",
                    bd=0,
                )
                button.grid(row=row_index, column=col_index, padx=8, pady=8, sticky="nsew")

        for col in range(3):
            keypad.grid_columnconfigure(col, weight=1)

        btns = tk.Frame(frm, bg=BC)
        btns.pack(fill=tk.X, pady=(14, 0))
        tk.Button(btns, text="Annuler", command=close_pin_prompt, bg="#3a1a1a", fg=TX, relief="flat", padx=18, pady=10, font=("Segoe UI", 12, "bold")).pack(side=tk.RIGHT)
        tk.Button(btns, text="✓ VALIDER", command=validate_pin, bg="#1a4d2e", fg=GR, relief="flat", padx=22, pady=10, font=("Segoe UI", 13, "bold")).pack(side=tk.RIGHT, padx=(0, 10))

        pw.bind("<Return>", validate_pin)
        pw.protocol("WM_DELETE_WINDOW", close_pin_prompt)
        pw.grab_set()
        pin_entry.focus_force()

    def open_config():
        release_popup_for_config()
        if config_win[0] and config_win[0].winfo_exists():
            config_win[0].lift()
            config_win[0].focus_force()
            return
        if protect_config_access.get():
            ask_config_pin_then_open()
        else:
            build_config_window()

    def open_config_from_fullscreen(event=None):
        if root.attributes("-fullscreen"):
            open_config()

    root.bind("<Escape>", open_config_from_fullscreen)

    btn_cfg = tk.Button(popup_inner, text="⚙", font=("Segoe UI", 12), bg=POPUP_BADGE, fg="#475569",
                         activebackground=POPUP_BTN_HOVER, activeforeground=POPUP_TEXT,
                         relief="flat", bd=0, padx=8, pady=3, command=open_config, cursor="hand2")
    btn_cfg.pack(side=tk.LEFT, padx=(12, 0), anchor="center")

    def on_root_close():
        if popup_close_allowed.get():
            if hotfolder[0]:
                hotfolder[0].stop()
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_root_close)

    def save_popup_options():
        gui_cfg["allow_popup_close"] = bool(popup_close_allowed.get())
        gui_cfg["fullscreen_on_error"] = bool(fullscreen_on_error.get())
        gui_cfg["hide_popup_when_ready"] = bool(hide_popup_when_ready.get())
        save_gui_config(gui_cfg)

    def save_access_settings():
        gui_cfg["protect_config_access"] = bool(protect_config_access.get())
        pin_value = str(config_access_pin_var.get()).strip()
        if pin_value:
            gui_cfg["config_access_pin_hash"] = _hash_config_pin(pin_value)
            config_access_pin_var.set("")
        elif gui_cfg["protect_config_access"] and not str(gui_cfg.get("config_access_pin_hash", "")).strip():
            gui_cfg["config_access_pin_hash"] = _hash_config_pin("1234")
        save_gui_config(gui_cfg)

    def _current_mode():
        """Retourne le mode d'affichage correspondant au dernier résultat de scan."""
        with mon._lock:
            last = mon._last_result or {}
        _pr = last.get('imprimantes', [])
        _er = last.get('erreurs_systeme', [])
        if last.get('peut_imprimer', False):
            return "ready"
        if _pr and _pr[0].get("statut", {}).get("gravite") == "attente":
            return "waiting"
        if bool(_pr) or bool(_er):
            return "error"
        return "normal"

    def set_popup_theme(bg, fg, accent=None, badge=None, detail=None):
        accent = accent or fg
        badge = badge or POPUP_BADGE
        detail = detail or POPUP_MUTED
        root.configure(bg=bg)
        popup.configure(bg=accent)
        popup_inner.configure(bg=bg)
        status_accent.configure(bg=accent)
        status_badge.configure(bg=badge)
        status_dot.configure(bg=badge, fg=fg)
        status_block.configure(bg=bg)
        status_top.configure(bg=bg)
        text_fg = POPUP_TEXT if bg == POPUP_BG else fg
        status_text.configure(bg=bg, fg=text_fg)
        status_detail.configure(bg=bg, fg=detail)
        btn_cfg.configure(bg=badge, fg="#475569" if bg == POPUP_BG else AM, activebackground=POPUP_BTN_HOVER, activeforeground=POPUP_TEXT)
        version_lbl_popup.configure(bg=bg)

    def set_popup_border(thickness):
        thickness = max(1, int(thickness))
        popup.configure(padx=thickness, pady=thickness)

    def pack_popup_inline_layout():
        status_accent.pack_forget()
        status_badge.pack_forget()
        status_block.pack_forget()
        btn_cfg.pack_forget()
        status_accent.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 11))
        status_badge.pack(side=tk.LEFT, anchor="center", padx=(0, 12), pady=0)
        status_block.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, anchor="center")
        btn_cfg.pack(side=tk.LEFT, padx=(12, 0), anchor="center")

    def pack_popup_fullscreen_layout():
        status_accent.pack_forget()
        status_badge.pack_forget()
        status_block.pack_forget()
        btn_cfg.pack_forget()
        status_block.pack(side=tk.TOP, fill=tk.NONE, expand=False)
        btn_cfg.pack(side=tk.TOP, padx=0, pady=(28, 0), anchor="center")

    def apply_popup_mode(display_mode="normal"):
        fz = _fs[0]  # taille de base choisie par l'utilisateur
        fullscreen = (display_mode == "error") and bool(fullscreen_on_error.get())
        popup_bg = POPUP_BG
        if fullscreen:
            hide_gear()
            root.deiconify()
            root.overrideredirect(False)
            root.attributes("-fullscreen", True)
            if not config_visible[0]:
                root.attributes("-topmost", True)
            else:
                root.attributes("-topmost", False)
            popup_bg = "#2a0000"
            set_popup_theme(popup_bg, "#ffffff", accent="#ff455a", badge="#4a0710", detail="#ffd5da")
            set_popup_border(1)
            popup.pack_forget()
            popup.place(relx=0.5, rely=0.5, anchor="center")
            pack_popup_fullscreen_layout()
            status_dot.configure(font=("Segoe UI", fz * 6))
            status_text.configure(font=("Segoe UI Semibold", fz * 4), anchor="center", justify="center")
            status_text.pack_configure(side=tk.TOP, fill=tk.NONE, expand=False, anchor="center")
            sw = max(800, root.winfo_screenwidth() - 200)
            status_detail.configure(font=("Segoe UI Semibold", fz * 3), anchor="center", justify="center", wraplength=sw)
            status_detail.pack_configure(anchor="center", pady=(20, 0))
            version_lbl_popup.configure(font=("Segoe UI Semibold", max(12, fz + 6)), fg="#ffd5da")
            version_lbl_popup.pack(side=tk.TOP, pady=(18, 0), anchor="center")
        elif display_mode == "waiting":
            hide_gear()
            root.deiconify()
            root.attributes("-fullscreen", False)
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            popup_bg = POPUP_BG
            set_popup_theme(popup_bg, "#b7791f", accent="#f59e0b", badge="#fff7db", detail="#745c22")
            set_popup_border(1)
            popup.place_forget()
            popup.pack(fill=tk.BOTH, expand=True)
            pack_popup_inline_layout()
            status_top.pack_configure(pady=0)
            version_lbl_popup.configure(font=("Segoe UI Semibold", 9), fg="#64748b")
            version_lbl_popup.pack(side=tk.RIGHT, padx=(12, 0), anchor="e")
            status_dot.configure(font=("Segoe UI", fz + 7))
            status_text.configure(font=("Segoe UI Semibold", fz), anchor="w", justify="left")
            status_text.pack_configure(side=tk.LEFT, fill=tk.X, expand=True, anchor="w")
            status_detail.configure(font=("Segoe UI", max(8, fz - 1)), wraplength=max(340, fz * 44), anchor="w", justify="left")
            status_detail.pack_configure(anchor="w", pady=(3, 0))
            root.update_idletasks()
            root.geometry(f"{root.winfo_reqwidth()}x{root.winfo_reqheight()}")
            position_popup()
        elif display_mode == "printing":
            # Toujours visible pendant l'impression, même si hide_popup_when_ready activé
            hide_gear()
            root.deiconify()
            root.attributes("-fullscreen", False)
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            popup_bg = POPUP_BG
            set_popup_theme(popup_bg, "#18864b", accent="#22c55e", badge="#e7f8ee", detail="#3a7b55")
            set_popup_border(1)
            popup.place_forget()
            popup.pack(fill=tk.BOTH, expand=True)
            pack_popup_inline_layout()
            status_top.pack_configure(pady=0)
            version_lbl_popup.configure(font=("Segoe UI Semibold", 9), fg="#64748b")
            version_lbl_popup.pack(side=tk.RIGHT, padx=(12, 0), anchor="e")
            status_dot.configure(font=("Segoe UI", fz + 7))
            status_text.configure(font=("Segoe UI Semibold", fz), anchor="w", justify="left")
            status_text.pack_configure(side=tk.LEFT, fill=tk.X, expand=True, anchor="w")
            status_detail.configure(font=("Segoe UI", max(8, fz - 1)), wraplength=max(340, fz * 44), anchor="w", justify="left")
            status_detail.pack_configure(anchor="w", pady=(3, 0))
            root.update_idletasks()
            root.geometry(f"{root.winfo_reqwidth()}x{root.winfo_reqheight()}")
            position_popup()
        elif display_mode == "ready":
            if hide_popup_when_ready.get():
                root.withdraw()
                show_gear()
                return
            hide_gear()
            root.deiconify()
            root.attributes("-fullscreen", False)
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            popup_bg = POPUP_BG
            set_popup_theme(popup_bg, "#18864b", accent="#22c55e", badge="#e7f8ee", detail="#3a7b55")
            set_popup_border(1)
            popup.place_forget()
            popup.pack(fill=tk.BOTH, expand=True)
            pack_popup_inline_layout()
            status_top.pack_configure(pady=(7, 0))
            version_lbl_popup.configure(font=("Segoe UI Semibold", 9), fg="#64748b")
            version_lbl_popup.pack(side=tk.RIGHT, padx=(12, 0), anchor="e")
            status_dot.configure(font=("Segoe UI", fz + 7))
            status_text.configure(font=("Segoe UI Semibold", fz), anchor="w", justify="left")
            status_text.pack_configure(side=tk.LEFT, fill=tk.X, expand=True, anchor="w")
            status_detail.pack_forget()
            root.update_idletasks()
            root.geometry(f"{root.winfo_reqwidth()}x{root.winfo_reqheight()}")
            position_popup()
        else:
            hide_gear()
            root.deiconify()
            root.attributes("-fullscreen", False)
            root.overrideredirect(True)
            root.attributes("-topmost", True)
            _blink_active = hasattr(refresh_popup, '_blink_active') and refresh_popup._blink_active
            if not _blink_active:
                popup_bg = POPUP_BG
                set_popup_theme(popup_bg, TD, accent=POPUP_BORDER, badge=POPUP_BADGE, detail=POPUP_MUTED)
                set_popup_border(1)
                status_dot.configure(font=("Segoe UI", fz + 7))
                status_text.configure(font=("Segoe UI Semibold", fz), anchor="w", justify="left")
            else:
                set_popup_border(8)
                status_dot.configure(font=("Segoe UI", fz + 7))
                status_text.configure(font=("Segoe UI Semibold", fz), anchor="w", justify="left")
            popup.place_forget()
            popup.pack(fill=tk.BOTH, expand=True)
            pack_popup_inline_layout()
            status_top.pack_configure(pady=0)
            version_lbl_popup.configure(font=("Segoe UI Semibold", 9), fg="#64748b")
            version_lbl_popup.pack(side=tk.RIGHT, padx=(12, 0), anchor="e")
            status_text.pack_configure(side=tk.LEFT, fill=tk.X, expand=True, anchor="w")
            status_detail.configure(font=("Segoe UI", max(8, fz - 1)), wraplength=max(340, fz * 44), anchor="w", justify="left")
            status_detail.pack_configure(anchor="w", pady=(3, 0))
            root.update_idletasks()
            root.geometry(f"{root.winfo_reqwidth()}x{root.winfo_reqheight()}")
            position_popup()

    # === DRAG DU POPUP (glisser pour déplacer) ===
    _drag = {"x": 0, "y": 0}

    def _popup_start_drag(e):
        _drag["x"] = e.x_root - root.winfo_x()
        _drag["y"] = e.y_root - root.winfo_y()

    def _popup_do_drag(e):
        x = e.x_root - _drag["x"]
        y = e.y_root - _drag["y"]
        root.geometry(f"+{x}+{y}")

    def _popup_save_pos(_e):
        gui_cfg["popup_x"] = root.winfo_x()
        gui_cfg["popup_y"] = root.winfo_y()
        save_gui_config(gui_cfg)

    for _w in (popup, popup_inner, status_accent, status_badge, status_dot, status_block, status_top, status_text, status_detail):
        _w.bind("<ButtonPress-1>", _popup_start_drag)
        _w.bind("<B1-Motion>", _popup_do_drag)
        _w.bind("<ButtonRelease-1>", _popup_save_pos)

    # Position : restaurer la position sauvegardée ou se placer en bas à droite
    def position_popup():
        root.update_idletasks()
        sx = gui_cfg.get("popup_x", -1)
        sy = gui_cfg.get("popup_y", -1)
        if sx >= 0 and sy >= 0:
            root.geometry(f"+{sx}+{sy}")
        else:
            sw = root.winfo_screenwidth()
            sh = root.winfo_screenheight()
            w = root.winfo_reqwidth()
            h = root.winfo_reqheight()
            root.geometry(f"+{sw - w - 20}+{sh - h - 60}")
    root.after(100, position_popup)

    # === MINI ICÔNE FLOTTANTE (visible quand popup masqué) ===
    gear_win = tk.Toplevel(root)
    gear_win.overrideredirect(True)
    gear_win.attributes("-topmost", True)
    gear_win.configure(bg="#1a1a1a")
    gear_win.geometry("40x40")
    gear_win.withdraw()  # masqué par défaut

    # Charger l'icône imprimante redimensionnée en 32×32
    _gear_img = None
    try:
        from PIL import Image, ImageTk
        _pil = Image.open(_resource("icone-imprimante.png")).resize((32, 32), Image.LANCZOS)
        _gear_img = ImageTk.PhotoImage(_pil)
    except Exception:
        pass

    if _gear_img:
        gear_btn = tk.Label(gear_win, image=_gear_img, bg="#1a1a1a", cursor="hand2", borderwidth=0)
        gear_btn._img_ref = _gear_img  # garder une référence pour éviter le garbage collect
    else:
        gear_btn = tk.Label(gear_win, text="⚙", font=("Segoe UI", 16), bg="#1a1a1a", fg=AM, cursor="hand2")
    gear_btn.pack(fill=tk.BOTH, expand=True)
    gear_btn.bind("<Button-1>", lambda e: open_config())

    # Drag de la mini icône
    _gear_drag = {"x": 0, "y": 0}
    def gear_start_drag(e):
        _gear_drag["x"] = e.x
        _gear_drag["y"] = e.y
    def gear_do_drag(e):
        x = gear_win.winfo_x() + e.x - _gear_drag["x"]
        y = gear_win.winfo_y() + e.y - _gear_drag["y"]
        gear_win.geometry(f"+{x}+{y}")
    gear_btn.bind("<ButtonPress-3>", gear_start_drag)
    gear_btn.bind("<B3-Motion>", gear_do_drag)

    def position_gear():
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        gear_win.geometry(f"+{sw - 56}+{sh - 96}")
    position_gear()

    def show_gear():
        gear_win.deiconify()
        gear_win.lift()
    def hide_gear():
        gear_win.withdraw()

    # === FENÊTRE CONFIGURATION ===
    def build_config_window():
        release_popup_for_config()

        # Recharger la config depuis le disque dès l'ouverture pour être toujours à jour
        fresh = load_gui_config()
        gui_cfg.update(fresh)
        # Synchroniser les variables du popup avec la config fraîchement lue
        popup_close_allowed.set(bool(gui_cfg.get("allow_popup_close", False)))
        fullscreen_on_error.set(bool(gui_cfg.get("fullscreen_on_error", False)))
        hide_popup_when_ready.set(bool(gui_cfg.get("hide_popup_when_ready", False)))
        protect_config_access.set(bool(gui_cfg.get("protect_config_access", False)))
        config_access_pin_var.set("")
        _fs[0] = max(8, min(36, gui_cfg.get("popup_font_size", 11)))

        cw = tk.Toplevel(root)
        config_win[0] = cw
        cw.title("Kodak Monitor — Configuration")
        cw.configure(bg=BG)
        sw = cw.winfo_screenwidth()
        sh = cw.winfo_screenheight()
        cw_w = min(1180, max(980, sw - 120))
        cw_h = max(720, sh - 80)
        cw_x = max(0, (sw - cw_w) // 2)
        cw_y = 20
        cw.geometry(f"{cw_w}x{cw_h}+{cw_x}+{cw_y}")
        cw.minsize(900, min(720, cw_h))
        cw.attributes("-topmost", True)

        # Header
        h=tk.Frame(cw,bg="#0b0e14",padx=20,pady=12); h.pack(fill=tk.X)
        tk.Label(h,text=" K ",font=("Consolas",16,"bold"),bg=AM,fg="#fff",padx=6).pack(side=tk.LEFT)
        tf=tk.Frame(h,bg="#0b0e14"); tf.pack(side=tk.LEFT,padx=(12,0))
        tk.Label(tf,text="KODAK PRINTER MONITOR",font=("Segoe UI",13,"bold"),bg="#0b0e14",fg=TX).pack(anchor="w")
        tk.Label(tf,text="SDK USB Direct — Configuration",font=("Segoe UI",9),bg="#0b0e14",fg=TD).pack(anchor="w")
        tk.Label(tf,text=f"Version {APP_VERSION}",font=("Segoe UI",10,"bold"),bg="#0b0e14",fg=BL).pack(anchor="w")

        # Barre de statut voyant — visible sur tous les onglets
        sb_frame = tk.Frame(cw, bg="#0d1117", padx=20, pady=5, highlightbackground="#1c2333", highlightthickness=1)
        sb_frame.pack(fill=tk.X)
        cfg_voyant_dot = tk.Label(sb_frame, text="●", font=("Segoe UI", 10), bg="#0d1117", fg=TD)
        cfg_voyant_dot.pack(side=tk.LEFT, padx=(0, 6))
        cfg_voyant_lbl = tk.Label(sb_frame, text="Chargement...", font=("Segoe UI", 9, "bold"), bg="#0d1117", fg=TD)
        cfg_voyant_lbl.pack(side=tk.LEFT)
        tk.Label(sb_frame, text=f"Version {APP_VERSION}", font=("Segoe UI", 10, "bold"), bg="#0d1117", fg=BL).pack(side=tk.RIGHT)
        # Ligne chemin config — aide au diagnostic
        cfg_path_frame = tk.Frame(cw, bg="#060a0f", padx=20, pady=3)
        cfg_path_frame.pack(fill=tk.X)
        tk.Label(cfg_path_frame, text="Fichier config :", font=("Segoe UI", 8), bg="#060a0f", fg="#3a4a5a").pack(side=tk.LEFT)
        tk.Label(cfg_path_frame, text=GUI_CONFIG_FILE, font=("Consolas", 8), bg="#060a0f",
                 fg="#58a6ff" if os.path.exists(GUI_CONFIG_FILE) else "#f85149").pack(side=tk.LEFT, padx=(6,0))

        # Notebook
        sty=ttk.Style(); sty.theme_use("clam")
        sty.configure("TNotebook",background=BG,borderwidth=0)
        sty.configure("TNotebook.Tab",background=BC,foreground=TD,padding=[14,8],font=("Segoe UI",10,"bold"))
        sty.map("TNotebook.Tab",background=[("selected","#1c2333")],foreground=[("selected",AM)])
        nb=ttk.Notebook(cw); nb.pack(fill=tk.BOTH,expand=True)

        t1=tk.Frame(nb,bg=BG)
        t2=tk.Frame(nb,bg=BG); t3=tk.Frame(nb,bg=BG); t4=tk.Frame(nb,bg=BG); t5=tk.Frame(nb,bg=BG); t6=tk.Frame(nb,bg=BG)
        nb.add(t1,text="  Statut  ")
        nb.add(t4,text="  Paramètres  ")
        nb.add(t5,text="  Hotfolder  ")
        nb.add(t6,text="  Logs  ")
        nb.add(t2,text="  Codes erreur  ")

        # === TAB PARAMÈTRES (scrollable) ===
        pc=tk.Canvas(t4,bg=BG,highlightthickness=0); ps=ttk.Scrollbar(t4,orient="vertical",command=pc.yview)
        pi=tk.Frame(pc,bg=BG); pi.bind("<Configure>",lambda e:pc.configure(scrollregion=pc.bbox("all")))
        pc.create_window((0,0),window=pi,anchor="nw"); pc.configure(yscrollcommand=ps.set)
        pc.pack(side=tk.LEFT,fill=tk.BOTH,expand=True); ps.pack(side=tk.RIGHT,fill=tk.Y)

        # -- APPARENCE POPUP --
        appear_opts = tk.Frame(pi,bg=BC,padx=20,pady=12,highlightbackground=BO,highlightthickness=1)
        appear_opts.pack(fill=tk.X,padx=20,pady=(12,0))
        tk.Label(appear_opts,text="APPARENCE DU POPUP",font=("Segoe UI",11,"bold"),bg=BC,fg=AM).pack(anchor="w",pady=(0,8))

        font_row = tk.Frame(appear_opts,bg=BC); font_row.pack(fill=tk.X,pady=2)
        tk.Label(font_row,text="Taille des caractères :",font=("Segoe UI",10),bg=BC,fg=TX).pack(side=tk.LEFT)
        font_size_var = tk.IntVar(value=_fs[0])
        font_spin = tk.Spinbox(font_row,from_=8,to=36,textvariable=font_size_var,width=4,
                               font=("Consolas",11),bg=BD,fg=TX,buttonbackground="#1c2333")
        font_spin.pack(side=tk.LEFT,padx=(8,12))
        font_lbl = tk.Label(font_row,text="",font=("Segoe UI",9),bg=BC,fg=TD)
        font_lbl.pack(side=tk.LEFT)

        def apply_font_size():
            fz = max(8, min(36, font_size_var.get()))
            _fs[0] = fz
            gui_cfg["popup_font_size"] = fz
            save_gui_config(gui_cfg)
            font_lbl.config(text=f"Appliqué ({fz}pt)")
            apply_popup_mode(_current_mode())

        tk.Button(appear_opts,text="✓ Appliquer",command=apply_font_size,
                  bg="#1a4d2e",fg=GR,font=("Segoe UI",10,"bold"),relief="flat",padx=14).pack(anchor="w",pady=(6,0))
        tk.Label(appear_opts,text="Glisser le popup avec la souris pour le repositionner — position sauvegardée automatiquement",
                 font=("Segoe UI",9),bg=BC,fg=TD,wraplength=550,justify="left").pack(anchor="w",pady=(6,0))

        # -- OPTIONS POPUP --
        popup_opts = tk.Frame(pi,bg=BC,padx=20,pady=12,highlightbackground=BO,highlightthickness=1)
        popup_opts.pack(fill=tk.X,padx=20,pady=(14,0))
        tk.Label(popup_opts,text="OPTIONS POPUP",font=("Segoe UI",11,"bold"),bg=BC,fg=AM).pack(anchor="w",pady=(0,8))
        tk.Checkbutton(popup_opts,text="Autoriser la fermeture du popup avec la croix Windows",variable=popup_close_allowed,
                       bg=BC,fg=TX,selectcolor=BD,activebackground=BC,activeforeground=TX,
                       font=("Segoe UI",10)).pack(anchor="w")
        tk.Checkbutton(popup_opts,text="Afficher le popup en plein écran uniquement en cas d'erreur",variable=fullscreen_on_error,
                       bg=BC,fg=TX,selectcolor=BD,activebackground=BC,activeforeground=TX,
                       font=("Segoe UI",10)).pack(anchor="w",pady=(4,0))
        tk.Checkbutton(popup_opts,text="Masquer le popup quand l'imprimante est OK",variable=hide_popup_when_ready,
                       bg=BC,fg=TX,selectcolor=BD,activebackground=BC,activeforeground=TX,
                       font=("Segoe UI",10)).pack(anchor="w",pady=(4,0))
        popup_state_lbl = tk.Label(popup_opts,text="",font=("Segoe UI",9),bg=BC,fg=TD)
        popup_state_lbl.pack(anchor="w",pady=(8,0))

        # -- PROTECTION ACCÈS --
        access_opts = tk.Frame(pi,bg=BC,padx=20,pady=12,highlightbackground=BO,highlightthickness=1)
        access_opts.pack(fill=tk.X,padx=20,pady=(14,0))
        tk.Label(access_opts,text="PROTECTION ACCÈS PARAMÉTRAGE",font=("Segoe UI",11,"bold"),bg=BC,fg=AM).pack(anchor="w",pady=(0,8))
        tk.Checkbutton(access_opts,text="Protéger l'accès au paramétrage avec un code PIN",variable=protect_config_access,
                       bg=BC,fg=TX,selectcolor=BD,activebackground=BC,activeforeground=TX,
                       font=("Segoe UI",10)).pack(anchor="w")
        pin_row = tk.Frame(access_opts, bg=BC)
        pin_row.pack(fill=tk.X, pady=(8,0))
        tk.Label(pin_row,text="Nouveau PIN :",font=("Segoe UI",10),bg=BC,fg=TX).pack(side=tk.LEFT)
        pin_entry = tk.Entry(pin_row,textvariable=config_access_pin_var,show="*",width=16,font=("Segoe UI",10),
                             bg=BD,fg=TX,insertbackground=TX,relief="flat")
        pin_entry.pack(side=tk.LEFT,padx=(8,0))
        tk.Label(pin_row,text="laisser vide pour conserver le PIN actuel",font=("Segoe UI",9),bg=BC,fg=TD).pack(side=tk.LEFT,padx=(8,0))
        access_state_lbl = tk.Label(access_opts,text="",font=("Segoe UI",9),bg=BC,fg=TD)
        access_state_lbl.pack(anchor="w",pady=(8,0))

        # -- ETAT IMPRESSION SDK --
        timing_opts = tk.Frame(pi,bg=BC,padx=20,pady=12,highlightbackground=BO,highlightthickness=1)
        timing_opts.pack(fill=tk.X,padx=20,pady=(14,0))
        tk.Label(timing_opts,text="ETAT IMPRESSION SDK",font=("Segoe UI",11,"bold"),bg=BC,fg=AM).pack(anchor="w",pady=(0,8))

        def _trow(parent, label, key, minv, maxv, step=1, default=30, unit="sec"):
            row = tk.Frame(parent,bg=BC); row.pack(fill=tk.X,pady=2)
            tk.Label(row,text=label,font=("Segoe UI",10),bg=BC,fg=TD,width=30,anchor="w").pack(side=tk.LEFT)
            var = tk.IntVar(value=int(gui_cfg.get(key, default)))
            tk.Spinbox(row,from_=minv,to=maxv,increment=step,textvariable=var,width=5,
                       font=("Consolas",10),bg=BD,fg=TX,buttonbackground="#1c2333").pack(side=tk.LEFT,padx=(4,4))
            tk.Label(row,text=unit,font=("Segoe UI",9),bg=BC,fg=TD).pack(side=tk.LEFT)
            return var

        v_sdk_ntc = _trow(timing_opts,"Notice impression SDK:","sdk_print_notice_seconds",5,120,1,30)
        tk.Label(
            timing_opts,
            text="Conserve temporairement le message d'impression SDK dans le popup\napres l'envoi du travail vers l'imprimante.",
            font=("Segoe UI",9),
            bg=BC,
            fg=TD,
            justify="left",
        ).pack(anchor="w",pady=(6,0))

        timing_state_lbl = tk.Label(timing_opts,text="",font=("Segoe UI",9),bg=BC,fg=TD)
        timing_state_lbl.pack(anchor="w",pady=(8,0))

        def save_timings():
            gui_cfg["sdk_print_notice_seconds"] = max(5, v_sdk_ntc.get())
            save_gui_config(gui_cfg)
            timing_state_lbl.config(text="✓ Délai SDK enregistré", fg=GR)

        tk.Button(timing_opts,text="✓ Appliquer",command=save_timings,bg="#1a4d2e",fg=GR,
                  font=("Segoe UI",10,"bold"),relief="flat",padx=14).pack(anchor="w",pady=(8,0))

        # -- PARAMÈTRES SCAN SDK --
        scan_opts = tk.Frame(pi,bg=BC,padx=20,pady=12,highlightbackground=BO,highlightthickness=1)
        scan_opts.pack(fill=tk.X,padx=20,pady=(14,0))
        tk.Label(scan_opts,text="PARAMÈTRES SCAN SDK",font=("Segoe UI",11,"bold"),bg=BC,fg=AM).pack(anchor="w",pady=(0,8))

        scan_row1=tk.Frame(scan_opts,bg=BC); scan_row1.pack(fill=tk.X,pady=2)
        tk.Label(scan_row1,text="Scan statut (sec):",font=("Segoe UI",10),bg=BC,fg=TD,width=22,anchor="w").pack(side=tk.LEFT)
        scan_int_var=tk.StringVar(value=str(gui_cfg.get("scan_interval", 2)))
        tk.Spinbox(scan_row1,from_=1,to=30,textvariable=scan_int_var,width=5,font=("Consolas",10),
                   bg=BD,fg=TX,buttonbackground="#1c2333").pack(side=tk.LEFT,padx=(4,0))
        tk.Label(scan_row1,text="(open → status → close)",font=("Segoe UI",9),bg=BC,fg=TD).pack(side=tk.LEFT,padx=(10,0))

        scan_row2=tk.Frame(scan_opts,bg=BC); scan_row2.pack(fill=tk.X,pady=2)
        tk.Label(scan_row2,text="Scan détaillé (sec):",font=("Segoe UI",10),bg=BC,fg=TD,width=22,anchor="w").pack(side=tk.LEFT)
        scan_det_var=tk.StringVar(value=str(gui_cfg.get("scan_detail_interval", 60)))
        tk.Spinbox(scan_row2,from_=10,to=600,increment=10,textvariable=scan_det_var,width=5,font=("Consolas",10),
                   bg=BD,fg=TX,buttonbackground="#1c2333").pack(side=tk.LEFT,padx=(4,0))
        tk.Label(scan_row2,text="(firmware + compteurs)",font=("Segoe UI",9),bg=BC,fg=TD).pack(side=tk.LEFT,padx=(10,0))

        tk.Label(scan_opts,text="Augmentez les valeurs si l'imprimante se bloque pendant les impressions",
                 font=("Segoe UI",9),bg=BC,fg=TD).pack(anchor="w",pady=(6,0))

        def save_scan_settings():
            try:
                v1 = max(1, int(scan_int_var.get()))
                v2 = max(10, int(scan_det_var.get()))
            except ValueError:
                return
            gui_cfg["scan_interval"] = v1
            gui_cfg["scan_detail_interval"] = v2
            save_gui_config(gui_cfg)
            mon.interval = v1
            mon.detail_interval = v2
            scan_state_lbl.config(text=f"Statut: {v1}s | Détails: {v2}s — Appliqué")
        tk.Button(scan_opts,text="✓ Appliquer",command=save_scan_settings,bg="#1a4d2e",fg=GR,
                  font=("Segoe UI",10,"bold"),relief="flat",padx=14).pack(anchor="w",pady=(8,0))
        scan_state_lbl = tk.Label(scan_opts,text=f"Statut: {mon.interval}s | Détails: {mon.detail_interval}s",
                                   font=("Segoe UI",9),bg=BC,fg=TD)
        scan_state_lbl.pack(anchor="w",pady=(4,0))

        # -- COMPTEUR D'IMPRESSIONS --
        counter_opts = tk.Frame(pi,bg=BC,padx=20,pady=12,highlightbackground=BO,highlightthickness=1)
        counter_opts.pack(fill=tk.X,padx=20,pady=(14,16))
        tk.Label(counter_opts,text="COMPTEUR D'IMPRESSIONS",font=("Segoe UI",11,"bold"),bg=BC,fg=AM).pack(anchor="w",pady=(0,8))
        counter_row=tk.Frame(counter_opts,bg=BC); counter_row.pack(fill=tk.X)
        tk.Label(counter_row,text="Fichier compteur:",font=("Segoe UI",10),bg=BC,fg=TD,width=18,anchor="w").pack(side=tk.LEFT)
        # Utiliser _get_counter_path() pour toujours afficher le chemin absolu résolu
        # et synchroniser gui_cfg immédiatement pour que tout save ultérieur soit correct
        counter_default = _get_counter_path()
        gui_cfg["print_counter_file"] = counter_default
        hf_counter=tk.StringVar(value=counter_default)
        tk.Entry(counter_row,textvariable=hf_counter,font=("Consolas",10),bg=BD,fg=TX,insertbackground=TX,
                 relief="flat",highlightthickness=1,highlightbackground=BO,width=40).pack(side=tk.LEFT,padx=(4,4))

        def _save_counter():
            v = hf_counter.get().strip()
            if v:
                gui_cfg["print_counter_file"] = v
                save_gui_config(gui_cfg)
                counter_lbl.config(text="✓ Enregistré", fg=GR)

        def import_counter():
            f = filedialog.askopenfilename(
                initialdir=os.path.dirname(hf_counter.get()) or SCRIPT_DIR,
                filetypes=[("JSON", "*.json"), ("Tous", "*.*")],
                title="Importer un fichier compteur"
            )
            if not f:
                return
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if not isinstance(data, dict):
                    raise ValueError("Le JSON doit contenir un objet à la racine")
            except Exception as e:
                log_print(f"⚠ Import compteur impossible ({f}): {e}")
                return
            hf_counter.set(f)
            _save_counter()
            log_print(f"📊 Fichier compteur importé → {f}")

        def create_counter():
            f = filedialog.asksaveasfilename(
                initialdir=os.path.dirname(hf_counter.get()) or SCRIPT_DIR,
                initialfile=os.path.basename(hf_counter.get()) or "kodak_compteur.json",
                defaultextension=".json",
                filetypes=[("JSON", "*.json"), ("Tous", "*.*")],
                title="Créer un fichier compteur"
            )
            if not f:
                return
            hf_counter.set(f)
            if os.path.exists(f):
                _save_counter()
                log_print(f"📊 Fichier compteur existant sélectionné → {f}")
                return
            try:
                d = os.path.dirname(f)
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(f, "w", encoding="utf-8") as fh:
                    json.dump({}, fh, indent=2)
                _save_counter()
                log_print(f"📊 Fichier compteur créé → {f}")
            except Exception as e:
                log_print(f"⚠ Impossible de créer le fichier compteur: {e}")

        tk.Button(counter_row,text="Importer",command=import_counter,bg="#1c2333",fg=TX,relief="flat",padx=8).pack(side=tk.LEFT,padx=(0,4))
        tk.Button(counter_row,text="Nouveau",command=create_counter,bg="#1c2333",fg=TX,relief="flat",padx=8).pack(side=tk.LEFT)
        tk.Button(counter_row,text="✓ Appliquer",command=_save_counter,bg="#1a4d2e",fg=GR,font=("Segoe UI",9,"bold"),relief="flat",padx=8).pack(side=tk.LEFT,padx=(8,0))
        counter_lbl = tk.Label(counter_opts,text="",font=("Segoe UI",9),bg=BC,fg=TD)
        counter_lbl.pack(anchor="w",pady=(4,0))
        tk.Label(counter_opts,text="Compte automatiquement chaque impression (jour / mois / an)",font=("Segoe UI",9),bg=BC,fg=TD).pack(anchor="w")

        def apply_popup_settings():
            save_popup_options()
            popup_state_lbl.config(
                text=f"Fermeture popup: {'AUTORISÉE' if popup_close_allowed.get() else 'BLOQUÉE'}  |  Plein écran sur erreur: {'ACTIF' if fullscreen_on_error.get() else 'INACTIF'}"
            )
            apply_popup_mode(_current_mode())

        def apply_access_settings():
            save_access_settings()
            access_state_lbl.config(
                text=f"Protection accès: {'ACTIVE' if protect_config_access.get() else 'INACTIVE'}  |  PIN défini: {'OUI' if str(gui_cfg.get('config_access_pin_hash', '')).strip() else 'NON'}"
            )

        tk.Button(popup_opts,text="✓ Valider options popup",command=apply_popup_settings,bg="#1a4d2e",fg=GR,
                  font=("Segoe UI",10,"bold"),relief="flat",padx=14).pack(anchor="w",pady=(8,0))
        tk.Button(access_opts,text="✓ Valider protection",command=apply_access_settings,bg="#1a4d2e",fg=GR,
                  font=("Segoe UI",10,"bold"),relief="flat",padx=14).pack(anchor="w",pady=(8,0))
        # Initialisation des labels sans sauvegarder (la config vient d'être lue depuis le disque)
        popup_state_lbl.config(
            text=f"Fermeture popup: {'AUTORISÉE' if popup_close_allowed.get() else 'BLOQUÉE'}  |  Plein écran sur erreur: {'ACTIF' if fullscreen_on_error.get() else 'INACTIF'}"
        )
        access_state_lbl.config(
            text=f"Protection accès: {'ACTIVE' if protect_config_access.get() else 'INACTIVE'}  |  PIN défini: {'OUI' if str(gui_cfg.get('config_access_pin_hash', '')).strip() else 'NON'}"
        )
        apply_popup_mode(_current_mode())

        # === TAB LOGS ===
        log_main = tk.Frame(t6,bg=BG,padx=14,pady=12)
        log_main.pack(fill=tk.BOTH,expand=True)

        log_tools = tk.Frame(log_main,bg=BC,padx=14,pady=10,highlightbackground=BO,highlightthickness=1)
        log_tools.pack(fill=tk.X)
        tk.Label(log_tools,text="SUIVI DU LOG",font=("Segoe UI",11,"bold"),bg=BC,fg=AM).pack(anchor="w",pady=(0,8))

        log_path_row = tk.Frame(log_tools,bg=BC); log_path_row.pack(fill=tk.X,pady=(0,6))
        tk.Label(log_path_row,text="Fichier :",font=("Segoe UI",9),bg=BC,fg=TD).pack(side=tk.LEFT)
        tk.Label(log_path_row,text=LOG_FILE,font=("Consolas",9),bg=BC,fg=BL,anchor="w").pack(side=tk.LEFT,padx=(6,0),fill=tk.X,expand=True)

        log_policy = tk.Frame(log_tools,bg=BC); log_policy.pack(fill=tk.X,pady=(4,0))
        tk.Label(log_policy,text="Garder :",font=("Segoe UI",10),bg=BC,fg=TX).pack(side=tk.LEFT)
        log_days_var = tk.IntVar(value=max(1, int(gui_cfg.get("log_retention_days", 30))))
        tk.Spinbox(log_policy,from_=1,to=365,textvariable=log_days_var,width=5,
                   font=("Consolas",10),bg=BD,fg=TX,buttonbackground="#1c2333").pack(side=tk.LEFT,padx=(8,4))
        tk.Label(log_policy,text="jours  ou max",font=("Segoe UI",10),bg=BC,fg=TX).pack(side=tk.LEFT)
        log_mb_var = tk.IntVar(value=max(1, int(gui_cfg.get("log_max_mb", 10))))
        tk.Spinbox(log_policy,from_=1,to=500,textvariable=log_mb_var,width=5,
                   font=("Consolas",10),bg=BD,fg=TX,buttonbackground="#1c2333").pack(side=tk.LEFT,padx=(8,4))
        tk.Label(log_policy,text="Mo",font=("Segoe UI",10),bg=BC,fg=TX).pack(side=tk.LEFT)

        log_state_lbl = tk.Label(log_tools,text="",font=("Segoe UI",9),bg=BC,fg=TD)
        log_state_lbl.pack(anchor="w",pady=(8,0))

        log_text_frame = tk.Frame(log_main,bg=BC,highlightbackground=BO,highlightthickness=1)
        log_text_frame.pack(fill=tk.BOTH,expand=True,pady=(12,0))
        log_text = tk.Text(log_text_frame,bg=BD,fg=TX,insertbackground=TX,relief="flat",
                           font=("Consolas",9),wrap="none",height=18)
        log_y = ttk.Scrollbar(log_text_frame,orient="vertical",command=log_text.yview)
        log_x = ttk.Scrollbar(log_text_frame,orient="horizontal",command=log_text.xview)
        log_text.configure(yscrollcommand=log_y.set,xscrollcommand=log_x.set)
        log_text.grid(row=0,column=0,sticky="nsew")
        log_y.grid(row=0,column=1,sticky="ns")
        log_x.grid(row=1,column=0,sticky="ew")
        log_text_frame.grid_rowconfigure(0,weight=1)
        log_text_frame.grid_columnconfigure(0,weight=1)

        def refresh_log_view(message=None, color=None):
            content = read_log_tail()
            log_text.configure(state="normal")
            log_text.delete("1.0", tk.END)
            log_text.insert("1.0", content if content else "Log vide.")
            log_text.see(tk.END)
            log_text.configure(state="disabled")
            if message:
                log_state_lbl.config(text=f"{message} | Taille actuelle: {_format_bytes(get_log_size())}", fg=color or TD)
            else:
                log_state_lbl.config(text=f"Taille actuelle: {_format_bytes(get_log_size())}", fg=TD)

        def clear_log_view():
            _removed, errors = clear_log_files()
            if errors:
                refresh_log_view("Effacement partiel: " + " | ".join(errors[:2]), RD)
            else:
                refresh_log_view("Log effacé.", GR)

        def apply_log_policy():
            days = max(1, int(log_days_var.get()))
            max_mb = max(1, int(log_mb_var.get()))
            gui_cfg["log_retention_days"] = days
            gui_cfg["log_max_mb"] = max_mb
            save_gui_config(gui_cfg)
            removed_lines, _kept_lines = trim_log_by_days(days)
            old_size, new_size = trim_log_by_size(max_mb)
            size_msg = ""
            if old_size and old_size != new_size:
                size_msg = f" | Taille: {_format_bytes(old_size)} -> {_format_bytes(new_size)}"
            refresh_log_view(f"Purge appliquée: {removed_lines} ligne(s) retirée(s){size_msg}", GR)

        log_btns = tk.Frame(log_tools,bg=BC)
        log_btns.pack(fill=tk.X,pady=(10,0))
        tk.Button(log_btns,text="Rafraîchir",command=refresh_log_view,bg="#1c2333",fg=TX,
                  font=("Segoe UI",10,"bold"),relief="flat",padx=12).pack(side=tk.LEFT)
        tk.Button(log_btns,text="✓ Appliquer purge",command=apply_log_policy,bg="#1a4d2e",fg=GR,
                  font=("Segoe UI",10,"bold"),relief="flat",padx=12).pack(side=tk.LEFT,padx=(8,0))
        tk.Button(log_btns,text="Effacer le log",command=clear_log_view,bg="#3a1a1a",fg=TX,
                  font=("Segoe UI",10,"bold"),relief="flat",padx=12).pack(side=tk.RIGHT)

        refresh_log_view()

        # === TAB HOTFOLDER ===
        hc=tk.Canvas(t5,bg=BG,highlightthickness=0); hs=ttk.Scrollbar(t5,orient="vertical",command=hc.yview)
        hi=tk.Frame(hc,bg=BG); hi.bind("<Configure>",lambda e:hc.configure(scrollregion=hc.bbox("all")))
        hc.create_window((0,0),window=hi,anchor="nw"); hc.configure(yscrollcommand=hs.set)
        hc.pack(side=tk.LEFT,fill=tk.BOTH,expand=True); hs.pack(side=tk.RIGHT,fill=tk.Y)

        hf_main = tk.Frame(hi,bg=BC,padx=20,pady=12,highlightbackground=BO,highlightthickness=1)
        hf_main.pack(fill=tk.X,padx=20,pady=(12,0))
        tk.Label(hf_main,text="HOTFOLDER — IMPRESSION AUTOMATIQUE",font=("Segoe UI",11,"bold"),bg=BC,fg=AM).pack(anchor="w",pady=(0,4))
        tk.Label(hf_main,text="Les images déposées dans ce dossier sont imprimées automatiquement.",
                 font=("Segoe UI",9),bg=BC,fg=TD).pack(anchor="w",pady=(0,10))

        # Dossier
        tk.Label(hf_main,text="Dossier surveillé :",font=("Segoe UI",10),bg=BC,fg=TD).pack(anchor="w")
        hfp_row=tk.Frame(hf_main,bg=BC); hfp_row.pack(fill=tk.X,pady=(2,8))
        hf_path_var=tk.StringVar(value=gui_cfg.get("hotfolder_path",""))
        tk.Entry(hfp_row,textvariable=hf_path_var,font=("Consolas",10),bg=BD,fg=TX,insertbackground=TX,
                 relief="flat",highlightthickness=1,highlightbackground=BO,width=46).pack(side=tk.LEFT,padx=(0,6))
        def _browse_hf():
            import tkinter.filedialog as _fd
            d=_fd.askdirectory(initialdir=hf_path_var.get() or SCRIPT_DIR,title="Sélectionner le dossier hotfolder")
            if d: hf_path_var.set(d)
        tk.Button(hfp_row,text="...",command=_browse_hf,bg=BC,fg=TX,font=("Segoe UI",10),relief="flat",padx=10).pack(side=tk.LEFT)

        # Imprimante Windows surveillee (pour detecter une impression pilote)
        pr_row=tk.Frame(hf_main,bg=BC); pr_row.pack(fill=tk.X,pady=2)
        tk.Label(pr_row,text="Imprimante Windows :",font=("Segoe UI",10),bg=BC,fg=TD,width=22,anchor="w").pack(side=tk.LEFT)
        try:
            import win32print as _wp
            _printers=[p[2] for p in _wp.EnumPrinters(_wp.PRINTER_ENUM_LOCAL|_wp.PRINTER_ENUM_CONNECTIONS)]
        except Exception:
            _printers=[]
        hf_printer_var=tk.StringVar(value=gui_cfg.get("hotfolder_printer",""))
        ttk.Combobox(pr_row,textvariable=hf_printer_var,values=_printers,width=34,state="normal",
                     font=("Segoe UI",10)).pack(side=tk.LEFT)

        wd_row=tk.Frame(hf_main,bg=BC); wd_row.pack(fill=tk.X,pady=2)
        tk.Label(wd_row,text="Delay après impression du pilote Windows :",font=("Segoe UI",10),bg=BC,fg=TD,width=22,anchor="w").pack(side=tk.LEFT)
        wd_delay_var=tk.IntVar(value=int(gui_cfg.get("windows_driver_post_print_delay", WINDOWS_SPOOLER_GRACE_SECONDS)))
        tk.Spinbox(wd_row,from_=1,to=120,increment=1,textvariable=wd_delay_var,width=5,font=("Consolas",10),
                   bg=BD,fg=TX,buttonbackground="#1c2333").pack(side=tk.LEFT,padx=(0,4))
        tk.Label(wd_row,text="sec",font=("Segoe UI",9),bg=BC,fg=TD).pack(side=tk.LEFT)
        tk.Label(hf_main,
                 text="Le hotfolder imprime toujours via le SDK Kodak 68xx en 10x15.\n"
                      "Ce champ sert uniquement a detecter une impression lancee via le pilote Windows.",
                 font=("Segoe UI",9),bg=BC,fg=TD,justify="left").pack(anchor="w",pady=(2,6))

        # Copies
        cp_row=tk.Frame(hf_main,bg=BC); cp_row.pack(fill=tk.X,pady=2)
        tk.Label(cp_row,text="Nombre de copies :",font=("Segoe UI",10),bg=BC,fg=TD,width=22,anchor="w").pack(side=tk.LEFT)
        hf_copies_var=tk.IntVar(value=int(gui_cfg.get("hotfolder_copies",1)))
        tk.Spinbox(cp_row,from_=1,to=99,textvariable=hf_copies_var,width=4,font=("Consolas",10),
                   bg=BD,fg=TX,buttonbackground="#1c2333").pack(side=tk.LEFT)

        # Action après impression
        act_row=tk.Frame(hf_main,bg=BC); act_row.pack(fill=tk.X,pady=2)
        tk.Label(act_row,text="Après impression :",font=("Segoe UI",10),bg=BC,fg=TD,width=22,anchor="w").pack(side=tk.LEFT)
        hf_action_var=tk.StringVar(value=gui_cfg.get("hotfolder_action","Supprimer apres impression"))
        ttk.Combobox(act_row,textvariable=hf_action_var,values=HotFolder.ACTIONS,width=28,state="readonly",
                     font=("Segoe UI",10)).pack(side=tk.LEFT)

        # Délai entre impressions
        gap_row=tk.Frame(hf_main,bg=BC); gap_row.pack(fill=tk.X,pady=2)
        tk.Label(gap_row,text="Délai entre impressions :",font=("Segoe UI",10),bg=BC,fg=TD,width=22,anchor="w").pack(side=tk.LEFT)
        hf_gap_var=tk.IntVar(value=int(gui_cfg.get("sdk_print_gap_seconds",75)))
        tk.Spinbox(gap_row,from_=1,to=300,increment=1,textvariable=hf_gap_var,width=5,font=("Consolas",10),
                   bg=BD,fg=TX,buttonbackground="#1c2333").pack(side=tk.LEFT,padx=(0,4))
        tk.Label(gap_row,text="sec",font=("Segoe UI",9),bg=BC,fg=TD).pack(side=tk.LEFT)

        hf_status_lbl=tk.Label(hf_main,text="",font=("Segoe UI",9),bg=BC,fg=TD)
        hf_status_lbl.pack(anchor="w",pady=(10,0))

        def _hf_is_active():
            return hotfolder[0] and hotfolder[0].is_running()

        def _update_hf_btn():
            if _hf_is_active():
                hf_toggle_btn.config(text="■ Arrêter hotfolder",bg="#4d1a1a",fg=RD)
                hf_status_lbl.config(text=f"● Actif — {gui_cfg.get('hotfolder_path','')}",fg=GR)
            else:
                hf_toggle_btn.config(text="▶ Démarrer hotfolder",bg="#1c2333",fg=BL)
                hf_status_lbl.config(text="Hotfolder inactif",fg=TD)

        def save_hotfolder():
            gui_cfg["hotfolder_path"]       = hf_path_var.get().strip()
            gui_cfg["hotfolder_printer"]    = hf_printer_var.get().strip()
            gui_cfg["hotfolder_copies"]     = max(1, hf_copies_var.get())
            gui_cfg["hotfolder_action"]     = hf_action_var.get()
            gui_cfg["sdk_print_gap_seconds"]= max(1, hf_gap_var.get())
            gui_cfg["windows_driver_post_print_delay"] = max(1, wd_delay_var.get())
            save_gui_config(gui_cfg)
            mon.preferred_windows_printer = gui_cfg["hotfolder_printer"]
            mon.windows_spooler_grace_seconds = gui_cfg["windows_driver_post_print_delay"]
            # Redémarrer le hotfolder avec les nouveaux paramètres
            if hotfolder[0]:
                hotfolder[0].stop()
                hotfolder[0] = None
            p = gui_cfg["hotfolder_path"]
            if p and os.path.isdir(p):
                hotfolder[0] = HotFolder(p, gui_cfg["hotfolder_copies"],
                                         gui_cfg["hotfolder_action"], gui_cfg["sdk_print_gap_seconds"], mon)
                hotfolder[0].start()
            _update_hf_btn()
            hf_status_lbl.config(text="✅ Enregistré" + (" — Hotfolder démarré" if _hf_is_active() else " — Dossier non configuré"),fg=GR)

        def toggle_hotfolder():
            if _hf_is_active():
                hotfolder[0].stop()
                hotfolder[0] = None
                _update_hf_btn()
            else:
                save_hotfolder()

        btn_row=tk.Frame(hf_main,bg=BC); btn_row.pack(fill=tk.X,pady=(10,0))
        tk.Button(btn_row,text="✓ Enregistrer",command=save_hotfolder,bg="#1a4d2e",fg=GR,
                  font=("Segoe UI",10,"bold"),relief="flat",padx=14).pack(side=tk.LEFT)
        hf_toggle_btn=tk.Button(btn_row,text="",command=toggle_hotfolder,
                                 font=("Segoe UI",10,"bold"),relief="flat",padx=14)
        hf_toggle_btn.pack(side=tk.LEFT,padx=(8,0))
        _update_hf_btn()

        tk.Label(hf_main,
                 text="Formats supportes : JPG, JPEG, PNG, TIF, TIFF, BMP\n"
                      "Les fichiers sont détectés 2 secondes après leur apparition dans le dossier.",
                 font=("Segoe UI",9),bg=BC,fg=TD,justify="left").pack(anchor="w",pady=(10,0))

        def close_config_window():
            try:
                cw.destroy()
            finally:
                config_win[0] = None
                restore_popup_after_config()

        cw.protocol("WM_DELETE_WINDOW", close_config_window)

        # Bouton arrêter le service
        def stop_service():
            cw.destroy()
            root.destroy()
        tk.Button(h,text=" ◀ Retour popup ",command=close_config_window,
                   bg="#1c2333",fg=BL,font=("Segoe UI",10,"bold"),relief="flat",padx=12).pack(side=tk.RIGHT, padx=(0,8))
        tk.Button(h,text=" ■ Arrêter le service ",command=stop_service,
                   bg="#4d1a1a",fg=RD,font=("Segoe UI",10,"bold"),relief="flat",padx=12).pack(side=tk.RIGHT)


        # === TAB STATUT ===
        dc=tk.Canvas(t1,bg=BG,highlightthickness=0); ds=ttk.Scrollbar(t1,orient="vertical",command=dc.yview)
        di=tk.Frame(dc,bg=BG); di.bind("<Configure>",lambda e:dc.configure(scrollregion=dc.bbox("all")))
        dc.create_window((0,0),window=di,anchor="nw"); dc.configure(yscrollcommand=ds.set)
        dc.pack(side=tk.LEFT,fill=tk.BOTH,expand=True); ds.pack(side=tk.RIGHT,fill=tk.Y)

        sf_c=tk.Frame(di,bg=BC,padx=20,pady=16,highlightbackground=BO,highlightthickness=1); sf_c.pack(fill=tk.X,padx=20,pady=(12,0))
        si_c=tk.Label(sf_c,text="...",font=("Segoe UI",28),bg=BC,fg=TD); si_c.pack(side=tk.LEFT,padx=(0,16))
        sff_c=tk.Frame(sf_c,bg=BC); sff_c.pack(side=tk.LEFT,fill=tk.X,expand=True)
        st_c=tk.Label(sff_c,text="Chargement...",font=("Segoe UI",16,"bold"),bg=BC,fg=TD); st_c.pack(anchor="w")
        ss_c=tk.Label(sff_c,text="",font=("Segoe UI",10),bg=BC,fg=TD); ss_c.pack(anchor="w")

        imf=tk.Frame(di,bg=BC,padx=20,pady=12,highlightbackground=BO,highlightthickness=1); imf.pack(fill=tk.X,padx=20,pady=(12,0))
        tk.Label(imf,text="AUTO-DÉTECTION SDKs",font=("Segoe UI",11,"bold"),bg=BC,fg=AM).pack(anchor="w")
        for k,m in mon.msgs:
            ic={"ok":"✅","err":"⚠ï¸","skip":"⬜"}.get(k,"?"); cl={"ok":GR,"err":RD,"skip":TD}.get(k,TX)
            tk.Label(imf,text=f"  {ic}  {m}",font=("Segoe UI",10),bg=BC,fg=cl).pack(anchor="w",pady=1)

        pf_c=tk.Frame(di,bg=BG); pf_c.pack(fill=tk.X,padx=20,pady=(12,16))


        # === TAB CODES ===
        ff=tk.Frame(t2,bg=BG,padx=20,pady=12); ff.pack(fill=tk.X)
        tk.Label(ff,text="Rechercher:",font=("Segoe UI",10),bg=BG,fg=TD).pack(side=tk.LEFT)
        sv=tk.StringVar()
        tk.Entry(ff,textvariable=sv,width=25,font=("Consolas",10),bg=BD,fg=TX,insertbackground=TX,relief="flat",
                 highlightthickness=1,highlightbackground=BO).pack(side=tk.LEFT,padx=(8,16))
        tk.Label(ff,text="Gravité:",font=("Segoe UI",10),bg=BG,fg=TD).pack(side=tk.LEFT)
        gv=tk.StringVar(value="Toutes")
        gc=ttk.Combobox(ff,textvariable=gv,width=12,values=["Toutes","ok","attente","erreur","critique"],state="readonly",font=("Segoe UI",9))
        gc.pack(side=tk.LEFT,padx=(8,16))
        cl_=tk.Label(ff,text="",font=("Segoe UI",9),bg=BG,fg=TD); cl_.pack(side=tk.RIGHT)

        sty.configure("C.Treeview",background=BC,foreground=TX,fieldbackground=BC,font=("Segoe UI",10),rowheight=26)
        sty.configure("C.Treeview.Heading",background="#1c2333",foreground=AM,font=("Segoe UI",9,"bold"))
        sty.map("C.Treeview",background=[("selected","#1c3a5e")])
        tf2=tk.Frame(t2,bg=BG,padx=20); tf2.pack(fill=tk.BOTH,expand=True,pady=(0,20))
        tr=ttk.Treeview(tf2,columns=("code","cat","desc","sev"),show="headings",style="C.Treeview")
        tr.heading("code",text="Code"); tr.heading("cat",text="Catégorie"); tr.heading("desc",text="Description"); tr.heading("sev",text="Gravité")
        tr.column("code",width=70,anchor="center"); tr.column("cat",width=130); tr.column("desc",width=450); tr.column("sev",width=80,anchor="center")
        tr.tag_configure("ok",foreground=GR); tr.tag_configure("attente",foreground=YL)
        tr.tag_configure("erreur",foreground=RD); tr.tag_configure("critique",foreground="#ff4040")
        tsb=ttk.Scrollbar(tf2,orient="vertical",command=tr.yview); tr.configure(yscrollcommand=tsb.set)
        tr.pack(side=tk.LEFT,fill=tk.BOTH,expand=True); tsb.pack(side=tk.RIGHT,fill=tk.Y)

        ac=sorted(STATUS_FR.items())
        def ftree(*_):
            for i in tr.get_children(): tr.delete(i)
            s=sv.get().lower(); g=gv.get(); n=0
            for code,(cat,desc,sev) in ac:
                if g!="Toutes" and sev!=g: continue
                if s and s not in f"{code} {cat} {desc}".lower(): continue
                tr.insert("","end",values=(code,cat,desc,sev.upper()),tags=(sev,)); n+=1
            cl_.config(text=f"{n} codes")
        sv.trace_add("write",ftree); gc.bind("<<ComboboxSelected>>",ftree); ftree()

        # === TAB INSTALL ===
        ic_=tk.Canvas(t3,bg=BG,highlightthickness=0); is_=ttk.Scrollbar(t3,orient="vertical",command=ic_.yview)
        ifr=tk.Frame(ic_,bg=BG); ifr.bind("<Configure>",lambda e:ic_.configure(scrollregion=ic_.bbox("all")))
        ic_.create_window((0,0),window=ifr,anchor="nw"); ic_.configure(yscrollcommand=is_.set)
        ic_.pack(side=tk.LEFT,fill=tk.BOTH,expand=True); is_.pack(side=tk.RIGHT,fill=tk.Y)

        w=tk.Frame(ifr,bg="#2d1600",padx=20,pady=16,highlightbackground="#92400e",highlightthickness=1); w.pack(fill=tk.X,padx=20,pady=(12,0))
        tk.Label(w,text="⚠ Python 32-bit + Pillow + pywin32 obligatoires",font=("Segoe UI",12,"bold"),bg="#2d1600",fg="#fbbf24").pack(anchor="w")
        tk.Label(w,text="Python: https://www.python.org/downloads/release/python-31210/\n→ Windows installer (32-bit)\n\npip install -r requirements.txt",
                 font=("Segoe UI",10),bg="#2d1600",fg="#fde68a",justify="left").pack(anchor="w",pady=(8,0))

        tt="MonDossier/\n├── kodak_monitor.py\n├── requirements.txt\n├── 68xx/\n│   ├── chcusb.dll + ...\n└── 6900/\n    ├── KA6900.dll + ..."
        sc=tk.Frame(ifr,bg=BC,padx=20,pady=16,highlightbackground=BO,highlightthickness=1); sc.pack(fill=tk.X,padx=20,pady=(12,16))
        tk.Label(sc,text="📁 STRUCTURE DOSSIERS",font=("Segoe UI",11,"bold"),bg=BC,fg=AM).pack(anchor="w")
        cf=tk.Frame(sc,bg=BD,padx=14,pady=12,highlightbackground=BO,highlightthickness=1); cf.pack(fill=tk.X,pady=(10,0))
        tk.Label(cf,text=tt,font=("Consolas",10),bg=BD,fg=PU,justify="left").pack(anchor="w")

        # Refresh config window — utilise le dernier scan du popup, pas de re-scan
        def refresh_config():
            if not cw.winfo_exists(): return
            with mon._lock:
                r = mon._last_result or {"peut_imprimer":False,"imprimantes":[],"erreurs_systeme":[],"horodatage":""}
            can=r.get("peut_imprimer",False); pr=r.get("imprimantes",[]); er=r.get("erreurs_systeme",[])
            _is_printing_cfg = pr and (2100 <= pr[0].get("statut_code", -1) <= 2199)
            if _is_printing_cfg:
                _step = pr[0].get("statut",{}).get("description","Impression en cours")
                si_c.config(text="🖨",fg=GR); st_c.config(text="Imprimante — IMPRESSION EN COURS",fg=GR); sf_c.config(highlightbackground="#1a4d2e")
                cfg_voyant_dot.configure(fg=GR); cfg_voyant_lbl.configure(text=f"Impression en cours — {_step}", fg=GR)
            elif can:
                si_c.config(text="✅",fg=GR); st_c.config(text="Imprimante — PRÊTE",fg=GR); sf_c.config(highlightbackground="#1a4d2e")
                cfg_voyant_dot.configure(fg=GR); cfg_voyant_lbl.configure(text="Imprimante — PRÊTE", fg=GR)
            elif not mon.sdks:
                si_c.config(text="⚠",fg=YL); st_c.config(text="Aucun SDK détecté",fg=YL); sf_c.config(highlightbackground="#4d3a00")
                cfg_voyant_dot.configure(fg=YL); cfg_voyant_lbl.configure(text="Aucun SDK détecté", fg=YL)
            else:
                si_c.config(text="❌",fg=RD); st_c.config(text="Imprimante — NON DISPONIBLE",fg=RD); sf_c.config(highlightbackground="#4d1a1a")
                cfg_voyant_dot.configure(fg=RD); cfg_voyant_lbl.configure(text="Imprimante — NON DISPONIBLE", fg=RD)
            n=len(pr); ss_c.config(text=f"{n} imprimante{'s' if n!=1 else ''}"+(f" — {len(er)} erreur(s)" if er else ""))

            for w_ in pf_c.winfo_children(): w_.destroy()
            for p in pr:
                s=p.get("statut",{}); sc0=p.get("statut_code",-1)
                sev = "impression" if 2100 <= sc0 <= 2199 else s.get("gravite","erreur")
                sc_=SV.get(sev,SV["erreur"])
                pf_=tk.Frame(pf_c,bg=BC,padx=16,pady=10,highlightbackground=sc_["fg"],highlightthickness=1); pf_.pack(fill=tk.X,pady=(0,6))
                l1=tk.Frame(pf_,bg=BC); l1.pack(fill=tk.X)
                tk.Label(l1,text=f"{p.get('modele','?')} #{p.get('id','?')}",font=("Segoe UI",12,"bold"),bg=BC,fg=TX).pack(side=tk.LEFT)
                sev_lbl = "IMPRESSION" if sev == "impression" else sev.upper()
                tk.Label(l1,text=f" {sev_lbl} ",font=("Consolas",9,"bold"),bg=sc_["bg"],fg=sc_["fg"],padx=8).pack(side=tk.RIGHT)
                tk.Label(l1,text=f"Code {p.get('statut_code','?')}",font=("Consolas",10),bg=BC,fg=TD).pack(side=tk.RIGHT,padx=(0,10))
                tk.Label(pf_,text=f"[{s.get('categorie','')}] {s.get('description','')}",font=("Segoe UI",10,"bold"),bg=BC,fg=sc_["fg"]).pack(anchor="w",pady=(3,0))
                if s.get("conseil"):
                    tk.Label(pf_,text=f"→ {s['conseil']}",font=("Segoe UI",9),bg=BC,fg=AM,wraplength=700,justify="left").pack(anchor="w",pady=(1,0))
                det=[]
                fw=p.get("firmware",{})
                if "serial" in fw: det.append(f"S/N: {fw['serial']}")
                if "main_control" in fw: det.append(f"FW: v{fw['main_control']}")
                c=p.get("compteurs",{})
                if "total" in c: det.append(f"Total: {c['total']}")
                if "media_restant" in c: det.append(f"Média: {c['media_restant']}")
                if det: tk.Label(pf_,text="  │  ".join(det),font=("Consolas",9),bg=BC,fg=TD).pack(anchor="w",pady=(3,0))

            cw.after(int(mon.interval*1000), refresh_config)

        cw.after(500, refresh_config)

    # === SCAN EN THREAD SÉPARÉ (évite de geler la GUI pendant les appels USB) ===
    def _do_scan_async():
        """Lance mon.scan() dans un thread daemon, résultat dans mon._last_result."""
        def _worker():
            try:
                mon.scan()
            except Exception as e:
                log_print(f"[Scan] Erreur inattendue: {e}")
        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    # === REFRESH MINI POPUP ===
    def refresh_popup():
        # Lire le dernier résultat mis en cache par le thread de scan (verrou pour cohérence)
        with mon._lock:
            r = mon._last_result or {"peut_imprimer": False, "imprimantes": [], "erreurs_systeme": []}
        can = r.get("peut_imprimer", False)
        pr = r.get("imprimantes", [])
        er = r.get("erreurs_systeme", [])

        conseil = ""
        # Détecter impression : code SDK 21xx dans le résultat
        _is_printing = pr and (2100 <= pr[0].get("statut_code", -1) <= 2199)

        if _is_printing:
            st0 = pr[0].get("statut", {})
            title = "Imprimante — Impression en cours"
            bg = POPUP_BG
            fg = "#18864b"
            display_mode = "printing"
            conseil = f"{st0.get('description', 'Impression en cours')} — Ne pas accéder à l'imprimante."
        elif can:
            title = "Imprimante — Prête"
            bg = POPUP_BG
            fg = "#18864b"
            display_mode = "ready"
        elif not mon.sdks:
            title = "Aucun SDK détecté"
            conseil = "Placez les DLLs dans les dossiers 68xx/ et/ou 6900/"
            bg = POPUP_BG
            fg = "#b7791f"
            display_mode = "normal"
        else:
            if pr:
                p = pr[0]
                st = p.get("statut", {})
                gravite = st.get("gravite", "erreur")
                if gravite == "attente":
                    bg = POPUP_BG
                    fg = "#b7791f"
                    display_mode = "waiting"
                    title = "Imprimante — Veuillez patienter"
                    conseil = st.get("conseil", "Opération en cours...")
                else:
                    bg = POPUP_BG
                    fg = "#ff0033"
                    display_mode = "error"
                    title = f"Imprimante — {st.get('categorie', 'ERREUR')}"
                    conseil = st.get("conseil", "")
            elif er:
                bg = POPUP_BG
                fg = "#ff0033"
                display_mode = "error"
                title = "Imprimante — Erreur"
                conseil = er[0].get("conseil", "")
            else:
                bg = POPUP_BG
                fg = "#ff0033"
                display_mode = "error"
                title = "Imprimante — Non disponible"
                conseil = "Vérifiez le câble USB et que l'imprimante est allumée"

        # --- Gestion clignotement + couleurs ---
        # Pas de clignotement en mode attente (jaune), seulement en mode erreur (rouge)
        _should_blink = (display_mode == "error") and not fullscreen_on_error.get()

        # Mettre à jour textes toujours
        status_text.configure(text=title)
        status_detail.configure(text=f"→ {conseil}" if conseil else "")

        if _should_blink:
            # Ne pas écraser les couleurs, le blink s'en charge
            # Démarrer le blink si pas déjà actif
            if not refresh_popup._blink_active:
                refresh_popup._blink_active = True
                refresh_popup._blink_on = False
        else:
            # Arrêter le blink
            refresh_popup._blink_active = False
            # Appliquer couleurs normales
            if display_mode == "waiting":
                set_popup_theme(bg, "#b7791f", accent="#f59e0b", badge="#fff7db", detail="#745c22")
            elif display_mode in {"ready", "printing"}:
                set_popup_theme(bg, "#18864b", accent="#22c55e", badge="#e7f8ee", detail="#3a7b55")
            elif display_mode == "error":
                set_popup_theme(bg, "#ff0033", accent="#ff0033", badge="#ffe3e8", detail="#ff0033")
            else:
                set_popup_theme(bg, fg, accent=POPUP_BORDER, badge=POPUP_BADGE, detail=POPUP_MUTED)

        apply_popup_mode(display_mode)
        # Lancer un nouveau scan en arrière-plan
        scan_delay = mon.interval
        _do_scan_async()
        root.after(int(scan_delay * 1000), refresh_popup)

    # Blink timer indépendant (tourne en permanence, agit seulement si _blink_active)
    refresh_popup._blink_active = False
    refresh_popup._blink_on = False
    def _blink_tick():
        if refresh_popup._blink_active:
            refresh_popup._blink_on = not refresh_popup._blink_on
            if refresh_popup._blink_on:
                c_bg = "#fff0f3"; c_fg = "#ff0033"
            else:
                c_bg = POPUP_BG; c_fg = "#ff0033"
            set_popup_theme(c_bg, c_fg, accent="#ff0033", badge="#ffe3e8", detail="#ff0033")
            set_popup_border(8)
        root.after(500, _blink_tick)
    root.after(500, _blink_tick)

    apply_popup_mode("normal")
    # Démarrer le hotfolder si un dossier est configuré
    _hf_path = gui_cfg.get("hotfolder_path", "")
    if _hf_path and os.path.isdir(_hf_path):
        hotfolder[0] = HotFolder(
            _hf_path,
            gui_cfg.get("hotfolder_copies", 1),
            gui_cfg.get("hotfolder_action", "Supprimer apres impression"),
            gui_cfg.get("sdk_print_gap_seconds", 75),
            mon,
        )
        hotfolder[0].start()
    _do_scan_async()           # premier scan immédiat en arrière-plan
    root.after(500, refresh_popup)
    root.mainloop()


# ============================================================================
#  SINGLE INSTANCE (empêche les doublons)
# ============================================================================

LOCK_FILE = os.path.join(SCRIPT_DIR, ".kodak_monitor.lock")

def kill_previous_instance():
    """Tue l'ancienne instance si elle existe (compatible Windows)."""
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE, 'r') as f:
                old_pid = int(f.read().strip())
            # Sur Windows, SIGTERM est ignoré — utiliser TerminateProcess via ctypes
            try:
                PROCESS_TERMINATE = 0x0001
                handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, old_pid)
                if handle:
                    ctypes.windll.kernel32.TerminateProcess(handle, 1)
                    ctypes.windll.kernel32.CloseHandle(handle)
                    time.sleep(0.5)
            except Exception:
                pass  # process déjà mort ou inaccessible
            try:
                os.remove(LOCK_FILE)
            except Exception:
                pass
    except Exception:
        pass

def write_lock():
    """Écrit le PID actuel dans le fichier lock."""
    try:
        with open(LOCK_FILE, 'w') as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

def remove_lock():
    """Supprime le fichier lock."""
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception:
        pass


# ============================================================================
#  MAIN
# ============================================================================

def is_frozen():
    """Détecte si on tourne en exe (PyInstaller)."""
    return getattr(sys, 'frozen', False)

def show_error(msg):
    """Affiche une erreur en popup si exe, en console sinon."""
    if is_frozen():
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk(); root.withdraw()
        messagebox.showerror("Kodak Monitor — Erreur", msg)
        root.destroy()
    else:
        print(f"\n❌ {msg}")
        try: input("\nEntrée pour quitter...")
        except (EOFError, KeyboardInterrupt): pass

def main():
    # Tuer l'ancienne instance et prendre le lock
    kill_previous_instance()
    write_lock()

    import atexit
    atexit.register(remove_lock)

    bits = struct.calcsize('P') * 8

    if not is_frozen():
        print("╔═══════════════════════════════════════════════════════╗")
        print("║  KODAK MONITOR + HOTFOLDER                           ║")
        print("╚═══════════════════════════════════════════════════════╝")
        print(f"  Python {sys.version.split()[0]} ({bits}-bit)\n")

    if bits != 32:
        show_error(f"Python {bits}-bit détecté — il faut Python 32-bit !\n\nhttps://www.python.org/downloads/release/python-31210/")
        sys.exit(1)

    try:
        from PIL import Image as _PIL_Image; del _PIL_Image
        if not is_frozen(): print("  ✅ Pillow installé")
    except ImportError:
        show_error("Pillow non installé.\n\nInstallez: pip install Pillow")

    mon = Monitor()
    log_print(f"Démarrage Kodak Monitor (Python {bits}-bit)")
    for k, m in mon.msgs:
        log_print(m)
    if not mon.sdks:
        show_error("Aucun SDK Kodak trouvé.\n\nPlacez les DLLs dans les dossiers 68xx/ et/ou 6900/ à côté de l'application.")
        sys.exit(1)

    if not is_frozen():
        for k, m in mon.msgs:
            ic = {"ok":"✅","err":"⚠ï¸","skip":"⬜"}.get(k, "?")
            print(f"  {ic} {m}")
        print(f"\n  JSON: {mon.json_path}\n  Lancement fenêtre...\n")

    run_gui(mon)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        show_error(f"{type(e).__name__}: {e}")
        sys.exit(1)
