import io
import os
import re
import time
import requests
import threading
import csv  # Importation du module CSV pour une écriture propre
from concurrent.futures import ThreadPoolExecutor
from pypdf import PdfReader

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

# Verrou de sécurité pour éviter les conflits d'écriture simultanée dans le fichier CSV
verrou_fichier = threading.Lock()

def initialiser_navigateur():
    """Lance Google Chrome adapté à l'environnement d'exécution (Local ou GitHub)."""
    options = webdriver.ChromeOptions()
    
    # Activez ces 3 lignes si vous lancez sur GITHUB ACTIONS (mode invisible)
    # options.add_argument("--headless=new") 
    # options.add_argument("--no-sandbox")
    # options.add_argument("--disable-dev-shm-usage")
    
    # Options pour le lancement LOCAL (visible)
    options.add_argument("--start-maximized")
    
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    
    driver = webdriver.Chrome(options=options)
    return driver

def chercher_urls_pdf_via_duckduckgo(driver, nom, prenom):
    """Effectue la recherche sur DuckDuckGo et extrait les liens PDF."""
    nom_q = nom.strip().replace('"', '')
    prenom_q = prenom.strip().replace('"', '')
    
    # Syntaxe de recherche DuckDuckGo (identique à Google)
    requete = f'"{prenom_q} {nom_q}" (registre OR ordre OR annuaire OR medecins) filetype:pdf'
    liens_pdf = []
    
    try:
        # Navigation vers DuckDuckGo
        driver.get("https://duckduckgo.com/?q=")
        time.sleep(2)
        
        # Saisie de la requête dans la barre de recherche DuckDuckGo
        # Le champ de recherche DuckDuckGo utilise l'identifiant 'search_form_input' ou le nom 'q'
        try:
            barre_recherche = driver.find_element(By.NAME, "q")
        except:
            barre_recherche = driver.find_element(By.ID, "search_form_input")
            
        barre_recherche.send_keys(requete)
        barre_recherche.send_keys(Keys.ENTER)
        time.sleep(3) # Laisse le temps aux résultats dynamiques de charger
        
        # Extraction de tous les liens de la page de résultats
        elements_liens = driver.find_elements(By.XPATH, '//a[@href]')
        for elem in elements_liens:
            href = elem.get_attribute("href")
            # Élimine les redirections internes DuckDuckGo et ne garde que les PDF
            if href and href.lower().endswith('.pdf') and "duckduckgo.com/?q=" not in href.lower():
                liens_pdf.append(href)
                
    except Exception as e:
        print(f"    ⚠️ Problème de navigation DuckDuckGo pour {prenom} {nom}")
        
    return list(set(liens_pdf))[:2]

def extraire_tous_les_emails(texte_brut):
    """Extrait les e-mails en corrigeant les espacements induits par les PDF."""
    texte_nettoye = re.sub(r'\s*@\s*', '@', texte_brut)
    texte_nettoye = re.sub(r'\s*\.\s*([a-zA-Z]{2,4})\b', r'.\1', texte_nettoye)

    regex_email = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    matches = re.findall(regex_email, texte_nettoye)

    liste_noire_technique = ['ccsd', 'api', 'w3.org', 'example', 'pappers', 'adobe', 'macrovision']
    emails_valides = set()
    for email in matches:
        if any(ex in email.lower() for ex in liste_noire_technique):
            continue
        emails_valides.add(email.lower())
    return list(emails_valides)

def scanner_pdf_integral(url):
    """Télécharge en tâche de fond le PDF trouvé pour extraire son contenu."""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 200 and b"%PDF" in response.content[:4]:
            pdf_file = io.BytesIO(response.content)
            reader = PdfReader(pdf_file)
            texte_accumule = ""
            for page in reader.pages:
                text_page = page.extract_text()
                if text_page:
                    texte_accumule += text_page + "\n"
            return texte_accumule
    except Exception:
        pass
    return ""

