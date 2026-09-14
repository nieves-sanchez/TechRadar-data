"""
Protege _strip_external_tail() en extract.py — Fix B (FASE B, hardening previo al Paso 6).

Todos los textos de este archivo marcados como "texto real" son description_full
capturados tal cual durante el cuarto piloto de FASE B (2026-09-11, commit da7ad19),
recuperados del snapshot local %TEMP%\\techradar_audit\\after_pilot_pro_da7ad19.json
(nunca escritos aquí de memoria). Detalle completo del hallazgo:
notes/PROJECT_MASTER_CONTEXT.md §57.6.15-B.

_strip_external_tail() corta el texto en el primer encabezado de contenido ajeno
(ofertas similares, búsquedas populares, estadísticas salariales) reconocido como
LÍNEA COMPLETA — nunca como subcadena dentro de una frase normal.
"""

from scripts.extract import _strip_external_tail


# =============================================================================
# A) Caso real grave: job 5878750762 (PL, Senior Oracle Developer, Beinit)
# =============================================================================
# Texto real completo (825 caracteres) — el cuerpo genuino son solo las 4 primeras
# líneas de contenido; todo lo posterior a "Podobne oferty" es una oferta distinta
# (Senior Oracle/Python Developer, GET IT TOGETHER) más navegación y cookies.

_JOB_5878750762_REAL = """\
Senior Oracle Developer
Beinit
Poland
NOWA OFERTA
Be in IT specjalizuje się w kompleksowej rekrutacji specjalistów IT dla polskich i międzynarodowych firm. Wspieramy klientów w pozyskiwaniu ekspertów z obszarów software development, AI, data, cloud, cybersecurity, ERP, CRM oraz zarządzania IT.
Stawiamy na znajomość technologii, przejrzystą komunikację i partnerskie podejście, dzięki którym skutecznie dopasowujemy kandydatów do potrzeb organizacji i realizowanych projektów.
Obecnie dla naszego klienta poszukujemy osoby na stanowisko: Senior Oracle Developer
Podobne oferty
Senior Oracle/Python Developer
GET IT TOGETHER SPÓŁKA Z OGRANICZONĄ ODPOWIEDZIALNOŚCIĄ
Wrocław, dolnośląskie
Popularne wyszukiwania
Tworząc powiadomienie, akceptujesz nasz T & Cs i Privacy Notice oraz zgadzasz się na wykorzystywanie plików cookie."""


def test_5878750762_cuerpo_real_permanece_y_cola_desaparece():
    """No afirma recuperar la descripción completa: solo que la contaminación desaparece."""
    resultado = _strip_external_tail(_JOB_5878750762_REAL)

    # Cuerpo real (anterior a "Podobne oferty") permanece íntegro
    assert "Senior Oracle Developer" in resultado
    assert "Be in IT specjalizuje się" in resultado
    assert "Obecnie dla naszego klienta poszukujemy osoby na stanowisko" in resultado

    # Cola externa desaparece por completo
    assert "Podobne oferty" not in resultado
    assert "Popularne wyszukiwania" not in resultado
    assert "GET IT TOGETHER" not in resultado
    assert "Wrocław" not in resultado
    assert "cookie" not in resultado
    assert "Privacy Notice" not in resultado


def test_5878750762_python_de_la_oferta_relacionada_desaparece():
    """'Python' solo aparecía en el título de la oferta relacionada (Zona B, §57.6.15-B)."""
    resultado = _strip_external_tail(_JOB_5878750762_REAL)
    assert "Python" not in resultado


# =============================================================================
# B) Caso real limpio: job 5878707555 (DE, Receptionist, Agile Robots SE)
# =============================================================================
# Sin ninguna cola externa. El Fix B NO debe tocar este texto en absoluto, y en
# particular no corrige (ni pretende corregir) el falso positivo semántico de la
# skill "Agile" — el nombre de la empresa está en el cuerpo real, no en una cola.

