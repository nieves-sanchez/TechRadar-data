"""
skills_catalog.py — Catálogo de skills, roles y palabras clave remote para TechRadar.

Cada skill se define como una tupla:
    (nombre_canónico, categoría, [patrones_regex])

Los patrones son case-insensitive y usan word boundaries para evitar falsos positivos.
El nombre canónico es el que se almacena en la tabla skills de la BD.

Categorías de skill disponibles (según el schema):
    language | framework | cloud | database | tool | methodology

Este módulo solo contiene datos. La lógica de extracción vive en transform.py.
"""

# =============================================================================
# Skills
# =============================================================================

# Focalizado en los roles del proyecto: data engineering, data science,
# data analyst, backend, devops/cloud, ai_ml y frontend.
SKILLS: list[tuple[str, str, list[str]]] = [

    # -------------------------------------------------------------------------
    # Lenguajes de programación
    # -------------------------------------------------------------------------
    ("Python",      "language", [r"\bpython\b"]),
    ("SQL",         "language", [r"\bsql\b"]),
    ("JavaScript",  "language", [r"\bjavascript\b"]),
    ("TypeScript",  "language", [r"\btypescript\b"]),
    ("Java",        "language", [r"\bjava\b"]),
    ("Scala",       "language", [r"\bscala\b"]),
    ("Go",          "language", [r"\bgolang\b", r"\bgo\s+programming\b", r"\blanguage\s+go\b"]),
    # R: patrón conservador para evitar falsos positivos con letras sueltas
    ("R",           "language", [
        r"\br\s+programming\b",
        r"\bprogramming\s+in\s+r\b",
        r"\br\s+language\b",
        r"\brstudio\b",
        r"\btidyverse\b",
        r"\bggplot2\b",
        r"\bdplyr\b",
    ]),
    ("Bash",        "language", [r"\bbash\b", r"\bshell\s+scripting\b"]),
    ("Kotlin",      "language", [r"\bkotlin\b"]),
    ("Swift",       "language", [r"\bswift\b"]),
    ("Dart",        "language", [r"\bdart\b"]),
    ("Rust",        "language", [r"\brust\b"]),
    ("PHP",         "language", [r"\bphp\b"]),
    ("Ruby",        "language", [r"\bruby\b"]),
    ("C#",          "language", [r"\bc#\b", r"\bcsharp\b"]),
    ("C++",         "language", [r"\bc\+\+"]),
    (".NET",        "language", [r"\b\.net\b", r"\bdotnet\b", r"\basp\.net\b"]),
    ("Julia",       "language", [r"\bjulia\b"]),
    ("MATLAB",      "language", [r"\bmatlab\b"]),

    # -------------------------------------------------------------------------
    # Frameworks y librerías
    # -------------------------------------------------------------------------
    ("React",        "framework", [r"\breact\b", r"\breact\.?js\b"]),
    ("Angular",      "framework", [r"\bangular\b"]),
    # r"\bvue\b" omitido: "vue" es palabra común en francés — falsos positivos en ofertas FR/BE
    ("Vue",          "framework", [r"\bvue\.?js\b", r"\bvue\s+(?:js|framework|component|3|2)\b"]),
    ("Next.js",      "framework", [r"\bnext\.?js\b"]),
    ("Django",       "framework", [r"\bdjango\b"]),
    ("FastAPI",      "framework", [r"\bfastapi\b"]),
    ("Flask",        "framework", [r"\bflask\b"]),
    ("Spring",       "framework", [r"\bspring\s+boot\b", r"\bspring\b"]),
    # r"\bnode\b" cubre el alias bare "Node"/"node" que Ollama suele devolver;
    # word boundary excluye node_exporter, nodepool, nodeport (underscore/letra = no boundary)
    ("Node.js",      "framework", [r"\bnode\.?js\b", r"\bnode\b"]),
    ("Pandas",       "framework", [r"\bpandas\b"]),
    ("Polars",       "framework", [r"\bpolars\b"]),
    ("NumPy",        "framework", [r"\bnumpy\b"]),
    ("Scikit-learn", "framework", [r"\bscikit[-\s]?learn\b", r"\bsklearn\b"]),
    ("TensorFlow",   "framework", [r"\btensorflow\b"]),
    ("PyTorch",      "framework", [r"\bpytorch\b"]),
    ("Keras",        "framework", [r"\bkeras\b"]),
    ("Spark",        "framework", [r"\bapache\s+spark\b", r"\bpyspark\b", r"\bspark\b"]),
    ("Flink",        "framework", [r"\bapache\s+flink\b", r"\bflink\b"]),
    ("Hadoop",       "framework", [r"\bhadoop\b"]),
    ("LangChain",    "framework", [r"\blangchain\b"]),
    ("LlamaIndex",   "framework", [r"\bllamaindex\b", r"\bllama[-\s]?index\b"]),
    ("SQLAlchemy",   "framework", [r"\bsqlalchemy\b"]),
    ("Celery",       "framework", [r"\bcelery\b"]),
    ("Pydantic",     "framework", [r"\bpydantic\b"]),
    ("Flutter",      "framework", [r"\bflutter\b"]),

    # -------------------------------------------------------------------------
    # Cloud e infraestructura
    # -------------------------------------------------------------------------
    ("AWS",        "cloud", [r"\baws\b", r"\bamazon\s+web\s+services\b"]),
    ("Azure",      "cloud", [r"\bazure\b", r"\bmicrosoft\s+azure\b"]),
    ("GCP",        "cloud", [r"\bgcp\b", r"\bgoogle\s+cloud\b"]),
    ("Kubernetes", "cloud", [r"\bkubernetes\b", r"\bk8s\b"]),
    ("Docker",     "cloud", [r"\bdocker\b"]),
    ("Terraform",  "cloud", [r"\bterraform\b"]),
    ("Helm",       "cloud", [r"\bhelm\b"]),
    ("Ansible",    "cloud", [r"\bansible\b"]),

    # -------------------------------------------------------------------------
    # Bases de datos
    # -------------------------------------------------------------------------
    ("PostgreSQL",    "database", [r"\bpostgresql\b", r"\bpostgres\b"]),
    ("MySQL",         "database", [r"\bmysql\b"]),
    ("MongoDB",       "database", [r"\bmongodb\b", r"\bmongo\b"]),
    ("Redis",         "database", [r"\bredis\b"]),
    ("Elasticsearch", "database", [r"\belasticsearch\b", r"\bopensearch\b"]),
    ("Cassandra",     "database", [r"\bcassandra\b"]),
    ("BigQuery",      "database", [r"\bbigquery\b"]),
    ("Snowflake",     "database", [r"\bsnowflake\b"]),
    ("Databricks",    "database", [r"\bdatabricks\b"]),
    ("Redshift",      "database", [r"\bredshift\b"]),
    ("ClickHouse",    "database", [r"\bclickhouse\b"]),
    ("SQL Server",    "database", [r"\bsql\s+server\b", r"\bmssql\b"]),
    ("Oracle DB",     "database", [r"\boracle\s+db\b", r"\boracle\s+database\b"]),
    ("DynamoDB",      "database", [r"\bdynamodb\b"]),
    ("Neo4j",         "database", [r"\bneo4j\b"]),
    ("MariaDB",       "database", [r"\bmariadb\b"]),
    ("SQLite",        "database", [r"\bsqlite\b"]),

    # -------------------------------------------------------------------------
    # Herramientas
    # -------------------------------------------------------------------------
    ("dbt",              "tool", [r"\bdbt\b"]),
    ("Airflow",          "tool", [r"\bairflow\b", r"\bapache\s+airflow\b"]),
    ("Prefect",          "tool", [r"\bprefect\b"]),
    ("Dagster",          "tool", [r"\bdagster\b"]),
    ("MLflow",           "tool", [r"\bmlflow\b"]),
    ("Kafka",            "tool", [r"\bkafka\b", r"\bapache\s+kafka\b"]),
    ("Git",              "tool", [r"\bgit\b"]),
    # GitHub Actions se detecta antes que GitHub para evitar doble match
    ("GitHub Actions",   "tool", [r"\bgithub\s+actions\b"]),
    ("GitHub",           "tool", [r"\bgithub\b"]),
    ("GitLab",           "tool", [r"\bgitlab\b"]),
    ("Jenkins",          "tool", [r"\bjenkins\b"]),
    ("Power BI",         "tool", [r"\bpower\s*bi\b"]),
    ("Tableau",          "tool", [r"\btableau\b"]),
    ("Looker",           "tool", [r"\blooker\b"]),
    ("Grafana",          "tool", [r"\bgrafana\b"]),
    ("Prometheus",       "tool", [r"\bprometheus\b"]),
    ("Hugging Face",     "tool", [r"\bhugging\s*face\b"]),
    ("OpenAI",           "tool", [r"\bopenai\b"]),
    ("Qlik",             "tool", [r"\bqlik\b", r"\bqlikview\b", r"\bqlik\s+sense\b"]),
    ("Metabase",         "tool", [r"\bmetabase\b"]),
    ("Jira",             "tool", [r"\bjira\b"]),
    ("Confluence",       "tool", [r"\bconfluence\b"]),
    # SonarQube/SonarLint/SonarCloud son productos distintos — no usar r"\bsonar\b" genérico
    ("SonarQube",        "tool", [r"\bsonarqube\b"]),
    ("SonarLint",        "tool", [r"\bsonarlint\b"]),
    ("SonarCloud",       "tool", [r"\bsonarcloud\b"]),
    ("Vault",            "tool", [r"\bhashicorp\s+vault\b", r"\bvault\b"]),
    ("Great Expectations","tool", [r"\bgreat\s+expectations\b"]),
    ("Spark Streaming",  "tool", [r"\bspark\s+streaming\b", r"\bstructured\s+streaming\b"]),

    # -------------------------------------------------------------------------
    # Metodologías
    # -------------------------------------------------------------------------
    ("CI/CD",         "methodology", [r"\bci/cd\b", r"\bci-cd\b",
                                      r"\bcontinuous\s+integration\b",
                                      r"\bcontinuous\s+delivery\b",
                                      r"\bcontinuous\s+deployment\b"]),
    ("DevOps",        "methodology", [r"\bdevops\b"]),
    ("MLOps",         "methodology", [r"\bmlops\b"]),
    ("Agile",         "methodology", [r"\bagile\b"]),
    ("Scrum",         "methodology", [r"\bscrum\b"]),
    ("REST API",      "methodology", [r"\brest\s*api\b", r"\brestful\b"]),
    ("GraphQL",       "methodology", [r"\bgraphql\b"]),
    ("gRPC",          "methodology", [r"\bgrpc\b"]),
    ("Microservices", "methodology", [r"\bmicroservices?\b"]),
    ("TDD",           "methodology", [r"\btdd\b", r"\btest[-\s]driven\s+development\b"]),

    # -------------------------------------------------------------------------
    # Sistemas operativos y administración de sistemas
    # -------------------------------------------------------------------------
    ("Linux",          "tool", [r"\blinux\b", r"\bubuntu\b", r"\bdebian\b", r"\brhel\b", r"\bcentos\b", r"\bfedora\b"]),
    ("Windows Server", "tool", [r"\bwindows\s+server\b", r"\bwindows\s+active\s+directory\b"]),
    ("Active Directory","tool", [r"\bactive\s+directory\b", r"\bad\s+ds\b", r"\bldap\b"]),
    ("PowerShell",     "language", [r"\bpowershell\b"]),
    ("COBOL",          "language", [r"\bcobol\b"]),
    ("VBA",            "language", [r"\bvba\b", r"\bvisual\s+basic\s+for\s+applications\b"]),

    # -------------------------------------------------------------------------
    # Testing y QA
    # -------------------------------------------------------------------------
    ("Selenium",       "tool", [r"\bselenium\b"]),
    ("Playwright",     "tool", [r"\bplaywright\b"]),
    ("Cypress",        "tool", [r"\bcypress\b"]),
    ("pytest",         "tool", [r"\bpytest\b"]),
    ("JUnit",          "tool", [r"\bjunit\b"]),
    ("Jest",           "tool", [r"\bjest\b"]),
    ("Postman",        "tool", [r"\bpostman\b"]),

    # -------------------------------------------------------------------------
    # Build tools
    # -------------------------------------------------------------------------
    ("Maven",          "tool", [r"\bmaven\b"]),
    ("Gradle",         "tool", [r"\bgradle\b"]),

    # -------------------------------------------------------------------------
    # Frameworks backend adicionales
    # -------------------------------------------------------------------------
    ("NestJS",         "framework", [r"\bnestjs\b", r"\bnest\.?js\b"]),
    ("Express.js",     "framework", [r"\bexpress\.?js\b", r"\bexpressjs\b"]),
    ("Quarkus",        "framework", [r"\bquarkus\b"]),
    ("Micronaut",      "framework", [r"\bmicronaut\b"]),
    ("Ktor",           "framework", [r"\bktor\b"]),

    # -------------------------------------------------------------------------
    # ML / IA adicional
    # -------------------------------------------------------------------------
    ("XGBoost",        "framework", [r"\bxgboost\b"]),
    ("LightGBM",       "framework", [r"\blightgbm\b"]),
    ("spaCy",          "framework", [r"\bspacy\b"]),
    ("OpenCV",         "framework", [r"\bopencv\b"]),
    ("NLTK",           "framework", [r"\bnltk\b"]),

    # -------------------------------------------------------------------------
    # Messaging / Streaming
    # -------------------------------------------------------------------------
    ("RabbitMQ",       "tool", [r"\brabbitmq\b"]),
    ("ActiveMQ",       "tool", [r"\bactivemq\b"]),
    ("Pulsar",         "tool", [r"\bapache\s+pulsar\b", r"\bpulsar\b"]),

    # -------------------------------------------------------------------------
    # Infraestructura y DevOps adicional
    # -------------------------------------------------------------------------
    ("Nginx",          "tool", [r"\bnginx\b"]),
    ("Azure DevOps",   "tool", [r"\bazure\s+devops\b"]),
    ("Azure Data Factory", "tool", [r"\bazure\s+data\s+factory\b", r"\badf\b"]),
    ("OpenShift",      "cloud", [r"\bopenshift\b"]),
    ("ArgoCD",         "tool", [r"\bargcd\b", r"\bargo\s+cd\b"]),
    ("Bitbucket",      "tool", [r"\bbitbucket\b"]),
    ("CircleCI",       "tool", [r"\bcircleci\b"]),
    ("Travis CI",      "tool", [r"\btravis\s+ci\b"]),

    # -------------------------------------------------------------------------
    # Observabilidad / Monitorización
    # -------------------------------------------------------------------------
    ("Datadog",        "tool", [r"\bdatadog\b"]),
    ("Kibana",         "tool", [r"\bkibana\b"]),
    ("Splunk",         "tool", [r"\bsplunk\b"]),
    ("New Relic",      "tool", [r"\bnew\s+relic\b"]),
    ("Dynatrace",      "tool", [r"\bdynatrace\b"]),

    # -------------------------------------------------------------------------
    # API y especificaciones
    # -------------------------------------------------------------------------
    ("OpenAPI",        "methodology", [r"\bopenapi\b", r"\bswagger\b"]),

    # -------------------------------------------------------------------------
    # Plataformas empresariales
    # -------------------------------------------------------------------------
    ("SharePoint",     "tool", [r"\bsharepoint\b"]),
    ("ServiceNow",     "tool", [r"\bservicenow\b"]),
    ("Salesforce",     "tool", [r"\bsalesforce\b"]),
    ("Zendesk",        "tool", [r"\bzendesk\b"]),

    # -------------------------------------------------------------------------
    # Data engineering adicional
    # -------------------------------------------------------------------------
    ("Talend",         "tool", [r"\btalend\b"]),
    ("SSIS",           "tool", [r"\bssis\b", r"\bsql\s+server\s+integration\s+services\b"]),
    ("Informatica",    "tool", [r"\binformatica\b"]),
    ("Airbyte",        "tool", [r"\bairbyte\b"]),
    ("Delta Lake",     "tool", [r"\bdelta\s+lake\b"]),
    ("Debezium",       "tool", [r"\bdebezium\b"]),
    ("Apache Iceberg", "tool", [r"\biceberg\b", r"\bapache\s+iceberg\b"]),
    ("dbt Cloud",      "tool", [r"\bdbt\s+cloud\b"]),
    ("Fivetran",       "tool", [r"\bfivetran\b"]),
    ("Stitch",         "tool", [r"\bstitch\s+data\b"]),

    # -------------------------------------------------------------------------
    # Vector databases / AI infra
    # -------------------------------------------------------------------------
    ("Pinecone",       "database", [r"\bpinecone\b"]),
    ("Weaviate",       "database", [r"\bweaviate\b"]),
    ("Qdrant",         "database", [r"\bqdrant\b"]),
    ("Milvus",         "database", [r"\bmilvus\b"]),

    # -------------------------------------------------------------------------
    # Cloud adicional
    # -------------------------------------------------------------------------
    ("AWS Lambda",     "cloud", [r"\baws\s+lambda\b", r"\blambda\s+function\b"]),
    ("AWS S3",         "cloud", [r"\baws\s+s3\b", r"\bamazon\s+s3\b"]),
    ("Azure Synapse",  "cloud", [r"\bazure\s+synapse\b"]),
    ("Google BigQuery","database", [r"\bgoogle\s+bigquery\b"]),  # alias más específico
    ("Vertex AI",      "cloud", [r"\bvertex\s+ai\b"]),
    ("SageMaker",      "cloud", [r"\bsagemaker\b"]),
]


