"""Vector-based help search using ChromaDB with built-in embeddings."""

import os
import re

from django.conf import settings

# Persistent storage so we don't re-embed on every gunicorn worker restart
_chromadb = None

def _get_chromadb():
    global _chromadb
    if _chromadb is None:
        import chromadb
        _chromadb = chromadb
    return _chromadb

def _get_db_dir():
    # saasclaw user can't write to app dir; use /srv/saasclaw
    db_dir = "/srv/saasclaw/.chroma_help"
    os.makedirs(db_dir, exist_ok=True)
    return db_dir


def _strip_html(text):
    """Remove HTML tags and clean whitespace."""
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _load_docs():
    """Load help page templates and chunk them."""
    template_dir = os.path.join(
        os.path.dirname(__file__),
        "templates",
        "studio",
        "help",
    )
    if not os.path.isdir(template_dir):
        return []

    docs = []
    for fname in sorted(os.listdir(template_dir)):
        if not fname.endswith(".html") or fname == "index.html":
            continue
        source = fname.replace(".html", "")
        fpath = os.path.join(template_dir, fname)
        with open(fpath) as f:
            raw = f.read()
        text = _strip_html(raw)
        if not text:
            continue
        # Split into chunks of ~300 words with 50-word overlap
        words = text.split()
        chunk_size = 300
        overlap = 50
        for i in range(0, len(words), chunk_size - overlap):
            chunk_words = words[i : i + chunk_size]
            if len(chunk_words) < 30:
                continue
            chunk_text = " ".join(chunk_words)
            docs.append({"source": source, "text": chunk_text})
    return docs


def _init_collection():
    """Get or create the ChromaDB collection, indexing docs if needed."""
    client = _get_chromadb().PersistentClient(path=_get_db_dir())
    collection = client.get_or_create_collection(
        name="help_docs",
        metadata={"hnsw:space": "cosine"},
    )
    # Only index if collection is empty
    if collection.count() == 0:
        docs = _load_docs()
        if docs:
            ids = [f"{d['source']}_{i}" for i, d in enumerate(docs)]
            texts = [d["text"] for d in docs]
            metadatas = [{"source": d["source"]} for d in docs]
            # ChromaDB default embedding model handles this
            collection.add(ids=ids, documents=texts, metadatas=metadatas)
    return collection


# Initialize at import time
_collection = None


def _get_collection():
    global _collection
    if _collection is None:
        _collection = _init_collection()
    return _collection


def search(query: str, max_chunks: int = 3) -> list[dict]:
    """Search help docs using vector similarity. Returns list of chunks."""
    collection = _get_collection()
    results = collection.query(query_texts=[query], n_results=max_chunks)
    
    chunks = []
    if results and results["documents"]:
        for i, doc in enumerate(results["documents"][0]):
            metadata = results["metadatas"][0][i] if results["metadatas"] else {}
            distance = results["distances"][0][i] if results["distances"] else 0
            chunks.append({
                "source": metadata.get("source", "unknown"),
                "text": doc,
                "score": 1 - distance,  # cosine similarity (0-1)
            })
    return chunks
