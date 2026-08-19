import os
import csv
import time
import re
import tempfile
import threading
import sys
import requests  # Requis pour la recherche stable de secours
from urllib.parse import quote

# Force Python à vider son buffer d'affichage immédiatement pour GitHub Actions
sys.stdout.reconfigure(line_buffering=True)

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
        print(f"Erreur : Le fichier '{FICHIER_SOURCE}' est introuvable.", flush=True)
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
            print(f"Fichier '{chemin_fichier}' lu avec succès en encodage : {enc}", flush=True)
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
            print(f"⚠️ Erreur lors de la sauvegarde du fichier source : {e}", flush=True)
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
        print(f"⏸ [Pause globale] {int(attente)}s restantes...", flush=True)
        time.sleep(attente)


def signaler_medecin_traite(nom_thread):
    with verrou_etat:
        etat_global['compteur'] += 1
        compteur_actuel = etat_global['compteur']
        if compteur_actuel % PAUSE_TOUS_LES_N == 0:
            etat_global['pause_jusqu_a'] = time.time() + DUREE_PAUSE
            print(f"\n🛑 [{nom_thread}] {compteur_actuel} médecins traités au total. "
                  f"Pause de {DUREE_PAUSE // 60} minute(s) pour tous les threads...\n", flush=True)


def creer_driver():
    from selenium import webdriver
    options = webdriver.ChromeOptions()
    if MODE_HEADLESS:
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--remote-debugging-pipe")
        options.add_argument("--window-size=1920,1080")
    else:
        options.add_argument("--start-maximized")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    with verrou_demarrage_driver:
        return webdriver.Chrome(options=options)
def verifier_si_captcha(driver, nom_thread, prenom, nom):
    mots_cles_bloquants = [
        "captcha", "g-recaptcha", "cloudflare", "hcaptcha", "checking your browser",
        "please verify you are a robot", "pas un robot", "automated access"
    ]
    try:
        html_page = driver.page_source.lower()
        url_actuelle = driver.current_url.lower()
        for mot in mots_cles_bloquants:
            if mot in html_page or mot in url_actuelle:
                print(f"\n╔════════════════════════════════════════════════════════════╗", flush=True)
                print(f"  🛑 [{nom_thread}] [ALERTE CAPTCHA SUR SITE WEB EXTERNE] 🛑", flush=True)
                print(f"  ⚠️  Le site visité pour Dr {prenom} {nom} bloque l'accès.", flush=True)
                print(f"  🌐 URL : {driver.current_url}", flush=True)
                print(f"╚════════════════════════════════════════════════════════════╝\n", flush=True)
                return True
    except:
        pass
    return False


def executer_recherche_http_robuste(requete, nom_thread):
    """Effectue une recherche textuelle et affiche une alerte si un Captcha moteur surgit."""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    liens_extraits = []
    
    # 1. Tentative sur DuckDuckGo HTML
    try:
        url_ddg = f"https://duckduckgo.com{quote(requete)}"
        res = requests.get(url_ddg, headers=headers, timeout=10)
        
        # Détection de Captcha Moteur (Code 403 / 429 ou mot clé dans la page)
        if res.status_code in [403, 429] or "captcha" in res.text.lower() or "anti-bot" in res.text.lower():
            print(f"\n╔════════════════════════════════════════════════════════════╗", flush=True)
            print(f"  🛑 [{nom_thread}] [CAPTCHA DÉTECTÉ SUR DUCKDUCKGO] 🛑", flush=True)
            print(f"  ⚠️  DuckDuckGo bloque temporairement les requêtes de GitHub.", flush=True)
            print(f"╚════════════════════════════════════════════════════════════╝\n", flush=True)
        elif res.status_code == 200:
            liens_extraits.extend(re.findall(r'href="(https?://[^"]+)"', res.text))
    except Exception as e:
        print(f"    ℹ️ [{nom_thread}] DuckDuckGo indisponible (Tentative Secours Google...)", flush=True)

    # 2. Secours sur Google HTML si DDG n'a rien renvoyé
    if not liens_extraits:
        try:
            url_google = f"https://google.com{quote(requete)}"
            res = requests.get(url_google, headers=headers, timeout=10)
            
            # Détection de Captcha Moteur sur Google
            if res.status_code in [403, 429] or "captcha" in res.text.lower() or "not_found_error" in res.text.lower():
                print(f"\n╔════════════════════════════════════════════════════════════╗", flush=True)
                print(f"  🛑 [{nom_thread}] [CAPTCHA DÉTECTÉ SUR GOOGLE SECOURS] 🛑", flush=True)
                print(f"  ⚠️  Google bloque également l'adresse IP du serveur Cloud.", flush=True)
                print(f"╚════════════════════════════════════════════════════════════╝\n", flush=True)
            elif res.status_code == 200:
                liens_extraits.extend(re.findall(r'/url\?q=(https?://[^&]+)', res.text))
        except:
            pass

    # Filtrage des liens publicitaires/internes
    liens_propres = []
    for url in liens_extraits:
        if not any(x in url.lower() for x in ["duckduckgo", "google", "w3.org", "adobe", "pappers"]):
            if url.startswith("http") and url not in liens_propres:
                liens_propres.append(url)
                
    return liens_propres[:3]