_JOB_5878707555_REAL = """\
Receptionist (m/f(d)
In this role, you will be responsible for the German- and English-speaking front desk. To ensure reliable front office coverage, you will work on-site Monday to Thursday from 8 a.m. to 5 p.m., and Friday from 8 a.m. to 4 p.m.
Your Responsibilities- Friendly and professional reception of customers, guests, and business partners in German and English
- Ensuring a professional and welcoming reception area
- Answering and forwarding phone calls, handling emails and postal mail
- Managing travel arrangements
- Organizing meetings, managing and booking meeting rooms, ensuring technical equipment and hospitality services, including catering coordination
- Acting as the direct contact person for service providers, especially cleaning staff
- Managing and ordering office supplies via SAP
- Professional experience in front office or reception
- Completed commercial/business training or a comparable qualification
- Very good German and English skills are essential in our international environment
- Availability to work on-site at our headquarters in Munich
- Independent, structured, and reliable way of working
- Enjoy working with people and strong communication skills
- A strong service orientation and hands-on mentality
What we offer
- A dynamic high-tech company, combined with financial soundness and world-class investors.
- Join an interdisciplinary, international team with 60+ different nationalities in a collaborative work environment.
- Lots of development opportunities as we continue to grow.
- Challenging tasks and impactful projects alongside experts that enable professional and personal growth.
- Corporate Benefits Program covering health, mobility, and learning for 100€ net per month.
- Modern office facilities with a rooftop terrace overlooking Munich, free drinks & fruits, and regular company events contribute to a good working environment
About us
Agile Robots SE is an international high-tech company based in Munich, Germany with a production site in Kaufbeuren and more than 3500 employees worldwide. Our mission is to bridge the gap between artificial intelligence and robotics by developing systems that combine state-of-the-art force-moment-sensing and world-leading image-processing technology. This unique combination of technologies allows us to provide user-friendly and affordable robotic solutions that enable intelligent precision assembly.
This is made possible by our employees, who bring out the best in each and every day with creativity and enthusiasm. Become part of this team and shape the future of robotics with us!
We are proud of our diversity and welcome your application regardless of gender and sexual identity, nationality, ethnicity, religion, age, or disability."""


def test_5878707555_agile_robots_texto_identico():
    """Sin cola externa: el texto debe salir EXACTAMENTE igual, carácter a carácter."""
    resultado = _strip_external_tail(_JOB_5878707555_REAL)
    assert resultado == _JOB_5878707555_REAL
    assert "Agile Robots SE" in resultado


# =============================================================================
# C) Un ejemplo real por país de las colas observadas (DE, FR, ES, NL) + caso IT
# =============================================================================

# DE — job 5878725906 (System Engineer Intune/MECM, Rhenus Logistics)
_JOB_DE_TAIL_REAL = """\
System Engineer (m/w/d) Intune / MECM
NEU
What You Can Expect:
- Du bist für den Betrieb und die Weiterentwicklung der Endpoint-Management-Plattform mit Fokus auf Microsoft Intune zuständig.
Ähnliche Jobs
Häufige Suchvorgänge
Mit dem Klick auf "Job-E-Mail bestellen" stimmst du unseren AGBs, unseren Datenschutzbestimmungen und der Nutzung von Cookies zu. Du kannst dich jederzeit von unseren E-Mails & Services abmelden."""


def test_cola_real_de_se_elimina():
    resultado = _strip_external_tail(_JOB_DE_TAIL_REAL)
    assert resultado.endswith("Microsoft Intune zuständig.")
    assert "Ähnliche Jobs" not in resultado
    assert "Häufige Suchvorgänge" not in resultado
    assert "Cookies" not in resultado


# FR — job 5878730112 (Responsable support technique, Skaelia). Tiene DOS
# marcadores de cola ("Stats pour cet emploi" y, más abajo, "Postes similaires");
# debe cortar en el PRIMERO.
_JOB_FR_TAIL_REAL = """\
Responsable support technique - H/F - Haïti (Port-au-Prince)
Skaelia
Une maîtrise parfaite du français est obligatoire (la connaissance du créole n'est pas exigée au démarrage).
Stats pour cet emploi
Comparaison de salaire:
Salaires
Le nombre d'emplois pour chaque niveau de salaire dans cette catégorie:
Postes similaires
Responsable Support Technique - Haïti Port-Au-Prince H/F
4300 - 5300 EUR MONTHLY
Skaelia
Paris, 75000
Recherches populaires
En créant une alerte email, vous acceptez nos Termes & et Conditions, Avis de Confidentialité, et l'utilisation de cookies."""


