import os
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = Path(os.environ.get("LITERATURE_MANAGER_DB", PROJECT_DIR / "paper_library.db"))
APP_NAME = "文献管理系统"
APP_VERSION = "0.1.0"

CROSSREF_BASE = "https://api.crossref.org"
OPENALEX_BASE = "https://api.openalex.org"
NETWORK_TIMEOUT_SECONDS = 12
USER_AGENT = "LocalLiteratureManager/0.1 (mailto:local-user@example.invalid)"
