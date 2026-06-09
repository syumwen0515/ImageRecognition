# models.py — SQLAlchemy ORM models
from datetime import datetime
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from database import Base


class User(Base):
    """Registered user account."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    display_name = Column(String, nullable=False)          # shown in UI after login
    username = Column(String, unique=True, nullable=False, index=True)  # used for login
    email = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=False)
    role = Column(String, default="user", nullable=False)  # "user" | "admin"
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    albums = relationship("Album", back_populates="owner")


class Album(Base):
    """One row per race event / photo album."""
    __tablename__ = "albums"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    event_date = Column(String, nullable=True)    # "2025-10-01"
    description = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    owner = relationship("User", back_populates="albums")
    photos = relationship("Photo", back_populates="album")


class Photo(Base):
    """One row per uploaded image file."""
    __tablename__ = "photos"

    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String, unique=True, nullable=False)
    original_filename = Column(String, nullable=False)
    upload_time = Column(DateTime, default=datetime.utcnow, nullable=False)
    album_id = Column(Integer, ForeignKey("albums.id"), nullable=True)
    ocr_status = Column(String, default="done", nullable=False)  # pending | done | failed
    storage_url = Column(String, nullable=True)  # Cloudinary URL; None = local disk

    album = relationship("Album", back_populates="photos")
    bib_numbers = relationship("BibNumber", back_populates="photo", cascade="all, delete-orphan")


class BibNumber(Base):
    """One row per recognised bib number per photo."""
    __tablename__ = "bib_numbers"

    id = Column(Integer, primary_key=True, index=True)
    photo_id = Column(Integer, ForeignKey("photos.id"), nullable=False)
    bib_number = Column(String(10), nullable=False, index=True)

    photo = relationship("Photo", back_populates="bib_numbers")
