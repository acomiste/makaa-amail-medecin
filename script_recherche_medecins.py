import os
import csv
import time
import re
import tempfile
import threading
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

FICHIER_SOURCE = "medecins.csv"
FICHIER_RESULTAT = "resultats_medecins.csv"

NB_THREADS = 2          # nombre de médecins traités simultanément
PAUSE_TOUS_LES_N = 50   # déclenche une pause tous les N médecins traités (au total)
DUREE_PAUSE = 120       # durée de la pause en secondes (2 minutes)

# --- Verrous partagés entre threads ---
verrou_fichier = threading.Lock()   # protège l'écriture des CSV
verrou_etat = threading.Lock()      # protège le compteur / la pause globale

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
    """
    Réécrit le fichier source SANS jamais risquer de le vider.
    Écriture dans un fichier temporaire puis remplacement atomique.
    Protégé par verrou_fichier car appelé depuis plusieurs threads.
    """
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
    """Si une pause globale est active, attend qu'elle se termine."""
    with verrou_etat:
        pause_jusqu_a = etat_global['pause_jusqu_a']
    maintenant = time.time()
    if maintenant < pause_jusqu_a:
        attente = pause_jusqu_a - maintenant
        print(f"⏸ [Pause globale] {int(attente)}s restantes...")
        time.sleep(attente)


def signaler_medecin_traite(nom_thread):
    """
    Incrémente le compteur global. Toutes les PAUSE_TOUS_LES_N fiches
    traitées (tous threads confondus), déclenche une pause de DUREE_PAUSE
    secondes pour TOUS les threads.
    """
    with verrou_etat:
        etat_global['compteur'] += 1
        compteur_actuel = etat_global['compteur']
        if compteur_actuel % PAUSE_TOUS_LES_N == 0:
            etat_global['pause_jusqu_a'] = time.time() + DUREE_PAUSE
            print(f"\n🛑 [{nom_thread}] {compteur_actuel} médecins traités au total. "
                  f"Pause de {DUREE_PAUSE // 60} minute(s) pour tous les threads...\n")


def creer_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--start-maximized")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    return webdriver.Chrome(options=options)


def traiter_medecin(driver, medecin, cle_prenom, cle_nom, nom_thread):
    prenom = medecin.get(cle_prenom, '').strip()
    nom = medecin.get(cle_nom, '').strip()

    print(f"[{nom_thread}] Scan en cours : {prenom} {nom}...")

    driver.get("https://google.com")
    time.sleep(1.5)

    try:
        bouton_cookies = driver.find_element(
            By.XPATH, '//button[contains(., "Tout accepter") or contains(., "I agree")]'
        )
        bouton_cookies.click()
        time.sleep(0.5)
    except Exception:
        pass

    email_sauvegarde = "Non disponible"
    try:
        barre_saisie = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.NAME, "q"))
        )
        mots_cles = '(cpts OR msp OR sisa OR thèse OR ird OR @gmail.com OR @orange.fr)'
        requete_complete = f'"{prenom}" "{nom}" {mots_cles}'

        barre_saisie.clear()
        barre_saisie.send_keys(requete_complete)
        time.sleep(0.5)
        barre_saisie.send_keys(Keys.ENTER)

        time.sleep(3)

        contenu_page = driver.find_element(By.TAG_NAME, "body").text
        mails_detectes = extraire_emails_du_texte_page(contenu_page)

        if mails_detectes:
            email_sauvegarde = " ; ".join(mails_detectes)
            print(f"[{nom_thread}] -> [OK] Trouvé : {email_sauvegarde}")
        else:
            print(f"[{nom_thread}] -> Pas de mail visible sur cette page.")

    except Exception as e:
        print(f"[{nom_thread}] Erreur lors du traitement de la page : {e}")

    url_source_actuelle = driver.current_url
    ajouter_resultat(prenom, nom, email_sauvegarde, url_source_actuelle)

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
                # Sauvegarde après chaque fiche, même en cas d'erreur partielle
                sauvegarder_source_de_maniere_sure(champs, lignes_medecins)
                signaler_medecin_traite(nom_thread)
    finally:
        driver.quit()
        print(f"[{nom_thread}] Terminé.")


def recherche_automatique():
    if not initialiser_fichiers():
        return

    lignes_medecins = []
    # encoding='utf-8-sig' : supprime le BOM éventuel (fichiers Excel)
    with open(FICHIER_SOURCE, mode='r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f, delimiter=';')
        champs = list(reader.fieldnames)
        for row in reader:
            lignes_medecins.append(row)

    print(f"Colonnes détectées dans {FICHIER_SOURCE} : {champs}")

    # Tolère la faute de frappe 'preenom' au lieu de 'prenom'
    cle_prenom = 'prenom' if 'prenom' in champs else ('preenom' if 'preenom' in champs else 'prenom')
    cle_nom = 'nom' if 'nom' in champs else 'nom'
    if cle_prenom != 'prenom':
        print(f"⚠️ Colonne prénom détectée sous le nom '{cle_prenom}' — prise en compte automatiquement.")

    medecins_restants = [m for m in lignes_medecins if m.get('statut', '').strip().lower() != 'traité'
                          and m.get(cle_prenom, '').strip() and m.get(cle_nom, '').strip()]

    if not medecins_restants:
        print("Tous les médecins ont déjà été traités !")
        return

    print(f"--- Lancement du scan ({NB_THREADS} en parallèle, pause {DUREE_PAUSE // 60}min "
          f"tous les {PAUSE_TOUS_LES_N}) — Reste : {len(medecins_restants)} médecin(s) ---")

    # Répartition round-robin entre les threads (charge équilibrée)
    lots = [medecins_restants[i::NB_THREADS] for i in range(NB_THREADS)]

    threads = []
    for i, lot in enumerate(lots):
        nom_thread = f"Thread-{i+1}"
        t = threading.Thread(
            target=travail_thread,
            args=(nom_thread, lot, champs, cle_prenom, cle_nom, lignes_medecins),
        )
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    print("\n--- Session terminée. Les résultats ont été enregistrés. ---")


if __name__ == "__main__":
    recherche_automatique()