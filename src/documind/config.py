from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gcp_project_id: str = Field(alias="GCP_PROJECT_ID")
    gcp_region: str = Field(default="us-central1", alias="GCP_REGION")

    raw_docs_bucket: str = Field(alias="RAW_DOCS_BUCKET")
    docai_processor_id: str = Field(alias="DOCAI_PROCESSOR_ID")
    docai_location: str = Field(default="us", alias="DOCAI_LOCATION")

    db_instance_connection_name: str = Field(alias="DB_INSTANCE_CONNECTION_NAME")
    db_name: str = Field(default="documind", alias="DB_NAME")
    db_user: str = Field(default="documind_admin", alias="DB_USER")
    db_password_secret_id: str = Field(alias="DB_PASSWORD_SECRET_ID")
    db_host: str = Field(alias="DB_HOST")
    db_port: int = Field(default=5432, alias="DB_PORT")
    db_sslmode: str = Field(default="require", alias="DB_SSLMODE")
    
    embedding_model: str = Field(default="text-embedding-005", alias="EMBEDDING_MODEL")
    chunk_size: int = Field(default=1000, alias="CHUNK_SIZE")
    chunk_overlap: int = Field(default=150, alias="CHUNK_OVERLAP")


@lru_cache
def get_settings() -> Settings:
    return Settings()