# =============================================================================
# Patrones de roles NO IT — devuelven None en lugar de 'other'
# =============================================================================

# Lista de substrings (en minúsculas) que identifican ofertas claramente no-IT.
# Se evalúan contra el título en minúsculas antes de intentar clasificar.
# CRITERIO: solo incluir patrones con riesgo de falso positivo MUY bajo.
# Si hay duda, dejar que caiga en 'other' antes que eliminar una oferta legítima.
NON_IT_PATTERNS: list[str] = [

    # DE: formaciones duales y prácticas (Ausbildung, Werkstudent, Praktikum)
    "ausbildung zum", "ausbildung zur", "ausbildung als", "ausbildungsstelle",
    "duales studium bwl", "duales studium handel", "duales studium wirtschaft",
    "duales studium marketing", "duales studium logistik",
    "duale ausbildung",
    "werkstudent",
    "praktikant",
    "praktikum",

    # FR: alternancia y prácticas
    "en alternance",
    "contrat d'apprentissage",
    "contrat en alternance",
    "apprenti",

    # FR: sanitario y social
    "infirmier", "infirmière", "aide-soignant", "aide soignant",
    "aide à domicile", "auxiliaire de vie",
    "éducateur spécialisé", "educateur specialise",
    "assistant social", "assistante sociale",
    "médecin", "chirurgien", "pharmacien", "kinésithérapeute",
    "sage-femme", "puéricultrice",

    # FR: logística y producción
    "agent de fabrication", "opérateur de production", "opérateur production",
    "préparateur de commandes", "preparateur de commandes",
    "magasinier", "cariste", "manutentionnaire",
    "conducteur de ligne", "conducteur ligne",
    "technicien fibre optique", "chef de chantier fibre",
    "monteur fibre", "technicien fibre",

    # FR: retail y hostelería
    "merchandiseur", "animateur commercial",
    "hôtesse d'accueil", "hotesse d'accueil",
    "réceptionniste", "agent d'accueil",
    "cuisinier", "chef cuisinier",

    # FR: construcción y mantenimiento
    "soudeur", "électricien", "plombier",
    "menuisier", "charpentier",
    "technicien de maintenance", "agent de maintenance",

    # DE: oficios manuales e industria (claramente no-IT)
    "mechatroniker",
    "metallbauer",
    "cnc-dreher",
    "cnc dreher",
    "zerspanungsmechaniker",
    "industriemechaniker",
    "elektrotechniker",
    "elektroniker",
    "staplerfahrer",
    "fahrlehrer",
    "lagermitarbeiter",
    "fachlagerist",
    "mechaniker",
    "bilanzbuchhalter",
    "buchhalter",
    "finanzbuchhalter",
    "lagerlogistik",

    # FR: oficios no-IT y perfiles comerciales evidentes
    "ingénieur d'affaires",
    "ingénieur commercial",
    "technico-commercial",
    "chargé d'affaires",
    "représentant commercial",
    "attaché commercial",
    "conseiller commercial",
    "conseiller financier",
    "conseiller bancaire",
    "comptable",
    "contrôleur de gestion",
    "controleur de gestion",
    "automaticien",
    "ingénieur automatisme",
    "vendeur",
    "hôte de caisse",

    # DE: retail y ventas
    "verkäufer",
    "kassierer",
    "kaufmann im einzelhandel",
    "vertriebsmitarbeiter",
    "außendienstmitarbeiter",
    "aussendienstmitarbeiter",
    "versicherungsvertreter",
    "kundenberater",

    # NL: obras civiles y retail
    "werkvoorbereider",
    "verkoopmedewerker",
    "klantenadviseur",
    "spoorwerken",

    # PL: roles no-IT claramente identificables
    "partner biznesowy",
    "franczyzobiorca",
    "agent ubezpieczeniowy",
    "doradca ubezpieczeniowy",
    "doradca leasingowy",
    "doradca energetyczny",
    "przedstawiciel handlowy",
    "przedstawiciel medyczny",
    "operator cysterny",
    "operator maszyn",
    "monter maszyn",
    "pracownik magazynu",
    "pomoc kuchenna",
    "diagnosta laboratoryjny",
    "pielęgniarka", "pielęgniarz",
    "opiekun klienta",  # sales, not IT
    "doradca klienta",  # sales, not IT
    "handlowiec",
    "kierowca",
    "spawacz",
    "elektryk",
    "mechanik samochodowy",

    # ES: roles no-IT
    "clases particulares",
    "profesor particular",
    "agente de seguros",
]