def traiter_medecin(driver, medecin, cle_prenom, cle_nom, nom_thread):
    prenom = medecin.get(cle_prenom, '').strip()
    nom = medecin.get(cle_nom, '').strip()

    affichage_nom = f"{prenom} {nom}" if prenom.lower() != nom.lower() else nom
    print(f"[{nom_thread}] Extraction en cours : {affichage_nom}...", flush=True)

    mots_cles = "cpts msp sisa thèse"
    requete_complete = f'"{prenom}" "{nom}" {mots_cles}'
    
    # Lancement de la recherche robuste avec détection intégrée
    urls_a_visiter = executer_recherche_http_robuste(requete_complete, nom_thread)

    email_trouve = "Non disponible"
    url_source_finale = f"https://duckduckgo.com{quote(requete_complete)}"

    if urls_a_visiter:
        print(f"[{nom_thread}] -> {len(urls_a_visiter)} site(s) web détecté(s). Analyse en cours...", flush=True)
        for url in urls_a_visiter:
            try:
                if any(excl in url.lower() for excl in ["pagesjaunes", "mappy", "facebook", "linkedin", "twitter"]):
                    continue
                    
                driver.get(url)
                time.sleep(2)

                if verifier_si_captcha(driver, nom_thread, prenom, nom):
                    continue

                texte_site = driver.find_element(By.TAG_NAME, "body").text
                mails_site = extraire_emails_du_texte_page(texte_site)

                if mails_site:
                    email_trouve = " ; ".join(mails_site)
                    url_source_finale = url
                    print(f"[{nom_thread}] -> [SUCCÈS] Mail trouvé sur : {url}", flush=True)
                    break
            except:
                continue
    else:
        print(f"[{nom_thread}] -> Aucun site web externe analysable (Moteurs saturés ou bloqués).", flush=True)

    ajouter_resultat(prenom, nom, email_trouve, url_source_finale)
    medecin['statut'] = 'Traité'


def travail_thread(champs, lignes_medecins, cle_prenom, cle_nom, nom_thread):
    driver = None
    try:
        driver = creer_driver()
        while True:
            attendre_si_pause_en_cours()
            medecin_a_traiter = None
            
            with verrou_etat:
                for row in lignes_medecins:
                    if row.get('statut', '').strip().lower() != 'traité':
                        row['statut'] = 'Traité'
                        medecin_a_traiter = row
                        break
            
            if not medecin_a_traiter:
                print(f"🏁 [{nom_thread}] Plus aucun médecin à traiter. Fermeture.", flush=True)
                break
                
            try:
                traiter_medecin(driver, medecin_a_traiter, cle_prenom, cle_nom, nom_thread)
            except Exception as e:
                print(f"❌ [{nom_thread}] Erreur sur une ligne : {e}", flush=True)
                
            sauvegarder_source_de_maniere_sure(champs, lignes_medecins)
            signaler_medecin_traite(nom_thread)
    finally:
        if driver:
            driver.quit()


if __name__ == "__main__":
    if not initialiser_fichiers():
        exit(1)

    print("📖 Chargement et analyse du fichier source...", flush=True)
    champs, lignes_medecins = lire_csv_avec_fallback_encodage(FICHIER_SOURCE)

    cle_prenom = next((c for c in champs if 'prénom' in c.lower() or 'prenom' in c.lower()), None)
    cle_nom = next((c for c in champs if 'nom' in c.lower()), None)

    if not cle_prenom or not cle_nom:
        print(f"❌ Erreur : Colonnes 'Prénom' ou 'Nom' manquantes dans {FICHIER_SOURCE}.", flush=True)
        exit(1)

    if 'statut' not in champs:
        champs.append('statut')

    lignes_filtrées = [l for l in lignes_medecins if l.get('statut', '').strip().lower() != 'traité']
    print(f"🔥 {len(lignes_filtrées)} médecin(s) restant(s) à extraire.", flush=True)

    if not lignes_filtrées:
        print("🏁 Tous les médecins de la liste ont déjà été marqués comme 'Traité'.", flush=True)
        exit(0)

    threads_actifs = []
    for i in range(NB_THREADS):
        nom_t = f"Thread-{i+1}"
        t = threading.Thread(
            target=travail_thread, 
            args=(champs, lignes_medecins, cle_prenom, cle_nom, nom_t),
            name=nom_t
        )
        threads_actifs.append(t)
        t.start()
        time.sleep(2.0)

    for t in threads_actifs:
        t.join()

    print("\n🏁 [FIN DU SCRIPT] Exécution terminée.", flush=True)
