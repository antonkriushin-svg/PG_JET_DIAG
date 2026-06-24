import json
from pathlib import Path
from urllib.parse import quote_plus


CONFIG_PATH = Path(__file__).with_name("db_config.json")
REQUIRED_KEYS = {"host", "port", "user", "database", "password"}


def load_db_config(config_path: Path = CONFIG_PATH) -> dict:
    with config_path.open("r", encoding="utf-8") as f:
        config = json.load(f)

    missing = REQUIRED_KEYS - set(config.keys())
    if missing:
        missing_keys = ", ".join(sorted(missing))
        raise ValueError(f"Missing database config keys: {missing_keys}")

    return config


def build_psycopg2_dsn(config: dict | None = None) -> str:
    cfg = config or load_db_config()
    return (
        f"host={cfg['host']} "
        f"port={cfg['port']} "
        f"dbname={cfg['database']} "
        f"user={cfg['user']} "
        f"password={cfg['password']}"
    )


def build_sqlalchemy_url(config: dict | None = None) -> str:
    cfg = config or load_db_config()
    user = quote_plus(str(cfg["user"]))
    password = quote_plus(str(cfg["password"]))
    host = cfg["host"]
    port = cfg["port"]
    database = quote_plus(str(cfg["database"]))

    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}"