# =============================================================================
# Clasificación de roles
# =============================================================================

# Palabras clave para asignar role_category desde el título de la oferta.
# El orden importa: se evalúa de arriba a abajo y se devuelve la primera
# coincidencia. Los roles más específicos van primero para evitar que un título
# como "ML Engineer" caiga en "backend" antes de llegar a "ai_ml".
ROLE_KEYWORDS: dict[str, list[str]] = {

    # -------------------------------------------------------------------------
    # management — primero porque contiene palabras (manager, lead) que también
    # aparecen en otros roles. Debe evaluarse antes que ai_ml o backend.
    # -------------------------------------------------------------------------
    "management": [
        # EN — C-level y VP
        "cto", "chief technology officer", "chief data officer",
        "chief digital officer", "cdo",
        "vp of engineering", "vp of technology",
        # EN — Head of
        "head of engineering", "head of technology", "head of it",
        "head of data", "head of product", "head of software",
        "head of digital", "head of cloud",
        # EN — Director
        "it director", "technical director", "digital director",
        "technology director", "product director",
        # EN — Manager
        "engineering manager", "it manager", "technical manager",
        "it project manager", "technical project manager", "project manager",
        "program manager", "programme manager", "delivery manager",
        "portfolio manager", "it portfolio manager",
        "service manager", "release manager",
        "product manager", "digital manager", "vendor manager",
        # EN — Lead y Scrum
        "tech lead", "technical lead", "team lead", "software team lead",
        "project lead", "program lead",
        "scrum master", "agile coach",
        # EN — Product Owner
        "product owner",
        # FR — Direcciones y responsables
        "directeur technique", "directeur web", "directeur digital",
        "directeur it", "directeur numérique", "directeur de projet",
        "directeur d'agence web",
        "responsable technique", "responsable it", "responsable projet",
        "responsable data", "responsable cloud", "responsable système",
        "responsable informatique", "responsable numérique",
        "responsable digital", "responsable applicati",
        "responsable logiciel", "responsable développement",
        "chef de projet it", "chef de projet informatique",
        "chef de projet digital", "chef de projet data", "chef de projet cloud",
        "chargé de projet it", "chargé de projet informatique",
        "chargé de projet digital",
        "manager it",
        # IT Governance
        "it governance", "it compliance",
        # DE — Leiter e IT-Management
        "it leiter", "technischer leiter", "entwicklungsleiter",
        "portfoliomanager",
        "projektleiter it", "projektleiter", "projektmanager",
        "teamleiter it", "teamleiter software", "teamleiter digital",
        "teamleiter data", "teamleiter cloud", "teamleiter entwicklung",
        "leiter it", "leiter software", "leiter digital",
        "leiter data", "leiter cloud", "leiter entwicklung", "leiter informatik",
        # ES
        "director de tecnología", "responsable de tecnología",
        "jefe de proyecto it", "gerente de tecnología",
        "responsable de proyecto", "jefe de proyecto",
        "gestor de proyecto",
        # IT
        "responsabile tecnico", "direttore tecnico",
        "responsabile di progetto", "capo progetto",
        "project manager it",
        # PL
        "kierownik it", "menedżer techniczny", "lider techniczny",
        "kierownik projektu",
        # NL
        "it manager", "technisch manager", "it directeur",
        "projectmanager", "projectleider",
    ],

    # -------------------------------------------------------------------------
    # ai_ml — antes que data_science y backend para capturar "ML Engineer"
    # -------------------------------------------------------------------------
    "ai_ml": [
        # EN — roles explícitos
        "ai engineer", "ai specialist", "ai lead", "ai expert",
        "ai architect", "ai consultant", "ai developer",
        "ai researcher", "ai scientist",
        "ai solutions", "ai advisor", "ai senior", "ai product lead",
        "artificial intelligence",
        "machine learning engineer", "ml engineer",
        "deep learning", "deep learning engineer",
        "nlp engineer", "computer vision engineer", "computer vision",
        "llm engineer", "llm specialist", "llm developer",
        "generative ai", "gen ai", "prompt engineer",
        "reinforcement learning",
        "rag engineer",
        # Abreviaturas / términos cross-language
        "machine learning",  # captura "Senior ML ...", "Lead ML ..."
        "nlp", "llm", "rag",
        # DE — KI (Künstliche Intelligenz)
        "ki ingenieur", "ki entwickler", "ki spezialist",
        "ki experte", "ki berater",
        "machine learning ingenieur",
        "künstliche intelligenz",
        # FR — IA (Intelligence Artificielle)
        "ingénieur ia", "ingénieur machine learning",
        "intelligence artificielle",
        "spécialiste ia", "expert ia",
        # ES
        "ingeniero ia", "inteligencia artificial",
        "especialista ia", "experto ia",
        # IT
        "ingegnere ia", "intelligenza artificiale",
        # PL
        "inżynier ml", "sztuczna inteligencja",
    ],

    # -------------------------------------------------------------------------
    # data_science
    # -------------------------------------------------------------------------
    "data_science": [
        # EN
        "data scientist", "research scientist", "applied scientist",
        # FR
        "scientifique des données", "data science",
        # DE
        "datenwissenschaftler",
        # ES
        "científico de datos",
        # IT
        "scienziato dei dati",
        # PL
        "naukowiec danych",
    ],

    # -------------------------------------------------------------------------
    # data_engineering
    # -------------------------------------------------------------------------
    "data_engineering": [
        # EN — roles principales
        "data engineer", "analytics engineer", "data platform engineer",
        "data infrastructure", "etl developer", "etl engineer",
        "data pipeline", "dataops engineer",
        "data architect", "big data engineer",
        "data warehouse", "data warehousing", "data warehouse consultant",
        "lakehouse", "data platform", "data mesh",
        "data migration", "data modeler", "data modeller",
        "data integration", "data transformation",
        "data reliability engineer", "data quality engineer",
        "data governance engineer", "data infrastructure engineer",
        "data operations engineer", "data lead",
        "dataops",
        # FR
        "ingénieur données", "ingénieur data",
        "ingénieur bi", "ingénieur reporting", "ingénieur etl",
        "ingénieur big data",
        "développeur etl",
        "architecte données", "architecte data",
        "architecte data",
        # DE
        "dateningenieur", "daten ingenieur",
        "datenarchitekt", "big data ingenieur",
        # ES
        "ingeniero de datos", "ingeniero datos",
        "arquitecto de datos", "ingeniero big data",
        # IT
        "ingegnere dati", "architetto dati",
        # PL
        "inżynier danych", "architekt danych",
        # NL
        "data ingenieur",
    ],

    # -------------------------------------------------------------------------
    # data_analyst
    # -------------------------------------------------------------------------
    "data_analyst": [
        # EN — roles directos
        "data analyst", "business analyst", "bi analyst",
        "reporting analyst", "business intelligence analyst",
        "bi specialist", "bi developer", "bi engineer", "bi expert",
        "reporting specialist", "data specialist",
        "intelligence analyst", "data steward",
        "data quality analyst", "data governance analyst",
        "data product", "data consultant", "data manager",
        "data visualization", "data visualisation",
        "data coordinator", "data insights", "data reporting",
        "data officer", "data expert",
        "functional analyst", "functional consultant",
        "system analyst", "systems analyst",
        # FR
        "analyste données", "analyste de données",
        "analyste bi", "analyste data",
        "analyste fonctionnel", "analyste systèmes",
        "spécialiste bi", "développeur bi",
        "chargé d'analyse",
        # DE
        "datenanalyst", "business analyst",
        "systemanalytiker", "business analytiker",
        "bi spezialist", "bi entwickler",
        # ES
        "analista de datos", "analista bi",
        "analista funcional", "analista de sistemas",
        "especialista bi", "desarrollador bi",
        # IT
        "analista dati", "analista di dati",
        "analista funzionale", "analista di sistema",
        "analista applicativo", "analista programmatore",
        # PL
        "analityk danych", "analityk biznesowy",
        "analityk",  # como primera palabra cubre muchos casos PL
        "analiza danych",
        "specjalista ds. analiz", "specjalista ds analiz",
        "koordynator ds. analiz",
        "it analyst",
        # NL
        "data analist", "business analist",
        "functioneel analist", "systeem analist",
    ],

    # -------------------------------------------------------------------------
    # mobile
    # -------------------------------------------------------------------------
    "mobile": [
        # EN
        "mobile developer", "mobile engineer",
        "ios developer", "ios engineer",
        "android developer", "android engineer",
        "flutter developer", "flutter engineer",
        "react native developer", "react native engineer",
        "swift developer", "kotlin developer",
        # Cross-language (tecnologías que identifican el rol)
        "android", " ios ",  # espacios para evitar falsos positivos
        "flutter", "react native",
        # FR
        "développeur mobile", "développeur android", "développeur ios",
        # DE
        "mobile entwickler", "app entwickler",
        # ES
        "desarrollador móvil", "desarrollador android",
        # IT
        "sviluppatore mobile", "sviluppatore android",
        # PL
        "programista android", "programista mobile",
        # NL
        "mobile ontwikkelaar",
    ],

    # -------------------------------------------------------------------------
    # security
    # -------------------------------------------------------------------------
    "security": [
        # EN — roles completos (engineer, analyst, specialist, consultant, architect)
        "security engineer", "cybersecurity", "cyber security",
        "security analyst", "security specialist",
        "security consultant", "security architect",
        "security manager", "security officer", "security expert",
        "security lead", "security operations", "security administrator",
        "penetration test", "pentest", "infosec",
        "information security",
        "soc analyst", "soc engineer",
        "cloud security", "application security",
        "cyber analyst", "cyber specialist",
        "secops",
        "vulnerability", "threat",  # Threat Analyst, Vulnerability Engineer
        # Gestión de identidad y acceso
        "sailpoint", "iam consultant", "iam engineer", "iam specialist",
        "identity access management", "identity governance",
        "privileged access", "pam consultant",
        "grc consultant",
        "siem engineer",
        "forgerock", "cyberark", "okta",
        # Data protection / privacidad
        "data privacy", "data protection officer", "dpo",
        "privacy officer", "data security",
        "data protection specialist", "data protection manager",
        "data protection engineer",
        # FR
        "ingénieur sécurité", "ingénieur cybersécurité",
        "cybersécurité", "cyber sécurité",
        "sécurité informatique", "sécurité des systèmes",
        # DE
        "cybersicherheit", "it sicherheit",
        "informationssicherheit", "cyber sicherheit",
        "ingenieur sicherheit",
        # ES
        "seguridad informática", "ciberseguridad",
        "analista de seguridad", "consultor de seguridad",
        "especialista en seguridad",
        # IT
        "sicurezza informatica", "cybersicurezza",
        # PL
        "bezpieczeństwo it", "cyberbezpieczeństwo",
        "inżynier bezpieczeństwa",
        "bezpieczeństwa informacji",
        "ds. bezpieczeństwa",
    ],

    # -------------------------------------------------------------------------
    # qa_testing
    # -------------------------------------------------------------------------
    "qa_testing": [
        # EN
        "qa engineer", "quality assurance engineer", "test engineer",
        "software tester", "sdet", "qa automation engineer",
        "test automation engineer", "quality engineer", "qa analyst",
        "testing engineer", "automation tester", "manual tester",
        "performance tester", "qa lead",
        "test manager", "testing manager", "qa manager", "quality manager",
        "qa manual", "manual qa",
        # FR
        "ingénieur test", "ingénieur qualité", "testeur logiciel",
        "ingénieur qa", "responsable qualité logicielle",
        # DE
        "testingenieur", "qualitätsingenieur", "softwaretester",
        "qa ingenieur", "testautomatisierung",
        # ES
        "ingeniero de pruebas", "analista de calidad",
        "tester de software", "ingeniero qa",
        # IT
        "ingegnere di test", "tester software", "ingegnere della qualità",
        # PL
        "inżynier testów", "tester oprogramowania", "inżynier qa",
        # NL
        "testingenieur", "kwaliteitsingenieur", "software tester",
    ],

    # -------------------------------------------------------------------------
    # sysadmin
    # -------------------------------------------------------------------------
    "sysadmin": [
        # EN
        "system administrator", "sysadmin", "systems administrator",
        "network engineer", "network administrator", "it administrator",
        "systems engineer", "it support engineer", "linux administrator",
        "windows administrator", "infrastructure administrator",
        "helpdesk engineer", "it operations engineer",
        "it support specialist", "it support agent",
        "it operations specialist", "it helpdesk",
        "helpdesk specialist", "service desk", "servicedesk",
        "help desk", "helpdesk",
        "it operations", "application support", "app support",
        "noc engineer", "noc analyst", "network operations",
        "it consultant",
        "application manager", "application administrator",
        "dba",  # Database Administrator
        "database administrator",
        "it-system engineer", "it system engineer",
        "data center technician", "datacenter technician",
        "it specialist",
        # Automatización industrial (relevante en contexto IT en algunos países)
        "plc programmer", "plc engineer", "plc programmeur",  # EN + NL
        "scada engineer", "scada developer",
        # Telecomunicaciones técnicas
        "network operations center",
        # FR
        "administrateur système", "administrateur réseau",
        "ingénieur systèmes", "administrateur it",
        "technicien informatique", "technicien it",
        "technicien systèmes", "technicien réseau",
        "technicien support", "support informatique",
        "ingénieur réseaux", "ingénieur réseau",
        "ingénieur télécoms", "ingénieur télécom",
        "ingénieur support", "ingénieur exploitation",
        # DE
        "systemadministrator", "netzwerkingenieur", "it administrator",
        "systemingenieur", "netzwerkadministrator",
        "it spezialist", "it fachmann", "it techniker",
        "netzwerk techniker", "fachinformatiker",
        "it-administrator", "it-specialist", "it-fachmann",
        "ingenieur netzwerk", "ingenieur systeme",
        # ES
        "administrador de sistemas", "administrador de red",
        "técnico de sistemas", "administrador it",
        "técnico informático", "técnico de redes", "soporte it",
        "técnico it",
        # IT
        "amministratore di sistema", "ingegnere di rete",
        "amministratore di rete", "sistemista",
        "tecnico informatico", "tecnico it", "tecnico sistemista",
        "supporto it", "tecnici informatici",
        "ingegnere sistemi", "ingegnere reti",
        # PL
        "administrator systemu", "administrator sieci",
        "inżynier sieci", "inżynier systemów",
        "specjalista it", "administrator it",
        "inżynier infrastruktury", "inżynier chmury",
        # NL
        "systeembeheerder", "netwerkingenieur", "it beheerder",
        "applicatiebeheerder", "netwerktechnicus",
        "it specialist", "ict specialist",
        # Microsoft 365 / O365
        "microsoft 365 admin", "microsoft 365 consultant",
        "m365 consultant", "o365 admin",
    ],

    # -------------------------------------------------------------------------
    # erp_sap
    # -------------------------------------------------------------------------
    "erp_sap": [
        # SAP — el más común en EU, especialmente DE y PL
        "sap consultant", "sap developer", "sap abap", "sap basis",
        "sap s/4hana", "sap hana", "sap fiori", "sap mm", "sap sd",
        "sap fi", "sap co", "sap analyst", "sap architect", "sap engineer",
        "sap fico", "sap ewm", "sap wm", "sap pp", "sap pm",
        "sap successfactors", "sap ariba", "sap cloud alm", "sap alm",
        "sap bw", "sap bi", "sap btp", "sap commerce",
        "sap integration", "sap crm",
        "sap expert", "sap technical", "sap functional",
        "sap ",  # "SAP [cualquier módulo]" como inicio de título
        # ERP genérico
        "erp consultant", "erp developer", "erp analyst",
        "erp engineer", "erp specialist", "erp support",
        # CRM / Soporte
        "zendesk", "zendesk administrator",
        "crm specialist", "crm spezialist", "crm consultant",
        # Otras plataformas ERP/CRM comunes en EU
        "salesforce developer", "salesforce consultant", "salesforce engineer",
        "dynamics developer", "dynamics consultant", "dynamics 365", "d365",
        "oracle consultant", "oracle developer", "oracle retail",
        "navision developer", "netsuite developer",
        "veeva vault", "veeva",
        "infor erp", "erp infor", "infor m3", "infor ln",
        "workday consultant", "workday",
        "servicenow",
        # Power Platform / RPA (automatización empresarial)
        "power platform", "power apps", "power automate",
        "rpa developer", "rpa engineer", "rpa automation",
        "blue prism", "uipath", "automation anywhere",
        "business integration",
        # FR
        "consultant sap", "développeur sap", "consultant erp",
        # DE
        "sap berater", "sap entwickler", "erp berater",
        # ES
        "consultor sap", "desarrollador sap", "consultor erp",
        # IT
        "consulente sap", "sviluppatore sap", "consulente erp",
        "consulente applicativo", "consulente crm",
        # PL
        "konsultant sap", "programista sap", "konsultant erp",
        "konsultant it",
        # NL
        "sap consultant", "erp consultant",
    ],

    # -------------------------------------------------------------------------
    # devops
    # -------------------------------------------------------------------------
    "devops": [
        # EN
        "devops engineer", "platform engineer", "site reliability engineer",
        "sre", "infrastructure engineer", "release engineer",
        "build engineer", "ci engineer", "pipeline engineer",
        "build and release",
        "mlops engineer", "mlops",
        "infrastructure specialist",
        # Cross-language (devops se usa igual en todos los idiomas)
        "devops",
        # Herramientas que identifican el rol
        "atlassian",
        "jira administrator", "confluence administrator",
        "platform tooling",
        # FR
        "ingénieur devops", "ingénieur plateforme",
        "ingénieur infrastructure",
        # DE
        "devops ingenieur", "infrastruktur ingenieur",
        "plattform ingenieur", "ingenieur devops",
        # ES
        "ingeniero devops", "ingeniero infraestructura",
        # IT
        "ingegnere devops", "ingegnere infrastrutture",
        # PL
        "inżynier devops", "inżynier infrastruktury",
        "inżynier platformy",
    ],

    # -------------------------------------------------------------------------
    # cloud
    # -------------------------------------------------------------------------
    "cloud": [
        # EN
        "cloud engineer", "cloud architect", "solutions architect",
        "solution architect",
        "software architect", "enterprise architect",
        "infrastructure architect",
        "aws engineer", "azure engineer", "gcp engineer",
        "aws specialist", "azure specialist", "azure architect",
        "gcp specialist", "cloud specialist", "cloud consultant",
        "cloud expert", "cloud solutions", "cloud migration",
        "cloud operations", "cloud advisor",
        # FR
        "architecte cloud", "architecte solutions", "architecte logiciel",
        "architecte d'entreprise", "architecte infrastructure",
        "architecte si", "architecte système d'information",
        "architecte applicatif", "architecte fonctionnel",
        "architecte technique", "architecte réseau",
        "ingénieur cloud", "ingénieur aws", "architecte azure", "architecte aws",
        # DE
        "cloud architekt", "cloud ingenieur",
        "lösungsarchitekt", "softwarearchitekt",
        "it architekt", "enterprise architekt",
        "cloud berater",
        # ES
        "arquitecto cloud", "arquitecto de soluciones",
        "arquitecto de sistemas", "arquitecto software",
        "ingeniero cloud",
        # IT
        "architetto cloud", "ingegnere cloud",
        "architetto software", "architetto di sistema",
        "solution architect",
        # PL
        "architekt cloud", "inżynier cloud",
        "architekt rozwiązań", "architekt oprogramowania",
        "architekt systemów",
        # NL
        "cloud architect", "cloud ingenieur",
        "oplossingsarchitect", "software architect",
    ],

    # -------------------------------------------------------------------------
    # fullstack
    # -------------------------------------------------------------------------
    "fullstack": [
        "fullstack", "full-stack", "full stack",
        "développeur fullstack",
        "fullstack entwickler",
        "desarrollador fullstack",
        "sviluppatore fullstack",
        "programista fullstack",
    ],

    # -------------------------------------------------------------------------
    # frontend
    # -------------------------------------------------------------------------
    "frontend": [
        # EN
        "frontend", "front-end", "front end",
        "ui developer", "ui engineer",
        "react developer", "angular developer", "vue developer",
        # UX/UI
        "ux/ui", "ui/ux", "ux designer", "ui designer",
        "ux researcher", "product designer", "interaction designer",
        # FR
        "développeur front", "intégrateur web",
        "designer ux", "designer ui",
        # DE
        "frontend entwickler", "webentwickler",
        # ES
        "desarrollador front", "maquetador web",
        # IT
        "sviluppatore front", "sviluppatore web frontend",
        # PL
        "programista frontend", "developer frontend",
        # NL
        "frontend ontwikkelaar",
    ],

    # -------------------------------------------------------------------------
    # backend — más general, va último
    # -------------------------------------------------------------------------
    "backend": [
        # EN — patrones directos
        "backend", "back-end", "back end",
        "api developer", "api engineer",
        "software engineer", "software developer",
        # Lenguajes / tecnologías → backend por defecto
        "java developer", "java engineer",
        "python developer", "python engineer",
        "node developer", "node.js developer", "node.js engineer",
        "php developer", "php engineer",
        ".net developer", ".net engineer",
        "net developer", "net engineer",  # sin el punto
        "asp.net", "asp net",
        "c# developer", "c# engineer",
        "ruby developer", "ruby engineer",
        "scala developer", "scala engineer",
        "golang developer", "golang engineer",
        "go developer", "go engineer",
        "kotlin developer", "kotlin engineer",
        "rust developer", "rust engineer",
        "c++ developer", "c++ engineer",
        "spring engineer",
        "web developer", "web engineer",
        "backend developer", "backend engineer",
        "application engineer",
        # Lenguajes legacy relevantes en EU
        "delphi developer", "delphi programmer",
        "cobol developer", "cobol programmer",
        "embedded developer", "embedded software", "embedded engineer",
        "firmware developer", "firmware engineer",
        # C++/Qt — común en embedded/industria
        "qt developer", "qt engineer", "qt programmer",
        "c++ / qt", "qt/c++",
        # Patrones "lead [tech]"
        "lead developer", "lead engineer", "lead software",
        # FR
        "développeur", "ingénieur logiciel",
        "développeur backend", "développeur web",
        "développeur java", "développeur python",
        "ingénieur java", "ingénieur python", "ingénieur php",
        "ingénieur node", "ingénieur .net", "ingénieur scala",
        "ingénieur golang", "ingénieur backend",
        "ingénieur applicatif", "ingénieur développement", "ingénieur web",
        "ingénieur logiciel",
        # DE
        "entwickler", "softwareentwickler",
        "programmierer", "software entwickler",
        "backend entwickler", "java entwickler",
        "python entwickler",
        "ingenieur software", "ingenieur java", "ingenieur python",
        "ingenieur backend",
        # ES
        "desarrollador", "ingeniero de software",
        "programador", "desarrollador backend",
        "desarrollador web",
        # IT
        "sviluppatore", "ingegnere software",
        "programmatore", "sviluppatore backend",
        "sviluppatore web",
        "ingegnere software", "ingegnere informatico",
        "ingegnere backend", "ingegnere java", "ingegnere python",
        "ingegnere web", "ingegnere .net", "ingegnere php",
        # PL
        "programista", "inżynier oprogramowania",
        "developer backend", "programista java",
        "programista python",
        # NL
        "ontwikkelaar", "software engineer",
        "backend ontwikkelaar", "webontwikkelaar",
        "software ontwikkelaar", "java ontwikkelaar",
    ],
}


