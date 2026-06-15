"""TRON-X ingestion package (Phase 38): universal attachment processing."""
from src.ingestion.attachments import (
    Attachment,
    AttachmentProcessor,
    process_attachment,
    process_bytes,
)

__all__ = [
    "Attachment",
    "AttachmentProcessor",
    "process_attachment",
    "process_bytes",
]