def test_cola_real_fr_se_elimina_en_el_primer_marcador():
    resultado = _strip_external_tail(_JOB_FR_TAIL_REAL)
    assert resultado.endswith("la connaissance du créole n'est pas exigée au démarrage).")
    assert "Stats pour cet emploi" not in resultado
    assert "Postes similaires" not in resultado
    assert "Recherches populaires" not in resultado
    assert "cookies" not in resultado


# ES — job 5878732792 (Operario/a mecanizado, Qualis)
_JOB_ES_TAIL_REAL = """\
OPERARIO/A DE MECANIZADO SECTOR AERONÁUTICO
Se ofrece incorporación a empresa consolidada en el sector aeroespacial, posición con contratación estable, vuen ambiente de trabajo y claras posibilidades de desarrollo profesional en un proyecto en crecimiento.
Estadísticas para este empleo
Comparación salarial
Salarios
Número de empleos en el rango salarial:"""


def test_cola_real_es_se_elimina():
    resultado = _strip_external_tail(_JOB_ES_TAIL_REAL)
    assert resultado.endswith("un proyecto en crecimiento.")
    assert "Estadísticas para este empleo" not in resultado
    assert "Comparación salarial" not in resultado


# NL — job 5878719834 (Software Architect, Air Apps)
_JOB_NL_TAIL_REAL = """\
Software Architect
Application Disclaimer
At Air Apps, we value transparency and integrity in our hiring process. Applicants must submit their own work without any AI-generated assistance. Any use of AI in application materials, assessments, or interviews will result in disqualification.
Statistieken voor deze baan
Salaris vergelijking:
Salarisverdeling
Bekijk de salarisrange voor alle:"""


def test_cola_real_nl_se_elimina():
    resultado = _strip_external_tail(_JOB_NL_TAIL_REAL)
    assert resultado.endswith("will result in disqualification.")
    assert "Statistieken voor deze baan" not in resultado
    assert "Salarisverdeling" not in resultado


def test_cola_real_it_se_elimina_variante_share_pegado():
    """
    Job real 5878715232 (Sales Account Manager, Hitachi Vantara). Trafilatura pega
    el botón "Share:" a la cabecera en la MISMA línea, sin separador:
    "Share:Statistiche per questo lavoro". Se añadió esa variante EXACTA como
    marcador adicional (sin endswith/substring/fuzzy — sigue siendo coincidencia
    de línea completa) porque procede de datos reales, no de una hipótesis.
    """
    texto = (
        "Sales Account Manager – Rail & IT\n"
        "We’re proud to say we’re an equal opportunity employer and welcome all "
        "applicants for employment without attention to race, colour, religion, "
        "sex, sexual orientation, gender identity, national origin, veteran, age, "
        "disability status or any other protected characteristic.\n"
        "Share:Statistiche per questo lavoro\n"
        "Stipendi\n"
        "Il numero di annunci per questa fascia salariale:"
    )
    resultado = _strip_external_tail(texto)

    # Contenido genuino anterior permanece
    assert resultado.startswith("Sales Account Manager – Rail & IT")
    assert "equal opportunity employer" in resultado
    assert resultado.endswith("or any other protected characteristic.")

    # Cola italiana completa desaparece
    assert "Share:Statistiche per questo lavoro" not in resultado
    assert "Stipendi" not in resultado
    assert "Il numero di annunci per questa fascia salariale" not in resultado


# =============================================================================
# D) Protección contra falsos cortes: substring dentro de una frase normal
# =============================================================================


def test_marcador_dentro_de_frase_normal_no_corta():
    """
    Frase sintética (no observada tal cual en el piloto) construida a propósito
    para blindar la regla "línea completa, no subcadena": el marcador ES exacto
    y misma capitalización, pero forma parte de una frase más larga en la misma
    línea, así que NO debe activar el corte.
    """
    texto = (
        "Introducción al puesto\n"
        "El manual interno explica el apartado de Estadísticas para este empleo "
        "dentro de la guía de onboarding de RRHH.\n"
        "Requisitos del puesto y responsabilidades habituales del equipo."
    )
    resultado = _strip_external_tail(texto)
    assert resultado == texto


def test_texto_sin_marcadores_no_se_modifica():
    """Sin ningún encabezado reconocido, el texto vuelve exactamente igual."""
    texto = "Título del puesto\nDescripción normal de la oferta sin ninguna cola externa."
    assert _strip_external_tail(texto) == texto


def test_texto_vacio_o_none():
    assert _strip_external_tail("") == ""
    assert _strip_external_tail(None) is None
