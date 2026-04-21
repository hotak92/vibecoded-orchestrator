# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 VibeCoded Tools
"""
Text Chunking Utilities for Claude MCP Weaviate Server

Unified chunking implementation with:
- Ollama-based token counting (accurate) with character approximation fallback
- Flexible metadata support
- Document and generic text chunking
"""

import re
import os
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from datetime import datetime, timezone

# Try to import langchain_ollama for accurate token counting
try:
    from langchain_ollama import ChatOllama
    OLLAMA_AVAILABLE = True
except ImportError:
    OLLAMA_AVAILABLE = False


UTC = timezone.utc


@dataclass
class Chunk:
    """A chunk of text with metadata"""
    content: str
    chunk_number: int
    total_chunks: int
    token_count: int
    source_id: str
    metadata: Dict[str, Any]
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for Weaviate storage"""
        import json
        return {
            "content": self.content,
            "chunk_number": self.chunk_number,
            "total_chunks": self.total_chunks,
            "token_count": self.token_count,
            "source_id": self.source_id,
            "metadata_json": json.dumps(self.metadata),
            "created_at": self.created_at
        }


@dataclass
class DocumentChunk:
    """A chunk of a document with specific document metadata (backward compatibility)"""
    content: str
    chunk_number: int
    total_chunks: int
    token_count: int
    source_document_id: str
    source_document_title: str
    is_first: bool
    is_last: bool
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for Weaviate storage"""
        return {
            "content": self.content,
            "chunk_number": self.chunk_number,
            "total_chunks": self.total_chunks,
            "token_count": self.token_count,
            "source_document_id": self.source_document_id,
            "source_document_title": self.source_document_title,
            "is_first": self.is_first,
            "is_last": self.is_last,
            "created_at": self.created_at
        }


