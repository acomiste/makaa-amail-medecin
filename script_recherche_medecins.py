import os
import csv
import time
import re
import tempfile
import threading
from urllib.parse import quote

FICHIER_SOURCE = "medecins.csv"
FICHIER_RESULTAT = "resultats_medecins.csv"

NB_THREADS = 2          # nombre de médecins traités simultanément
PAUSE_TOUS_LES_N = 50   # déclenche une pause tous les N médecins traités (au total)
DUREE_PAUSE = 120       # durée de la pause en secondes (2 minutes)

# True  = navigateur invisible (à utiliser sur GitHub Actions, pas d'écran)
# False = navigateur visible (à utiliser en local pour voir ce qui se passe)
MODE_HEADLESS = os.environ.get("RUN_HEADLESS", "false").lower() == "true"

# --- Verrous partagés entre threads ---
verrou_fichier = threading.Lock()   # protège l'écriture des CSV
verrou_etat = threading.Lock()      # protège le compteur / la pause globale
verrou_demarrage_driver = threading.Lock()  # évite que 2 Chrome démarrent en même temps

etat_global = {
    'compteur': 0,
    'pause_jusqu_a': 0.0,
}


def initialiser_fichiers():
    if not os.path.exists(FICHIER_SOURCE):
        print(f"Erreur : Le fichier '{FICHIER_SOURCE}' est introuvable.")
        return False
    if not os.path.exists(FICHIER_RESULTAT):
        with open(FICHIER_RESULTAT, mode='w', encoding='utf-8', newline='') as f:
            writer = csv.writer(f, delimiter=';')
            writer.writerow(['Prénom', 'Nom', 'Email trouvé sur le web', 'URL Source'])
    return True


def lire_csv_avec_fallback_encodage(chemin_fichier):
    encodages_a_tester = ['utf-8-sig', 'cp1252', 'latin-1']
    derniere_erreur = None

    for enc in encodages_a_tester:
        try:
            with open(chemin_fichier, mode='r', encoding=enc, newline='') as f:
                reader = csv.DictReader(f, delimiter=';')
                champs = list(reader.fieldnames)
                lignes = list(reader)
            print(f"Fichier '{chemin_fichier}' lu avec succès en encodage : {enc}")
            return champs, lignes
        except UnicodeDecodeError as e:
            derniere_erreur = e
            continue

    raise UnicodeDecodeError(
        "utf-8", b"", 0, 1,
        f"Impossible de lire {chemin_fichier} avec les encodages testés {encodages_a_tester} : {derniere_erreur}"
    )


def extraire_emails_du_texte_page(texte_brut):
    motif = r'[a-zA-Z0-9_.-]+@[a-zA-Z0-9_.-]+\.[a-zA-Z]{2,4}'
    trouves = re.findall(motif, texte_brut)
    emails_propres = [e for e in trouves if not e.lower().endswith(('.png', '.jpg', '.gif', 'sentry.io', 'w3.org'))]
    return list(set(emails_propres))


def sauvegarder_source_de_maniere_sure(champs, lignes_medecins):
    with verrou_fichier:
        dossier = os.path.dirname(os.path.abspath(FICHIER_SOURCE)) or "."
        fd, chemin_temp = tempfile.mkstemp(prefix="medecins_tmp_", suffix=".csv", dir=dossier)
        try:
            with os.fdopen(fd, mode='w', encoding='utf-8', newline='') as f_tmp:
                writer_src = csv.DictWriter(f_tmp, fieldnames=champs, delimiter=';', extrasaction='ignore')
                writer_src.writeheader()
                writer_src.writerows(lignes_medecins)
            os.replace(chemin_temp, FICHIER_SOURCE)
        except Exception as e:
            print(f"⚠️ Erreur lors de la sauvegarde du fichier source, données préservées : {e}")
            if os.path.exists(chemin_temp):
                os.remove(chemin_temp)


def ajouter_resultat(prenom, nom, email, url):
    with verrou_fichier:
        with open(FICHIER_RESULTAT, mode='a', encoding='utf-8', newline='') as f_res:
            writer = csv.writer(f_res, delimiter=';')
            writer.writerow([prenom, nom, email, url])


def attendre_si_pause_en_cours():
    with verrou_etat:
        pause_jusqu_a = etat_global['pause_jusqu_a']
    maintenant = time.time()
    if maintenant < pause_jusqu_a:
        attente = pause_jusqu_a - maintenant
        print(f"⏸ [Pause globale] {int(attente)}s restantes...")
        time.sleep(attente)


def signaler_medecin_traite(nom_thread):
    with verrou_etat:
        etat_global['compteur'] += 1
        compteur_actuel = etat_global['compteur']
        if compteur_actuel % PAUSE_TOUS_LES_N == 0:
            etat_global['pause_jusqu_a'] = time.time() + DUREE_PAUSE
            print(f"\n🛑 [{nom_thread}] {compteur_actuel} médecins traités au total. "
                  f"Pause de {DUREE_PAUSE // 60} minute(s) pour tous les threads...\n")


def creer_driver():
    from selenium import webdriver
    options = webdriver.ChromeOptions()
    if MODE_HEADLESS:
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
    else:
        options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    with verrou_demarrage_driver:
        return webdriver.Chrome(options=options)
