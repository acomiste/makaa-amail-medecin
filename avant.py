import os
import csv
import time
import re
import tempfile
import threading
from urllib.parse import quote_plus
from selenium import webdriver
from selenium.webdriver.common.by import By

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
    options = webdriver.ChromeOptions()
    if MODE_HEADLESS:
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
    else:
        options.add_argument("--start-maximized")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    with verrou_demarrage_driver:
        return webdriver.Chrome(options=options)


def collecter_liens_ddg(driver):
    liens_trouves = []
    try:
        elements = driver.find_elements(By.CLASS_NAME, "result__url")
        for elem in elements:
            href = elem.get_attribute("href")
            if href and "duckduckgo.com" not in href:
                liens_trouves.append(href)
    except:
        pass
    return liens_trouves


def traiter_medecin(driver, medecin, cle_prenom, cle_nom, nom_thread):
    prenom = medecin.get(cle_prenom, '').strip()
    nom = medecin.get(cle_nom, '').strip()

    print(f"[{nom_thread}] Extraction en cours : {prenom} {nom}...")

    mots_cles = '(cpts OR msp OR sisa OR thèse OR ird OR @gmail.com OR @orange.fr)'
    requete_complete = f'"{prenom}" "{nom}" {mots_cles}'
    url_initiale = f"https://html.duckduckgo.com/html/?q={quote_plus(requete_complete)}"

    urls_a_visiter = []

    # --- ÉTAPE 1 : COLLECTE DES LIENS (PAGE 1) ---
    try:
        driver.get(url_initiale)
        time.sleep(2)
        urls_a_visiter.extend(collecter_liens_ddg(driver))

        # --- NAVIGATION VERS PAGE 2 ---
        try:
            bouton_suivant = driver.find_element(By.XPATH, '//input[@type="submit" and (@value="Next" or @value="Suivant" or contains(@class, "nav-btn"))]')
            bouton_suivant.click()
            time.sleep(2)
            urls_a_visiter.extend(collecter_liens_ddg(driver))
        except:
            pass

    except Exception as e:
        print(f"[{nom_thread}] Erreur lors de la lecture des résultats DuckDuckGo : {e}")

    urls_a_visiter = list(dict.fromkeys(urls_a_visiter))

    email_trouve = "Non disponible"
    url_source_finale = f"https://html.duckduckgo.com/html/?q={quote_plus(requete_complete)}"

    # --- ÉTAPE 2 : VISITE INDIVIDUELLE DES SITES WEB POUR CHERCHER LE MAIL ---
    if urls_a_visiter:
        print(f"[{nom_thread}] -> {len(urls_a_visiter)} site(s) web trouvé(s) à analyser pour ce médecin.")
        
        for url in urls_a_visiter:
            try:
                if any(excl in url.lower() for excl in ["pagesjaunes", "mappy", "facebook", "linkedin", "twitter"]):
                    continue

                driver.get(url)
                time.sleep(2)

                texte_site = driver.find_element(By.TAG_NAME, "body").text
                mails_site = extraire_emails_du_texte_page(texte_site)

                if mails_site:
                    email_trouve = " ; ".join(mails_site)
                    url_source_finale = url
                    print(f"[{nom_thread}] -> [SUCCÈS] Mail trouvé directement sur : {url}")
                    break

            except:
                continue
    else:
        print(f"[{nom_thread}] -> Aucun site web externe détecté sur DuckDuckGo.")

    ajouter_resultat(prenom, nom, email_trouve, url_source_finale)
    medecin['statut'] = 'Traité'


def travail_thread(nom_thread, medecins_assignes, champs, cle_prenom, cle_nom, lignes_medecins):
    driver = creer_driver()
    try:
        for medecin in medecins_assignes:
            attendre_si_pause_en_cours()
            try:
                traiter_medecin(driver, medecin, cle_prenom, cle_nom, nom_thread)
            except Exception as e:
                print(f"[{nom_thread}] Interruption inattendue sur une fiche : {e}")
            finally:
                sauvegarder_source_de_maniere_sure(champs, lignes_medecins)
                signaler_medecin_traite(nom_thread)
    finally:
        driver.quit()
        print(f"[{nom_thread}] Terminé.")


def recherche_automatique():
    if not initialiser_fichiers():
        return

    lignes_medecins = []
    with open(FICHIER_SOURCE, mode='r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f, delimiter=';')
        champs = list(reader.fieldnames)
        for row in reader:
            lignes_medecins.append(row)

    print(f"Colonnes détectées dans {FICHIER_SOURCE} : {champs}")

    if 'statut' not in champs:
        champs.append('statut')
        for row in lignes_medecins:
            row.setdefault('statut', '')

    cle_prenom = 'prenom' if 'prenom' in champs else ('preenom' if 'preenom' in champs else 'prenom')
    cle_nom = 'nom' if 'nom' in champs else 'nom'

    medecins_a_traiter = [m for m in lignes_medecins if m.get('statut', '').strip().lower() != 'traité']

    if not medecins_a_traiter:
        print("Tous les médecins ont déjà été marqués comme 'Traité' !")
        return

    print(f"--- Début de l'analyse approfondie : {len(medecins_a_traiter)} médecin(s) restant(s) ---")

    threads = []
    for i in range(NB_THREADS):
        lot_thread = medecins_a_traiter[i::NB_THREADS]
        if not lot_thread:
            continue
        nom_t = f"Thread-{i+1}"
        t = threading.Thread(
            target=travail_thread, 
            args=(nom_t, lot_thread, champs, cle_prenom, cle_nom, lignes_medecins)
        )
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    print("\n--- Extraction terminée. Vos vraies URL sources sont dans 'resultats_medecins.csv' ! ---")


if __name__ == "__main__":
    recherche_automatique()
s