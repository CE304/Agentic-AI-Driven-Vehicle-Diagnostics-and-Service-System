# -*- coding: utf-8 -*-
r"""建立汽車診斷用的輕量本機 RAG 索引。

預設資料位置（以此程式所在資料夾為基準）：
    rag_sources\  原始 TXT / MD / JSON / CSV 文件
    rag_db\rag_index.json  建立完成的 TF-IDF 向量索引

建立索引：
    py -X utf8 C:\n8n_python\ingest_rag.py

建立後立即測試搜尋：
    py -X utf8 C:\n8n_python\ingest_rag.py --query "P0171 燃油修正過高"

此版本不需要第三方套件，適合先驗證 n8n + MCP + RAG 全流程。
未來可在不改變 rag_sources 結構的情況下升級成 Embedding 向量資料庫。
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Iterable


SUPPORTED_EXTENSIONS = {".txt", ".md", ".json", ".csv"}
INDEX_VERSION = 1
DEFAULT_CHUNK_SIZE = 900
DEFAULT_CHUNK_OVERLAP = 150


def read_text_file(path: Path) -> str:
    """以常見中文編碼讀取文字文件。"""
    for encoding in ("utf-8-sig", "utf-8", "cp950", "big5"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise UnicodeError(f"無法辨識檔案編碼：{path}")


def normalize_text(text: str) -> str:
    """統一換行與空白，同時保留段落結構。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\t\u3000]+", " ", text)
    text = re.sub(r" +", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_long_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    """優先按段落切割，過長段落再使用字元視窗切割。"""
    if chunk_size < 200:
        raise ValueError("chunk_size 不可小於 200。")
    if overlap < 0 or overlap >= chunk_size:
        raise ValueError("overlap 必須大於等於 0 且小於 chunk_size。")

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    current = ""

    def add_windowed(value: str) -> None:
        step = chunk_size - overlap
        for start in range(0, len(value), step):
            piece = value[start:start + chunk_size].strip()
            if piece:
                chunks.append(piece)
            if start + chunk_size >= len(value):
                break

    for paragraph in paragraphs:
        if len(paragraph) > chunk_size:
            if current:
                chunks.append(current.strip())
                current = ""
            add_windowed(paragraph)
            continue

        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= chunk_size:
            current = candidate
        else:
            chunks.append(current.strip())
            prefix = current[-overlap:].strip() if overlap else ""
            current = f"{prefix}\n\n{paragraph}".strip() if prefix else paragraph

    if current:
        chunks.append(current.strip())
    return chunks


def tokenize(text: str) -> list[str]:
    """建立適合中英文汽車資料的詞元：英文單字、代碼、中文單字與雙字詞。"""
    lowered = text.lower()
    tokens = re.findall(r"[a-z]+(?:[-_][a-z0-9]+)*|p\d{4}|u\d{4}|b\d{4}|c\d{4}|\d+(?:\.\d+)?", lowered)
    chinese_groups = re.findall(r"[\u4e00-\u9fff]+", lowered)
    for group in chinese_groups:
        tokens.extend(group)
        tokens.extend(group[index:index + 2] for index in range(len(group) - 1))
    return [token for token in tokens if token.strip()]


def iter_source_files(source_dir: Path) -> Iterable[Path]:
    """依固定順序列出支援的來源文件。"""
    for path in sorted(source_dir.rglob("*"), key=lambda item: str(item).lower()):
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            yield path


def build_chunks(source_dir: Path, chunk_size: int, overlap: int) -> tuple[list[dict[str, Any]], list[str]]:
    """讀取所有文件並建立帶來源資訊的文字區塊。"""
    chunks: list[dict[str, Any]] = []
    warnings: list[str] = []

    for path in iter_source_files(source_dir):
        try:
            text = normalize_text(read_text_file(path))
        except (OSError, UnicodeError) as error:
            warnings.append(f"略過 {path}：{error}")
            continue
        if not text:
            warnings.append(f"略過空白文件：{path}")
            continue

        relative_path = path.relative_to(source_dir).as_posix()
        category = relative_path.split("/")[1] if "/" in relative_path else "未分類"
        for chunk_number, content in enumerate(split_long_text(text, chunk_size, overlap), start=1):
            digest = hashlib.sha256(f"{relative_path}\n{chunk_number}\n{content}".encode("utf-8")).hexdigest()[:16]
            chunks.append(
                {
                    "id": digest,
                    "source": relative_path,
                    "category": category,
                    "chunk_number": chunk_number,
                    "content": content,
                }
            )
    return chunks, warnings


def create_tfidf_index(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """將所有區塊轉換成稀疏 TF-IDF 向量。"""
    token_counts: list[Counter[str]] = []
    document_frequency: Counter[str] = Counter()

    for chunk in chunks:
        counts = Counter(tokenize(chunk["content"]))
        token_counts.append(counts)
        document_frequency.update(counts.keys())

    document_count = len(chunks)
    idf = {
        token: math.log((document_count + 1) / (frequency + 1)) + 1.0
        for token, frequency in document_frequency.items()
    }

    indexed_chunks: list[dict[str, Any]] = []
    for chunk, counts in zip(chunks, token_counts):
        total = sum(counts.values()) or 1
        vector = {token: (count / total) * idf[token] for token, count in counts.items()}
        norm = math.sqrt(sum(value * value for value in vector.values())) or 1.0
        normalized_vector = {token: round(value / norm, 8) for token, value in vector.items()}
        indexed_chunks.append({**chunk, "vector": normalized_vector})

    return {
        "index_version": INDEX_VERSION,
        "vectorizer": "character_bigram_and_word_tfidf",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "chunk_count": document_count,
        "idf": {token: round(value, 8) for token, value in idf.items()},
        "chunks": indexed_chunks,
    }


def save_index(index: dict[str, Any], output_path: Path) -> None:
    """使用暫存檔安全寫入索引。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary_path.replace(output_path)


def cosine_search(index: dict[str, Any], query: str, top_k: int = 5) -> list[dict[str, Any]]:
    """在已載入的索引中搜尋最相關的文字區塊。"""
    if not query.strip():
        raise ValueError("搜尋問題不可為空白。")
    idf: dict[str, float] = index["idf"]
    counts = Counter(tokenize(query))
    total = sum(counts.values()) or 1
    query_vector = {
        token: (count / total) * idf.get(token, 0.0)
        for token, count in counts.items()
        if token in idf
    }
    norm = math.sqrt(sum(value * value for value in query_vector.values())) or 1.0
    query_vector = {token: value / norm for token, value in query_vector.items()}

    results: list[dict[str, Any]] = []
    for chunk in index["chunks"]:
        vector: dict[str, float] = chunk["vector"]
        score = sum(value * vector.get(token, 0.0) for token, value in query_vector.items())
        if score > 0:
            results.append(
                {
                    "score": round(score, 6),
                    "source": chunk["source"],
                    "category": chunk["category"],
                    "chunk_number": chunk["chunk_number"],
                    "content": chunk["content"],
                }
            )
    results.sort(key=lambda item: item["score"], reverse=True)
    return results[:max(1, min(top_k, 10))]


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="建立汽車診斷用的本機 RAG TF-IDF 向量索引。")
    parser.add_argument("--source-dir", type=Path, default=script_dir / "rag_sources")
    parser.add_argument("--db-dir", type=Path, default=script_dir / "rag_db")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--overlap", type=int, default=DEFAULT_CHUNK_OVERLAP)
    parser.add_argument("--query", help="建立索引後立即測試此搜尋問題。")
    parser.add_argument("--top-k", type=int, default=3)
    args = parser.parse_args()

    source_dir = args.source_dir.resolve()
    db_dir = args.db_dir.resolve()
    output_path = db_dir / "rag_index.json"

    if not source_dir.exists():
        raise FileNotFoundError(f"找不到來源資料夾：{source_dir}")

    chunks, warnings = build_chunks(source_dir, args.chunk_size, args.overlap)
    for warning in warnings:
        print(f"警告：{warning}", file=sys.stderr)
    if not chunks:
        raise RuntimeError(f"在 {source_dir} 找不到可建立索引的文字資料。")

    index = create_tfidf_index(chunks)
    save_index(index, output_path)
    source_count = len({chunk["source"] for chunk in chunks})

    print("RAG 索引建立成功。")
    print(f"來源資料夾：{source_dir}")
    print(f"文件數量：{source_count}")
    print(f"文字區塊：{len(chunks)}")
    print(f"索引位置：{output_path}")

    if args.query:
        results = cosine_search(index, args.query, args.top_k)
        print("\n測試搜尋結果：")
        if not results:
            print("沒有找到相關內容。")
        for number, result in enumerate(results, start=1):
            preview = result["content"].replace("\n", " ")[:240]
            print(f"{number}. 分數={result['score']:.4f} 來源={result['source']}")
            print(f"   {preview}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消建立 RAG 索引。")
        raise SystemExit(130)
    except Exception as error:
        print(f"錯誤：{error}", file=sys.stderr)
        raise SystemExit(1)