class TokenCounter:
    """
    Token counter with Ollama LLM tokenizer (accurate) and character approximation fallback

    Uses Ollama qwen3:0.6b for accurate token counting when available.
    Falls back to approximation (1 token ≈ 4 characters) if Ollama unavailable.
    """

    _llm = None
    _use_approximation = False
    _ollama_url = None

    @classmethod
    def _get_llm(cls):
        """Get or create LLM instance for token counting"""
        if cls._llm is None:
            if not OLLAMA_AVAILABLE:
                cls._use_approximation = True
                return None

            try:
                cls._llm = ChatOllama(
                    model=os.getenv("TOKENIZER_MODEL", "qwen3:0.6b"),
                    base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11435"),
                    temperature=0
                )
            except Exception:
                cls._use_approximation = True
                return None

        return cls._llm

    @classmethod
    def set_ollama_url(cls, ollama_url: str):
        """Set Ollama URL for token counting"""
        cls._ollama_url = ollama_url
        cls._llm = None  # Reset to use new URL

    @staticmethod
    def count_tokens(text: str) -> int:
        """
        Count tokens in text using Ollama LLM tokenizer or approximation

        Args:
            text: Text to count tokens for

        Returns:
            Number of tokens
        """
        if not text:
            return 0

        llm = TokenCounter._get_llm()

        if llm is None or TokenCounter._use_approximation:
            # Fallback: approximation (1 token ≈ 4 characters)
            return len(text) // 4

        try:
            token_count = llm.get_num_tokens(text)
            return token_count
        except Exception:
            # If Ollama call fails, fall back to approximation
            TokenCounter._use_approximation = True
            return len(text) // 4

    @staticmethod
    def count_dict_tokens(data: Dict[str, Any]) -> int:
        """
        Count tokens in a dictionary (recursively)

        Args:
            data: Dictionary to count tokens for

        Returns:
            Total number of tokens
        """
        total = 0
        for key, value in data.items():
            # Add tokens for key
            total += TokenCounter.count_tokens(str(key))

            # Add tokens for value
            if isinstance(value, dict):
                total += TokenCounter.count_dict_tokens(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        total += TokenCounter.count_dict_tokens(item)
                    else:
                        total += TokenCounter.count_tokens(str(item))
            else:
                total += TokenCounter.count_tokens(str(value))

        return total


# Model-specific context limits (max input tokens).
# Used to auto-configure chunk sizes when the embedding model is known.
MODEL_TOKEN_LIMITS: dict[str, int] = {
    # Text embedding models
    "snowflake-arctic-embed2:latest": 2_048,     # proven working at 2048 tokens
    "snowflake-arctic-embed2": 2_048,
    "qwen3-embedding:0.6b": 8_192,              # Ollama default ctx=4096; we set num_ctx=8192 in API calls
    "qwen3-embedding": 8_192,                    # Supports up to 32k with num_ctx override
    "text-embedding-3-small": 8_191,
    # Code embedding models
    "unclemusclez/jina-embeddings-v2-base-code:latest": 8_192,
    "jina-embeddings-v2-base-code": 8_192,
    "codesage/codesage-large-v2": 2_048,          # CodeSage uses 2048 max seq len
    "codesage-large-v2": 2_048,
}

# Default chunking presets by model class.
# (min_tokens, max_tokens, target_tokens)
CHUNKING_PRESETS: dict[str, tuple[int, int, int]] = {
    "small_context":  (300,  500,  400),    # models with ≤512 token context
    "medium_context": (500,  1500, 1000),   # models with 2k-8k token context (arctic, jina, codesage)
    "large_context":  (1000, 2000, 1500),   # models with ≥32k token context (qwen3-embedding)
}


def chunking_preset_for_model(model_name: str) -> tuple[int, int, int]:
    """Return (min_tokens, max_tokens, target_tokens) for a given model.

    Falls back to 'large_context' if the model is unknown.
    """
    limit = MODEL_TOKEN_LIMITS.get(model_name)
    if limit is None:
        # Try partial match
        for key, val in MODEL_TOKEN_LIMITS.items():
            if key in model_name or model_name in key:
                limit = val
                break
    if limit is None:
        return CHUNKING_PRESETS["large_context"]
    if limit <= 512:
        return CHUNKING_PRESETS["small_context"]
    if limit <= 8192:
        return CHUNKING_PRESETS["medium_context"]
    return CHUNKING_PRESETS["large_context"]


class Chunker:
    """
    Chunk text into optimal pieces

    Strategy:
    - Prefer splitting on sentence boundaries (.)
    - Fallback to double newline (\\n\\n)
    - Target: 1500 tokens per chunk (configurable per model)
    - Min: 1000 tokens (unless end of text)
    - Max: 2000 tokens
    - Use chunking_preset_for_model() to auto-configure for specific embedding models
    """

    def __init__(self, min_tokens: int = 1000, max_tokens: int = 2000, target_tokens: int = 1500):
        self.min_tokens = min_tokens
        self.max_tokens = max_tokens
        self.target_tokens = target_tokens

    @classmethod
    def for_model(cls, model_name: str) -> "Chunker":
        """Create a Chunker with preset token limits for the given embedding model."""
        min_t, max_t, target_t = chunking_preset_for_model(model_name)
        return cls(min_tokens=min_t, max_tokens=max_t, target_tokens=target_t)

    def chunk_text(
        self,
        text: str,
        source_id: str,
        metadata: Optional[Dict[str, Any]] = None
    ) -> List[Chunk]:
        """
        Chunk text into optimal pieces

        Args:
            text: Text to chunk
            source_id: Unique identifier for source
            metadata: Optional metadata to attach to chunks

        Returns:
            List of Chunk objects
        """
        text = text.strip()
        if not text:
            return []

        raw_chunks = self._split_text(text)
        chunks = []
        total_chunks = len(raw_chunks)

        for i, chunk_text in enumerate(raw_chunks):
            chunks.append(Chunk(
                content=chunk_text.strip(),
                chunk_number=i,
                total_chunks=total_chunks,
                token_count=TokenCounter.count_tokens(chunk_text),
                source_id=source_id,
                metadata=metadata or {},
                created_at=datetime.now(UTC).isoformat()
            ))

        return chunks

    def chunk_document(
        self,
        text: str,
        document_id: str,
        document_title: str
    ) -> List[DocumentChunk]:
        """
        Chunk a document into optimal pieces (backward compatibility)

        Args:
            text: Document text to chunk
            document_id: Unique document identifier
            document_title: Document title

        Returns:
            List of DocumentChunk objects
        """
        text = text.strip()
        if not text:
            return []

        raw_chunks = self._split_text(text)
        chunks = []
        total_chunks = len(raw_chunks)

        for i, chunk_text in enumerate(raw_chunks):
            chunks.append(DocumentChunk(
                content=chunk_text.strip(),
                chunk_number=i,
                total_chunks=total_chunks,
                token_count=TokenCounter.count_tokens(chunk_text),
                source_document_id=document_id,
                source_document_title=document_title,
                is_first=(i == 0),
                is_last=(i == total_chunks - 1),
                created_at=datetime.now(UTC).isoformat()
            ))

        return chunks

    def _split_text(self, text: str) -> List[str]:
        """Split text into chunks respecting boundaries"""
        chunks = []
        current_chunk = ""
        current_tokens = 0

        sentences = self._split_into_sentences(text)

        for sentence in sentences:
            sentence_tokens = TokenCounter.count_tokens(sentence)

            if sentence_tokens > self.max_tokens:
                if current_chunk:
                    chunks.append(current_chunk)
                    current_chunk = ""
                    current_tokens = 0

                sub_chunks = self._split_on_newlines(sentence)
                for sub_chunk in sub_chunks:
                    chunks.append(sub_chunk)
                continue

            potential_tokens = current_tokens + sentence_tokens

            if potential_tokens > self.max_tokens:
                if current_chunk:
                    chunks.append(current_chunk)
                current_chunk = sentence
                current_tokens = sentence_tokens
            elif potential_tokens > self.target_tokens and current_tokens >= self.min_tokens:
                chunks.append(current_chunk)
                current_chunk = sentence
                current_tokens = sentence_tokens
            else:
                current_chunk += " " + sentence if current_chunk else sentence
                current_tokens = potential_tokens

        if current_chunk:
            chunks.append(current_chunk)

        return chunks

    def _split_into_sentences(self, text: str) -> List[str]:
        """Split text into sentences"""
        pattern = r'(?<=[.!?])\s+(?=[A-Z])'
        sentences = re.split(pattern, text)
        return [s.strip() for s in sentences if s.strip()]

    def _split_on_newlines(self, text: str) -> List[str]:
        """Split text on double newlines"""
        chunks = []
        current = ""
        current_tokens = 0

        paragraphs = text.split('\n\n')

        for para in paragraphs:
            para_tokens = TokenCounter.count_tokens(para)

            if para_tokens > self.max_tokens:
                if current:
                    chunks.append(current)
                    current = ""
                    current_tokens = 0

                char_limit = self.max_tokens * 4
                for i in range(0, len(para), char_limit):
                    chunks.append(para[i:i+char_limit])
                continue

            potential_tokens = current_tokens + para_tokens

            if potential_tokens > self.max_tokens:
                if current:
                    chunks.append(current)
                current = para
                current_tokens = para_tokens
            else:
                current += "\n\n" + para if current else para
                current_tokens = potential_tokens

        if current:
            chunks.append(current)

        return chunks


# Convenience aliases for backward compatibility
DocumentChunker = Chunker


def chunk_text(
    text: str,
    source_id: str,
    metadata: Optional[Dict[str, Any]] = None,
    min_tokens: int = 1000,
    max_tokens: int = 2000
) -> List[Chunk]:
    """Convenience function to chunk text"""
    chunker = Chunker(min_tokens=min_tokens, max_tokens=max_tokens)
    return chunker.chunk_text(text, source_id, metadata)