# =============================================================================
# Palabras clave para clasificación por DESCRIPCIÓN (fallback)
# =============================================================================

# Se usan cuando el título no permite clasificar la oferta (cae en 'other').
# Los patrones son más conservadores (frases compuestas) para evitar
# falsos positivos, ya que el texto de descripción es mucho más extenso
# y contiene más contexto variado que el título.
ROLE_DESC_KEYWORDS: dict[str, list[str]] = {
    "management": [
        "engineering manager", "it manager", "tech lead",
        "technical lead", "head of engineering", "head of technology",
        "vp of engineering", "chief technology officer",
    ],
    "ai_ml": [
        "machine learning engineer", "ml engineer", "ai engineer",
        "llm engineer", "deep learning engineer", "nlp engineer",
        "computer vision engineer", "generative ai engineer",
    ],
    "data_science": [
        "data scientist", "research scientist", "applied scientist",
        "científico de datos", "scientifique des données",
        "datenwissenschaftler",
    ],
    "data_engineering": [
        "data engineer", "analytics engineer", "etl developer",
        "etl engineer", "data pipeline engineer", "dataops engineer",
        "data architect", "ingénieur data", "ingénieur données",
        "dateningenieur", "ingeniero de datos", "inżynier danych",
        "data ingenieur",
    ],
    "data_analyst": [
        "data analyst", "business analyst", "business intelligence analyst",
        "reporting analyst", "bi analyst", "analyste données",
        "analyste data", "datenanalyst", "analista de datos",
        "analista dati", "analityk danych", "data analist",
    ],
    "mobile": [
        "mobile developer", "mobile engineer", "ios developer",
        "android developer", "flutter developer", "react native developer",
        "développeur mobile", "mobile entwickler", "sviluppatore mobile",
    ],
    "security": [
        "security engineer", "cybersecurity engineer",
        "penetration tester", "security analyst",
        "information security engineer", "soc analyst",
        "ingénieur sécurité", "ingénieur cybersécurité",
    ],
    "qa_testing": [
        "qa engineer", "quality assurance engineer", "test engineer",
        "test automation engineer", "qa automation engineer",
        "software tester", "automation tester",
        "ingénieur test", "testingenieur", "ingeniero de pruebas",
        "tester oprogramowania",
    ],
    "sysadmin": [
        "system administrator", "systems administrator",
        "network engineer", "network administrator",
        "it administrator", "linux administrator",
        "administrateur système", "systemadministrator",
        "administrador de sistemas", "administrator systemu",
    ],
    "erp_sap": [
        "sap consultant", "sap developer", "sap abap",
        "erp consultant", "erp developer",
        "salesforce developer", "dynamics 365", "servicenow developer",
        "consultant sap", "sap berater", "consultor sap",
        "konsultant sap",
    ],
    "devops": [
        "devops engineer", "platform engineer",
        "site reliability engineer", "infrastructure engineer",
        "ingénieur devops", "devops ingenieur", "ingeniero devops",
    ],
    "cloud": [
        "cloud engineer", "cloud architect", "solutions architect",
        "aws engineer", "azure engineer", "gcp engineer",
        "architecte cloud", "cloud architekt", "arquitecto cloud",
    ],
    "fullstack": [
        "fullstack developer", "full-stack developer",
        "full stack developer", "fullstack engineer",
        "full-stack engineer",
    ],
    "frontend": [
        "frontend developer", "frontend engineer", "front-end developer",
        "ui developer", "react developer", "angular developer",
        "développeur frontend", "frontend entwickler",
        "desarrollador frontend", "sviluppatore frontend",
    ],
    "backend": [
        "backend developer", "backend engineer", "back-end developer",
        "software engineer", "software developer", "java developer",
        "python developer", "api developer", "api engineer",
        "développeur backend", "développeur logiciel",
        "ingénieur logiciel", "softwareentwickler",
        "desarrollador backend", "sviluppatore backend",
        "programista backend", "backend ontwikkelaar",
    ],
}


# =============================================================================
# Detección de trabajo remoto
# =============================================================================

REMOTE_POSITIVE: list[str] = [
    r"\bremoto\b",
    r"\bremote\b",
    r"\bteletrabajo\b",
    r"\bwork\s+from\s+home\b",
    r"\bwfh\b",
    r"\bfully\s+remote\b",
    r"\b100%\s*remote\b",
    r"\bdistributed\s+team\b",
    r"\btelearbeit\b",
    r"\bhomeworking\b",
    r"\btélétravail\b",
    r"\bhomeoffice\b",
    r"\bhome\s+office\b",
]

REMOTE_NEGATIVE: list[str] = [
    r"\bon[-\s]?site\b",
    r"\bin[-\s]?office\b",
    r"\bpresencial\b",
    r"\bin\s+person\b",
    r"\bon\s+location\b",
    r"\bvor\s*ort\b",
    r"\bsur\s+site\b",
    r"sur\s+place",
]
