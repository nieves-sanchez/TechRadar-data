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
    ("Go",          "language", [r"\bgolang\b"]),
    ("R",           "language", [r"\bR\s+programming\b", r"\bprogramming\s+in\s+R\b", r"\blanguage\s+R\b"]),
    ("Bash",        "language", [r"\bbash\b", r"\bshell\s+scripting\b"]),
    ("Kotlin",      "language", [r"\bkotlin\b"]),
    ("Rust",        "language", [r"\brust\b"]),
    ("PHP",         "language", [r"\bphp\b"]),
    ("Ruby",        "language", [r"\bruby\b"]),
    ("C#",          "language", [r"\bc#\b", r"\bcsharp\b"]),
    ("C++",         "language", [r"\bc\+\+"]),

    # -------------------------------------------------------------------------
    # Frameworks y librerías
    # -------------------------------------------------------------------------
    ("React",        "framework", [r"\breact\b", r"\breact\.?js\b"]),
    ("Angular",      "framework", [r"\bangular\b"]),
    ("Vue",          "framework", [r"\bvue\.?js\b", r"\bvue\b"]),
    ("Next.js",      "framework", [r"\bnext\.?js\b"]),
    ("Django",       "framework", [r"\bdjango\b"]),
    ("FastAPI",      "framework", [r"\bfastapi\b"]),
    ("Flask",        "framework", [r"\bflask\b"]),
    ("Spring",       "framework", [r"\bspring\s+boot\b", r"\bspring\b"]),
    ("Node.js",      "framework", [r"\bnode\.?js\b"]),
    ("Pandas",       "framework", [r"\bpandas\b"]),
    ("NumPy",        "framework", [r"\bnumpy\b"]),
    ("Scikit-learn", "framework", [r"\bscikit[-\s]?learn\b", r"\bsklearn\b"]),
    ("TensorFlow",   "framework", [r"\btensorflow\b"]),
    ("PyTorch",      "framework", [r"\bpytorch\b"]),
    ("Keras",        "framework", [r"\bkeras\b"]),
    ("Spark",        "framework", [r"\bapache\s+spark\b", r"\bpyspark\b", r"\bspark\b"]),
    ("Flink",        "framework", [r"\bapache\s+flink\b", r"\bflink\b"]),
    ("Hadoop",       "framework", [r"\bhadoop\b"]),
    ("LangChain",    "framework", [r"\blangchain\b"]),

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
    ("Oracle",        "database", [r"\boracle\s+db\b", r"\boracle\s+database\b"]),
    ("DynamoDB",      "database", [r"\bdynamodb\b"]),

    # -------------------------------------------------------------------------
    # Herramientas
    # -------------------------------------------------------------------------
    ("dbt",            "tool", [r"\bdbt\b"]),
    ("Airflow",        "tool", [r"\bairflow\b", r"\bapache\s+airflow\b"]),
    ("Prefect",        "tool", [r"\bprefect\b"]),
    ("Dagster",        "tool", [r"\bdagster\b"]),
    ("MLflow",         "tool", [r"\bmlflow\b"]),
    ("Kafka",          "tool", [r"\bkafka\b", r"\bapache\s+kafka\b"]),
    ("Git",            "tool", [r"\bgit\b"]),
    # GitHub Actions se detecta antes que GitHub para evitar doble match
    ("GitHub Actions", "tool", [r"\bgithub\s+actions\b"]),
    ("GitHub",         "tool", [r"\bgithub\b"]),
    ("GitLab",         "tool", [r"\bgitlab\b"]),
    ("Jenkins",        "tool", [r"\bjenkins\b"]),
    ("Power BI",       "tool", [r"\bpower\s*bi\b"]),
    ("Tableau",        "tool", [r"\btableau\b"]),
    ("Looker",         "tool", [r"\blooker\b"]),
    ("Grafana",        "tool", [r"\bgrafana\b"]),
    ("Prometheus",     "tool", [r"\bprometheus\b"]),
    ("Hugging Face",   "tool", [r"\bhugging\s*face\b"]),
    ("OpenAI",         "tool", [r"\bopenai\b"]),

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
    ("Microservices", "methodology", [r"\bmicroservices?\b"]),
    ("TDD",           "methodology", [r"\btdd\b", r"\btest[-\s]driven\s+development\b"]),
]


# =============================================================================
# Clasificación de roles
# =============================================================================

# Palabras clave para asignar role_category desde el título de la oferta.
# El orden importa: se evalúa de arriba a abajo y se devuelve la primera
# coincidencia. Los roles más específicos van primero para evitar que un título
# como "ML Engineer" caiga en "backend" antes de llegar a "ai_ml".
ROLE_KEYWORDS: dict[str, list[str]] = {
    "ai_ml": [
        "ai engineer", "artificial intelligence", "deep learning",
        "nlp engineer", "computer vision", "llm engineer",
        "generative ai", "prompt engineer", "machine learning engineer",
        "ml engineer",
    ],
    "data_science": [
        "data scientist", "research scientist", "applied scientist",
    ],
    "data_engineering": [
        "data engineer", "analytics engineer", "data platform engineer",
        "data infrastructure", "etl developer", "data pipeline",
        "dataops engineer",
    ],
    "data_analyst": [
        "data analyst", "business analyst", "bi analyst",
        "reporting analyst", "business intelligence analyst",
    ],
    "mobile": [
        "mobile developer", "ios developer", "android developer",
        "flutter developer", "react native developer",
    ],
    "security": [
        "security engineer", "cybersecurity", "penetration test",
        "information security", "soc analyst", "cloud security",
        "application security",
    ],
    "devops": [
        "devops engineer", "platform engineer", "site reliability engineer",
        "sre", "infrastructure engineer", "release engineer",
    ],
    "cloud": [
        "cloud engineer", "cloud architect", "solutions architect",
        "aws engineer", "azure engineer", "gcp engineer",
    ],
    "fullstack": [
        "fullstack", "full-stack", "full stack",
    ],
    "frontend": [
        "frontend", "front-end", "front end", "ui developer",
        "react developer", "angular developer", "vue developer",
    ],
    "backend": [
        "backend", "back-end", "back end", "api developer",
        "software engineer", "software developer",
        "java developer", "python developer", "node developer",
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
]

REMOTE_NEGATIVE: list[str] = [
    r"\bon[-\s]?site\b",
    r"\bin[-\s]?office\b",
    r"\bpresencial\b",
    r"\bin\s+person\b",
    r"\bon\s+location\b",
    r"\bvor\s*ort\b",
]