def traiter_un_medecin(donnees_medecin, fichier_sortie):
    """Fonction exécutée en parallèle pour traiter un médecin de A à Z via DuckDuckGo."""
    idx, prenom, nom = donnees_medecin
    print(f"🔍 [Thread Actif] Dr {prenom} {nom} (Ligne {idx})")
    
    driver = initialiser_navigateur()
    trouve_au_moins_un_mail = False
    
    try:
        # Appel de la recherche DuckDuckGo
        urls_pdf = chercher_urls_pdf_via_duckduckgo(driver, nom, prenom)
        
        if urls_pdf:
            for url_pdf in urls_pdf:
                print(f"    📄 PDF trouvé pour Dr {nom} : {url_pdf[:60]}...")
                texte = scanner_pdf_integral(url_pdf)
                if texte:
                    emails = extraire_tous_les_emails(texte)
                    if emails:
                        print(f"    ✅ {len(emails)} mail(s) extrait(s) pour Dr {prenom} {nom} !")
                        trouve_au_moins_un_mail = True
                        emails_fusionnes = ",".join(emails)
                        
                        # ÉCRITURE EN TEMPS RÉEL CSV
                        with verrou_fichier:
                            with open(fichier_sortie, mode='a', encoding='utf-8', newline='') as f_out:
                                writer = csv.writer(f_out, delimiter=',', quoting=csv.QUOTE_MINIMAL)
                                writer.writerow([nom, prenom, emails_fusionnes, url_pdf])
                                f_out.flush()
        
        # Si aucun e-mail n'a été trouvé (pas de PDF ou PDF exempt d'e-mails)
        if not trouve_au_moins_un_mail:
            print(f"    ❌ Aucun mail trouvé pour Dr {prenom} {nom}.")
            with verrou_fichier:
                with open(fichier_sortie, mode='a', encoding='utf-8', newline='') as f_out:
                    writer = csv.writer(f_out, delimiter=',', quoting=csv.QUOTE_MINIMAL)
                    writer.writerow([nom, prenom, "Non trouvé", ""])
                    f_out.flush()
    finally:
        driver.quit()

if __name__ == "__main__":
    FICHIER_CSV = "villes.csv"
    FICHIER_SORTIE = "emails_visuels.csv"
    NB_THREADS_SIMULTANES = 4 

    if not os.path.exists(FICHIER_CSV):
        print(f"❌ Erreur : '{FICHIER_CSV}' introuvable.")
        exit(1)

    with open(FICHIER_CSV, mode='r', encoding='utf-8', errors='ignore') as f:
        lignes_brutes = [l.strip() for l in f.read().splitlines() if l.strip()]

    print(f"🚀 Initialisation. {len(lignes_brutes) - 1} lignes prêtes à être réparties sur DuckDuckGo...")

    # Création de l'en-tête CSV s'il n'existe pas encore
    if not os.path.exists(FICHIER_SORTIE):
        with open(FICHIER_SORTIE, mode='w', encoding='utf-8', newline='') as f_init:
            writer = csv.writer(f_init, delimiter=',')
            writer.writerow(["Nom", "Prénom", "E-mail Extrait", "Source PDF"])

    liste_medecins_A_traiter = []

    for idx, ligne_propre in enumerate(lignes_brutes):
        if idx == 0: 
            continue 
        
        if ';' in ligne_propre:
            elements = ligne_propre.split(';')
        elif '\t' in ligne_propre:
            elements = re.split(r'\t+', ligne_propre)
        else:
            elements = re.split(r'\s{2,}', ligne_propre)
        elements = [el.strip() for el in elements if el.strip()]
        
        if len(elements) < 2: 
            continue

        prenom = elements[0]
        nom = elements[1]

        if prenom.lower() in ['prenom', 'prénom', 'nom', 'name'] or nom.lower() in ['nom', 'name']:
            continue

        liste_medecins_A_traiter.append((idx, prenom, nom))

    print(f"🔥 Lancement des {NB_THREADS_SIMULTANES} instances en parallèle via DuckDuckGo...")
    
    with ThreadPoolExecutor(max_workers=NB_THREADS_SIMULTANES) as executor:
        executor.map(lambda med: traiter_un_medecin(med, FICHIER_SORTIE), liste_medecins_A_traiter)

    print("\n🏁 [FIN DU SCRIPT] Toutes les lignes ont été traitées.")
